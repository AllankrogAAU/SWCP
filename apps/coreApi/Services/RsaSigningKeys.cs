using System.Security.Cryptography;

namespace coreApi.Services;

public sealed class RsaSigningKeys : IDisposable
{
    private RsaSigningKeys(RSA rsa)
    {
        Rsa = rsa;
        KeyId = Convert.ToHexString(SHA256.HashData(rsa.ExportSubjectPublicKeyInfo())).ToLowerInvariant();
    }

    public RSA Rsa { get; }
    public string KeyId { get; }

    public static RsaSigningKeys LoadOrCreate(string privateDirectory, string publicDirectory)
    {
        Directory.CreateDirectory(privateDirectory);
        Directory.CreateDirectory(publicDirectory);
        var privatePath = Path.Combine(privateDirectory, "jwt-private.pem");
        var publicPath = Path.Combine(publicDirectory, "jwt-public.pem");
        var rsa = RSA.Create();

        if (File.Exists(privatePath))
        {
            rsa.ImportFromPem(File.ReadAllText(privatePath));
        }
        else
        {
            rsa.KeySize = 3072;
            File.WriteAllText(privatePath, rsa.ExportPkcs8PrivateKeyPem());
            File.WriteAllText(publicPath, rsa.ExportSubjectPublicKeyInfoPem());
        }

        if (!File.Exists(publicPath))
        {
            File.WriteAllText(publicPath, rsa.ExportSubjectPublicKeyInfoPem());
        }

        return new RsaSigningKeys(rsa);
    }

    public void Dispose() => Rsa.Dispose();
}