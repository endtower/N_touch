using System.IO;
using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;

namespace AbyssLauncher;

/// <summary>普通用户只下载启动器 EXE；首次运行把随包管理服务释放到用户程序目录。</summary>
public static class Bootstrap
{
    public static bool Available => Assembly.GetExecutingAssembly().GetManifestResourceInfo("Abyss.Bootstrap.zip") is not null;
    public static string Root => Path.GetFullPath(Environment.GetEnvironmentVariable("ABYSS_INSTALL_DIR")
        ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Abyss", "app"));
    public static string Data => Path.Combine(Path.GetDirectoryName(Root)!, "data");

    // 下载目录中的入口始终转到已安装版本，组件更新后再次双击旧入口也会使用新版。
    public static bool LaunchInstalled(string[] arguments)
    {
        if (!Available) return false;
        var current = Environment.ProcessPath ?? throw new IOException("无法确定启动器位置");
        var beside = Path.GetDirectoryName(current)!;
        if (File.Exists(Path.Combine(beside, "components", "manager", "release.json"))
            || File.Exists(Path.Combine(beside, "manager.exe"))) return false;
        var installed = Path.Combine(Root, "AbyssLauncher.exe");
        if (!File.Exists(installed))
        {
            var temporary = installed + ".install-tmp";
            File.Copy(current, temporary, true);
            File.Move(temporary, installed, false);
        }
        var start = new ProcessStartInfo(installed) { UseShellExecute = false, WorkingDirectory = Root };
        foreach (var argument in arguments) start.ArgumentList.Add(argument);
        Process.Start(start);
        return true;
    }

    public static void EnsureInstalled()
    {
        var besideLauncher = Path.GetDirectoryName(Environment.ProcessPath ?? "") ?? "";
        if (File.Exists(Path.Combine(besideLauncher, "components", "manager", "release.json"))
            || File.Exists(Path.Combine(besideLauncher, "manager.exe"))) return;
        if (!Available || File.Exists(Path.Combine(Root, "components", "manager", "release.json"))) return;
        var parent = Path.GetDirectoryName(Root)!;
        Directory.CreateDirectory(parent);
        var staging = Path.Combine(parent, "bootstrap-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(staging);
        try
        {
            using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("Abyss.Bootstrap.zip")!;
            using var archive = new ZipArchive(stream);
            var manifestEntry = archive.GetEntry("bootstrap.manifest.json") ?? throw new InvalidDataException("启动器缺少安装校验清单");
            using var manifestStream = manifestEntry.Open();
            var manifest = JsonSerializer.Deserialize<Dictionary<string, string>>(manifestStream) ?? throw new InvalidDataException("安装校验清单无效");
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var entry in archive.Entries)
            {
                var name = entry.FullName;
                if (name == "bootstrap.manifest.json") continue;
                if (name.Contains('\\') || name.Contains(':') || name.Split('/').Any(p => p is "" or "." or ".." || p.EndsWith('.') || p.EndsWith(' '))
                    || !seen.Add(name) || !manifest.TryGetValue(name, out var expected)
                    || !(AbyssUpdater.Installer.Allowed("manager", name) || name is "component_versions.json" or "update_defaults.json"))
                    throw new InvalidDataException("启动器安装包包含未知路径");
                var target = Path.GetFullPath(Path.Combine(staging, name));
                if (!target.StartsWith(staging + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("安装路径越界");
                Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                using (var source = entry.Open()) using (var output = File.Create(target)) source.CopyTo(output);
                using var check = File.OpenRead(target);
                if (!Convert.ToHexString(SHA256.HashData(check)).Equals(expected, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("启动器安装文件校验失败");
            }
            if (seen.Count != manifest.Count || !seen.Contains("components/manager/source/manager_service.py")
                || !seen.Contains("components/manager/runtime/python.exe")) throw new InvalidDataException("启动器安装包不完整");
            File.WriteAllText(Path.Combine(staging, "abyss-data-dir.txt"), "../data");
            File.WriteAllText(Path.Combine(staging, "abyss-storage-dir.txt"), "..");
            using var versions = JsonDocument.Parse(File.ReadAllText(Path.Combine(staging, "component_versions.json")));
            AbyssUpdater.Installer.WriteJson(Path.Combine(staging, ".versions", "manager.json"), new {
                Version = versions.RootElement.GetProperty("manager").GetString(),
                Files = manifest.Where(entry => AbyssUpdater.Installer.Allowed("manager", entry.Key)).ToDictionary(entry => entry.Key, entry => entry.Value),
                TargetRoot = Root, BaselineKind = "official-bootstrap"
            });
            if (Directory.Exists(Root))
            {
                if (Directory.EnumerateFileSystemEntries(Root).Any()) throw new IOException("程序目录已有未完成的安装，请先保留现有文件再修复");
                Directory.Delete(Root, false);
            }
            Directory.Move(staging, Root);
            Directory.CreateDirectory(Data);
        }
        finally { if (Directory.Exists(staging)) Directory.Delete(staging, true); }
    }
}
