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
    string SystemPromptTemplate,
    JsonElement TestCases);
public sealed record AssignmentResponse(
    Guid Id,
    string Title,
    string Description,
    string SystemPromptTemplate,
    JsonElement TestCases,
    DateTimeOffset CreatedAtUtc);
public sealed record CreateSubmissionRequest(
    Guid AssignmentId,
    string SourceCode,
    string LlmBackend = "azure",
    string Action = "submit");
public sealed record CodeActionRequest(Guid AssignmentId, string SourceCode, string LlmBackend = "azure");
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