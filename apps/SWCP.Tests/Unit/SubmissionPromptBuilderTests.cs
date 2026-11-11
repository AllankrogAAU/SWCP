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
        Assert.Contains("Review the code for significant non-crashing quality issues first", prompt.SystemPrompt);
        Assert.Contains("int main()", prompt.UserPrompt);
        Assert.Contains("Fibonacci", prompt.UserPrompt);
    }

    [Fact]
    public void Build_HintAction_IncludesHintInstructions()
    {
        var assignment = CreateAssignment();
        var submission = CreateSubmission("hint");
        var sandboxResult = CreateSandboxResult();

        var prompt = SubmissionPromptBuilder.Build(assignment, submission, sandboxResult);

        Assert.Equal("hint", prompt.EvaluationMode);
        Assert.Contains("The student requested a hint. Give one concise next step", prompt.SystemPrompt);
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

    [Theory]
    [InlineData("submit")]
    [InlineData("hint")]
    public void Build_FailedTestCase_MarksSandboxFailedForSubmitAndHint(string action)
    {
        var assignment = CreateAssignment();
        var submission = CreateSubmission(action);
        var sandboxResult = CreateSandboxResult(compileSuccess: true, testPassed: false);

        var prompt = SubmissionPromptBuilder.Build(assignment, submission, sandboxResult);

        Assert.Equal(action, prompt.EvaluationMode);
        Assert.True(prompt.SandboxFailed);
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

