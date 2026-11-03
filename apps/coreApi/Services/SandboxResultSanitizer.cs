using SWCP.Contracts;

namespace coreApi.Services;

public static class SandboxResultSanitizer
{
    public static SandboxResult Sanitize(SandboxResult result)
    {
        ArgumentNullException.ThrowIfNull(result);
        return result with
        {
            Compilation = result.Compilation with
            {
                Stdout = RemoveNullCharacters(result.Compilation.Stdout),
                Stderr = RemoveNullCharacters(result.Compilation.Stderr)
            },
            StaticAnalysis = result.StaticAnalysis
                .Select(warning => warning with
                {
                    Severity = RemoveNullCharacters(warning.Severity),
                    Message = RemoveNullCharacters(warning.Message)
                })
                .ToArray(),
            TestResults = result.TestResults
                .Select(test => test with
                {
                    Stdout = RemoveNullCharacters(test.Stdout),
                    Stderr = RemoveNullCharacters(test.Stderr)
                })
                .ToArray(),
            ErrorClassification = result.ErrorClassification is null
                ? null
                : RemoveNullCharacters(result.ErrorClassification),
            LintOutput = result.LintOutput is null
                ? null
                : RemoveNullCharacters(result.LintOutput)
        };
    }

    private static string RemoveNullCharacters(string value) =>
        value.Replace("\0", string.Empty, StringComparison.Ordinal);

}
