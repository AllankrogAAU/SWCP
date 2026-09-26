using cloudLLM;
using cloudLLM.Agents;
using Azure;
using Azure.AI.Inference;
using cloudLLM.Interfaces;

EnvFileLoader.Load(AppContext.BaseDirectory);

var builder = Host.CreateApplicationBuilder(args);

builder.Services.AddSingleton(sp =>
{
    var config = builder.Configuration.GetSection("AzureAIFoundry");
    var endpoint = new Uri(config["Endpoint"]!);
    var credential = new AzureKeyCredential(config["ApiKey"]!);

    return new ChatCompletionsClient(endpoint, credential);
});

builder.Services.AddSingleton<ILlmAgent, CompilerAgent>();
builder.Services.AddSingleton<ILlmAgent, LintAgent>();
builder.Services.AddSingleton<ILlmAgent, RuntimeAgent>();
builder.Services.AddSingleton<AgentFactory>();

builder.Services.AddHostedService<Worker>();

var host = builder.Build();
host.Run();
