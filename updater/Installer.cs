using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace AbyssUpdater;

public sealed class Job
{
    public string Id { get; set; } = "";
    public string Component { get; set; } = "";
    public string Version { get; set; } = "";
    public string TargetRoot { get; set; } = "";
    public string AppRoot { get; set; } = "";
    public string DataRoot { get; set; } = "";
    public string Payload { get; set; } = "";
    public string Receipt { get; set; } = "";
    public string Result { get; set; } = "";
    public string ManagerUrl { get; set; } = "";
    public int WaitPid { get; set; }
    public Dictionary<string, string> Files { get; set; } = new();
    public Dictionary<string, string> ExpectedFiles { get; set; } = new();
    public bool ProtectLocalChanges { get; set; }
    public JsonElement Release { get; set; }
    public string[] PreviousFiles { get; set; } = Array.Empty<string>();
}

public sealed class Journal
{
    public string State { get; set; } = "prepared";
    public Dictionary<string, bool> Existed { get; set; } = new();
    public bool ReceiptExisted { get; set; }
}

public static class Installer
{
    public static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        WriteIndented = true,
    };

    public static void WriteJson(string path, object value)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var tmp = path + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(value, Json));
        File.Move(tmp, path, true);
    }

    public static T Read<T>(string path) => JsonSerializer.Deserialize<T>(File.ReadAllText(path), Json)
        ?? throw new InvalidDataException("Empty update metadata");

    public static string Hash(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
    }

    public static string SafePath(string root, string relative)
    {
        if (string.IsNullOrWhiteSpace(relative) || relative.Contains('\\') || relative.Contains(':'))
            throw new InvalidDataException("Invalid package path");
        foreach (var part in relative.Split('/'))
            if (part is "" or "." or ".." || part.EndsWith('.') || part.EndsWith(' ') ||
                part.Any(c => c < 32 || "<>\"|?*".Contains(c)) ||
                Regex.IsMatch(part, @"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", RegexOptions.IgnoreCase))
                throw new InvalidDataException("Invalid package path");
        root = Path.GetFullPath(root);
        var path = Path.GetFullPath(Path.Combine(root, relative.Replace('/', Path.DirectorySeparatorChar)));
        if (!path.StartsWith(root.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Package path escapes root");
        for (var cursor = path; cursor is not null; cursor = Path.GetDirectoryName(cursor))
        {
            if ((File.Exists(cursor) || Directory.Exists(cursor)) &&
                (File.GetAttributes(cursor) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("Refusing to update through a link or junction");
            if (string.Equals(cursor, root, StringComparison.OrdinalIgnoreCase)) break;
        }
        return path;
    }

    public static bool Allowed(string component, string name)
    {
        // Keep this boundary in sync with update_policy.py.
        var parts = name.Split('/');
        if (component == "core" && name.StartsWith("components/core/"))
        {
            if (name == "components/core/release.json") return true;
            if (name.StartsWith("components/core/runtime/"))
                return parts.Length > 3 && !parts.Any(x => x is "__pycache__" or ".git");
            const string source = "components/core/source/";
            if (!name.StartsWith(source)) return false;
            var relative = name[source.Length..];
            if (relative.Split('/').Any(x => x is "__pycache__" or ".git" or "node_modules" or "logs" or "data" or "persistence" or "history")) return false;
            return relative.StartsWith("web_frontend/dist/") || relative.EndsWith(".py") || relative.EndsWith(".md")
                || relative is "requirements.txt" or "requirements.lock" or ".env.example";
        }
        if (component is "core" or "manager" or "ingress")
            return name == component + ".exe" || (parts.Length > 1 && parts[0] == component + "_internal");
        if (component == "snowluma")
            return parts.Length > 1 ? parts[0] is "native" or "client" or "webui" :
                name is "index.mjs" or "launcher.bat" or "node.exe" or "package.json" or "package-lock.json" or "release.json" or "LICENSE" or "LICENSE.txt"
                || name.EndsWith(".js") || name.EndsWith(".mjs") || name.EndsWith(".cjs") || name.EndsWith(".md");
        if (component == "launcher")
            return parts.Length > 1 ? new[] { "runtimes", "cs", "de", "es", "fr", "it", "ja", "ko", "pl", "pt-BR", "ru", "tr", "zh-Hans", "zh-Hant" }.Contains(parts[0])
                || name == "updater/AbyssUpdater.exe" : name.EndsWith(".dll") || name is "AbyssLauncher.exe" or "AbyssLauncher.deps.json" or "AbyssLauncher.runtimeconfig.json" or "createdump.exe";
        return false;
    }

    public static void Validate(Job job)
    {
        if (!new[] { "core", "snowluma", "ingress", "manager", "launcher" }.Contains(job.Component) || job.Files.Count == 0)
            throw new InvalidDataException("Unknown or empty component");
        if (job.Component != "snowluma" && Path.GetFullPath(job.TargetRoot) != Path.GetFullPath(job.AppRoot))
            throw new InvalidDataException("Incorrect component destination");
        var receipt = Path.Combine(job.AppRoot, ".versions", job.Component + ".json");
        if (!string.Equals(Path.GetFullPath(job.Receipt), Path.GetFullPath(receipt), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Incorrect receipt destination");
        SafePath(job.AppRoot, ".versions/" + job.Component + ".json");
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var (name, digest) in job.Files)
        {
            var source = SafePath(job.Payload, name);
            SafePath(job.TargetRoot, name);
            if (!Allowed(job.Component, name) || !seen.Add(name) || !Regex.IsMatch(digest, "^[a-fA-F0-9]{64}$") ||
                !string.Equals(Hash(source), digest, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Staged package failed verification: " + name);
        }
        foreach (var name in job.PreviousFiles)
            if (!Allowed(job.Component, name)) throw new InvalidDataException("Invalid owned file: " + name);
            else SafePath(job.TargetRoot, name);
        foreach (var (name, digest) in job.ExpectedFiles)
        {
            var installed = SafePath(job.TargetRoot, name);
            if (!Allowed(job.Component, name) || !File.Exists(installed) || Hash(installed) != digest)
                throw new InvalidDataException("Program changed after confirmation; preserving local changes: " + name);
        }
        if (job.ProtectLocalChanges)
        {
            if (File.Exists(Path.Combine(job.TargetRoot, "core.exe")) && !job.ExpectedFiles.ContainsKey("core.exe"))
                throw new InvalidDataException("Core EXE appeared after confirmation");
            foreach (var directory in new[] { "core_internal", "components/core" })
            {
                var installed = Path.Combine(job.TargetRoot, directory);
                if (!Directory.Exists(installed)) continue;
                foreach (var file in Directory.EnumerateFiles(installed, "*", SearchOption.AllDirectories))
                {
                    var name = Path.GetRelativePath(job.TargetRoot, file).Replace('\\', '/');
                    if (Allowed(job.Component, name) && !job.ExpectedFiles.ContainsKey(name))
                        throw new InvalidDataException("New local program file appeared after confirmation: " + name);
                }
            }
        }
    }

    private static void CopyAtomic(string source, string target)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        var temp = target + ".abyss-update-tmp";
        try
        {
            // Windows 扫描器可能短暂占用新 EXE；只在有限次数内重试，失败仍走原事务回滚。
            for (var attempt = 0; ; attempt++)
            {
                try { File.Copy(source, temp, true); File.Move(temp, target, true); break; }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException)
                {
                    if (attempt >= 4) throw new IOException($"Cannot replace '{target}': {error.Message}", error);
                    Thread.Sleep(250);
                }
            }
        }
        finally { if (File.Exists(temp)) File.Delete(temp); }
    }

    public static void Apply(Job job, string directory, Action<string>? afterCopy = null)
    {
        Validate(job); // Verify every staged file before touching the installed version.
        var journal = new Journal();
        var backup = Path.Combine(directory, "backup");
        foreach (var name in job.PreviousFiles.Concat(job.Files.Keys).Distinct(StringComparer.OrdinalIgnoreCase))
        {
            var target = SafePath(job.TargetRoot, name);
            journal.Existed[name] = File.Exists(target);
            if (journal.Existed[name]) CopyAtomic(target, SafePath(backup, name));
        }
        journal.ReceiptExisted = File.Exists(job.Receipt);
        if (journal.ReceiptExisted) File.Copy(job.Receipt, Path.Combine(directory, "previous-receipt.json"), true);
        journal.State = "applying";
        WriteJson(Path.Combine(directory, "journal.json"), journal);
        foreach (var name in job.Files.Keys)
        {
            CopyAtomic(SafePath(job.Payload, name), SafePath(job.TargetRoot, name));
            afterCopy?.Invoke(name);
        }
        foreach (var name in job.PreviousFiles.Except(job.Files.Keys, StringComparer.OrdinalIgnoreCase))
            if (File.Exists(SafePath(job.TargetRoot, name))) File.Delete(SafePath(job.TargetRoot, name));
        WriteJson(job.Receipt, new { job.Version, job.Files, job.TargetRoot, Job = job.Id,
            Release = job.Release.ValueKind == JsonValueKind.Undefined ? (object?)null : job.Release });
        journal.State = "installed";
        WriteJson(Path.Combine(directory, "journal.json"), journal);
    }

    public static void Restore(Job job, string directory)
    {
        var path = Path.Combine(directory, "journal.json");
        if (!File.Exists(path)) return; // No journal means no installed file was changed.
        var journal = Read<Journal>(path);
        foreach (var (name, existed) in journal.Existed)
        {
            if (!Allowed(job.Component, name)) throw new InvalidDataException("Invalid recovery path");
            var target = SafePath(job.TargetRoot, name);
            if (existed)
            {
                var saved = SafePath(Path.Combine(directory, "backup"), name);
                // A loaded DLL may have blocked installation without ever changing.
                // Leave unchanged files alone so that recovery doesn't hit the same lock.
                if (!File.Exists(target) || Hash(target) != Hash(saved)) CopyAtomic(saved, target);
            }
            else if (File.Exists(target)) File.Delete(target);
            if (File.Exists(target + ".abyss-update-tmp")) File.Delete(target + ".abyss-update-tmp");
        }
        SafePath(job.AppRoot, ".versions/" + job.Component + ".json");
        if (journal.ReceiptExisted) CopyAtomic(Path.Combine(directory, "previous-receipt.json"), job.Receipt);
        else if (File.Exists(job.Receipt)) File.Delete(job.Receipt);
        journal.State = "rolled_back";
        WriteJson(path, journal);
    }
}
