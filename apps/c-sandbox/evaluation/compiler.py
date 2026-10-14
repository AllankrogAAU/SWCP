from sandbox.interface import RunResult, Sandbox

SOURCE_FILENAME = "submission.c"
BINARY_NAME = "submission"
COMPILE_TIMEOUT_SECONDS = 10


def compile_submission(sandbox: Sandbox, source: str) -> RunResult:
    sandbox.write_file(SOURCE_FILENAME, source)
    return sandbox.run(
        ["gcc", "-Wall", "-Wextra", SOURCE_FILENAME, "-o", BINARY_NAME],
        timeout=COMPILE_TIMEOUT_SECONDS,
    )