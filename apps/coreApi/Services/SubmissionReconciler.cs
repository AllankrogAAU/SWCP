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
        var pendingCutoff = DateTimeOffset.UtcNow - _pendingStuckAfter;
        var queuedCutoff = DateTimeOffset.UtcNow - _queuedStuckAfter;
        var stale = await database.Submissions
            .FromSqlInterpolated($"SELECT * FROM \"Submissions\" WHERE ((\"Status\" = 'PENDING' AND \"UpdatedAtUtc\" < {pendingCutoff}) OR (\"Status\" = 'SANDBOX_QUEUED' AND \"UpdatedAtUtc\" < {queuedCutoff})) ORDER BY \"UpdatedAtUtc\" LIMIT 25 FOR UPDATE SKIP LOCKED")
            .ToListAsync(cancellationToken);

        foreach (var submission in stale)
        {
            var assignment = await database.Assignments
                .SingleOrDefaultAsync(item => item.Id == submission.AssignmentId, cancellationToken);
            if (assignment is null)
            {
                submission.Status = SubmissionStatus.FAILED;
                submission.ErrorMessage = "Assignment no longer exists.";
                submission.UpdatedAtUtc = DateTimeOffset.UtcNow;
                continue;
            }

            submission.RetryCount++;
            submission.Status = SubmissionStatus.SANDBOX_QUEUED;
            submission.UpdatedAtUtc = DateTimeOffset.UtcNow;
            var cases = System.Text.Json.JsonSerializer.Deserialize<List<SandboxTestCase>>(assignment.TestCasesJson) ?? [];
            await publisher.PublishSandboxTaskAsync(new SandboxTask(
                submission.Id,
                assignment.Id,
                submission.SourceCode,
                ["-Wall", "-Wextra", "-O2", "-std=c11"],
                cases), cancellationToken, submission.RetryCount);
            await events.PublishAsync(submission, "retry", "Recovered and requeued by Core API", 5, cancellationToken);
        }

        await database.SaveChangesAsync(cancellationToken);
        await transaction.CommitAsync(cancellationToken);
        if (stale.Count > 0)
        {
            logger.LogInformation("Reconciled {Count} stale submissions", stale.Count);
        }
    }
}
