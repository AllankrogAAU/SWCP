from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SandboxTestCase(StrictContract):
    stdin: str
    expectedStdout: str
    timeoutMilliseconds: int = Field(gt=0)
    memoryLimitMegabytes: int = Field(gt=0)


class SandboxTask(StrictContract):
    submissionId: UUID
    assignmentId: UUID
    source: str
    compilerFlags: list[str]
    testCases: list[SandboxTestCase]
    traceParent: str | None = None


class CompilationResult(StrictContract):
    success: bool
    stdout: str
    stderr: str
    exitCode: int | None = None
    durationMilliseconds: int | None = Field(default=None, ge=0)


class StaticAnalysisWarning(StrictContract):
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    severity: str
    message: str


class SandboxTestResult(StrictContract):
    passed: bool
    stdout: str
    stderr: str
    exitCode: int | None = None
    durationMilliseconds: int | None = Field(default=None, ge=0)
    memoryConsumedMegabytes: float | None = Field(default=None, ge=0)


class SandboxResult(StrictContract):
    submissionId: UUID
    compilation: CompilationResult
    staticAnalysis: list[StaticAnalysisWarning]
    testResults: list[SandboxTestResult]
    errorClassification: str | None = None


class LlmInferenceParameters(StrictContract):
    temperature: float = Field(ge=0)
    maxTokens: int = Field(gt=0)
    topP: float = Field(gt=0, le=1)


class LlmTask(StrictContract):
    submissionId: UUID
    backend: Literal["azure", "local"]
    systemPrompt: str
    userPrompt: str
    parameters: LlmInferenceParameters


class LlmTokenUsage(StrictContract):
    promptTokens: int = Field(ge=0)
    completionTokens: int = Field(ge=0)
    totalTokens: int = Field(ge=0)


class LlmResult(StrictContract):
    submissionId: UUID
    status: Literal["success", "error"]
    feedbackMarkdown: str | None = None
    tokenUsage: LlmTokenUsage | None = None
    error: str | None = None


class SubmissionNotification(StrictContract):
    submissionId: UUID
    eventType: str
    stage: str
    message: str
    percentComplete: int = Field(ge=0, le=100)
    timestampUtc: str