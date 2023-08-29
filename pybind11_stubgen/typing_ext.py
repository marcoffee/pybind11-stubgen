class FixedSize:
    def __init__(self, *dim: int):
        self.dim: tuple[int, ...] = dim

    def __repr__(self):
        return (
            f"{self.__module__}."
            f"{self.__class__.__qualname__}"
            f"({', '.join(str(d) for d in self.dim)})"
        )
