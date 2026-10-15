#include <stdio.h>
#include <string.h>

#define LIMIT 50

static int is_prime(int n) {
    if (n < 2) return 0;
    for (int i = 2; i * i <= n; i++) {
        if (n % i == 0) return 0;
    }
    return 1;
}

static unsigned long fib(int n) {
    unsigned long a = 0, b = 1;
    for (int i = 0; i < n; i++) {
        unsigned long next = a + b;
        a = b;
        b = next;
    }
    return a;
}

static void reverse(char *s) {
    size_t len = strlen(s);
    for (size_t i = 0; i < len / 2; i++) {
        char tmp = s[i];
        s[i] = s[len - 1 - i];
        s[len - 1 - i] = tmp;
    }
}

int main(void) {
    printf("Primes up to %d:", LIMIT);
    for (int n = 2; n <= LIMIT; n++) {
        if (is_prime(n)) printf(" %d", n);
    }
    printf("\n");

    printf("Fibonacci:");
    for (int i = 0; i < 15; i++) printf(" %lu", fib(i));
    printf("\n");

    int values[] = {42, 7, 19, 3, 88, 61, 25};
    int count = (int)(sizeof(values) / sizeof(values[0]));
    int min = values[0], max = values[0], sum = 0;
    for (int i = 0; i < count; i++) {
        if (values[i] < min) min = values[i];
        if (values[i] > max) max = values[i];
        sum += values[i];
    }
    printf("min=%d max=%d mean=%.2f\n", min, max, (double)sum / count);

    char word[] = "sandbox";
    reverse(word);
    printf("reversed: %s\n", word);
    return 0;
}