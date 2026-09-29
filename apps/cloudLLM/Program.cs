using cloudLLM;
using cloudLLM.Agents;
using cloudLLM.Interfaces;
using cloudLLM.Services;

EnvFileLoader.Load(AppContext.BaseDirectory);

var builder = Host.CreateApplicationBuilder(args);

builder.Services.AddSingleton<IChatClientProvider, ChatClientProvider>();

builder.Services.AddSingleton<ILlmAgent, CompilerAgent>();
builder.Services.AddSingleton<ILlmAgent, LintAgent>();
builder.Services.AddSingleton<ILlmAgent, RuntimeAgent>();
builder.Services.AddSingleton<AgentFactory>();

builder.Services.AddHostedService<Worker>();

var host = builder.Build();
host.Run();
