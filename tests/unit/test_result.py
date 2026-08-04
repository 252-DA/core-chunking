"""
Tests for shared/result.py — Ok và Err types.
"""
import pytest

from document_chunk.shared.result import Err, Ok


class TestOk:
    def test_is_ok_true(self):
        assert Ok(42).is_ok() is True

    def test_is_err_false(self):
        assert Ok(42).is_err() is False

    def test_error_access_still_fails(self):
        with pytest.raises(AttributeError, match="Ok has no error"):
            _ = Ok(42).error

    def test_unwrap_returns_value(self):
        assert Ok("hello").unwrap() == "hello"

    def test_unwrap_none_value(self):
        assert Ok(None).unwrap() is None

    def test_unwrap_or_returns_value_not_default(self):
        assert Ok(42).unwrap_or(0) == 42

    def test_map_transforms_value(self):
        result = Ok(10).map(lambda x: x * 2)
        assert result.is_ok()
        assert result.unwrap() == 20

    def test_map_returns_new_ok(self):
        original = Ok(5)
        mapped = original.map(lambda x: str(x))
        assert isinstance(mapped, Ok)
        assert mapped.unwrap() == "5"

    def test_repr(self):
        assert repr(Ok(1)) == "Ok(1)"

    def test_ok_with_list(self):
        result = Ok([1, 2, 3])
        assert result.unwrap() == [1, 2, 3]


class TestErr:
    def test_is_ok_false(self):
        assert Err(ValueError("oops")).is_ok() is False

    def test_is_err_true(self):
        assert Err(ValueError("oops")).is_err() is True

    def test_unwrap_raises_original_exception(self):
        error = ValueError("test error")
        with pytest.raises(ValueError, match="test error"):
            Err(error).unwrap()

    def test_unwrap_raises_exact_instance(self):
        error = RuntimeError("specific")
        with pytest.raises(RuntimeError):
            Err(error).unwrap()

    def test_unwrap_or_returns_default(self):
        assert Err(ValueError()).unwrap_or("default") == "default"

    def test_unwrap_or_returns_none_default(self):
        assert Err(ValueError()).unwrap_or(None) is None

    def test_map_skips_fn_propagates_error(self):
        error = RuntimeError("failed")
        calls = []
        result = Err(error).map(lambda x: calls.append(x) or x)
        assert result.is_err()
        assert result.error is error
        assert calls == []  # fn never called

    def test_repr_contains_err(self):
        assert repr(Err(ValueError("x"))).startswith("Err(")


class TestResultComposition:
    def test_ok_map_chain(self):
        result = Ok(5).map(lambda x: x + 1).map(lambda x: x * 2)
        assert result.unwrap() == 12

    def test_err_map_chain_short_circuits(self):
        calls = []
        result = (
            Err(ValueError("fail"))
            .map(lambda x: calls.append("first") or x)
            .map(lambda x: calls.append("second") or x)
        )
        assert result.is_err()
        assert calls == []

    def test_ok_then_err_behavior(self):
        # If the mapped fn produces an Err-like value that's actually an Ok...
        # Ok.map always returns Ok(fn(value)) — it doesn't "catch" exceptions
        with pytest.raises(ZeroDivisionError):
            Ok(0).map(lambda x: 1 / x).unwrap()

    def test_type_preservation_in_err(self):
        error = ConnectionError("db down")
        result = Err(error)
        assert isinstance(result.error, ConnectionError)
