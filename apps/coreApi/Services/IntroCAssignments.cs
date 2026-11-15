using SWCP.Contracts;

namespace coreApi.Services;

internal static class IntroCAssignments
{
    public static readonly AssignmentSeed[] All =
    [
        new(
            Id(1),
            "01. Hello, kursus (L1)",
            "Write a complete C program that prints exactly \"Hello, kursus! C programming.\" followed by a newline.",
            "L1: A beginner main function and exact standard output.",
            [Test("", "Hello, kursus! C programming.\n")]),
        new(
            Id(2),
            "02. Temperature converter (L2)",
            "Implement double celsius_to_fahrenheit(double c). Return the Fahrenheit equivalent of celsius.",
            "L2: Implement the declared function and use floating-point arithmetic.",
            [Test("0\n", "32.00\n"), Test("100\n", "212.00\n")],
            """
            #include <stdio.h>
            double celsius_to_fahrenheit(double c);
            int main(void) {
                double c;
                if (scanf("%lf", &c) != 1) return 1;
                printf("%.2f\n", celsius_to_fahrenheit(c));
                return 0;
            }
            """),
        new(
            Id(3),
            "03. Seconds to h/m/s (L2)",
            "Read a non-negative number of seconds with scanf and print the equivalent hours, minutes, and seconds as \"H hours, M minutes, S seconds\".",
            "L2: Read from standard input, use integer division and remainder, and print the required format.",
            [Test("3661\n", "1 hours, 1 minutes, 1 seconds\n"), Test("59\n", "0 hours, 0 minutes, 59 seconds\n")]),
        new(
            Id(4),
            "04. Leap year (L3)",
            "Implement int is_leap_year(int year). Return 1 when year is a Gregorian leap year and 0 otherwise.",
            "L3: Apply the Gregorian rules: divisible by 400, or divisible by 4 but not by 100.",
            [Test("2000\n", "1\n"), Test("1900\n", "0\n"), Test("2024\n", "1\n"), Test("2023\n", "0\n")],
            """
            #include <stdio.h>
            int is_leap_year(int year);
            int main(void) {
                int year;
                if (scanf("%d", &year) != 1) return 1;
                printf("%d\n", is_leap_year(year));
                return 0;
            }
            """),
        new(
            Id(5),
            "05. 7-trinsskala (L3)",
            "Implement int grade_from_points(int points). For this sample exercise use these bands: 90-100 => 12, 80-89 => 10, 70-79 => 7, 60-69 => 4, 50-59 => 2, 40-49 => 0, and 0-39 => -3. This is example data, not an official grading conversion.",
            "L3: Use ordered threshold checks to map points to the seven sample grade values.",
            [Test("95\n", "12\n"), Test("80\n", "10\n"), Test("70\n", "7\n"), Test("60\n", "4\n"), Test("50\n", "2\n"), Test("40\n", "0\n"), Test("39\n", "-3\n")],
            """
            #include <stdio.h>
            int grade_from_points(int points);
            int main(void) {
                int points;
                if (scanf("%d", &points) != 1) return 1;
                printf("%d\n", grade_from_points(points));
                return 0;
            }
            """),
        new(
            Id(6),
            "06. Switch calculator (L3)",
            "Implement int calc(int a, char op, int b) using a switch for +, -, *, and /. Use integer division; return 0 for division by zero or an unsupported operator.",
            "L3: Implement the arithmetic cases with switch and guard the division case against zero.",
            [Test("8 + 2\n", "10\n"), Test("9 / 2\n", "4\n"), Test("4 / 0\n", "0\n"), Test("2 * 3\n", "6\n")],
            """
            #include <stdio.h>
            int calc(int a, char op, int b);
            int main(void) {
                int a, b;
                char op;
                if (scanf("%d %c %d", &a, &op, &b) != 3) return 1;
                printf("%d\n", calc(a, op, b));
                return 0;
            }
            """),
        new(
            Id(7),
            "07. Digit sum (L4)",
            "Implement int digit_sum(int n). Return the sum of the decimal digits, treating a negative input by its magnitude.",
            "L4: Handle zero and negative values, then accumulate digits in a loop.",
            [Test("1234\n", "10\n"), Test("-123\n", "6\n"), Test("0\n", "0\n")],
            """
            #include <stdio.h>
            int digit_sum(int n);
            int main(void) {
                int n;
                if (scanf("%d", &n) != 1) return 1;
                printf("%d\n", digit_sum(n));
                return 0;
            }
            """),
        new(
            Id(8),
            "08. Prime check (L4)",
            "Implement int is_prime(int n). Return 1 for primes and 0 otherwise; values less than 2 are not prime.",
            "L4: Handle values below 2 and test possible divisors only up to the square root.",
            [Test("2\n", "1\n"), Test("21\n", "0\n"), Test("1\n", "0\n"), Test("97\n", "1\n")],
            """
            #include <stdio.h>
            int is_prime(int n);
            int main(void) {
                int n;
                if (scanf("%d", &n) != 1) return 1;
                printf("%d\n", is_prime(n));
                return 0;
            }
            """),
        new(
            Id(9),
            "09. Star triangle (L4)",
            "Write a complete C program that reads a positive integer n and prints n rows: row 1 has one '*', row 2 has two, and so on. Each row ends with a newline.",
            "L4: Read input in main and use nested loops to print a left-aligned triangle.",
            [Test("3\n", "*\n**\n***\n"), Test("1\n", "*\n")]),
        new(
            Id(10),
            "10. Max of three (L5)",
            "Implement int max3(int a, int b, int c), ideally by using a helper function max2.",
            "L5: Compare all three values, including ties and negative inputs.",
            [Test("3 8 5\n", "8\n"), Test("-7 -2 -5\n", "-2\n"), Test("4 4 1\n", "4\n")],
            """
            #include <stdio.h>
            int max3(int a, int b, int c);
            int main(void) {
                int a, b, c;
                if (scanf("%d %d %d", &a, &b, &c) != 3) return 1;
                printf("%d\n", max3(a, b, c));
                return 0;
            }
            """),
        new(
            Id(11),
            "11. GCD (L5)",
            "Implement int gcd(int a, int b) iteratively with Euclid's algorithm. Return a non-negative greatest common divisor.",
            "L5: Use the iterative Euclidean algorithm and handle zero and negative inputs.",
            [Test("48 18\n", "6\n"), Test("-27 9\n", "9\n"), Test("0 5\n", "5\n")],
            """
            #include <stdio.h>
            int gcd(int a, int b);
            int main(void) {
                int a, b;
                if (scanf("%d %d", &a, &b) != 2) return 1;
                printf("%d\n", gcd(a, b));
                return 0;
            }
            """),
        new(
            Id(12),
            "12. Power without pow (L5)",
            "Implement long power(int base, int exp) without using pow. Return 1 for an exponent of zero and 0 for a negative exponent.",
            "L5: Multiply iteratively; no math-library pow call is needed.",
            [Test("2 10\n", "1024\n"), Test("5 0\n", "1\n"), Test("3 4\n", "81\n"), Test("2 -1\n", "0\n")],
            """
            #include <stdio.h>
            long power(int base, int exp);
            int main(void) {
                int base, exp;
                if (scanf("%d %d", &base, &exp) != 2) return 1;
                printf("%ld\n", power(base, exp));
                return 0;
            }
            """),
        new(
            Id(13),
            "13. Quadratic roots (L6)",
            "Implement int solve_quadratic(double a, double b, double c, double *r1, double *r2). Return the number of real roots and write them through the output parameters in ascending order. If a is zero, solve the remaining linear equation.",
            "L6: Handle positive, zero, and negative discriminants, repeated roots, and the linear a == 0 case.",
            [Test("1 0 -1\n", "2 -1.00 1.00\n"), Test("1 2 1\n", "1 -1.00\n"), Test("1 0 1\n", "0\n"), Test("0 2 -4\n", "1 2.00\n")],
            """
            #include <stdio.h>
            int solve_quadratic(double a, double b, double c, double *r1, double *r2);
            int main(void) {
                double a, b, c, r1 = 0.0, r2 = 0.0;
                int count;
                if (scanf("%lf %lf %lf", &a, &b, &c) != 3) return 1;
                count = solve_quadratic(a, b, c, &r1, &r2);
                printf("%d", count);
                if (count > 0) printf(" %.2f", r1);
                if (count > 1) printf(" %.2f", r2);
                putchar('\n');
                return 0;
            }
            """),
        new(
            Id(14),
            "14. ID generator (L6)",
            "Implement int next_id(void) using a static local variable. Consecutive calls must return 1, 2, 3, and so on.",
            "L6: Initialize a static counter so its value is retained between calls.",
            [Test("", "1 2 3\n")],
            """
            #include <stdio.h>
            int next_id(void);
            int main(void) {
                int first = next_id();
                int second = next_id();
                int third = next_id();
                printf("%d %d %d\n", first, second, third);
                return 0;
            }
            """),
        new(
            Id(15),
            "15. Defensive BMI (L7)",
            "Implement double bmi(double kg, double height_m). Return -1 for non-positive weight or height; otherwise return kg / (height_m * height_m). Add a brief header comment to your submission.",
            "L7: Validate inputs before dividing and include a short explanatory comment in the submitted source.",
            [Test("70 1.75\n", "22.86\n"), Test("0 1.7\n", "-1.00\n"), Test("70 0\n", "-1.00\n")],
            """
            #include <stdio.h>
            double bmi(double kg, double height_m);
            int main(void) {
                double kg, height_m;
                if (scanf("%lf %lf", &kg, &height_m) != 2) return 1;
                printf("%.2f\n", bmi(kg, height_m));
                return 0;
            }
            """),
        new(
            Id(16),
            "16. Weekday enum (L8)",
            "Define enum weekday with Sunday = 0 through Saturday = 6, then implement enum weekday next_day(enum weekday d). Saturday wraps to Sunday.",
            "L8: Include the enum definition in your submitted source and wrap the last weekday to the first.",
            [Test("0\n", "1\n"), Test("6\n", "0\n"), Test("3\n", "4\n")],
            """
            #include <stdio.h>
            enum weekday { Sunday = 0, Monday, Tuesday, Wednesday, Thursday, Friday, Saturday };
            enum weekday next_day(enum weekday d);
            int main(void) {
                int day;
                if (scanf("%d", &day) != 1) return 1;
                printf("%d\n", (int)next_day((enum weekday)day));
                return 0;
            }
            """),
        new(
            Id(17),
            "17. Exact average (L8)",
            "Implement double average3(int a, int b, int c). Return the exact arithmetic mean, including fractional results.",
            "L8: Convert to floating point before division to avoid integer division.",
            [Test("1 2 2\n", "1.67\n"), Test("-1 0 2\n", "0.33\n")],
            """
            #include <stdio.h>
            double average3(int a, int b, int c);
            int main(void) {
                int a, b, c;
                if (scanf("%d %d %d", &a, &b, &c) != 3) return 1;
                printf("%.2f\n", average3(a, b, c));
                return 0;
            }
            """),
        new(
            Id(18),
            "18. Array max (L9)",
            "Implement int array_max(const int a[], int n). For this exercise, n is positive; return the largest element, including when all elements are negative.",
            "L9: Traverse the array without assuming its first item is the maximum.",
            [Test("4 1 8 2 -3\n", "8\n"), Test("3 -5 -1 -9\n", "-1\n")],
            """
            #include <stdio.h>
            int array_max(const int a[], int n);
            int main(void) {
                int n, a[128];
                if (scanf("%d", &n) != 1 || n < 1 || n > 128) return 1;
                for (int i = 0; i < n; ++i) if (scanf("%d", &a[i]) != 1) return 1;
                printf("%d\n", array_max(a, n));
                return 0;
            }
            """),
        new(
            Id(19),
            "19. Reverse in place (L9)",
            "Implement void reverse(int a[], int n) to reverse the array in place without allocating another array.",
            "L9: Swap elements from both ends and stop at the midpoint.",
            [Test("4 1 2 3 4\n", "4 3 2 1\n"), Test("1 7\n", "7\n")],
            """
            #include <stdio.h>
            void reverse(int a[], int n);
            int main(void) {
                int n, a[128];
                if (scanf("%d", &n) != 1 || n < 1 || n > 128) return 1;
                for (int i = 0; i < n; ++i) if (scanf("%d", &a[i]) != 1) return 1;
                reverse(a, n);
                for (int i = 0; i < n; ++i) printf("%s%d", i ? " " : "", a[i]);
                putchar('\n');
                return 0;
            }
            """),
        new(
            Id(20),
            "20. Linear search (L9)",
            "Implement int find_index(const int a[], int n, int target). Return the first matching zero-based index, or -1 when not found.",
            "L9: Search from the beginning and return the index rather than the value.",
            [Test("4 2 4 2 7 2\n", "1\n"), Test("3 9 4 2 7\n", "-1\n")],
            """
            #include <stdio.h>
            int find_index(const int a[], int n, int target);
            int main(void) {
                int n, target, a[128];
                if (scanf("%d", &n) != 1 || n < 1 || n > 128 || scanf("%d", &target) != 1) return 1;
                for (int i = 0; i < n; ++i) if (scanf("%d", &a[i]) != 1) return 1;
                printf("%d\n", find_index(a, n, target));
                return 0;
            }
            """),
        new(
            Id(21),
            "21. Dynamic range (L9)",
            "Implement int *make_range(int n). Allocate an array containing 1 through n; return NULL when n is not positive. The caller owns and must free the returned array.",
            "L9: Allocate the correct number of integers, check allocation and input, and return owned memory.",
            [Test("4\n", "1 2 3 4\n"), Test("0\n", "NULL\n")],
            """
            #include <stdio.h>
            #include <stdlib.h>
            int *make_range(int n);
            int main(void) {
                int n;
                if (scanf("%d", &n) != 1) return 1;
                int *values = make_range(n);
                if (values == NULL) {
                    puts("NULL");
                    return 0;
                }
                for (int i = 0; i < n; ++i) printf("%s%d", i ? " " : "", values[i]);
                putchar('\n');
                free(values);
                return 0;
            }
            """),
        new(
            Id(22),
            "22. Own strlen (L10)",
            "Implement int my_strlen(const char *s) without including or using <string.h>. Return the number of characters before the null terminator.",
            "L10: Walk a const string pointer or index until the null terminator.",
            [Test("hello\n", "5\n"), Test("C\n", "1\n")],
            """
            #include <stdio.h>
            int my_strlen(const char *s);
            int main(void) {
                char s[128];
                if (scanf("%127s", s) != 1) return 1;
                printf("%d\n", my_strlen(s));
                return 0;
            }
            """),
        new(
            Id(23),
            "23. Palindrome (L10)",
            "Implement int is_palindrome(const char *s). Return 1 when the string reads the same in both directions and 0 otherwise. Compare characters exactly; ignore neither case nor punctuation.",
            "L10: Compare matching characters from the start and end without modifying the input.",
            [Test("radar\n", "1\n"), Test("hello\n", "0\n"), Test("level\n", "1\n")],
            """
            #include <stdio.h>
            int is_palindrome(const char *s);
            int main(void) {
                char s[128];
                if (scanf("%127s", s) != 1) return 1;
                printf("%d\n", is_palindrome(s));
                return 0;
            }
            """),
        new(
            Id(24),
            "24. Factorial (L11)",
            "Implement long factorial(int n) recursively. Return 1 for 0 and 1; return 0 for negative inputs. Tests use values whose result fits in long.",
            "L11: Use a recursive base case and recursive step.",
            [Test("0\n", "1\n"), Test("5\n", "120\n"), Test("10\n", "3628800\n"), Test("-1\n", "0\n")],
            """
            #include <stdio.h>
            long factorial(int n);
            int main(void) {
                int n;
                if (scanf("%d", &n) != 1) return 1;
                printf("%ld\n", factorial(n));
                return 0;
            }
            """),
        new(
            Id(25),
            "25. Fibonacci (L11)",
            "Implement int fib(int n) recursively using F(0) = 0 and F(1) = 1. Tests use non-negative values whose results fit in int.",
            "L11: Use the two recursive base cases and the recurrence fib(n - 1) + fib(n - 2).",
            [Test("0\n", "0\n"), Test("1\n", "1\n"), Test("10\n", "55\n")],
            """
            #include <stdio.h>
            int fib(int n);
            int main(void) {
                int n;
                if (scanf("%d", &n) != 1) return 1;
                printf("%d\n", fib(n));
                return 0;
            }
            """)
    ];

    private static Guid Id(int number) =>
        Guid.Parse($"b0000000-0000-4000-8000-{number:D12}");

    private static SandboxTestCase Test(string stdin, string expectedStdout) =>
        new(stdin, expectedStdout, 2000, 64);
}

internal sealed record AssignmentSeed(
    Guid Id,
    string Title,
    string Description,
    string TaskFocus,
    IReadOnlyList<SandboxTestCase> TestCases,
    string? HarnessSource = null);
