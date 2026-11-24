using SWCP.Contracts;

namespace coreApi.Services;

/// <summary>
/// Shared routing strategy resolution and deterministic sandbox error classification.
/// </summary>
public static class LocalLlmRouting
{
    public const string None = "none";
    public const string ConfidenceScore = "confidence_score";
    public const string SelfEvaluation = "self_evaluation";
    public const string ErrorType = "error_type";

    public const string DefaultEscalateErrorTypes = "wrong_output,runtime_error";

    public static class ErrorTypes
    {
        public const string CompileError = "compile_error";
        public const string CompileTimeout = "compile_timeout";
        public const string RuntimeCrash = "runtime_crash";
        public const string RuntimeTimeout = "runtime_timeout";
        public const string RuntimeError = "runtime_error";
        public const string WrongOutput = "wrong_output";
        public const string NoError = "no_error";
    }

    public static string GetRoutingMethod(IConfiguration configuration)
    {
        var routingMethod = (configuration["LocalLlm:RoutingMethod"] ?? ConfidenceScore)
            .Trim()
            .ToLowerInvariant();

        if (routingMethod is not (None or ConfidenceScore or SelfEvaluation or ErrorType))
        {
            throw new InvalidOperationException(
                $"LocalLlm:RoutingMethod '{routingMethod}' must be '{None}', '{ConfidenceScore}', '{SelfEvaluation}', or '{ErrorType}'.");
        }

        return routingMethod;
    }

    public static HashSet<string> ParseConfiguredSet(string? raw) =>
        string.IsNullOrWhiteSpace(raw)
            ? []
            : raw.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
                .Select(value => value.ToLowerInvariant())
                .ToHashSet(StringComparer.Ordinal);

    public static string ClassifySandboxError(SandboxResult result)
    {
        var primaryClassification = result.ErrorClassification?
            .Split(';', 2, StringSplitOptions.TrimEntries)[0]
            .Trim()
            .ToLowerInvariant();

        if (!result.Compilation.Success)
        {
            return primaryClassification == ErrorTypes.CompileTimeout
                ? ErrorTypes.CompileTimeout
                : ErrorTypes.CompileError;
        }

        if (result.TestResults.Count == 0 || result.TestResults.All(test => test.Passed))
        {
            return ErrorTypes.NoError;
        }

        var failedTests = result.TestResults.Where(test => !test.Passed).ToArray();
        if (primaryClassification == "run_timeout" || failedTests.Any(test => test.ExitCode == 124))
        {
            return ErrorTypes.RuntimeTimeout;
        }

        if (failedTests.Any(test => test.ExitCode is > 128 or < 0))
        {
            return ErrorTypes.RuntimeCrash;
        }

        if (failedTests.Any(test => test.ExitCode is not null and not 0))
        {
            return ErrorTypes.RuntimeError;
        }

        return ErrorTypes.WrongOutput;
    }

    public static bool ShouldRouteToAzureByErrorType(
        IConfiguration configuration,
        SandboxResult result,
        out string errorType)
    {
        errorType = ClassifySandboxError(result);
        var escalateTypes = ParseConfiguredSet(configuration["LocalLlm:EscalateErrorTypes"] ?? DefaultEscalateErrorTypes);
        return escalateTypes.Contains(errorType);
    }
}
