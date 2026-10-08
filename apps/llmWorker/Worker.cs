using NATS.Client.Core;
using NATS.Client.JetStream;
using NATS.Client.JetStream.Models;
using SWCP.Contracts;
using llmWorker.Services;

namespace llmWorker;

public sealed class Worker(
    ILogger<Worker> logger,
    NatsConnection nats,
    INatsJSContext jetStream,
    LlmTaskProcessor processor) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await jetStream.CreateOrUpdateStreamAsync(
            new StreamConfig(Messaging.LlmTasksStream,
                [Messaging.LlmAzureTaskSubject, Messaging.LlmLocalTaskSubject])
            {
                Retention = StreamConfigRetention.Workqueue,
                DuplicateWindow = TimeSpan.FromMinutes(10)
            }, stoppingToken);
        await jetStream.CreateOrUpdateStreamAsync(
            new StreamConfig(Messaging.LlmResultsStream, [Messaging.LlmResultSubject])
            {
                Retention = StreamConfigRetention.Workqueue,
                DuplicateWindow = TimeSpan.FromMinutes(10)
            }, stoppingToken);
        await jetStream.CreateOrUpdateStreamAsync(
            new StreamConfig(Messaging.LlmTasksDlqStream, [Messaging.LlmTasksDlqSubject])
            {
                Retention = StreamConfigRetention.Limits,
                MaxAge = TimeSpan.FromDays(7)
            }, stoppingToken);

        var azureConsumer = await CreateConsumerAsync(
            Messaging.LlmAzureTaskConsumer,
            Messaging.LlmAzureTaskSubject,
            50,
            TimeSpan.FromSeconds(30),
            stoppingToken);
        var localConsumer = await CreateConsumerAsync(
            Messaging.LlmLocalTaskConsumer,
            Messaging.LlmLocalTaskSubject,
            4,
            TimeSpan.FromSeconds(60),
            stoppingToken);

        logger.LogInformation("LLM worker consuming Azure and local JetStream task subjects");
        await Task.WhenAll(
            ConsumeAsync(azureConsumer, stoppingToken),
            ConsumeAsync(localConsumer, stoppingToken));
    }

    private Task<INatsJSConsumer> CreateConsumerAsync(
        string name,
        string subject,
        long maxAckPending,
        TimeSpan ackWait,
        CancellationToken cancellationToken) =>
        jetStream.CreateOrUpdateConsumerAsync(
            Messaging.LlmTasksStream,
            new ConsumerConfig(name)
            {
                FilterSubject = subject,
                AckWait = ackWait,
                MaxAckPending = maxAckPending,
                MaxDeliver = 3
            },
            cancellationToken).AsTask();

    private async Task ConsumeAsync(INatsJSConsumer consumer, CancellationToken stoppingToken)
    {
        await foreach (var message in consumer.ConsumeAsync<LlmTask>(cancellationToken: stoppingToken))
        {
            try
            {
                message.EnsureSuccess();
            }
            catch (Exception exception)
            {
                logger.LogError(exception, "Invalid LLM task payload; copying to DLQ");
                await jetStream.PublishAsync(Messaging.LlmTasksDlqSubject, message.Data,
                    cancellationToken: stoppingToken);
                await message.AckAsync(cancellationToken: stoppingToken);
                continue;
            }

            var task = message.Data;
            if (task is null)
            {
                await message.AckAsync(cancellationToken: stoppingToken);
                continue;
            }

            try
            {
                var headers = new NatsHeaders();
                if (!string.IsNullOrWhiteSpace(task.TraceParent))
                {
                    headers.Add(Messaging.TraceParentHeader, task.TraceParent);
                }
                await nats.PublishAsync(Messaging.WorkStartedSubject, new SubmissionWorkStarted(
                    task.SubmissionId, "llm-worker", "llm", DateTimeOffset.UtcNow), headers: headers, cancellationToken: stoppingToken);
                var completion = await processor.CompleteAsync(task, stoppingToken);
                await PublishResultAsync(new LlmResult(
                    task.SubmissionId,
                    "success",
                    completion.FeedbackMarkdown,
                    completion.TokenUsage,
                    null), stoppingToken, task.TraceParent);
                await message.AckAsync(cancellationToken: stoppingToken);
                logger.LogInformation("Completed {Backend} LLM task for submission {SubmissionId}", task.Backend, task.SubmissionId);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (RetryableLlmException exception)
            {
                var deliveryCount = message.Metadata?.NumDelivered ?? 1UL;
                if (deliveryCount >= 3)
                {
                    await jetStream.PublishAsync(Messaging.LlmTasksDlqSubject, task, cancellationToken: stoppingToken);
                    await PublishResultAsync(new LlmResult(task.SubmissionId, "error", null, null,
                        $"LLM retries exhausted: {exception.Message}"), stoppingToken, task.TraceParent);
                    await message.AckAsync(cancellationToken: stoppingToken);
                    logger.LogError(exception, "LLM task exhausted retries and was moved to the DLQ for submission {SubmissionId}", task.SubmissionId);
                }
                else
                {
                    logger.LogWarning(exception, "Retrying LLM task for submission {SubmissionId} after {Delay}", task.SubmissionId, exception.RetryDelay);
                    await message.NakAsync(exception.RetryDelay, stoppingToken);
                }
            }
            catch (Exception exception)
            {
                logger.LogError(exception, "LLM task failed for submission {SubmissionId}", task.SubmissionId);
                try
                {
                    await PublishResultAsync(new LlmResult(task.SubmissionId, "error", null, null, exception.Message), stoppingToken, task.TraceParent);
                    await message.AckAsync(cancellationToken: stoppingToken);
                }
                catch (Exception publishException)
                {
                    logger.LogError(publishException, "Could not publish failure result for submission {SubmissionId}", task.SubmissionId);
                    await message.NakAsync(TimeSpan.FromSeconds(5), stoppingToken);
                }
            }
        }
    }

    private Task PublishResultAsync(LlmResult result, CancellationToken cancellationToken, string? traceParent)
    {
        var headers = new NatsHeaders
        {
            { "Nats-Msg-Id", $"{result.SubmissionId:D}-llm-result" }
        };
        headers.Add(Messaging.TraceParentHeader, traceParent ?? Messaging.CurrentOrNewTraceParent());
        return jetStream.PublishAsync(
            Messaging.LlmResultSubject,
            result,
            headers: headers,
            cancellationToken: cancellationToken).AsTask();
    }
}