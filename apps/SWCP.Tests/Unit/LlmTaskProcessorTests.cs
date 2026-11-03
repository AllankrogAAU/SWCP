using llmWorker.Services;
using Microsoft.Extensions.Configuration;
using SWCP.Contracts;
using System.Net;
using System.Text;
using System.Text.Json;
using Xunit;

namespace SWCP.Tests.Unit;

public sealed class LlmTaskProcessorTests
{
    private sealed class StubHttpClientFactory(HttpClient client) : IHttpClientFactory
    {
        public HttpClient CreateClient(string name) => client;
    }

    private sealed class StubHttpMessageHandler(
        Func<HttpRequestMessage, HttpResponseMessage> handler) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request,
            CancellationToken cancellationToken) => Task.FromResult(handler(request));
    }

    [Fact]
    public async Task CompleteAsync_UnsupportedBackend_ThrowsInvalidOperationException()
    {
        var config = new ConfigurationBuilder().Build();
        var processor = new LlmTaskProcessor(config, new StubHttpClientFactory(new HttpClient()));
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
    public void FeedbackJsonStreamParser_ExtractsPartialEscapedFeedback()
    {
        var parser = new FeedbackJsonStreamParser();

        Assert.Equal("Check ", parser.Append("{\"feedback\":\"Check "));
        Assert.Equal("the \"loop\" ", parser.Append("the \\\"loop\\\" "));
        Assert.Equal("condition.\n", parser.Append("condition.\\n"));
        Assert.Equal(string.Empty, parser.Append("\",\"taskSolved\":null}"));

        parser.Complete();
    }

    [Fact]
    public async Task CompleteStreamingAsync_LocalStreamsFeedbackAndTokenUsage()
    {
        const string events =
            "data: {\"choices\":[{\"delta\":{\"content\":\"{\\\"feedback\\\":\\\"Check \"}}]}\n\n" +
            "data: {\"choices\":[{\"delta\":{\"content\":\"the loop.\\\\n\\\",\\\"taskSolved\\\":null}\"},\"finish_reason\":\"stop\"}]}\n\n" +
            "data: {\"choices\":[],\"usage\":{\"prompt_tokens\":12,\"completion_tokens\":5,\"total_tokens\":17}}\n\n" +
            "data: [DONE]\n\n";
        var handler = new StubHttpMessageHandler(request =>
        {
            Assert.Equal(HttpMethod.Post, request.Method);
            Assert.Equal("/v1/chat/completions", request.RequestUri?.AbsolutePath);
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StreamContent(new MemoryStream(Encoding.UTF8.GetBytes(events)))
            };
        });
        using var client = new HttpClient(handler) { BaseAddress = new Uri("http://vllm.test") };
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["LocalLlm:ModelName"] = "test-model"
            })
            .Build();
        var processor = new LlmTaskProcessor(configuration, new StubHttpClientFactory(client));
        var task = new LlmTask(
            Guid.NewGuid(),
            "local",
            "system prompt",
            "user prompt",
            new LlmInferenceParameters(0.2, 100, 1.0));
        var chunks = new List<string>();

        var completion = await processor.CompleteStreamingAsync(
            task,
            (delta, _) =>
            {
                chunks.Add(delta);
                return ValueTask.CompletedTask;
            },
            CancellationToken.None);

        Assert.Equal("Check the loop.\n", string.Concat(chunks));
        Assert.Equal("Check the loop.\n", completion.FeedbackMarkdown);
        Assert.Equal(new LlmTokenUsage(12, 5, 17), completion.TokenUsage);
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
