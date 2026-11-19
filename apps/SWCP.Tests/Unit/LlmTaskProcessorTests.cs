using System.Net;
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
}

