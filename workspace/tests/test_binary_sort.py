import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import pytest
import binary_sort


def test_returns_sorted_integers():
    arr = [3, 1, 4, 1, 5]
    result = binary_sort.binary_sort(arr)
    assert result == sorted(arr)


def test_original_list_not_modified():
    arr = [3, 1, 4, 1, 5]
    binary_sort.binary_sort(arr)
    assert arr == [3, 1, 4, 1, 5]


def test_returns_sorted_empty_list():
    assert binary_sort.binary_sort([]) == []


def test_returns_single_element_list():
    assert binary_sort.binary_sort([5]) == [5]


def test_returns_already_sorted_list():
    arr = [1, 2, 3, 4, 5]
    result = binary_sort.binary_sort(arr)
    assert result == arr


def test_returns_reverse_sorted_list():
    arr = [5, 4, 3, 2, 1]
    result = binary_sort.binary_sort(arr)
    assert result == sorted(arr)


def test_returns_sorted_with_negatives():
    arr = [-5, 0, 3, -1]
    result = binary_sort.binary_sort(arr)
    assert result == sorted(arr)


def test_returns_sorted_mixed_numeric_types():
    arr = [3, 1.5, 4, 2.7, 5]
    result = binary_sort.binary_sort(arr)
    assert result == sorted(arr)


def test_raises_type_error_on_incomparable_elements():
    with pytest.raises(TypeError):
        binary_sort.binary_sort([1, "a", 3])


def test_raises_type_error_on_string_input():
    with pytest.raises(TypeError):
        binary_sort.binary_sort("hello")


def test_raises_type_error_on_integer_input():
    with pytest.raises(TypeError):
        binary_sort.binary_sort(5)


def test_returns_list_type():
    arr = [3, 1, 4]
    result = binary_sort.binary_sort(arr)
    assert isinstance(result, list)


def test_returns_new_list_instance():
    arr = [3, 1, 4]
    result = binary_sort.binary_sort(arr)
    assert result is not arr


def test_returns_sorted_with_duplicate_values():
    arr = [2, 3, 2, 1, 4, 1]
    result = binary_sort.binary_sort(arr)
    assert result == sorted(arr)


def test_returns_sorted_with_large_values():
    arr = [100000, -99999, 0, 50000]
    result = binary_sort.binary_sort(arr)
    assert result == sorted(arr)


def test_returns_sorted_with_floats():
    arr = [3.14, 2.71, 1.618]
    result = binary_sort.binary_sort(arr)
    assert result == sorted(arr)


def test_returns_sorted_with_mixed_types_that_compare():
    arr = [1, 2.0, 3.0, 2]
    result = binary_sort.binary_sort(arr)
    assert result == sorted(arr)
