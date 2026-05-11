import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import fibonacci
import pytest


def test_fibonacci_0_returns_0():
    assert fibonacci.fibonacci(0) == 0


def test_fibonacci_1_returns_1():
    assert fibonacci.fibonacci(1) == 1


def test_fibonacci_2_returns_1():
    assert fibonacci.fibonacci(2) == 1


def test_fibonacci_3_returns_2():
    assert fibonacci.fibonacci(3) == 2


def test_fibonacci_4_returns_3():
    assert fibonacci.fibonacci(4) == 3


def test_fibonacci_5_returns_5():
    assert fibonacci.fibonacci(5) == 5


def test_fibonacci_10_returns_55():
    assert fibonacci.fibonacci(10) == 55


def test_fibonacci_large_n_returns_correct_value():
    assert fibonacci.fibonacci(20) == 6765


def test_fibonacci_negative_input_raises_value_error():
    with pytest.raises(ValueError):
        fibonacci.fibonacci(-1)


def test_fibonacci_boolean_input_raises_type_error():
    with pytest.raises(TypeError):
        fibonacci.fibonacci(True)


def test_fibonacci_float_input_raises_type_error():
    with pytest.raises(TypeError):
        fibonacci.fibonacci(2.5)


def test_fibonacci_string_input_raises_type_error():
    with pytest.raises(TypeError):
        fibonacci.fibonacci("5")


def test_fibonacci_list_input_raises_type_error():
    with pytest.raises(TypeError):
        fibonacci.fibonacci([5])


def test_fibonacci_returns_integer_type():
    assert isinstance(fibonacci.fibonacci(5), int)
