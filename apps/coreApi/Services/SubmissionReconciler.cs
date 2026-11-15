using coreApi.Data;
using coreApi.Models;
using Microsoft.EntityFrameworkCore;
using SWCP.Contracts;

namespace coreApi.Services;

public sealed class SubmissionReconciler(
    ILogger<SubmissionReconciler> logger,
    IServiceScopeFactory scopeFactory,
    NatsTaskPublisher publisher,
    SubmissionEvents events,
    IConfiguration? configuration = null) : BackgroundService
{
    private static readonly TimeSpan ScanInterval = TimeSpan.FromSeconds(30);
    private readonly TimeSpan _pendingStuckAfter = TimeSpan.FromSeconds(
        Math.Max(5, configuration?.GetValue<int?>("Reconciler:PendingStuckAfterSeconds") ?? 30));
    private readonly TimeSpan _queuedStuckAfter = TimeSpan.FromSeconds(
        Math.Max(30, configuration?.GetValue<int?>("Reconciler:QueuedStuckAfterSeconds") ?? 300));
    private readonly TimeSpan _sandboxProcessingStuckAfter = TimeSpan.FromSeconds(
        Math.Max(30, configuration?.GetValue<int?>("Reconciler:SandboxProcessingStuckAfterSeconds") ?? 180));
    private readonly TimeSpan _llmStuckAfter = TimeSpan.FromSeconds(
        Math.Max(60, configuration?.GetValue<int?>("Reconciler:LlmStuckAfterSeconds") ?? 360));
    private readonly int _maxSandboxRetries =
        Math.Max(1, configuration?.GetValue<int?>("Reconciler:MaxSandboxRetries") ?? 3);

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        using var timer = new PeriodicTimer(ScanInterval);
        while (await timer.WaitForNextTickAsync(stoppingToken))
        {
            try
            {
                await RequeueStuckSubmissionsAsync(stoppingToken);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                logger.LogError(exception, "Submission reconciliation pass failed");
            }
        }
    }

    private async Task RequeueStuckSubmissionsAsync(CancellationToken cancellationToken)
    {
        await using var scope = scopeFactory.CreateAsyncScope();
        var database = scope.ServiceProvider.GetRequiredService<CoreDbContext>();
        await using var transaction = await database.Database.BeginTransactionAsync(cancellationToken);
        var now = DateTimeOffset.UtcNow;
        var pendingCutoff = now - _pendingStuckAfter;
        var queuedCutoff = now - _queuedStuckAfter;
        var sandboxProcessingCutoff = now - _sandboxProcessingStuckAfter;
        var llmCutoff = now - _llmStuckAfter;
        var stale = await database.Submissions
            .FromSqlInterpolated(
                $"SELECT * FROM \"Submissions\" WHERE ((\"Status\" = 'PENDING' AND \"UpdatedAtUtc\" < {pendingCutoff}) OR (\"Status\" = 'SANDBOX_QUEUED' AND \"UpdatedAtUtc\" < {queuedCutoff}) OR (\"Status\" = 'SANDBOX_PROCESSING' AND \"UpdatedAtUtc\" < {sandboxProcessingCutoff}) OR (\"Status\" IN ('LLM_QUEUED', 'LLM_PROCESSING') AND \"UpdatedAtUtc\" < {llmCutoff})) ORDER BY \"UpdatedAtUtc\" LIMIT 25 FOR UPDATE SKIP LOCKED")
            .ToListAsync(cancellationToken);

        foreach (var submission in stale)
        {
            if (submission.Status is SubmissionStatus.LLM_QUEUED or SubmissionStatus.LLM_PROCESSING)
            {
                await FailStuckSubmissionAsync(
                    submission,
                    "Timed out waiting for AI feedback. The selected LLM service may be unavailable; please try again.",
                    now,
                    cancellationToken);
                continue;
            }

            if (submission.RetryCount >= _maxSandboxRetries)
            {
                await FailStuckSubmissionAsync(
                    submission,
                    "Submission timed out during sandbox evaluation.",
                    now,
                    cancellationToken);
                continue;
            }

            var assignment = await database.Assignments
                .SingleOrDefaultAsync(item => item.Id == submission.AssignmentId, cancellationToken);
            if (assignment is null)
            {
                await FailStuckSubmissionAsync(submission, "Assignment no longer exists.", now, cancellationToken);
                continue;
            }

            submission.RetryCount++;
            submission.Status = SubmissionStatus.SANDBOX_QUEUED;
            submission.UpdatedAtUtc = now;
            var cases = submission.Action == "run"
                ? []
                : System.Text.Json.JsonSerializer.Deserialize<List<SandboxTestCase>>(assignment.TestCasesJson) ?? [];
            await publisher.PublishSandboxTaskAsync(new SandboxTask(
                submission.Id,
                assignment.Id,
                submission.SourceCode,
                ["-Wall", "-Wextra", "-O2", "-std=c11"],
                cases,
                HarnessSource: assignment.HarnessSource,
                RunStdin: submission.Action == "run" ? submission.StandardInput : ""), cancellationToken, submission.RetryCount);
            await events.PublishAsync(submission, "retry", "Recovered and requeued by Core API", 5, cancellationToken);
        }

        await database.SaveChangesAsync(cancellationToken);
        await transaction.CommitAsync(cancellationToken);
        if (stale.Count > 0)
        {
            logger.LogInformation("Reconciled {Count} stale submissions", stale.Count);
        }
    }

    private Task FailStuckSubmissionAsync(
        Submission submission,
        string errorMessage,
        DateTimeOffset now,
        CancellationToken cancellationToken)
    {
        submission.Status = SubmissionStatus.FAILED;
        submission.ErrorMessage = errorMessage;
        submission.UpdatedAtUtc = now;
        logger.LogWarning(
            "Marking stale submission {SubmissionId} ({Action}, backend={Backend}) as FAILED: {Reason}",
            submission.Id, submission.Action, submission.LlmBackend, errorMessage);
        return events.PublishAsync(submission, "failed", errorMessage, 100, cancellationToken);
    }
}
