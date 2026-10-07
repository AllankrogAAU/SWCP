from sandbox import factory as sandbox_factory

from . import result
from .compiler import compile_submission
from .executor import execute_submission
from .result import EvaluationResult


def evaluate(source: str) -> EvaluationResult:
    sandbox = sandbox_factory.create()
    try:
        compile_result = compile_submission(sandbox, source)

        if compile_result.exit_code != 0 or compile_result.timed_out:
            return result.from_compile_failure(compile_result)

        run_result = execute_submission(sandbox)
        return result.from_run(compile_result, run_result)
    finally:
        sandbox.destroy()