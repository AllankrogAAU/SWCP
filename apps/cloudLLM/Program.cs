using cloudLLM;
using cloudLLM.Agents;
using cloudLLM.Interfaces;
using cloudLLM.Services;
using NATS.Client.Core;
using NATS.Client.JetStream;

var builder = Host.CreateApplicationBuilder(args);

builder.Services.AddSingleton<IChatClientProvider, ChatClientProvider>();

builder.Services.AddSingleton<ILlmAgent, CompilerAgent>();
builder.Services.AddSingleton<ILlmAgent, LintAgent>();
builder.Services.AddSingleton<ILlmAgent, RuntimeAgent>();
builder.Services.AddSingleton<AgentFactory>();
builder.Services.AddSingleton(_ => new NatsConnection(new NatsOpts
{
    Url = builder.Configuration["Nats:Url"] ?? "nats://localhost:4222",
    RetryOnInitialConnect = true
}));
builder.Services.AddSingleton<INatsJSContext>(services =>
    new NatsJSContext(services.GetRequiredService<NatsConnection>()));

builder.Services.AddHostedService<Worker>();

await builder.Build().RunAsync();
