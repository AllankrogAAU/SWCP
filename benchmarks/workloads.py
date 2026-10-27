"""C source code workload archetypes for benchmarking SWCP pipeline."""

from __future__ import annotations

import random
from typing import Mapping

# 1. Passing C Code (clean compilation, executes cleanly and exits 0)
PASS_TEMPLATE = """#include <stdio.h>
#include <stdbool.h>

static bool is_prime(int n) {
    if (n <= 1) return false;
    for (int i = 2; i * i <= n; i++) {
        if (n % i == 0) return false;
    }
    return true;
}

int main(void) {
    int primes = 0;
    for (int i = 2; i < 200; i++) {
        if (is_prime(i)) primes++;
    }
    printf("Computed primes count: %d\\n", primes);
    return 0;
}
"""

# 2. Compile Error (syntax failure, fails ~80ms in gcc stage, forwarded to LLM)
COMPILE_ERROR_TEMPLATE = """#include <stdio.h>

int main(void) {
    int x = 42
    printf("The answer is: %d\\n", x);
    retrn 0;
}
"""

# 3. Runtime Error / Crash (compiles cleanly, SIGSEGV NULL pointer dereference in gVisor sandbox)
RUNTIME_ERROR_TEMPLATE = """#include <stdio.h>
#include <stdlib.h>

int main(void) {
    printf("Simulating fatal memory violation...\\n");
    fflush(stdout);
    volatile int *bad_ptr = NULL;
    *bad_ptr = 1337;
    return 0;
}
"""

# 4. Infinite Loop (compiles cleanly, burns 100% of 1 CPU core in gVisor until 2.0s timeout)
INFINITE_LOOP_TEMPLATE = """#include <stdio.h>

int main(void) {
    volatile long counter = 0;
    while (1) {
        counter++;
        if (counter == 0) {
            break;
        }
    }
    printf("Counter: %ld\\n", counter);
    return 0;
}
"""

ARCHETYPES = {
    "pass": PASS_TEMPLATE,
    "compile_error": COMPILE_ERROR_TEMPLATE,
    "runtime_error": RUNTIME_ERROR_TEMPLATE,
    "infinite_loop": INFINITE_LOOP_TEMPLATE,
}


def pick_archetype(weights: Mapping[str, float] | None = None) -> str:
    """Randomly select a workload archetype according to weights."""
    if not weights:
        weights = {
            "pass": 0.40,
            "compile_error": 0.30,
            "runtime_error": 0.20,
            "infinite_loop": 0.10,
        }
    keys = list(weights.keys())
    probs = [weights[k] for k in keys]
    return random.choices(keys, weights=probs, k=1)[0]


def get_workload_source(archetype: str, run_id: str, seq: int) -> str:
    """Retrieve C source code for the requested archetype with tracing header."""
    template = ARCHETYPES.get(archetype, PASS_TEMPLATE)
    header = f"/* bench_run={run_id} seq={seq} archetype={archetype} */\n"
    return header + template

