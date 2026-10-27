using System.Collections.Concurrent;
using System.Diagnostics;
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
    LlmTaskProcessor processor,
    IConfiguration? configuration = null) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        var localMaxConcurrent = Math.Max(1,
            configuration?.GetValue<int?>("LocalLlm:MaxConcurrent")
            ?? configuration?.GetValue<int?>("VLLM_MAX_NUM_SEQS")
            ?? 4);
        var localMaxAckPending = Math.Max(localMaxConcurrent,
            configuration?.GetValue<int?>("LocalLlm:MaxAckPending")
            ?? localMaxConcurrent);
        var localAckWait = TimeSpan.FromSeconds(Math.Max(30,
            configuration?.GetValue<int?>("LocalLlm:AckWaitSeconds") ?? 180));

        var azureMaxConcurrent = Math.Max(1,
            configuration?.GetValue<int?>("AzureAIFoundry:MaxConcurrent") ?? 16);
        var azureMaxAckPending = Math.Max(azureMaxConcurrent,
            configuration?.GetValue<int?>("AzureAIFoundry:MaxAckPending") ?? 50);
        var azureAckWait = TimeSpan.FromSeconds(Math.Max(30,
            configuration?.GetValue<int?>("AzureAIFoundry:AckWaitSeconds") ?? 60));

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
            azureMaxAckPending,
            azureAckWait,
            stoppingToken);
        var localConsumer = await CreateConsumerAsync(
            Messaging.LlmLocalTaskConsumer,
            Messaging.LlmLocalTaskSubject,
            localMaxAckPending,
            localAckWait,
            stoppingToken);

        logger.LogInformation(
            "LLM worker consuming Azure (maxConcurrent={AzureMaxConcurrent}, maxAckPending={AzureMaxAckPending}) and local (maxConcurrent={LocalMaxConcurrent}, maxAckPending={LocalMaxAckPending}) JetStream task subjects",
            azureMaxConcurrent, azureMaxAckPending, localMaxConcurrent, localMaxAckPending);
        await Task.WhenAll(
            ConsumeAsync(azureConsumer, azureMaxConcurrent, stoppingToken),
            ConsumeAsync(localConsumer, localMaxConcurrent, stoppingToken));
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

    private async Task ConsumeAsync(INatsJSConsumer consumer, int maxConcurrent, CancellationToken stoppingToken)
    {
        using var concurrencyLimiter = new SemaphoreSlim(maxConcurrent, maxConcurrent);
        var inFlight = new ConcurrentDictionary<Task, byte>();
        var consumeOpts = new NatsJSConsumeOpts { MaxMsgs = maxConcurrent };

        try
        {
            await foreach (var message in consumer.ConsumeAsync<LlmTask>(opts: consumeOpts, cancellationToken: stoppingToken))
            {
                await concurrencyLimiter.WaitAsync(stoppingToken);
                var task = ProcessMessageWithPermitAsync(message, concurrencyLimiter, stoppingToken);
                inFlight.TryAdd(task, 0);
                _ = task.ContinueWith(
                    completed => inFlight.TryRemove(completed, out _),
                    CancellationToken.None,
                    TaskContinuationOptions.ExecuteSynchronously,
                    TaskScheduler.Default);
            }
        }
        finally
        {
            if (!inFlight.IsEmpty)
            {
                await Task.WhenAll(inFlight.Keys);
            }
        }
    }

    private async Task ProcessMessageWithPermitAsync(
        INatsJSMsg<LlmTask> message,
        SemaphoreSlim concurrencyLimiter,
        CancellationToken stoppingToken)
    {
        try
        {
            await HandleMessageAsync(message, stoppingToken);
        }
        catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            logger.LogError(exception, "Unhandled error while processing LLM task message");
        }
        finally
        {
            concurrencyLimiter.Release();
        }
    }

    private async Task HandleMessageAsync(INatsJSMsg<LlmTask> message, CancellationToken stoppingToken)
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
            return;
        }

        var task = message.Data;
        if (task is null)
        {
            await message.AckAsync(cancellationToken: stoppingToken);
            return;
        }

        var stageTimer = Stopwatch.StartNew();
        var attempt = message.Metadata?.NumDelivered ?? 1UL;
        var traceId = task.TraceParent?.Split('-') is { Length: 4 } traceParts ? traceParts[1] : null;
        try
        {
            logger.LogInformation(
                "Pipeline stage started {event_name} {stage} {submission_id} {trace_id} {backend} {attempt} {status}",
                "pipeline.stage.started", "llm", task.SubmissionId, traceId, task.Backend, attempt, "processing");
            var headers = new NatsHeaders();
            if (!string.IsNullOrWhiteSpace(task.TraceParent))
            {
                headers.Add(Messaging.TraceParentHeader, task.TraceParent);
            }
            await nats.PublishAsync(Messaging.WorkStartedSubject, new SubmissionWorkStarted(
                task.SubmissionId, "llm-worker", "llm", DateTimeOffset.UtcNow), headers: headers, cancellationToken: stoppingToken);
            var completion = await processor.CompleteAsync(task, stoppingToken);
            stageTimer.Stop();
            var promptTokens = completion.TokenUsage?.PromptTokens;
            var completionTokens = completion.TokenUsage?.CompletionTokens;
            var totalTokens = completion.TokenUsage?.TotalTokens;
            var tokensPerSec = completionTokens > 0 && stageTimer.ElapsedMilliseconds > 0
                ? Math.Round((double)completionTokens.Value / (stageTimer.ElapsedMilliseconds / 1000.0), 1)
                : 0.0;
            logger.LogInformation(
                "Pipeline stage completed {event_name} {stage} {submission_id} {trace_id} {duration_ms} {backend} {attempt} {status} {prompt_tokens} {completion_tokens} {total_tokens} {tokens_per_second}",
                "pipeline.stage.completed", "llm", task.SubmissionId, traceId, stageTimer.ElapsedMilliseconds,
                task.Backend, attempt, "completed", promptTokens, completionTokens, totalTokens, tokensPerSec);
            await PublishResultAsync(new LlmResult(
                task.SubmissionId,
                "success",
                completion.FeedbackMarkdown,
                completion.TokenUsage,
                null,
                completion.TaskSolved), stoppingToken, task.TraceParent);
            await message.AckAsync(cancellationToken: stoppingToken);
        }
        catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
        {
            throw;
        }
        catch (RetryableLlmException exception)
        {
            var deliveryCount = message.Metadata?.NumDelivered ?? 1UL;
            if (deliveryCount >= 3)
            {
                logger.LogError(exception,
                    "Pipeline stage failed {event_name} {stage} {submission_id} {trace_id} {duration_ms} {backend} {attempt} {status} {error_type}",
                    "pipeline.stage.failed", "llm", task.SubmissionId,
                    task.TraceParent?.Split('-') is { Length: 4 } failedTraceParts ? failedTraceParts[1] : null,
                    stageTimer.ElapsedMilliseconds, task.Backend, deliveryCount, "failed", exception.GetType().Name);
                await jetStream.PublishAsync(Messaging.LlmTasksDlqSubject, task, cancellationToken: stoppingToken);
                await PublishResultAsync(new LlmResult(task.SubmissionId, "error", null, null,
                    $"LLM retries exhausted: {exception.Message}"), stoppingToken, task.TraceParent);
                await message.AckAsync(cancellationToken: stoppingToken);
                logger.LogError(exception, "LLM task exhausted retries and was moved to the DLQ for submission {SubmissionId}", task.SubmissionId);
            }
            else
            {
                logger.LogWarning(exception,
                    "Pipeline stage retrying {event_name} {stage} {submission_id} {trace_id} {duration_ms} {backend} {attempt} {status} {retry_delay_ms} {error_type}",
                    "pipeline.stage.retrying", "llm", task.SubmissionId,
                    task.TraceParent?.Split('-') is { Length: 4 } retryTraceParts ? retryTraceParts[1] : null,
                    stageTimer.ElapsedMilliseconds, task.Backend, deliveryCount, "retrying",
                    exception.RetryDelay.TotalMilliseconds, exception.GetType().Name);
                await message.NakAsync(exception.RetryDelay, stoppingToken);
            }
        }
        catch (Exception exception)
        {
            logger.LogError(exception,
                "Pipeline stage failed {event_name} {stage} {submission_id} {trace_id} {duration_ms} {backend} {attempt} {status} {error_type}",
                "pipeline.stage.failed", "llm", task.SubmissionId,
                task.TraceParent?.Split('-') is { Length: 4 } errorTraceParts ? errorTraceParts[1] : null,
                stageTimer.ElapsedMilliseconds, task.Backend, attempt, "failed", exception.GetType().Name);
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
