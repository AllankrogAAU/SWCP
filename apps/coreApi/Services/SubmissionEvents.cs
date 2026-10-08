using coreApi.Models;
using NATS.Client.Core;
using SWCP.Contracts;

namespace coreApi.Services;

public sealed class SubmissionEvents(NatsConnection nats)
{
    public async Task PublishAsync(
        Submission submission,
        string eventType,
        string message,
        int percentComplete,
        CancellationToken cancellationToken,
        string? traceParent = null)
    {
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
        await nats.PublishAsync(
            Messaging.NotificationSubject(submission.Id),
            notification,
            headers: headers,
            cancellationToken: cancellationToken);
    }
}