using System.Text.Json;
using coreApi.Data;
using coreApi.Models;
using coreApi.Services;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging.Abstractions;
using SWCP.Contracts;
using SWCP.Tests.Component;

namespace SWCP.Tests.Unit;

public sealed class LlmResultConsumerTests
{
    private static SandboxResult CreateSandboxResult(Guid submissionId) => new(
        submissionId,
        new CompilationResult(true, "", "", 0, 10),
        [],
        [new SandboxTestResult(false, "1", "2", 0, 5, 1.2)],
        "wrong-answer");

    [Fact]
    public async Task ProcessResultAsync_LocalScoreBelowThreshold_EscalatesToAzureAndUpdatesBackend()
    {
        using var fixture = new CoreApiTestFixture();
        var user = await fixture.SeedUserAsync("student-escalate", "Password123!", UserRole.Student);
        var assignmentId = Guid.NewGuid();
        var submissionId = Guid.NewGuid();

        using (var scope = fixture.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
            db.Assignments.Add(new Assignment
            {
                Id = assignmentId,
                Title = "Pointers",
                Description = "Fix the pointer bug",
                TaskFocus = "pointers",
                TestCasesJson = "[]",
                CreatedAtUtc = DateTimeOffset.UtcNow
            });
            db.Submissions.Add(new Submission
            {
                Id = submissionId,
                UserId = user.Id,
                AssignmentId = assignmentId,
                SourceCode = "int main() { return 0; }",
                Action = "submit",
                LlmBackend = "local",
                Status = SubmissionStatus.LLM_QUEUED,
                SandboxOutputJson = JsonSerializer.Serialize(CreateSandboxResult(submissionId)),
                CreatedAtUtc = DateTimeOffset.UtcNow,
                UpdatedAtUtc = DateTimeOffset.UtcNow
            });
            await db.SaveChangesAsync();
        }

        var config = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["LocalLlm:AzureFallbackThreshold"] = "60"
            })
            .Build();

        var consumer = new LlmResultConsumer(
            NullLogger<LlmResultConsumer>.Instance,
            null!,
            fixture.Services.GetRequiredService<IServiceScopeFactory>(),
            fixture.Services.GetRequiredService<SubmissionEvents>(),
            fixture.TaskPublisher,
            config);

        await consumer.ProcessResultAsync(
            new LlmResult(submissionId, "success", "Weak local hint", new LlmTokenUsage(10, 5, 15), null, false, 45.0),
            null,
            CancellationToken.None);

        var published = Assert.Single(fixture.TaskPublisher.PublishedLlmTasks);
        Assert.Equal(submissionId, published.SubmissionId);
        Assert.Equal("azure", published.Backend);

        using (var scope = fixture.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
            var updated = await db.Submissions.FindAsync(submissionId);
            Assert.NotNull(updated);
            Assert.Equal("azure", updated.LlmBackend);
            Assert.Equal(SubmissionStatus.LLM_QUEUED, updated.Status);
            Assert.Equal(45.0, updated.ConfidenceScorePercent);
            Assert.Null(updated.LlmFeedback);
        }

        // Now simulate the escalated Azure result arriving; it should complete and preserve the local confidence score.
        await consumer.ProcessResultAsync(
            new LlmResult(submissionId, "success", "Strong Azure hint", new LlmTokenUsage(20, 10, 30), null, false, null),
            null,
            CancellationToken.None);

        Assert.Single(fixture.TaskPublisher.PublishedLlmTasks);
        using (var scope = fixture.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
            var completed = await db.Submissions.FindAsync(submissionId);
            Assert.NotNull(completed);
            Assert.Equal("azure", completed.LlmBackend);
            Assert.Equal(SubmissionStatus.COMPLETED, completed.Status);
            Assert.Equal("Strong Azure hint", completed.LlmFeedback);
            Assert.Equal(45.0, completed.ConfidenceScorePercent);
        }
    }

    [Fact]
    public async Task ProcessResultAsync_LocalScoreAtOrAboveThreshold_CompletesWithoutEscalation()
    {
        using var fixture = new CoreApiTestFixture();
        var user = await fixture.SeedUserAsync("student-local-ok", "Password123!", UserRole.Student);
        var assignmentId = Guid.NewGuid();
        var submissionId = Guid.NewGuid();

        using (var scope = fixture.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
            db.Assignments.Add(new Assignment
            {
                Id = assignmentId,
                Title = "Loops",
                Description = "Print numbers",
                TaskFocus = "loops",
                TestCasesJson = "[]",
                CreatedAtUtc = DateTimeOffset.UtcNow
            });
            db.Submissions.Add(new Submission
            {
                Id = submissionId,
                UserId = user.Id,
                AssignmentId = assignmentId,
                SourceCode = "int main() { return 0; }",
                Action = "submit",
                LlmBackend = "local",
                Status = SubmissionStatus.LLM_QUEUED,
                SandboxOutputJson = JsonSerializer.Serialize(CreateSandboxResult(submissionId)),
                CreatedAtUtc = DateTimeOffset.UtcNow,
                UpdatedAtUtc = DateTimeOffset.UtcNow
            });
            await db.SaveChangesAsync();
        }

        var config = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["LocalLlm:AzureFallbackThreshold"] = "60"
            })
            .Build();

        var consumer = new LlmResultConsumer(
            NullLogger<LlmResultConsumer>.Instance,
            null!,
            fixture.Services.GetRequiredService<IServiceScopeFactory>(),
            fixture.Services.GetRequiredService<SubmissionEvents>(),
            fixture.TaskPublisher,
            config);

        await consumer.ProcessResultAsync(
            new LlmResult(submissionId, "success", "Confident local hint", new LlmTokenUsage(10, 5, 15), null, true, 82.5),
            null,
            CancellationToken.None);

        Assert.Empty(fixture.TaskPublisher.PublishedLlmTasks);
        using var verifyScope = fixture.Services.CreateScope();
        var verifyDb = verifyScope.ServiceProvider.GetRequiredService<CoreDbContext>();
        var completed = await verifyDb.Submissions.FindAsync(submissionId);
        Assert.NotNull(completed);
        Assert.Equal("local", completed.LlmBackend);
        Assert.Equal(SubmissionStatus.COMPLETED, completed.Status);
        Assert.Equal("Confident local hint", completed.LlmFeedback);
        Assert.Equal(82.5, completed.ConfidenceScorePercent);
    }

    [Theory]
    [InlineData("uncertain", "logic_simple", true)]
    [InlineData("certain", "pointer_memory", true)]
    [InlineData("likely", "logic_simple", false)]
    public async Task ProcessResultAsync_SelfEvaluationRouting_EscalatesOnlyOnConfiguredTriggers(
        string diagnosisConfidence,
        string rootCauseCategory,
        bool shouldEscalate)
    {
        using var fixture = new CoreApiTestFixture();
        var user = await fixture.SeedUserAsync($"student-{diagnosisConfidence}-{rootCauseCategory}", "Password123!", UserRole.Student);
        var assignmentId = Guid.NewGuid();
        var submissionId = Guid.NewGuid();

        using (var scope = fixture.Services.CreateScope())
        {
            var db = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
            db.Assignments.Add(new Assignment
            {
                Id = assignmentId,
                Title = "Memory",
                Description = "Debug memory",
                TaskFocus = "pointers",
                TestCasesJson = "[]",
                CreatedAtUtc = DateTimeOffset.UtcNow
            });
            db.Submissions.Add(new Submission
            {
                Id = submissionId,
                UserId = user.Id,
                AssignmentId = assignmentId,
                SourceCode = "int main() { return 0; }",
                Action = "submit",
                LlmBackend = "local",
                Status = SubmissionStatus.LLM_QUEUED,
                SandboxOutputJson = JsonSerializer.Serialize(CreateSandboxResult(submissionId)),
                CreatedAtUtc = DateTimeOffset.UtcNow,
                UpdatedAtUtc = DateTimeOffset.UtcNow
            });
            await db.SaveChangesAsync();
        }

        var config = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["LocalLlm:RoutingMethod"] = "self_evaluation",
                ["LocalLlm:EscalateDiagnosisConfidence"] = "uncertain,guessing",
                ["LocalLlm:EscalateRootCauseCategories"] = "pointer_memory"
            })
            .Build();

        var consumer = new LlmResultConsumer(
            NullLogger<LlmResultConsumer>.Instance,
            null!,
            fixture.Services.GetRequiredService<IServiceScopeFactory>(),
            fixture.Services.GetRequiredService<SubmissionEvents>(),
            fixture.TaskPublisher,
            config);

        await consumer.ProcessResultAsync(
            new LlmResult(
                submissionId,
                "success",
                "Local hint",
                new LlmTokenUsage(10, 5, 15),
                null,
                false,
                92.0,
                diagnosisConfidence,
                rootCauseCategory),
            null,
            CancellationToken.None);

        using var verifyScope = fixture.Services.CreateScope();
        var verifyDb = verifyScope.ServiceProvider.GetRequiredService<CoreDbContext>();
        var submission = await verifyDb.Submissions.FindAsync(submissionId);
        Assert.NotNull(submission);
        Assert.Equal(diagnosisConfidence, submission.DiagnosisConfidence);
        Assert.Equal(rootCauseCategory, submission.RootCauseCategory);
        Assert.Equal(92.0, submission.ConfidenceScorePercent);

        if (shouldEscalate)
        {
            var published = Assert.Single(fixture.TaskPublisher.PublishedLlmTasks);
            Assert.Equal("azure", published.Backend);
            Assert.Equal("azure", submission.LlmBackend);
            Assert.Equal(SubmissionStatus.LLM_QUEUED, submission.Status);
        }
        else
        {
            Assert.Empty(fixture.TaskPublisher.PublishedLlmTasks);
            Assert.Equal("local", submission.LlmBackend);
            Assert.Equal(SubmissionStatus.COMPLETED, submission.Status);
        }
    }

    [Fact]
    public async Task ProcessResultAsync_ErrorTypeRouting_CompletesLocalResultWithoutPostLocalEscalation()
    {
        using var fixture = new CoreApiTestFixture();
        var user = await fixture.SeedUserAsync("student-error-type", "Password123!", UserRole.Student);
        var assignment = await fixture.SeedAssignmentAsync("Arrays", "arrays");
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
                Status = SubmissionStatus.LLM_QUEUED,
                SandboxOutputJson = JsonSerializer.Serialize(CreateSandboxResult(submissionId)),
                CreatedAtUtc = DateTimeOffset.UtcNow,
                UpdatedAtUtc = DateTimeOffset.UtcNow
            });
            await db.SaveChangesAsync();
        }

        var config = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["LocalLlm:RoutingMethod"] = "error_type",
                ["LocalLlm:AzureFallbackThreshold"] = "99",
                ["LocalLlm:EscalateDiagnosisConfidence"] = "uncertain,guessing",
                ["LocalLlm:EscalateRootCauseCategories"] = "pointer_memory"
            })
            .Build();

        var consumer = new LlmResultConsumer(
            NullLogger<LlmResultConsumer>.Instance,
            null!,
            fixture.Services.GetRequiredService<IServiceScopeFactory>(),
            fixture.Services.GetRequiredService<SubmissionEvents>(),
            fixture.TaskPublisher,
            config);

        await consumer.ProcessResultAsync(
            new LlmResult(
                submissionId,
                "success",
                "Local hint",
                new LlmTokenUsage(10, 5, 15),
                null,
                false,
                15.0,
                "uncertain",
                "pointer_memory"),
            null,
            CancellationToken.None);

        Assert.Empty(fixture.TaskPublisher.PublishedLlmTasks);
        using var verifyScope = fixture.Services.CreateScope();
        var verifyDb = verifyScope.ServiceProvider.GetRequiredService<CoreDbContext>();
        var submission = await verifyDb.Submissions.FindAsync(submissionId);
        Assert.NotNull(submission);
        Assert.Equal("local", submission.LlmBackend);
        Assert.Equal(SubmissionStatus.COMPLETED, submission.Status);
        Assert.Equal("Local hint", submission.LlmFeedback);
    }
}
