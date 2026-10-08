using System.Text.Json;

namespace coreApi.Models;

public enum UserRole
{
    Student,
    Teacher
}

public enum SubmissionStatus
{
    PENDING,
    SANDBOX_QUEUED,
    SANDBOX_PROCESSING,
    LLM_QUEUED,
    LLM_PROCESSING,
    COMPLETED,
    FAILED
}

public sealed class User
{
    public Guid Id { get; set; }
    public string Username { get; set; } = string.Empty;
    public string PasswordHash { get; set; } = string.Empty;
    public UserRole Role { get; set; }
    public DateTimeOffset CreatedAtUtc { get; set; }
}

public sealed class Assignment
{
    public Guid Id { get; set; }
    public string Title { get; set; } = string.Empty;
    public string Description { get; set; } = string.Empty;
    public string SystemPromptTemplate { get; set; } = string.Empty;
    public string TestCasesJson { get; set; } = "[]";
    public DateTimeOffset CreatedAtUtc { get; set; }
}

public sealed class Submission
{
    public Guid Id { get; set; }
    public Guid UserId { get; set; }
    public Guid AssignmentId { get; set; }
    public string SourceCode { get; set; } = string.Empty;
    public string Action { get; set; } = "submit";
    public string LlmBackend { get; set; } = "azure";
    public SubmissionStatus Status { get; set; }
    public int RetryCount { get; set; }
    public string? SandboxOutputJson { get; set; }
    public string? LlmFeedback { get; set; }
    public bool? TaskSolved { get; set; }
    public string? ErrorMessage { get; set; }
    public DateTimeOffset CreatedAtUtc { get; set; }
    public DateTimeOffset UpdatedAtUtc { get; set; }
    public User? User { get; set; }
    public Assignment? Assignment { get; set; }

    public JsonElement? GetSandboxOutput()
    {
        if (SandboxOutputJson is null)
        {
            return null;
        }

        using var document = JsonDocument.Parse(SandboxOutputJson);
        return document.RootElement.Clone();
    }
}