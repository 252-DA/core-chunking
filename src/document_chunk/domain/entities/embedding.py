from dataclasses import dataclass


@dataclass(frozen=True)
class Embedding:
    """
    Vector embedding của một chunk.
    Immutable — một chunk + model → một embedding duy nhất.
    """
    chunk_id: str
    vector: tuple[float, ...]  # tuple để immutable (list không hashable)
    model: str                 # tên model, e.g. "BAAI/bge-m3"
    dimension: int             # số chiều vector, e.g. 1024

    def __post_init__(self) -> None:
        if len(self.vector) != self.dimension:
            raise ValueError(
                f"Vector dimension mismatch: expected {self.dimension}, got {len(self.vector)}"
            )
