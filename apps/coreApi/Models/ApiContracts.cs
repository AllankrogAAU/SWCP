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
    string? HarnessSource = null,
    string CompilerProfile = CompilerProfiles.Standard);
public sealed record ReplaceAssignmentTestCasesRequest(JsonElement TestCases);
public sealed record ReplaceAssignmentHarnessRequest(string? HarnessSource);
public sealed record AssignmentHarnessResponse(string? HarnessSource);

public static class CompilerProfiles
{
    public const string Standard = "standard";
    public const string CodeNetGnu11 = "codenet-gnu11";

    public static bool IsSupported(string? profile) =>
        profile is Standard or CodeNetGnu11;

    public static string[] FlagsFor(string profile) =>
        profile switch
        {
            Standard => ["-Wall", "-Wextra", "-O2", "-std=c11"],
            CodeNetGnu11 => ["-std=gnu11", "-Wall", "-Wextra", "-O2", "-lm"],
            _ => throw new ArgumentOutOfRangeException(nameof(profile), profile, "Unknown compiler profile.")
        };
}
public sealed record AssignmentResponse(
    Guid Id,
    string Title,
    string Description,
    string TaskFocus,
    JsonElement TestCases,
    DateTimeOffset CreatedAtUtc,
    string CompilerProfile);
public sealed record CreateSubmissionRequest(
    Guid AssignmentId,
    string SourceCode,
    string LlmBackend = "azure",
    string Action = "submit",
    string StandardInput = "",
    string ContextMode = "with_context_transfer",
    string? AzureModelKey = null);
public sealed record CodeActionRequest(
    Guid AssignmentId,
    string SourceCode,
    string LlmBackend = "azure",
    string StandardInput = "",
    string ContextMode = "with_context_transfer",
    string? AzureModelKey = null);
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