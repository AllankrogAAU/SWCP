using System.Collections.Concurrent;
using api.Models;

namespace api.Services
{
    /// <summary>
    /// In-memory placeholder for assignment persistence. Replace with a real database-backed
    /// implementation later; the public surface here should remain stable.
    /// </summary>
    public class AssignmentStore
    {
        private readonly ConcurrentDictionary<Guid, Assignment> _assignments = new();

        public Assignment Create(CreateAssignmentRequest request)
        {
            var assignment = new Assignment
            {
                Id = Guid.NewGuid(),
                Title = request.Title,
                AssignmentText = request.AssignmentText,
                Focus = request.Focus,
                CreatedAtUtc = DateTime.UtcNow
            };

            _assignments[assignment.Id] = assignment;
            return assignment;
        }

        public bool TryGet(Guid id, out Assignment? assignment) => _assignments.TryGetValue(id, out assignment);

        public IReadOnlyCollection<Assignment> GetAll() => _assignments.Values.ToList();

        public bool TryUpdate(Guid id, UpdateAssignmentRequest request, out Assignment? assignment)
        {
            if (!_assignments.TryGetValue(id, out var existing))
            {
                assignment = null;
                return false;
            }

            existing.Title = request.Title;
            existing.AssignmentText = request.AssignmentText;
            existing.Focus = request.Focus;
            existing.UpdatedAtUtc = DateTime.UtcNow;

            assignment = existing;
            return true;
        }

        public bool TryDelete(Guid id) => _assignments.TryRemove(id, out _);
    }
}
