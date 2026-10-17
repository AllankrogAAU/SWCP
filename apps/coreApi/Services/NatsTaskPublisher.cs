using NATS.Client.Core;
using NATS.Client.JetStream;
using NATS.Client.JetStream.Models;
using SWCP.Contracts;

namespace coreApi.Services;

public sealed class NatsTaskPublisher(INatsJSContext jetStream)
{
    public async Task PublishSandboxTaskAsync(
        SandboxTask task,
        CancellationToken cancellationToken,
        int attempt = 0)
    {
        await jetStream.CreateOrUpdateStreamAsync(
            new StreamConfig(Messaging.SandboxTasksStream, [Messaging.SandboxTaskSubject])
            {
                Retention = StreamConfigRetention.Workqueue,
                DuplicateWindow = TimeSpan.FromMinutes(10)
            }, cancellationToken);
        await jetStream.CreateOrUpdateStreamAsync(
            new StreamConfig(Messaging.SandboxTasksDlqStream, [Messaging.SandboxTasksDlqSubject])
            {
                Retention = StreamConfigRetention.Limits,
                MaxAge = TimeSpan.FromDays(7)
            }, cancellationToken);

        var headers = new NatsHeaders
        {
            { "Nats-Msg-Id", Messaging.SandboxMessageId(task.SubmissionId, attempt) },
            { Messaging.TraceParentHeader, task.TraceParent ?? Messaging.CurrentOrNewTraceParent() }
        };
        await jetStream.PublishAsync(
            Messaging.SandboxTaskSubject,
            task,
            headers: headers,
            cancellationToken: cancellationToken);
    }

    public async Task PublishLlmTaskAsync(LlmTask task, CancellationToken cancellationToken)
    {
        await jetStream.CreateOrUpdateStreamAsync(
            new StreamConfig(Messaging.LlmTasksStream,
                [Messaging.LlmAzureTaskSubject, Messaging.LlmLocalTaskSubject])
            {
                Retention = StreamConfigRetention.Workqueue,
                DuplicateWindow = TimeSpan.FromMinutes(10)
            }, cancellationToken);

        var subject = task.Backend == "local"
            ? Messaging.LlmLocalTaskSubject
            : Messaging.LlmAzureTaskSubject;
        var headers = new NatsHeaders
        {
            { "Nats-Msg-Id", Messaging.LlmMessageId(task.SubmissionId) },
            { Messaging.TraceParentHeader, task.TraceParent ?? Messaging.CurrentOrNewTraceParent() }
        };
        await jetStream.PublishAsync(subject, task, headers: headers, cancellationToken: cancellationToken);
    }
}