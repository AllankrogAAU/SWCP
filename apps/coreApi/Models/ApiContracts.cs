using System.Text.Json;
using System.Text.Json.Serialization;
using coreApi.Models;

namespace coreApi.Models;

public sealed record LoginRequest(string Username, string Password);
public sealed record LoginResponse(string AccessToken, string TokenType, DateTimeOffset ExpiresAtUtc);
public sealed record RegisterRequest(string Username, string Password);
public sealed record CreateAssignmentRequest(
    string Title,
    string Description,
    string TaskFocus,
    JsonElement TestCases,
    string? HarnessSource = null);
public sealed record ReplaceAssignmentTestCasesRequest(JsonElement TestCases);
public sealed record ReplaceAssignmentHarnessRequest(string? HarnessSource);
public sealed record AssignmentHarnessResponse(string? HarnessSource);
public sealed record AssignmentResponse(
    Guid Id,
    string Title,
    string Description,
    string TaskFocus,
    JsonElement TestCases,
    DateTimeOffset CreatedAtUtc);
public sealed record CreateSubmissionRequest(
    Guid AssignmentId,
    string SourceCode,
    string LlmBackend = "azure",
    string Action = "submit",
    string StandardInput = "");
public sealed record CodeActionRequest(
    Guid AssignmentId,
    string SourceCode,
    string LlmBackend = "azure",
    string StandardInput = "");
public sealed record SubmissionAcceptedResponse(Guid SubmissionId, SubmissionStatus Status);
public sealed record SubmissionResponse(
    Guid SubmissionId,
    Guid AssignmentId,
    string Action,
    string LlmBackend,
    SubmissionStatus Status,
    int RetryCount,
    JsonElement? SandboxOutput,
    string? LlmFeedback,
    bool? TaskSolved,
    string? ErrorMessage,
    DateTimeOffset CreatedAtUtc,
    DateTimeOffset UpdatedAtUtc);

public sealed record AssignmentListResponse(Guid Id, string Title, string Description);

[JsonConverter(typeof(JsonStringEnumConverter))]
public enum ErrorClassification
{
    CompileError,
    CompileTimeout,
    RuntimeFailure
}