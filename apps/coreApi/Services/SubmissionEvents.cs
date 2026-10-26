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
}
