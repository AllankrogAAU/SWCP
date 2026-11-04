import uuid
import pytest
from pydantic import ValidationError

from contracts import (
    CompilationResult,
    SandboxResult,
    SandboxTask,
    SandboxTestCase,
    SandboxTestResult,
    StaticAnalysisWarning,
)


def test_sandbox_task_valid_payload() -> None:
    task = SandboxTask(
        submissionId=uuid.uuid4(),
        assignmentId=uuid.uuid4(),
        source="int main() { return 0; }",
        compilerFlags=["-Wall", "-O2"],
        testCases=[
            SandboxTestCase(
                stdin="input",
                expectedStdout="output",
                timeoutMilliseconds=1000,
                memoryLimitMegabytes=64,
            )
        ],
        traceParent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )
    assert task.source == "int main() { return 0; }"
    assert len(task.testCases) == 1
    assert task.testCases[0].timeoutMilliseconds == 1000


def test_sandbox_task_missing_required_fields_raises_validation_error() -> None:
    with pytest.raises(ValidationError):
        SandboxTask.model_validate({"source": "int main() {}"})


def test_sandbox_task_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        SandboxTask.model_validate({
            "submissionId": str(uuid.uuid4()),
            "assignmentId": str(uuid.uuid4()),
            "source": "int main() {}",
            "compilerFlags": [],
            "testCases": [],
            "unsupportedExtraField": 123,
        })


def test_sandbox_test_case_negative_limits_rejected() -> None:
    with pytest.raises(ValidationError):
        SandboxTestCase(
            stdin="",
            expectedStdout="",
            timeoutMilliseconds=-5,
            memoryLimitMegabytes=64,
        )

    with pytest.raises(ValidationError):
        SandboxTestCase(
            stdin="",
            expectedStdout="",
            timeoutMilliseconds=1000,
            memoryLimitMegabytes=0,
        )


def test_sandbox_result_valid_serialization_roundtrip() -> None:
    result = SandboxResult(
        submissionId=uuid.uuid4(),
        compilation=CompilationResult(
            success=True,
            stdout="compile ok",
            stderr="",
            exitCode=0,
            durationMilliseconds=12,
        ),
        staticAnalysis=[
            StaticAnalysisWarning(
                line=5,
                column=2,
                severity="warning",
                message="unused variable",
            )
        ],
        testResults=[
            SandboxTestResult(
                passed=True,
                stdout="test out",
                stderr="",
                exitCode=0,
                durationMilliseconds=5,
                memoryConsumedMegabytes=3.5,
            )
        ],
    )
    serialized = result.model_dump_json()
    reparsed = SandboxResult.model_validate_json(serialized)
    assert reparsed.submissionId == result.submissionId
    assert reparsed.compilation.success is True
    assert len(reparsed.staticAnalysis) == 1
    assert reparsed.staticAnalysis[0].line == 5


def test_static_analysis_invalid_coordinates_rejected() -> None:
    with pytest.raises(ValidationError):
        StaticAnalysisWarning(line=0, column=1, severity="warning", message="msg")

    with pytest.raises(ValidationError):
        StaticAnalysisWarning(line=1, column=0, severity="warning", message="msg")

