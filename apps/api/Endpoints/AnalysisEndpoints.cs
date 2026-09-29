using api.Models;
using api.Services;

namespace api.Endpoints
{
    public static class AnalysisEndpoints
    {
        public static RouteGroupBuilder MapAnalysisEndpoints(this IEndpointRouteBuilder app)
        {
            var group = app.MapGroup("api/analysis").WithTags("Analysis");

            group.MapPost("/", Submit)
                .WithSummary("THIS IS JUST A TEMPLATE ENDPOINT FOR DEMONSTRATION. DO NOT USE THIS IS PROD")
                .WithDescription("Submits C code (and optional compiler/lint/runtime logs) for LLM-based analysis. The request is published to Kafka and processed asynchronously by the cloudLLM worker.")
                .Produces<AnalysisSubmitResponse>(StatusCodes.Status202Accepted);

            group.MapGet("/{jobId:guid}", GetStatus)
                .WithName("GetAnalysisStatus")
                .WithSummary("THIS IS JUST A TEMPLATE ENDPOINT FOR DEMONSTRATION. DO NOT USE THIS IS PROD")
                .WithDescription("Polls for the result of a previously submitted analysis job.")
                .Produces<AnalysisStatusResponse>();

            return group;
        }

        private static async Task<IResult> Submit(
            AnalysisSubmitRequest request,
            IKafkaProducerService producer,
            CancellationToken cancellationToken)
        {
            var jobId = Guid.NewGuid();

            var message = new AnalysisRequestMessage
            {
                JobId = jobId,
                Category = request.Category.ToString(),
                CCode = request.CCode,
                Logs = request.Logs
            };

            await producer.PublishAnalysisRequestAsync(message, cancellationToken);

            return Results.AcceptedAtRoute("GetAnalysisStatus", new { jobId }, new AnalysisSubmitResponse { JobId = jobId });
        }

        private static IResult GetStatus(Guid jobId, AnalysisResultStore resultStore)
        {
            if (!resultStore.TryGetResult(jobId, out var result) || result is null)
            {
                return Results.Ok(new AnalysisStatusResponse
                {
                    JobId = jobId,
                    Status = AnalysisJobStatus.Pending
                });
            }

            return Results.Ok(new AnalysisStatusResponse
            {
                JobId = jobId,
                Status = result.Success ? AnalysisJobStatus.Completed : AnalysisJobStatus.Failed,
                Response = result.Response,
                Error = result.Error
            });
        }
    }
}
