using coreApi.Models;
using coreApi.Services;
using SWCP.Contracts;
using Xunit;

namespace SWCP.Tests.Unit;

public sealed class SubmissionPromptBuilderTests
{
    private static Assignment CreateAssignment(string? taskFocus = null) => new()
    {
        Id = Guid.NewGuid(),
        Title = "Fibonacci",
        Description = "Write a C function to compute nth Fibonacci number.",
        TaskFocus = taskFocus ?? string.Empty,
        TestCasesJson = "[]",
        CreatedAtUtc = DateTimeOffset.UtcNow
    };

    private static Submission CreateSubmission(string action, string source = "int main() { return 0; }") => new()
    {
        Id = Guid.NewGuid(),
        UserId = Guid.NewGuid(),
        AssignmentId = Guid.NewGuid(),
        Action = action,
        SourceCode = source,
        LlmBackend = "azure",
        Status = SubmissionStatus.SANDBOX_PROCESSING,
        CreatedAtUtc = DateTimeOffset.UtcNow
    };

    private static SandboxResult CreateSandboxResult(bool compileSuccess = true, bool testPassed = true) => new(
        Guid.NewGuid(),
        new CompilationResult(compileSuccess, "compiled successfully", compileSuccess ? "" : "error", compileSuccess ? 0 : 1, 15),
        [],
        [new SandboxTestResult(testPassed, "output", "", testPassed ? 0 : 1, 10, 2.5)]);

    [Fact]
    public void Build_SuccessfulSubmit_GeneratesExpectedPrompt()
    {
        var assignment = CreateAssignment("Focus on recursive stack efficiency");
        var submission = CreateSubmission("submit");
        var sandboxResult = CreateSandboxResult(compileSuccess: true, testPassed: true);

        var prompt = SubmissionPromptBuilder.Build(assignment, submission, sandboxResult);

        Assert.Equal("submit", prompt.EvaluationMode);
        Assert.False(prompt.SandboxFailed);
        Assert.Contains("Focus on recursive stack efficiency", prompt.SystemPrompt);
        Assert.Contains("Review the submission in priority order", prompt.SystemPrompt);
        Assert.Contains("int main()", prompt.UserPrompt);
        Assert.Contains("Fibonacci", prompt.UserPrompt);
    }

    [Fact]
    public void Build_SuccessfulCompile_PresentsDiagnosticsAsNonFatalWarnings()
    {
        var assignment = CreateAssignment();
        var submission = CreateSubmission("submit");
        var sandboxResult = CreateSandboxResult() with
        {
            Compilation = new CompilationResult(true, "", "unused variable warning", 0, 15),
            LintOutput = "style warning"
        };

        var prompt = SubmissionPromptBuilder.Build(assignment, submission, sandboxResult);

        Assert.Contains("Non-fatal compiler/lint warnings (compilation succeeded)", prompt.UserPrompt);
        Assert.Contains("unused variable warning", prompt.UserPrompt);
        Assert.Contains("style warning", prompt.UserPrompt);
        Assert.False(prompt.SandboxFailed);
    }

    [Fact]
    public void Build_HintAction_IncludesHintInstructions()
    {
        var assignment = CreateAssignment();
        var submission = CreateSubmission("hint");
        var sandboxResult = CreateSandboxResult();

        var prompt = SubmissionPromptBuilder.Build(assignment, submission, sandboxResult);

        Assert.Equal("hint", prompt.EvaluationMode);
        Assert.Contains("The student requested a hint. Use the assignment, source, and available tool output", prompt.SystemPrompt);
    }

    [Fact]
    public void Build_FailedCompilation_MarksSandboxFailedAndSetsEducationalGuidance()
    {
        var assignment = CreateAssignment();
        var submission = CreateSubmission("submit");
        var sandboxResult = CreateSandboxResult(compileSuccess: false);

        var prompt = SubmissionPromptBuilder.Build(assignment, submission, sandboxResult);

        Assert.True(prompt.SandboxFailed);
        Assert.Contains("The sandbox reported a compilation or runtime failure", prompt.SystemPrompt);
    }

    [Fact]
    public void Build_RawCodeOnly_ExcludesAssignmentAndSandboxContext()
    {
        var assignment = CreateAssignment("Do not expose this focus");
        var submission = CreateSubmission("submit", "int main() { return 0; }");
        submission.ContextMode = "raw_code_only";
        var sandboxResult = CreateSandboxResult(compileSuccess: false);

        var prompt = SubmissionPromptBuilder.Build(assignment, submission, sandboxResult);

        Assert.Equal("raw_code_only", prompt.ContextMode);
        Assert.DoesNotContain("Fibonacci", prompt.UserPrompt);
        Assert.DoesNotContain("sandbox output", prompt.UserPrompt, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("Do not expose this focus", prompt.SystemPrompt);
        Assert.Contains("int main()", prompt.UserPrompt);
        Assert.Contains("set taskSolved to null", prompt.SystemPrompt);
    }

    [Fact]
    public void Build_UnsupportedAction_ThrowsArgumentException()
    {
        var assignment = CreateAssignment();
        var submission = CreateSubmission("run");
        var sandboxResult = CreateSandboxResult();

        var exception = Assert.Throws<ArgumentException>(() =>
            SubmissionPromptBuilder.Build(assignment, submission, sandboxResult));

        Assert.Contains("Only Hint and Submit actions create LLM prompts", exception.Message);
    }

    [Fact]
    public void Build_CompactsSandboxOutput_WhileRetainingFailuresAndResourceEvidence()
    {
        var assignment = CreateAssignment();
        var submission = CreateSubmission("submit");
        var sandboxResult = new SandboxResult(
            Guid.NewGuid(),
            new CompilationResult(true, "", "", 0, 15),
            [new StaticAnalysisWarning(10, 5, "warning", "unused variable")],
            [
                new SandboxTestResult(true, "passing output", "", 0, 10, 3.5),
                new SandboxTestResult(false, "failure output", "runtime error", 1, 21, 182)
            ],
            "test_failure");

        var prompt = SubmissionPromptBuilder.Build(assignment, submission, sandboxResult);

        Assert.DoesNotContain("passing output", prompt.UserPrompt);
        Assert.Contains("failure output", prompt.UserPrompt);
        Assert.Contains("runtime error", prompt.UserPrompt);
        Assert.Contains("unused variable", prompt.UserPrompt);
        Assert.Contains("maxMemoryConsumedMegabytes", prompt.UserPrompt);
        Assert.Contains("182", prompt.UserPrompt);
        Assert.Contains("maxDurationMilliseconds", prompt.UserPrompt);
    }
}
