using coreApi.Data;
using coreApi.Models;
using coreApi.Services;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging.Abstractions;
using SWCP.Contracts;
using SWCP.Tests.Component;

namespace SWCP.Tests.Unit;

public sealed class SandboxResultConsumerTests
{
    private static SandboxResult CreateSandboxResult(
        Guid submissionId,
        bool compiled = true,
        string? errorClassification = "success;compiler-flags-not-yet-applied",
        params SandboxTestResult[] testResults) => new(
            submissionId,
            new CompilationResult(compiled, "", compiled ? "" : "error: syntax", compiled ? 0 : 1, 10),
            [],
            testResults,
            errorClassification);

    [Fact]
    public void ClassifySandboxError_MapsAllCanonicalSandboxOutcomes()
    {
        var id = Guid.NewGuid();

        Assert.Equal(
            LocalLlmRouting.ErrorTypes.CompileError,
            LocalLlmRouting.ClassifySandboxError(CreateSandboxResult(id, compiled: false, "compile_error;compiler-flags-not-yet-applied")));

        Assert.Equal(
            LocalLlmRouting.ErrorTypes.CompileTimeout,
            LocalLlmRouting.ClassifySandboxError(CreateSandboxResult(id, compiled: false, "compile_timeout;compiler-flags-not-yet-applied")));

        Assert.Equal(
            LocalLlmRouting.ErrorTypes.RuntimeTimeout,
            LocalLlmRouting.ClassifySandboxError(CreateSandboxResult(
                id,
                compiled: true,
                "run_timeout;compiler-flags-not-yet-applied",
                new SandboxTestResult(false, "", "", 124, 2000, null))));

        Assert.Equal(
            LocalLlmRouting.ErrorTypes.RuntimeTimeout,
            LocalLlmRouting.ClassifySandboxError(CreateSandboxResult(
                id,
                compiled: true,
                "success",
                new SandboxTestResult(false, "", "", 124, 2000, null))));

        Assert.Equal(
            LocalLlmRouting.ErrorTypes.RuntimeCrash,
            LocalLlmRouting.ClassifySandboxError(CreateSandboxResult(
                id,
                compiled: true,
                "success",
                new SandboxTestResult(false, "", "Segmentation fault", 139, 5, null))));

        Assert.Equal(
            LocalLlmRouting.ErrorTypes.RuntimeError,
            LocalLlmRouting.ClassifySandboxError(CreateSandboxResult(
                id,
                compiled: true,
                "success",
                new SandboxTestResult(false, "", "", 1, 5, null))));

        Assert.Equal(
            LocalLlmRouting.ErrorTypes.WrongOutput,
            LocalLlmRouting.ClassifySandboxError(CreateSandboxResult(
                id,
                compiled: true,
                "success",
                new SandboxTestResult(false, "1", "", 0, 5, null))));

        Assert.Equal(
            LocalLlmRouting.ErrorTypes.NoError,
            LocalLlmRouting.ClassifySandboxError(CreateSandboxResult(
                id,
                compiled: true,
                "success",
                new SandboxTestResult(true, "42", "", 0, 5, null))));

        Assert.Equal(
            LocalLlmRouting.ErrorTypes.NoError,
            LocalLlmRouting.ClassifySandboxError(CreateSandboxResult(id, compiled: true, "success")));
    }

    [Theory]
    [InlineData(true, "success", 0, false, "azure")]
    [InlineData(true, "success", 1, false, "azure")]
    [InlineData(false, "compile_error;compiler-flags-not-yet-applied", null, false, "local")]
    [InlineData(true, "success", 139, false, "local")]
    [InlineData(true, "run_timeout;compiler-flags-not-yet-applied", 124, false, "local")]
    [InlineData(true, "success", 0, true, "local")]
    public async Task ProcessResultAsync_ErrorTypeRouting_RoutesConfiguredTypesToAzureAndKeepsOthersLocal(
        bool compiled,
        string errorClassification,
        int? testExitCode,
        bool testPassed,
        string expectedBackend)
    {
        using var fixture = new CoreApiTestFixture();
        var user = await fixture.SeedUserAsync($"student-{Guid.NewGuid():N}", "Password123!", UserRole.Student);
        var assignment = await fixture.SeedAssignmentAsync("Pointers", "pointers");
        var submissionId = Guid.NewGuid();

        using (var scope = fixture.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
            db.Submissions.Add(new Submission
            {
                Id = submissionId,
                UserId = user.Id,
                AssignmentId = assignment.Id,
                SourceCode = "int main() { return 0; }",
                Action = "submit",
                LlmBackend = "local",
                Status = SubmissionStatus.SANDBOX_PROCESSING,
                CreatedAtUtc = DateTimeOffset.UtcNow,
                UpdatedAtUtc = DateTimeOffset.UtcNow
            });
            await db.SaveChangesAsync();
        }

        var config = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["LocalLlm:RoutingMethod"] = "error_type",
                ["LocalLlm:EscalateErrorTypes"] = "wrong_output,runtime_error"
            })
            .Build();

        var consumer = new SandboxResultConsumer(
            NullLogger<SandboxResultConsumer>.Instance,
            null!,
            fixture.Services.GetRequiredService<IServiceScopeFactory>(),
            fixture.TaskPublisher,
            fixture.Services.GetRequiredService<SubmissionEvents>(),
            config);

        var testResults = compiled
            ? new[] { new SandboxTestResult(testPassed, "", "", testExitCode, 5, null) }
            : Array.Empty<SandboxTestResult>();

        await consumer.ProcessResultAsync(
            CreateSandboxResult(submissionId, compiled, errorClassification, testResults),
            null,
            CancellationToken.None);

        var published = Assert.Single(fixture.TaskPublisher.PublishedLlmTasks);
        Assert.Equal(submissionId, published.SubmissionId);
        Assert.Equal(expectedBackend, published.Backend);

        using var verifyScope = fixture.Services.CreateScope();
        var verifyDb = verifyScope.ServiceProvider.GetRequiredService<CoreDbContext>();
        var submission = await verifyDb.Submissions.FindAsync(submissionId);
        Assert.NotNull(submission);
        Assert.Equal(expectedBackend, submission.LlmBackend);
        Assert.Equal(SubmissionStatus.LLM_QUEUED, submission.Status);
    }

    [Theory]
    [InlineData("azure", "error_type", "wrong_output,runtime_error", false, "azure")]
    [InlineData("local", "error_type", "", true, "local")]
    [InlineData("local", "confidence_score", "wrong_output,runtime_error", true, "local")]
    public async Task ProcessResultAsync_RespectsExplicitAzureEmptyAllowlistAndOtherRoutingMethods(
        string initialBackend,
        string routingMethod,
        string escalateErrorTypes,
        bool compiled,
        string expectedBackend)
    {
        using var fixture = new CoreApiTestFixture();
        var user = await fixture.SeedUserAsync($"student-{Guid.NewGuid():N}", "Password123!", UserRole.Student);
        var assignment = await fixture.SeedAssignmentAsync("Loops", "loops");
        var submissionId = Guid.NewGuid();

        using (var scope = fixture.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
            db.Submissions.Add(new Submission
            {
                Id = submissionId,
                UserId = user.Id,
                AssignmentId = assignment.Id,
                SourceCode = "int main() { return 0; }",
                Action = "submit",
                LlmBackend = initialBackend,
                Status = SubmissionStatus.SANDBOX_PROCESSING,
                CreatedAtUtc = DateTimeOffset.UtcNow,
                UpdatedAtUtc = DateTimeOffset.UtcNow
            });
            await db.SaveChangesAsync();
        }

        var config = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["LocalLlm:RoutingMethod"] = routingMethod,
                ["LocalLlm:EscalateErrorTypes"] = escalateErrorTypes
            })
            .Build();

        var consumer = new SandboxResultConsumer(
            NullLogger<SandboxResultConsumer>.Instance,
            null!,
            fixture.Services.GetRequiredService<IServiceScopeFactory>(),
            fixture.TaskPublisher,
            fixture.Services.GetRequiredService<SubmissionEvents>(),
            config);

        var testResults = compiled
            ? new[] { new SandboxTestResult(false, "1", "", 0, 5, null) }
            : Array.Empty<SandboxTestResult>();

        await consumer.ProcessResultAsync(
            CreateSandboxResult(submissionId, compiled, compiled ? "success" : "compile_error", testResults),
            null,
            CancellationToken.None);

        var published = Assert.Single(fixture.TaskPublisher.PublishedLlmTasks);
        Assert.Equal(expectedBackend, published.Backend);
    }
}
