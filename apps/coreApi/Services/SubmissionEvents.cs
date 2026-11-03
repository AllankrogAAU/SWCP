using coreApi.Models;
using NATS.Client.Core;
using SWCP.Contracts;

namespace coreApi.Services;

public class SubmissionEvents
{
    private readonly NatsConnection? _nats;

    public SubmissionEvents()
    {
    }

    public SubmissionEvents(NatsConnection nats)
    {
        _nats = nats;
    }

    public virtual async Task PublishAsync(
        Submission submission,
        string eventType,
        string message,
        int percentComplete,
        CancellationToken cancellationToken,
        string? traceParent = null)
    {
        if (_nats is null) return;
        var notification = new SubmissionNotification(
            submission.Id,
            eventType,
            submission.Status.ToString(),
            message,
            percentComplete,
            DateTimeOffset.UtcNow);
        var headers = new NatsHeaders
        {
            { Messaging.TraceParentHeader, traceParent ?? Messaging.CurrentOrNewTraceParent() }
        };
        await _nats.PublishAsync(
            Messaging.NotificationSubject(submission.Id),
            notification,
            headers: headers,
            cancellationToken: cancellationToken);
    }

    public virtual async Task PublishFeedbackStreamAsync(
        LlmFeedbackStreamMessage update,
        CancellationToken cancellationToken)
    {
        if (_nats is null) return;

        var eventType = update.EventType switch
        {
            "reset" => "feedback.reset",
            "delta" => "feedback.delta",
            _ => throw new ArgumentOutOfRangeException(nameof(update), update.EventType, "Unknown feedback stream event.")
        };
        var notification = new SubmissionNotification(
            update.SubmissionId,
            eventType,
            "LLM_PROCESSING",
            update.Delta,
            90,
            DateTimeOffset.UtcNow,
            update.Attempt,
            update.Sequence);
        var headers = new NatsHeaders();
        if (!string.IsNullOrWhiteSpace(update.TraceParent))
        {
            headers.Add(Messaging.TraceParentHeader, update.TraceParent);
        }

        await _nats.PublishAsync(
            Messaging.NotificationSubject(update.SubmissionId),
            notification,
            headers: headers,
            cancellationToken: cancellationToken);
    }
}
