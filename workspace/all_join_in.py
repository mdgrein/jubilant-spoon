from fibonacci import fibonacci
from binary_sort import binary_sort
from case_converter import convert_case


def run_all():
    print("First 10 Fibonacci numbers:")
    for i in range(10):
        print(fibonacci(i), end=" ")
    print("\n")

    sample_list = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5]
    sorted_list = binary_sort(sample_list)
    print("Sorted list:", sorted_list)
    print()

    test_strings = ["HelloWorld", "TestString", "AnotherExample"]
    for s in test_strings:
        print(f"Original: {s}")
        print(f"Lowercase: {convert_case(s, 'lower')}")
        print(f"Uppercase: {convert_case(s, 'upper')}")
        print(f"Kebab-case: {convert_case(s, 'kebab')}")
        print(f"CamelCase: {convert_case(s, 'camel')}")
        print()


if __name__ == "__main__":
    run_all()
