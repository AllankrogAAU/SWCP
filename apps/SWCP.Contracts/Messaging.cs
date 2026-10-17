using System.Text.Json.Serialization;
using System.Diagnostics;

namespace SWCP.Contracts;

public static class Messaging
{
    public const string TraceParentHeader = "traceparent";
    public const string SandboxTasksStream = "SANDBOX_TASKS";
    public const string SandboxResultsStream = "SANDBOX_RESULTS";
    public const string LlmTasksStream = "LLM_TASKS";
    public const string LlmResultsStream = "LLM_RESULTS";
    public const string SandboxTasksDlqStream = "SANDBOX_TASKS_DLQ";
    public const string LlmTasksDlqStream = "LLM_TASKS_DLQ";
    public const string SandboxTaskSubject = "sandbox.tasks";
    public const string SandboxResultSubject = "sandbox.results";
    public const string LlmAzureTaskSubject = "llm.tasks.azure";
    public const string LlmLocalTaskSubject = "llm.tasks.local";
    public const string LlmResultSubject = "llm.results";
    public const string SandboxTasksDlqSubject = "sandbox.tasks.dlq";
    public const string LlmTasksDlqSubject = "llm.tasks.dlq";
    public const string SandboxTaskConsumer = "c-sandbox-workers";
    public const string SandboxResultConsumer = "core-api-sandbox-results";
    public const string LlmAzureTaskConsumer = "llm-azure-workers";
    public const string LlmLocalTaskConsumer = "llm-local-workers";
    public const string LlmResultConsumer = "core-api-llm-results";
    public const string WorkStartedSubject = "submissions.work.started";

    public static string SandboxMessageId(Guid submissionId, int attempt = 0) => $"{submissionId:D}-sandbox-{attempt}";
    public static string LlmMessageId(Guid submissionId) => $"{submissionId:D}-llm";
    public static string NotificationSubject(Guid submissionId) => $"notifications.{submissionId:D}";

    public static string CurrentOrNewTraceParent()
    {
        if (Activity.Current?.Id is { } currentId)
        {
            return currentId;
        }

        var traceId = Guid.NewGuid().ToString("N");
        var spanId = Guid.NewGuid().ToString("N")[..16];
        return $"00-{traceId}-{spanId}-01";
    }
}

public sealed record SandboxTask(
    [property: JsonPropertyName("submissionId")] Guid SubmissionId,
    [property: JsonPropertyName("assignmentId")] Guid AssignmentId,
    [property: JsonPropertyName("source")] string Source,
    [property: JsonPropertyName("compilerFlags")] IReadOnlyList<string> CompilerFlags,
    [property: JsonPropertyName("testCases")] IReadOnlyList<SandboxTestCase> TestCases,
    [property: JsonPropertyName("traceParent")] string? TraceParent = null);

public sealed record SandboxTestCase(
    [property: JsonPropertyName("stdin")] string Stdin,
    [property: JsonPropertyName("expectedStdout")] string ExpectedStdout,
    [property: JsonPropertyName("timeoutMilliseconds")] int TimeoutMilliseconds,
    [property: JsonPropertyName("memoryLimitMegabytes")] int MemoryLimitMegabytes);

public sealed record SandboxResult(
    [property: JsonPropertyName("submissionId")] Guid SubmissionId,
    [property: JsonPropertyName("compilation")] CompilationResult Compilation,
    [property: JsonPropertyName("staticAnalysis")] IReadOnlyList<StaticAnalysisWarning> StaticAnalysis,
    [property: JsonPropertyName("testResults")] IReadOnlyList<SandboxTestResult> TestResults,
    [property: JsonPropertyName("errorClassification")] string? ErrorClassification = null);

public sealed record CompilationResult(
    [property: JsonPropertyName("success")] bool Success,
    [property: JsonPropertyName("stdout")] string Stdout,
    [property: JsonPropertyName("stderr")] string Stderr,
    [property: JsonPropertyName("exitCode")] int? ExitCode,
    [property: JsonPropertyName("durationMilliseconds")] long? DurationMilliseconds);

public sealed record StaticAnalysisWarning(
    [property: JsonPropertyName("line")] int Line,
    [property: JsonPropertyName("column")] int Column,
    [property: JsonPropertyName("severity")] string Severity,
    [property: JsonPropertyName("message")] string Message);

public sealed record SandboxTestResult(
    [property: JsonPropertyName("passed")] bool Passed,
    [property: JsonPropertyName("stdout")] string Stdout,
    [property: JsonPropertyName("stderr")] string Stderr,
    [property: JsonPropertyName("exitCode")] int? ExitCode,
    [property: JsonPropertyName("durationMilliseconds")] long? DurationMilliseconds,
    [property: JsonPropertyName("memoryConsumedMegabytes")] double? MemoryConsumedMegabytes);

public sealed record LlmTask(
    [property: JsonPropertyName("submissionId")] Guid SubmissionId,
    [property: JsonPropertyName("backend")] string Backend,
    [property: JsonPropertyName("systemPrompt")] string SystemPrompt,
    [property: JsonPropertyName("userPrompt")] string UserPrompt,
    [property: JsonPropertyName("parameters")] LlmInferenceParameters Parameters,
    [property: JsonPropertyName("traceParent")] string? TraceParent = null,
    [property: JsonPropertyName("evaluationMode")] string EvaluationMode = "submit",
    [property: JsonPropertyName("sandboxFailed")] bool SandboxFailed = false);

public sealed record LlmInferenceParameters(
    [property: JsonPropertyName("temperature")] double Temperature,
    [property: JsonPropertyName("maxTokens")] int MaxTokens,
    [property: JsonPropertyName("topP")] double TopP);

public sealed record LlmResult(
    [property: JsonPropertyName("submissionId")] Guid SubmissionId,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("feedbackMarkdown")] string? FeedbackMarkdown,
    [property: JsonPropertyName("tokenUsage")] LlmTokenUsage? TokenUsage,
    [property: JsonPropertyName("error")] string? Error,
    [property: JsonPropertyName("taskSolved")] bool? TaskSolved = null);

public sealed record LlmTokenUsage(
    [property: JsonPropertyName("promptTokens")] int PromptTokens,
    [property: JsonPropertyName("completionTokens")] int CompletionTokens,
    [property: JsonPropertyName("totalTokens")] int TotalTokens);

public sealed record SubmissionNotification(
    [property: JsonPropertyName("submissionId")] Guid SubmissionId,
    [property: JsonPropertyName("eventType")] string EventType,
    [property: JsonPropertyName("stage")] string Stage,
    [property: JsonPropertyName("message")] string Message,
    [property: JsonPropertyName("percentComplete")] int PercentComplete,
    [property: JsonPropertyName("timestampUtc")] DateTimeOffset TimestampUtc);

public sealed record SubmissionWorkStarted(
    [property: JsonPropertyName("submissionId")] Guid SubmissionId,
    [property: JsonPropertyName("worker")] string Worker,
    [property: JsonPropertyName("stage")] string Stage,
    [property: JsonPropertyName("timestampUtc")] DateTimeOffset TimestampUtc);