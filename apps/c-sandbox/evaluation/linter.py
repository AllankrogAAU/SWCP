from sandbox.interface import RunResult, Sandbox

from .compiler import SOURCE_FILENAME

LINT_TIMEOUT_SECONDS = 10


def lint_submission(sandbox: Sandbox) -> RunResult:
    """Run cppcheck on the already-written submission.c inside the sandbox.
    cppcheck reports findings on stderr and exits 0 even when it finds
    issues.
    """
    return sandbox.run(
        [
            "cppcheck",
            "--enable=warning,style,performance,portability",
            "--max-configs=1",  # don't explore every #ifdef combination (time/memory DoS)
            "--suppress=normalCheckLevelMaxBranches",
            "--std=c11",
            "--quiet",
            "--template={line}:{column}: {severity}: {message} [{id}]",
            SOURCE_FILENAME,
        ],
        timeout=LINT_TIMEOUT_SECONDS,
    )