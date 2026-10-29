#!/bin/sh
N=$1
COMPILE_TIMEOUT=$2
LINT_TIMEOUT=$3
RUN_TIMEOUT=$4
CAP=${5:-65536}
TEST_COUNT=${6:-0}
HARNESS_LENGTH=${7:-0}
RUN_INPUT_LENGTH=${8:-0}
W=/tmp/w

mkdir -p "$W" && cd "$W" || exit 90
head -c "$N" > submission.c
head -c "$HARNESS_LENGTH" > harness.c
head -c "$RUN_INPUT_LENGTH" > run.in
shift 8

now_ms() { date +%s%3N; }
emit() { printf '%s=%s\n' "$1" "$2"; }

phase() {
    name=$1
    limit=$2
    input=$3
    shift 3
    started=$(now_ms)
    timeout -k 1 "$limit" "$@" < "$input" > "$W/$name.out" 2> "$W/$name.err"
    rc=$?
    case "$name" in
        run|test_*)
            if [ "$rc" -eq 153 ]; then rc=124; fi
            ;;
    esac
    finished=$(now_ms)
    emit "${name}_rc" "$rc"
    emit "${name}_ms" "$((finished - started))"
    emit "${name}_stdout" "$(head -c "$CAP" "$W/$name.out" | base64 -w0)"
    emit "${name}_stderr" "$(head -c "$CAP" "$W/$name.err" | base64 -w0)"
    return "$rc"
}

milliseconds_to_seconds() {
    seconds=$(( $1 / 1000 ))
    milliseconds=$(( $1 % 1000 ))
    printf '%s.%03d' "$seconds" "$milliseconds"
}

if [ "$HARNESS_LENGTH" -gt 0 ]; then
    phase compile "$COMPILE_TIMEOUT" /dev/null gcc -Wall -Wextra submission.c harness.c -o "$W/prog"
else
    phase compile "$COMPILE_TIMEOUT" /dev/null gcc -Wall -Wextra submission.c -o "$W/prog"
fi
if [ $? -ne 0 ]; then
    exit 0
fi

phase lint "$LINT_TIMEOUT" /dev/null cppcheck --enable=warning,style,performance,portability --max-configs=1 \
    --suppress=normalCheckLevelMaxBranches --std=c11 --quiet \
    "--template={line}:{column}: {severity}: {message} [{id}]" submission.c

if [ "$TEST_COUNT" -eq 0 ]; then
    phase run "$RUN_TIMEOUT" "$W/run.in" bash -c 'ulimit -f 2048; ulimit -u 64; ulimit -c 0; exec "$0"' "$W/prog"
else
    index=0
    while [ "$index" -lt "$TEST_COUNT" ]; do
        input_length=$1
        timeout_ms=$2
        memory_mb=$3
        shift 3
        head -c "$input_length" > "$W/test-$index.in"
        timeout_limit=$(milliseconds_to_seconds "$timeout_ms")
        memory_limit_kb=$((memory_mb * 1024))
        phase "test_$index" "$timeout_limit" "$W/test-$index.in" \
            bash -c 'ulimit -f 2048; ulimit -u 64; ulimit -c 0; ulimit -v "$1"; shift; exec "$@"' \
            test "$memory_limit_kb" "$W/prog"
        index=$((index + 1))
    done
fi
exit 0
