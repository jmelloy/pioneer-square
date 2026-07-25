"""Simple Fibonacci implementations for testing."""


def fibonacci_iterative(n):
    """Return the nth Fibonacci number using iteration."""
    if n <= 0:
        return 0
    if n == 1:
        return 1
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b


def fibonacci_recursive(n):
    """Return the nth Fibonacci number using recursion."""
    if n <= 0:
        return 0
    if n == 1:
        return 1
    return fibonacci_recursive(n - 1) + fibonacci_recursive(n - 2)


if __name__ == "__main__":
    print("First 20 Fibonacci numbers:")
    print()
    print(f"{'n':<4} {'Iterative':<15} {'Recursive':<15}")
    print("-" * 34)
    for i in range(20):
        iter_result = fibonacci_iterative(i)
        rec_result = fibonacci_recursive(i)
        print(f"{i:<4} {iter_result:<15} {rec_result:<15}")
