using System.Net;
using System.Net.Http.Json;
using System.Text;
using llmWorker.Services;
using Microsoft.Extensions.Configuration;
using SWCP.Contracts;
using Xunit;

namespace SWCP.Tests.Unit;

public sealed class LlmTaskProcessorTests
{
    private sealed class StubHttpClientFactory : IHttpClientFactory
    {
        public HttpClient CreateClient(string name) => new();
    }

    private sealed class ResponseHttpClientFactory(string responseBody) : IHttpClientFactory
    {
        private sealed class ResponseHandler(string content) : HttpMessageHandler
        {
            protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
                Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
                {
                    Content = new StringContent(content, Encoding.UTF8, "application/json")
                });
        }

        public HttpClient CreateClient(string name) => new(new ResponseHandler(responseBody))
        {
            BaseAddress = new Uri("http://localhost")
        };
    }

    [Fact]
    public async Task CompleteAsync_UnsupportedBackend_ThrowsInvalidOperationException()
    {
        var config = new ConfigurationBuilder().Build();
        var processor = new LlmTaskProcessor(config, new StubHttpClientFactory());
        var task = new LlmTask(
            Guid.NewGuid(),
            "unsupported_backend",
            "system prompt",
            "user prompt",
            new LlmInferenceParameters(0.2, 100, 1.0));

        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            processor.CompleteAsync(task, CancellationToken.None));

        Assert.Contains("Unsupported LLM backend 'unsupported_backend'", ex.Message);
    }

    [Fact]
    public async Task CompleteAsync_Local_ConvertsAverageLogProbabilityToPercent()
    {
        const string responseBody = """
            {
              "choices": [{
                "finish_reason": "stop",
                "message": { "content": "{\"feedback\":\"ok\",\"taskSolved\":null}" },
                "logprobs": { "content": [{ "token": "ok", "logprob": -0.6931471805599453 }] }
              }],
              "usage": { "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2 }
            }
            """;
        var config = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["LocalLlm:ModelName"] = "test-model",
                ["LocalLlm:AzureFallbackThreshold"] = "0"
            })
            .Build();
        var processor = new LlmTaskProcessor(config, new ResponseHttpClientFactory(responseBody));
        var task = new LlmTask(
            Guid.NewGuid(),
            "local",
            "system prompt",
            "user prompt",
            new LlmInferenceParameters(0.2, 100, 1.0));

        var completion = await processor.CompleteAsync(task, CancellationToken.None);

        Assert.Equal(50, completion.ConfidenceScorePercent, precision: 4);
        Assert.Equal("local", completion.BackendUsed);
    }

    [Fact]
    public void RetryableLlmException_PreservesInnerExceptionAndDelay()
    {
        var inner = new HttpRequestException("429 Too Many Requests");
        var retryDelay = TimeSpan.FromSeconds(5);

        var ex = new RetryableLlmException(retryDelay, inner);

        Assert.Equal(retryDelay, ex.RetryDelay);
        Assert.Same(inner, ex.InnerException);
        Assert.Contains("429", ex.Message);
    }

    private sealed class RoutingStubHandler(
        Func<string, HttpRequestMessage, HttpResponseMessage> responder) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
            => Task.FromResult(responder(request.RequestUri?.Host ?? string.Empty, request));
    }

    private sealed class NamedHttpClientFactory(
        Func<string, HttpRequestMessage, HttpResponseMessage> responder) : IHttpClientFactory
    {
        public HttpClient CreateClient(string name)
        {
            var client = new HttpClient(new RoutingStubHandler(responder))
            {
                BaseAddress = new Uri(name == "LocalLlmOverflow" ? "http://overflow.local:8080" : "http://primary.local:8080")
            };
            return client;
        }
    }

    private static HttpResponseMessage JsonCompletionResponse(string feedback) =>
        new(HttpStatusCode.OK)
        {
            Content = JsonContent.Create(new
            {
                choices = new[]
                {
                    new
                    {
                        finish_reason = "stop",
                        message = new { content = $$"""{"feedback":"{{feedback}}","taskSolved":true}""" }
                    }
                },
                usage = new { prompt_tokens = 10, completion_tokens = 5, total_tokens = 15 }
            })
        };

    [Fact]
    public async Task CompleteAsync_LocalRouting_ReportsPrimaryOverflowAndFallbackTargets()
    {
        var metricsLoad = 0;
        var overflowStatus = HttpStatusCode.OK;
        var config = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["LocalLlm:ModelName"] = "test-model",
                ["LocalLlm:OverflowBaseUrl"] = "http://overflow.local:8080",
                ["LocalLlm:PrimaryMaxConcurrent"] = "4"
            })
            .Build();

        var factory = new NamedHttpClientFactory((host, req) =>
        {
            if (req.RequestUri?.AbsolutePath == "/metrics")
            {
                return new HttpResponseMessage(HttpStatusCode.OK)
                {
                    Content = new StringContent($"vllm:num_requests_running {metricsLoad}\nvllm:num_requests_waiting 0\n")
                };
            }

            if (host == "overflow.local" && overflowStatus != HttpStatusCode.OK)
            {
                return new HttpResponseMessage(overflowStatus);
            }

            return JsonCompletionResponse(host == "overflow.local" ? "from-overflow" : "from-primary");
        });

        var processor = new LlmTaskProcessor(config, factory);
        var task = new LlmTask(
            Guid.NewGuid(),
            "local",
            "system",
            "user",
            new LlmInferenceParameters(0.2, 100, 1.0),
            EvaluationMode: "submit");

        var primaryResult = await processor.CompleteAsync(task, CancellationToken.None);
        Assert.Equal("primary", primaryResult.Target);
        Assert.Equal("from-primary", primaryResult.FeedbackMarkdown);

        // Wait past metrics cache (200ms) then simulate saturated primary
        await Task.Delay(250);
        metricsLoad = 4;
        var overflowResult = await processor.CompleteAsync(task, CancellationToken.None);
        Assert.Equal("overflow", overflowResult.Target);
        Assert.Equal("from-overflow", overflowResult.FeedbackMarkdown);

        overflowStatus = HttpStatusCode.ServiceUnavailable;
        var fallbackResult = await processor.CompleteAsync(task, CancellationToken.None);
        Assert.Equal("overflow_fallback", fallbackResult.Target);
        Assert.Equal("from-primary", fallbackResult.FeedbackMarkdown);
    }
}

