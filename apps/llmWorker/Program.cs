using llmWorker;
using llmWorker.Services;
using NATS.Client.Core;
using NATS.Client.Serializers.Json;
using NATS.Client.JetStream;

var builder = Host.CreateApplicationBuilder(args);
builder.Logging.AddJsonConsole(options => options.TimestampFormat = "yyyy-MM-ddTHH:mm:ss.fffZ");

builder.Services.AddHttpClient("LocalLlm", client =>
{
    client.BaseAddress = new Uri(builder.Configuration["LocalLlm:BaseUrl"] ?? "http://local-llm-server:8080");
    client.Timeout = TimeSpan.FromMinutes(5);
});
builder.Services.AddSingleton<LlmTaskProcessor>();
builder.Services.AddSingleton(_ => new NatsConnection(new NatsOpts
{
    Url = builder.Configuration["Nats:Url"] ?? "nats://localhost:4222",
    RetryOnInitialConnect = true,
    SerializerRegistry = NatsJsonSerializerRegistry.Default
}));
builder.Services.AddSingleton<INatsJSContext>(services =>
    new NatsJSContext(services.GetRequiredService<NatsConnection>()));

builder.Services.AddHostedService<Worker>();

await builder.Build().RunAsync();
