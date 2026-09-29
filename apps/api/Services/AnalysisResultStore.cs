using System.Collections.Concurrent;
using api.Models;

namespace api.Services
{
    public class AnalysisResultStore
    {
        private readonly ConcurrentDictionary<Guid, AnalysisResultMessage> _results = new();

        public void SetResult(AnalysisResultMessage result)
        {
            _results[result.JobId] = result;
        }

        public bool TryGetResult(Guid jobId, out AnalysisResultMessage? result)
        {
            return _results.TryGetValue(jobId, out result);
        }
    }
}
