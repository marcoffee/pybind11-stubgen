from __future__ import annotations

import builtins
import importlib
import inspect
import re
import types
from typing import Any

from pybind11_stubgen.parser.errors import NameResolutionError
from pybind11_stubgen.parser.interface import IParser
from pybind11_stubgen.structs import (
    Alias,
    Attribute,
    Class,
    Docstring,
    Field,
    Function,
    Identifier,
    Import,
    InvalidExpression,
    Method,
    Module,
    Property,
    QualifiedName,
    ResolvedType,
    Value,
)
from pybind11_stubgen.typing_ext import FixedSize


class RemoveSelfAnnotation(IParser):
    def handle_method(self, path: QualifiedName, method: Any) -> list[Method]:
        methods = super().handle_method(path, method)
        for method in methods:
            self._remove_self_arg_annotation(method.function)
        return methods

    def handle_property(self, path: QualifiedName, prop: Any) -> Property | None:
        prop = super().handle_property(path, prop)
        if prop is not None:
            if prop.getter is not None:
                self._remove_self_arg_annotation(prop.getter)
            if prop.setter is not None:
                self._remove_self_arg_annotation(prop.setter)

        return prop

    def _remove_self_arg_annotation(self, func: Function):
        if len(func.args) > 0 and func.args[0].name == "self":
            func.args[0].annotation = None


class FixMissingImports(IParser):
    def __init__(self):
        super().__init__()
        self.__extra_imports: set[Import] = set()
        self.__current_module: types.ModuleType | None = None
        self.__current_class: type | None = None

    def handle_alias(self, path: QualifiedName, origin: Any) -> Alias | None:
        result = super().handle_alias(path, origin)
        if result is None:
            return None
        self._add_import(result.origin)
        return result

    def handle_attribute(self, path: QualifiedName, attr: Any) -> Attribute | None:
        result = super().handle_attribute(path, attr)
        if result is None:
            return None
        if isinstance(result.annotation, ResolvedType):
            self._add_import(result.annotation.name)
        return result

    def handle_class(self, path: QualifiedName, class_: type) -> Class | None:
        old_class = self.__current_class
        self.__current_class = class_
        result = super().handle_class(path, class_)
        self.__current_class = old_class
        return result

    def handle_import(self, path: QualifiedName, origin: Any) -> Import | None:
        result = super().handle_import(path, origin)
        if result is None:
            return None
        self.__extra_imports.add(result)
        return result

    def handle_module(
        self, path: QualifiedName, module: types.ModuleType
    ) -> Module | None:
        old_imports = self.__extra_imports
        old_module = self.__current_module
        self.__extra_imports = set()
        self.__current_module = module
        result = super().handle_module(path, module)
        if result is not None:
            result.imports |= self.__extra_imports
        self.__extra_imports = old_imports
        self.__current_module = old_module
        return result

    def handle_type(self, type_: type) -> QualifiedName:
        result = super().handle_type(type_)
        if not inspect.ismodule(type):
            self._add_import(result)
        return result

    def parse_annotation_str(
        self, annotation_str: str
    ) -> ResolvedType | InvalidExpression | Value:
        result = super().parse_annotation_str(annotation_str)
        if isinstance(result, ResolvedType):
            self._add_import(result.name)
        return result

    def _add_import(self, name: QualifiedName) -> None:
        if len(name) > 0:
            if hasattr(builtins, name[0]):
                return
            if self.__current_class is not None and hasattr(
                self.__current_class, name[0]
            ):
                return
            if self.__current_module is not None and hasattr(
                self.__current_module, name[0]
            ):
                return
        module_name = self._get_parent_module(name)
        if module_name is None:
            self.report_error(NameResolutionError(name))
            return
        self.__extra_imports.add(Import(name=None, origin=module_name))

    def _get_parent_module(self, name: QualifiedName) -> QualifiedName | None:
        parent = name.parent
        while len(parent) != 0:
            if self._is_module(parent):
                if not self._is_accessible(name, from_module=parent):
                    return None
                return parent
            parent = parent.parent
        return None

    def _is_module(self, name: QualifiedName):
        try:
            return importlib.import_module(str(name)) is not None
        except ModuleNotFoundError:
            return False

    def _is_accessible(self, name: QualifiedName, from_module: QualifiedName) -> bool:
        try:
            parent = importlib.import_module(str(from_module))
        except ModuleNotFoundError:
            return False
        relative_path = name[len(from_module) :]
        for part in relative_path:
            if not hasattr(parent, part):
                return False
            parent = getattr(parent, part)
        return True


class FixMissing__future__AnnotationsImport(IParser):
    def handle_module(
        self, path: QualifiedName, module: types.ModuleType
    ) -> Module | None:
        result = super().handle_module(path, module)
        if result is None:
            return None
        result.imports.add(self._future(Identifier("annotations")))
        return result

    def _future(self, feature: Identifier) -> Import:
        return Import(
            name=feature, origin=QualifiedName((Identifier("__future__"), feature))
        )


class FixMissing__all__Attribute(IParser):
    def handle_module(
        self, path: QualifiedName, module: types.ModuleType
    ) -> Module | None:
        result = super().handle_module(path, module)
        if result is None:
            return None

        # don't override __all__
        for attr in result.attributes:
            if attr.name == Identifier("__all__"):
                return result

        all_names: list[str] = sorted(
            set(
                filter(
                    lambda name: not name.startswith("_"),
                    map(
                        str,
                        (
                            *(class_.name for class_ in result.classes),
                            *(attr.name for attr in result.attributes),
                            *(func.name for func in result.functions),
                            *(alias.name for alias in result.aliases),
                            *(
                                import_.name
                                for import_ in result.imports
                                if import_.name is not None
                            ),
                            *(sub_module.name for sub_module in result.sub_modules),
                        ),
                    ),
                )
            )
        )

        result.attributes.append(
            Attribute(
                name=Identifier("__all__"),
                value=self.handle_value(all_names),
                # annotation=ResolvedType(name=QualifiedName.from_str("list")),
            )
        )

        return result


class FixBuiltinTypes(IParser):
    _any_type = QualifiedName.from_str("typing.Any")

    def handle_type(self, type_: type) -> QualifiedName:
        if type_.__qualname__ == "PyCapsule" and type_.__module__ == "builtins":
            return self._any_type

        result = super().handle_type(type_)

        if result[0] == "builtins":
            if result[1] == "NoneType":
                return QualifiedName((Identifier("None"),))
            return QualifiedName(result[1:])

        return result


class FixRedundantBuiltinsAnnotation(IParser):
    def handle_attribute(self, path: QualifiedName, attr: Any) -> Attribute | None:
        result = super().handle_attribute(path, attr)
        if result is None:
            return None
        if attr is None or inspect.ismodule(attr):
            result.annotation = None
        return result


class FixMissingNoneHashFieldAnnotation(IParser):
    def handle_field(self, path: QualifiedName, field: Any) -> Field | None:
        result = super().handle_field(path, field)
        if result is None:
            return None
        if field is None and path[-1] == "__hash__":
            result.attribute.annotation = self.parse_annotation_str(
                "typing.ClassVar[None]"
            )
        return result


class FixTypingTypeNames(IParser):
    __typing_names: set[Identifier] = set(
        map(
            Identifier,
            [
                "Annotated",
                "Callable",
                "Dict",
                "Iterator",
                "ItemsView",
                "Iterable",
                "KeysView",
                "List",
                "Optional",
                "Set",
                "Sequence",
                "Tuple",
                "Union",
                "ValuesView",
                # Old pybind11 annotations were not capitalized
                "iterator",
                "iterable",
                "sequence",
            ],
        )
    )

    def parse_annotation_str(
        self, annotation_str: str
    ) -> ResolvedType | InvalidExpression | Value:
        result = super().parse_annotation_str(annotation_str)
        if not isinstance(result, ResolvedType):
            return result
        assert len(result.name) > 0

        word = result.name[0]
        if word in self.__typing_names:
            result.name = QualifiedName.from_str(f"typing.{word[0].upper()}{word[1:]}")

        return result


class FixTypingExtTypeNames(IParser):
    __typing_names: set[Identifier] = set(
        map(
            Identifier,
            ["buffer"],
        )
    )

    def parse_annotation_str(
        self, annotation_str: str
    ) -> ResolvedType | InvalidExpression | Value:
        result = super().parse_annotation_str(annotation_str)
        if not isinstance(result, ResolvedType):
            return result
        assert len(result.name) > 0

        word = result.name[0]
        if word in self.__typing_names and result.parameters is None:
            result.name = QualifiedName.from_str(
                f"typing_extensions.{word[0].upper()}{word[1:]}"
            )
        return result


class FixCurrentModulePrefixInTypeNames(IParser):
    def __init__(self):
        super().__init__()
        self.__current_module: QualifiedName = QualifiedName()

    def handle_alias(self, path: QualifiedName, origin: Any) -> Alias | None:
        result = super().handle_alias(path, origin)
        if result is None:
            return None
        result.origin = self._strip_current_module(result.origin)
        return result

    def handle_attribute(self, path: QualifiedName, attr: Any) -> Attribute | None:
        result = super().handle_attribute(path, attr)
        if result is None:
            return None
        if isinstance(result.annotation, ResolvedType):
            result.annotation.name = self._strip_current_module(result.annotation.name)
        return result

    def handle_module(
        self, path: QualifiedName, module: types.ModuleType
    ) -> Module | None:
        tmp = self.__current_module
        self.__current_module = path
        result = super().handle_module(path, module)
        self.__current_module = tmp
        return result

    def handle_type(self, type_: type) -> QualifiedName:
        result = super().handle_type(type_)
        return self._strip_current_module(result)

    def parse_annotation_str(
        self, annotation_str: str
    ) -> ResolvedType | InvalidExpression | Value:
        result = super().parse_annotation_str(annotation_str)
        if isinstance(result, ResolvedType):
            result.name = self._strip_current_module(result.name)
        return result

    def _strip_current_module(self, name: QualifiedName) -> QualifiedName:
        if name[: len(self.__current_module)] == self.__current_module:
            return QualifiedName(name[len(self.__current_module) :])
        return name


class FixValueReprRandomAddress(IParser):
    """
    repr examples:
        <capsule object NULL at 0x7fdfdf8b5f20> # PyCapsule
        <foo.bar.Baz object at 0x7fdfdf8b5f20>
    """

    _pattern = re.compile(
        r"<(?P<name>[\w.]+) object "
        r"(?P<capsule>\w+\s)*at "
        r"(?P<address>0x[a-fA-F0-9]+)>"
    )

    def value_to_repr(self, value: Any) -> str:
        result = super().value_to_repr(value)
        return self._pattern.sub(r"<\g<name> object>", result)


class FixNumpyArrayDimAnnotation(IParser):
    __ndarray_name = QualifiedName.from_str("numpy.ndarray")
    __annotated_name = QualifiedName.from_str("typing.Annotated")
    numpy_primitive_types: set[QualifiedName] = set(
        map(
            lambda name: QualifiedName.from_str(f"numpy.{name}"),
            (
                "int8",
                "int16",
                "int32",
                "int64",
                "float16",
                "float32",
                "float64",
                "complex32",
                "complex64",
                "longcomplex",
            ),
        )
    )

    def parse_annotation_str(
        self, annotation_str: str
    ) -> ResolvedType | InvalidExpression | Value:
        # Affects types of the following pattern:
        #       numpy.ndarray[PRIMITIVE_TYPE[*DIMS]]
        #       Annotated[numpy.ndarray, PRIMITIVE_TYPE, FixedSize[*DIMS]]

        result = super().parse_annotation_str(annotation_str)
        if (
            not isinstance(result, ResolvedType)
            or result.name != self.__ndarray_name
            or result.parameters is None
            or len(result.parameters) != 1
            or not isinstance(param := result.parameters[0], ResolvedType)
            or param.name not in self.numpy_primitive_types
            or param.parameters is None
            or any(not isinstance(dim, Value) for dim in param.parameters)
        ):
            return result

        # isinstance check is redundant, but makes mypy happy
        dims = [int(dim.repr) for dim in param.parameters if isinstance(dim, Value)]

        # override result with Annotated[...]
        result = ResolvedType(
            name=self.__annotated_name,
            parameters=[
                ResolvedType(self.__ndarray_name),
                ResolvedType(param.name),
            ],
        )

        if param.parameters is not None:
            # TRICK: Use `self.parse_type` to make `FixedSize`
            #        properly added to the list of imports
            self.handle_type(FixedSize)
            assert result.parameters is not None
            result.parameters += [self.handle_value(FixedSize(*dims))]

        return result


class FixNumpyArrayRemoveParameters(IParser):
    __ndarray_name = QualifiedName.from_str("numpy.ndarray")

    def parse_annotation_str(
        self, annotation_str: str
    ) -> ResolvedType | InvalidExpression | Value:
        result = super().parse_annotation_str(annotation_str)
        if isinstance(result, ResolvedType) and result.name == self.__ndarray_name:
            result.parameters = None
        return result


class FixRedundantMethodsFromBuiltinObject(IParser):
    def handle_method(self, path: QualifiedName, method: Any) -> list[Method]:
        result = super().handle_method(path, method)
        return [
            m
            for m in result
            if not (
                m.function.name == "__init__"
                and m.function.doc == object.__init__.__doc__
            )
        ]


class ReplaceReadWritePropertyWithField(IParser):
    def handle_class_member(
        self, path: QualifiedName, class_: type, obj: Any
    ) -> Docstring | Alias | Class | list[Method] | Field | Property | None:
        result = super().handle_class_member(path, class_, obj)
        if isinstance(result, Property):
            if (
                result.getter is not None
                and result.setter is not None
                and len(result.getter.args) == 1
                and len(result.setter.args) == 2
                and result.getter.returns == result.setter.args[1].annotation
            ):
                return Field(
                    attribute=Attribute(
                        name=result.name, annotation=result.getter.returns, value=None
                    ),
                    modifier=None,
                )
        return result


class FixMissingFixedSizeImport(IParser):
    def parse_annotation_str(
        self, annotation_str: str
    ) -> ResolvedType | InvalidExpression | Value:
        # Accommodate to
        # https://github.com/pybind/pybind11/pull/4679
        result = super().parse_annotation_str(annotation_str)
        if (
            isinstance(result, Value)
            and result.repr.startswith("FixedSize(")
            and result.repr.endswith(")")
        ):
            try:
                dimensions = map(
                    int,
                    result.repr[len("FixedSize(") : -len(")")].split(","),
                )
            except ValueError:
                pass
            else:
                # call `handle_type` to trigger implicit import
                self.handle_type(FixedSize)
                return self.handle_value(FixedSize(*dimensions))
        return result


class FixMissingEnumMembersAnnotation(IParser):
    __class_var_dict = ResolvedType(
        name=QualifiedName.from_str("typing.ClassVar"),
        parameters=[ResolvedType(name=QualifiedName.from_str("dict"))],
    )

    def handle_field(self, path: QualifiedName, field: Any) -> Field | None:
        result = super().handle_field(path, field)
        if result is None:
            return None
        if (
            path[-1] == "__members__"
            and isinstance(field, dict)
            and result.attribute.annotation == self.__class_var_dict
        ):
            assert isinstance(result.attribute.annotation, ResolvedType)
            dict_type = self._guess_dict_type(field)
            if dict_type is not None:
                result.attribute.annotation.parameters = [dict_type]
        return result

    def _guess_dict_type(self, d: dict) -> ResolvedType | None:
        if len(d) == 0:
            return None
        key_types = set()
        value_types = set()
        for key, value in d.items():
            key_types.add(self.handle_type(type(key)))
            value_types.add(self.handle_type(type(value)))
        if len(key_types) == 1:
            key_type = [ResolvedType(name=t) for t in key_types][0]
        else:
            union_t = self.parse_annotation_str("typing.Union")
            assert isinstance(union_t, ResolvedType)
            key_type = ResolvedType(
                name=union_t.name, parameters=[ResolvedType(name=t) for t in key_types]
            )
        if len(value_types) == 1:
            value_type = [ResolvedType(name=t) for t in value_types][0]
        else:
            union_t = self.parse_annotation_str("typing.Union")
            assert isinstance(union_t, ResolvedType)
            value_type = ResolvedType(
                name=union_t.name,
                parameters=[ResolvedType(name=t) for t in value_types],
            )
        dict_t = self.parse_annotation_str("typing.Dict")
        assert isinstance(dict_t, ResolvedType)
        return ResolvedType(
            name=dict_t.name,
            parameters=[key_type, value_type],
        )