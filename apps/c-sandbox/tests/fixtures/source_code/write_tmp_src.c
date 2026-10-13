#include <stdio.h>

int main(void) {
    FILE *f = fopen("/tmp/scratch.txt", "w");
    printf("fopen_result=%s\n", f ? "success" : "failed");
    if (f) fclose(f);
    return 0;
}