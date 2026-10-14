from sandbox.interface import RunResult, Sandbox

from .compiler import BINARY_NAME

RUN_TIMEOUT_SECONDS = 2


def execute_submission(sandbox: Sandbox) -> RunResult:
    return sandbox.run([f"./{BINARY_NAME}"], timeout=RUN_TIMEOUT_SECONDS)