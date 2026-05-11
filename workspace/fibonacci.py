def fibonacci(n):
    if not isinstance(n, int):
        raise TypeError("Input must be an integer.")
    if isinstance(n, bool):
        raise TypeError("Boolean input is not allowed.")
    if n < 0:
        raise ValueError("Input must be a non-negative integer.")
    if n == 0:
        return 0
    elif n == 1:
        return 1
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b
