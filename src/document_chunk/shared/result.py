from dataclasses import dataclass
from typing import Callable, Generic, Literal, NoReturn, TypeVar, Union

T = TypeVar("T")
E = TypeVar("E", bound=Exception)
U = TypeVar("U")


@dataclass(frozen=True)
class Ok(Generic[T]):
    value: T

    def is_ok(self) -> Literal[True]:
        return True

    def is_err(self) -> Literal[False]:
        return False

    @property
    def error(self) -> NoReturn:
        """Keep ``Result.error`` type-safe while preserving invalid access failure."""

        raise AttributeError("Ok has no error")

    def unwrap(self) -> T:
        return self.value

    def unwrap_or(self, default: T) -> T:
        return self.value

    def map(self, fn: Callable[[T], U]) -> "Ok[U]":
        return Ok(fn(self.value))

    def __repr__(self) -> str:
        return f"Ok({self.value!r})"


@dataclass(frozen=True)
class Err(Generic[E]):
    error: E

    def is_ok(self) -> Literal[False]:
        return False

    def is_err(self) -> Literal[True]:
        return True

    def unwrap(self) -> NoReturn:
        raise self.error

    def unwrap_or(self, default: T) -> T:
        return default

    def map(self, fn: Callable) -> "Err[E]":
        return self  # propagate error, skip fn

    def __repr__(self) -> str:
        return f"Err({self.error!r})"


# Result[T, E] = Ok[T] | Err[E]
Result = Union[Ok[T], Err[E]]
