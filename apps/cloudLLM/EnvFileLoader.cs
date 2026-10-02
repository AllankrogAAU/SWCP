namespace cloudLLM
{
    public static class EnvFileLoader
    {
        public static void Load(string startDirectory)
        {
            var envFilePath = FindEnvFile(startDirectory);
            if (envFilePath is null)
            {
                return;
            }

            foreach (var rawLine in File.ReadAllLines(envFilePath))
            {
                var line = rawLine.Trim();
                if (line.Length == 0 || line.StartsWith('#'))
                {
                    continue;
                }

                var separatorIndex = line.IndexOf('=');
                if (separatorIndex <= 0)
                {
                    continue;
                }

                var key = line[..separatorIndex].Trim();
                var value = line[(separatorIndex + 1)..].Trim().Trim('"');

                if (Environment.GetEnvironmentVariable(key) is null)
                {
                    Environment.SetEnvironmentVariable(key, value);
                }
            }
        }

        private static string? FindEnvFile(string startDirectory)
        {
            var directory = new DirectoryInfo(startDirectory);

            while (directory is not null)
            {
                var candidate = Path.Combine(directory.FullName, ".env");
                if (File.Exists(candidate))
                {
                    return candidate;
                }

                directory = directory.Parent;
            }

            return null;
        }
    }
}
