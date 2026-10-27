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

