namespace api.Services;

public sealed class PromptBuilder
{
    public string BuildCodeAnalysisPrompt(string code, string instruction, string language)
    {
        return $"{instruction}\n\n" +
            $"<source_code language=\"{language}\">\n" +
            "```\n" +
            $"{code}\n" +
            "```\n" +
            "</source_code>";
    }
}