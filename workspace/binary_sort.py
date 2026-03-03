def binary_sort(arr):
    if isinstance(arr, str):
        raise TypeError("Input must be a list")
    return sorted(arr, key=lambda x: (x is None, 0 if x is None else x))
