using api.Models;
using api.Services;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;

namespace api.Controllers
{
    [ApiController]
    [Route("api/[controller]")]
    public class AnalysisController(IKafkaProducerService producer, AnalysisResultStore resultStore) : ControllerBase
    {
        [HttpPost]
        [EndpointSummary("THIS IS JUST A TEMPLATE ENDPOINT FOR DEMONSTRATION. DO NOT USE THIS IS PROD")]
        [EndpointDescription("Submits C code (and optional compiler/lint/runtime logs) for LLM-based analysis. The request is published to Kafka and processed asynchronously by the cloudLLM worker.")]
        [ProducesResponseType(typeof(AnalysisSubmitResponse), StatusCodes.Status202Accepted)]
        public async Task<IActionResult> Submit([FromBody] AnalysisSubmitRequest request, CancellationToken cancellationToken)
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

            return AcceptedAtAction(nameof(GetStatus), new { jobId }, new AnalysisSubmitResponse { JobId = jobId });
        }

        [HttpGet("{jobId:guid}")]
        [EndpointSummary("THIS IS JUST A TEMPLATE ENDPOINT FOR DEMONSTRATION. DO NOT USE THIS IS PROD")]
        [EndpointDescription("Polls for the result of a previously submitted analysis job.")]
        [ProducesResponseType(typeof(AnalysisStatusResponse), StatusCodes.Status200OK)]
        public IActionResult GetStatus(Guid jobId)
        {
            if (!resultStore.TryGetResult(jobId, out var result) || result is null)
            {
                return Ok(new AnalysisStatusResponse
                {
                    JobId = jobId,
                    Status = AnalysisJobStatus.Pending
                });
            }

            return Ok(new AnalysisStatusResponse
            {
                JobId = jobId,
                Status = result.Success ? AnalysisJobStatus.Completed : AnalysisJobStatus.Failed,
                Response = result.Response,
                Error = result.Error
            });
        }
    }
}
