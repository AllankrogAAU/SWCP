using cloudLLM.Interfaces;

namespace cloudLLM.Agents
{
    public class AgentFactory(IEnumerable<ILlmAgent> agents)
    {
        public ILlmAgent GetAgent(ErrorCategory category)
        {
            var agent = agents.FirstOrDefault(a => a.Category == category);
            if (agent is null)
            {
                throw new InvalidOperationException($"No agent registered for category '{category}'.");
            }

            return agent;
        }
    }
}
