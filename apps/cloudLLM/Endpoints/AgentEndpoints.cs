using cloudLLM.Agents;
using cloudLLM.Models;

namespace cloudLLM.Endpoints
{
    public static class AgentEndpoints
    {
        public static RouteGroupBuilder MapAgentEndpoints(this IEndpointRouteBuilder app)
        {
            var group = app.MapGroup("api/agents").WithTags("Agents");

            group.MapPost("/{category}/analyze", Analyze)
                .WithName("AnalyzeWithAgent")
                .WithSummary("Directly invokes an LLM agent for synchronous testing, bypassing Kafka.")
                .WithDescription("Intended for manual testing/debugging of agent prompts. The normal production flow goes through Kafka via the Worker.")
                .Produces<AgentAnalyzeResponse>()
                .Produces(StatusCodes.Status400BadRequest);

            return group;
        }

        private static async Task<IResult> Analyze(
            ErrorCategory category,
            AgentAnalyzeRequest request,
            AgentFactory agentFactory,
            CancellationToken cancellationToken)
        {
            var agent = agentFactory.GetAgent(category);
            var response = await agent.AnalyzeAsync(request.CCode, request.Logs, cancellationToken);

            return Results.Ok(new AgentAnalyzeResponse
            {
                Category = category,
                Response = response
            });
        }
    }
}
