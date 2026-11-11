using System.Text.Json;
using llmWorker.Services;
using Microsoft.Extensions.Configuration;
using SWCP.Contracts;
using Xunit;

namespace SWCP.Tests.Unit;

public sealed class LlmTaskProcessorTests
{
    private sealed class StubHttpClientFactory(HttpMessageHandler handler) : IHttpClientFactory
    {
        public HttpClient CreateClient(string name) => new(handler)
        {
            BaseAddress = new Uri("http://localhost")
        };
    }

    private sealed class JsonHandler(string json) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(new HttpResponseMessage(System.Net.HttpStatusCode.OK)
            {
                Content = new StringContent(json, System.Text.Encoding.UTF8, "application/json")
            });
    }

    private static LlmTask CreateTask(
        string backend = "local", string evaluationMode = "submit", bool sandboxFailed = false) =>
        new(
            Guid.NewGuid(),
            backend,
            "system prompt",
            "user prompt",
            new LlmInferenceParameters(0.2, 100, 1.0),
            EvaluationMode: evaluationMode,
            SandboxFailed: sandboxFailed);

    private static string LocalResponse(string completionJson) =>
        JsonSerializer.Serialize(new
        {
            choices = new[] { new { message = new { content = completionJson } } }
        });

    [Fact]
    public async Task CompleteAsync_UnsupportedBackend_ThrowsInvalidOperationException()
    {
        var config = new ConfigurationBuilder().Build();
        var processor = new LlmTaskProcessor(config, new StubHttpClientFactory(new JsonHandler("{}")));

        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            processor.CompleteAsync(CreateTask("unsupported_backend"), CancellationToken.None));

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

    [Fact]
    public async Task CompleteAsync_LocalBackend_UsesLocalCompletion()
    {
        var config = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { ["LocalLlm:ModelName"] = "test-model" })
            .Build();
        var processor = new LlmTaskProcessor(
            config,
            new StubHttpClientFactory(new JsonHandler(
                LocalResponse("""{"feedback":"Looks good","taskSolved":true}"""))));

        var completion = await processor.CompleteAsync(CreateTask("local"), CancellationToken.None);

        Assert.Equal("Looks good", completion.FeedbackMarkdown);
        Assert.True(completion.TaskSolved);
    }

    [Fact]
    public async Task CompleteAsync_AzureBackend_UsesAzureConfiguration()
    {
        var config = new ConfigurationBuilder().Build();
        var processor = new LlmTaskProcessor(config, new StubHttpClientFactory(new JsonHandler("{}")));

        var exception = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            processor.CompleteAsync(CreateTask("azure"), CancellationToken.None));

        Assert.Contains("Azure AI Foundry configuration", exception.Message);
    }

    [Theory]
    [InlineData("""{"feedback":3,"taskSolved":true}""")]
    [InlineData("""{"feedback":"ok","taskSolved":"yes"}""")]
    [InlineData("""{"feedback":"ok"}""")]
    public async Task CompleteAsync_InvalidCompletionShape_ThrowsJsonException(string completionJson)
    {
        var config = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { ["LocalLlm:ModelName"] = "test-model" })
            .Build();
        var processor = new LlmTaskProcessor(
            config,
            new StubHttpClientFactory(new JsonHandler(LocalResponse(completionJson))));

        await Assert.ThrowsAsync<JsonException>(() =>
            processor.CompleteAsync(CreateTask("local"), CancellationToken.None));
    }

    [Theory]
    [InlineData("submit", false, true)]
    [InlineData("hint", false, null)]
    [InlineData("submit", true, null)]
    public async Task CompleteAsync_TaskSolvedIsRetainedOnlyForSuccessfulSubmit(
        string evaluationMode, bool sandboxFailed, bool? expectedTaskSolved)
    {
        var config = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?> { ["LocalLlm:ModelName"] = "test-model" })
            .Build();
        var processor = new LlmTaskProcessor(
            config,
            new StubHttpClientFactory(new JsonHandler(
                LocalResponse("""{"feedback":"feedback","taskSolved":true}"""))));

        var completion = await processor.CompleteAsync(
            CreateTask("local", evaluationMode, sandboxFailed), CancellationToken.None);

        Assert.Equal(expectedTaskSolved, completion.TaskSolved);
    }
}
