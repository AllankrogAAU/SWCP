from uuid import uuid4

from contracts import SandboxTask
from evaluation.result import EvaluationResult, Status
from worker import evaluate_task


class FakeProvider:
    pass


def task() -> SandboxTask:
    return SandboxTask(
        submissionId=uuid4(),
        assignmentId=uuid4(),
        source="int main(void) { return 0; }",
        compilerFlags=[],
        testCases=[],
    )


def test_test_stage_executes_after_successful_compilation(monkeypatch) -> None:
    monkeypatch.setattr(
        "worker.evaluate",
        lambda source, provider, limits: EvaluationResult(
            status=Status.SUCCESS,
            compile_stdout="compiled",
            compile_stderr="",
            compile_ms=1,
            exit_code=0,
            stdout="expected",
            stderr="",
            run_ms=1,
        ),
    )

    result = evaluate_task(task(), FakeProvider())

    assert result.compilation.success is True
    assert len(result.testResults) == 1
    assert result.testResults[0].passed is True
    assert result.testResults[0].stdout == "expected"


def test_test_stage_is_skipped_when_compilation_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "worker.evaluate",
        lambda source, provider, limits: EvaluationResult(
            status=Status.COMPILE_ERROR,
            compile_stdout="",
            compile_stderr="error",
            compile_ms=1,
        ),
    )

    result = evaluate_task(task(), FakeProvider())

    assert result.compilation.success is False
    assert result.testResults == []


def test_test_case_passes_only_with_zero_exit_status(monkeypatch) -> None:
    monkeypatch.setattr(
        "worker.evaluate",
        lambda source, provider, limits: EvaluationResult(
            status=Status.SUCCESS,
            compile_ms=1,
            exit_code=1,
            stdout="expected",
            stderr="",
            run_ms=1,
        ),
    )

    result = evaluate_task(task(), FakeProvider())

    assert result.testResults[0].passed is False
