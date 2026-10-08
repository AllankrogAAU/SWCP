using System.Security.Claims;
using System.Diagnostics;
using System.Text.Json;
using coreApi.Data;
using coreApi.Models;
using coreApi.Services;
using Microsoft.EntityFrameworkCore;
using SWCP.Contracts;

namespace coreApi.Endpoints;

public static class SubmissionEndpoints
{
    public static RouteGroupBuilder MapSubmissionEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/submissions").WithTags("Submissions").RequireAuthorization();
        group.MapPost("/", Create);
        group.MapPost("/hint", Hint);
        group.MapPost("/run", Run);
        group.MapGet("/{id:guid}", Get);
        group.MapGet("/", ListMine);
        return group;
    }

    private static async Task<IResult> Create(
        CreateSubmissionRequest request,
        ClaimsPrincipal principal,
        CoreDbContext database,
        NatsTaskPublisher publisher,
        SubmissionEvents events,
        ILoggerFactory loggerFactory,
        CancellationToken cancellationToken)
    {
        if (!Guid.TryParse(principal.FindFirstValue(ClaimTypes.NameIdentifier), out var userId))
        {
            return Results.Unauthorized();
        }

        var requestTimer = Stopwatch.StartNew();
        var traceParent = Activity.Current?.Id ?? Messaging.CurrentOrNewTraceParent();
        var logger = loggerFactory.CreateLogger("coreApi.SubmissionEndpoints");

        if (request.SourceCode.Length == 0 || System.Text.Encoding.UTF8.GetByteCount(request.SourceCode) > 1_000_000)
        {
            return Results.BadRequest(new { error = "Source code must be non-empty and at most 1 MB UTF-8." });
        }

        var backend = request.LlmBackend.Trim().ToLowerInvariant();
        if (backend is not ("azure" or "local"))
        {
            return Results.BadRequest(new { error = "LlmBackend must be 'azure' or 'local'." });
        }

        var assignment = await database.Assignments
            .SingleOrDefaultAsync(item => item.Id == request.AssignmentId, cancellationToken);
        if (assignment is null)
        {
            return Results.NotFound(new { error = "Assignment not found." });
        }

        var submission = new Submission
        {
            Id = Guid.NewGuid(),
            UserId = userId,
            AssignmentId = assignment.Id,
            SourceCode = request.SourceCode,
            LlmBackend = backend,
            Status = SubmissionStatus.PENDING,
            CreatedAtUtc = DateTimeOffset.UtcNow,
            UpdatedAtUtc = DateTimeOffset.UtcNow
        };
        await using (var transaction = await database.Database.BeginTransactionAsync(cancellationToken))
        {
            var lockedUser = await database.Users
                .FromSqlInterpolated($"SELECT * FROM \"Users\" WHERE \"Id\" = {userId} FOR UPDATE")
                .SingleOrDefaultAsync(cancellationToken);
            if (lockedUser is null)
            {
                return Results.Unauthorized();
            }

            var activeSubmission = await database.Submissions
                .Where(item => item.UserId == userId &&
                    item.Status != SubmissionStatus.COMPLETED && item.Status != SubmissionStatus.FAILED)
                .OrderByDescending(item => item.UpdatedAtUtc)
                .Select(item => new { item.Id, item.Status })
                .FirstOrDefaultAsync(cancellationToken);
            if (activeSubmission is not null)
            {
                return Results.Conflict(new
                {
                    error = "Wait for your current code action to finish before starting another.",
                    activeSubmissionId = activeSubmission.Id,
                    status = activeSubmission.Status
                });
            }

            database.Submissions.Add(submission);
            await database.SaveChangesAsync(cancellationToken);
            submission.Status = SubmissionStatus.SANDBOX_QUEUED;
            submission.UpdatedAtUtc = DateTimeOffset.UtcNow;
            await database.SaveChangesAsync(cancellationToken);
            await transaction.CommitAsync(cancellationToken);
        }

        var queueTimer = Stopwatch.StartNew();
        await events.PublishAsync(submission, "stage", "Queued for sandbox evaluation", 5, cancellationToken, traceParent);

        try
        {
            var testCases = JsonSerializer.Deserialize<List<SandboxTestCase>>(assignment.TestCasesJson) ?? [];
            await publisher.PublishSandboxTaskAsync(new SandboxTask(
                submission.Id,
                assignment.Id,
                submission.SourceCode,
                ["-Wall", "-Wextra", "-O2", "-std=c11"],
                testCases,
                traceParent), cancellationToken);
            logger.LogInformation(
                "Pipeline stage completed {event_name} {stage} {submission_id} {trace_id} {duration_ms} {queue_publish_ms} {status} {backend} {attempt}",
                "pipeline.stage.completed", "submission_enqueue", submission.Id, TraceId(traceParent), requestTimer.ElapsedMilliseconds,
                queueTimer.ElapsedMilliseconds, "queued", backend, 0);
        }
        catch (Exception exception) when (exception is not OperationCanceledException)
        {
            submission.Status = SubmissionStatus.PENDING;
            submission.ErrorMessage = exception.Message;
            submission.UpdatedAtUtc = DateTimeOffset.UtcNow;
            await database.SaveChangesAsync(cancellationToken);
            logger.LogError(exception,
                "Pipeline stage failed {event_name} {stage} {submission_id} {trace_id} {duration_ms} {status} {backend} {attempt} {error_type}",
                "pipeline.stage.failed", "submission_enqueue", submission.Id, TraceId(traceParent), requestTimer.ElapsedMilliseconds,
                "failed", backend, 0, exception.GetType().Name);
            throw;
        }

        return Results.Accepted($"/api/submissions/{submission.Id}", new SubmissionAcceptedResponse(submission.Id, submission.Status));
    }

    private static async Task<IResult> Hint(
        CodeActionRequest request,
        ClaimsPrincipal principal,
        CoreDbContext database,
        CancellationToken cancellationToken) =>
        await UnavailableActionAsync("hint", request, principal, database, cancellationToken);

    private static async Task<IResult> Run(
        CodeActionRequest request,
        ClaimsPrincipal principal,
        CoreDbContext database,
        CancellationToken cancellationToken) =>
        await UnavailableActionAsync("run", request, principal, database, cancellationToken);

    private static async Task<IResult> UnavailableActionAsync(
        string action,
        CodeActionRequest request,
        ClaimsPrincipal principal,
        CoreDbContext database,
        CancellationToken cancellationToken)
    {
        if (!Guid.TryParse(principal.FindFirstValue(ClaimTypes.NameIdentifier), out var userId))
        {
            return Results.Unauthorized();
        }

        var activeSubmission = await database.Submissions
            .AnyAsync(item => item.UserId == userId &&
                item.Status != SubmissionStatus.COMPLETED && item.Status != SubmissionStatus.FAILED,
                cancellationToken);
        if (activeSubmission)
        {
            return Results.Conflict(new { error = "Wait for your current code action to finish before starting another." });
        }

        return Results.Problem(
            title: $"The {action} action is not available yet.",
            detail: "This action is not connected to the sandbox or LLM workers yet.",
            statusCode: StatusCodes.Status501NotImplemented);
    }

    private static async Task<IResult> Get(
        Guid id,
        ClaimsPrincipal principal,
        CoreDbContext database,
        CancellationToken cancellationToken)
    {
        var submission = await database.Submissions.AsNoTracking()
            .SingleOrDefaultAsync(item => item.Id == id, cancellationToken);
        if (submission is null)
        {
            return Results.NotFound();
        }

        var isTeacher = principal.IsInRole(UserRole.Teacher.ToString());
        if (!isTeacher && submission.UserId.ToString() != principal.FindFirstValue(ClaimTypes.NameIdentifier))
        {
            return Results.Forbid();
        }

        return Results.Ok(ToResponse(submission));
    }

    private static async Task<IResult> ListMine(
        ClaimsPrincipal principal,
        CoreDbContext database,
        CancellationToken cancellationToken)
    {
        if (!Guid.TryParse(principal.FindFirstValue(ClaimTypes.NameIdentifier), out var userId))
        {
            return Results.Unauthorized();
        }

        var submissions = await database.Submissions.AsNoTracking()
            .Where(item => item.UserId == userId)
            .OrderByDescending(item => item.CreatedAtUtc)
            .Select(item => new SubmissionListResponse(item.Id, item.AssignmentId, item.Status, item.CreatedAtUtc))
            .ToListAsync(cancellationToken);
        return Results.Ok(submissions);
    }

    private static SubmissionResponse ToResponse(Submission item) => new(
        item.Id,
        item.AssignmentId,
        item.LlmBackend,
        item.Status,
        item.RetryCount,
        item.GetSandboxOutput(),
        item.LlmFeedback,
        item.ErrorMessage,
        item.CreatedAtUtc,
        item.UpdatedAtUtc);

    private static string? TraceId(string? traceParent) =>
        traceParent?.Split('-') is { Length: 4 } parts ? parts[1] : null;
}

public sealed record SubmissionListResponse(Guid SubmissionId, Guid AssignmentId, SubmissionStatus Status, DateTimeOffset CreatedAtUtc);