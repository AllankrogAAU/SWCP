#!/bin/sh
N=$1
COMPILE_TIMEOUT=$2
LINT_TIMEOUT=$3
RUN_TIMEOUT=$4
CAP=${5:-65536}
W=/tmp/w

mkdir -p "$W" && cd "$W" || exit 90
head -c "$N" > submission.c

now_ms() { date +%s%3N; }
emit() { printf '%s=%s\n' "$1" "$2"; }

phase() {
    name=$1
    limit=$2
    shift 2
    started=$(now_ms)
    timeout -k 1 "$limit" "$@" < /dev/null > "$W/$name.out" 2> "$W/$name.err"
    rc=$?
    if [ "$name" = run ] && [ "$rc" -eq 153 ]; then rc=124; fi
    finished=$(now_ms)
    emit "${name}_rc" "$rc"
    emit "${name}_ms" "$((finished - started))"
    emit "${name}_stdout" "$(head -c "$CAP" "$W/$name.out" | base64 -w0)"
    emit "${name}_stderr" "$(head -c "$CAP" "$W/$name.err" | base64 -w0)"
    return "$rc"
}

phase compile "$COMPILE_TIMEOUT" gcc -Wall -Wextra submission.c -o "$W/prog"
if [ $? -ne 0 ]; then
    exit 0
fi

phase lint "$LINT_TIMEOUT" cppcheck --enable=warning,style,performance,portability --max-configs=1 \
    --suppress=normalCheckLevelMaxBranches --std=c11 --quiet \
    "--template={line}:{column}: {severity}: {message} [{id}]" submission.c

phase run "$RUN_TIMEOUT" bash -c 'ulimit -f 2048; ulimit -u 64; ulimit -c 0; exec "$0"' "$W/prog"
exit 0
