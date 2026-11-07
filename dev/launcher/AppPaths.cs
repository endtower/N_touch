using System.IO;

namespace AbyssLauncher;

/// <summary>
/// 路径与服务地址解析。数据根规则与 Python paths.py 对齐：
/// 新布局将程序、数据、第三方安装、缓存、备份分区；已有旧数据保持原位。
/// ABYSS_DATA_DIR 优先，其次使用程序目录的 abyss-data-dir.txt。
/// </summary>
public static class AppPaths
{
    public static string DataRoot()
    {
        var env = Environment.GetEnvironmentVariable("ABYSS_DATA_DIR");
        if (!string.IsNullOrWhiteSpace(env)) return Path.GetFullPath(env.Trim());

        var appRoot = AppRoot();
        var descriptor = Path.Combine(appRoot, "abyss-data-dir.txt");
        if (!File.Exists(descriptor))
        {
            if (HasLegacyData(appRoot)) return appRoot;
            return Path.Combine(StorageRoot(), "data");
        }
        var configured = File.ReadAllText(descriptor).Trim();
        if (string.IsNullOrWhiteSpace(configured))
            throw new InvalidDataException("abyss-data-dir.txt 未指定数据目录");
        var root = Path.GetFullPath(configured, appRoot);
        if (!Directory.Exists(root))
            throw new DirectoryNotFoundException($"指定的数据目录不存在: {root}");
        return root;
    }

    private static bool HasLegacyData(string root)
    {
        if (new[] { ".env.dev", "memory/memory.db", "memory/chat_history.db", "memory/chat_history/chat_history.db",
            "manager/service_token.txt", "user_settings/system_personas.json" }
            .Any(name => File.Exists(Path.Combine(root, name)))) return true;
        return new[] { "brain/base/persona", "brain/amy/persistence", "adapt/web/data" }
            .Select(name => Path.Combine(root, name))
            .Any(path => Directory.Exists(path) && Directory.EnumerateFiles(path, "*.json").Any());
    }

    public static string StorageRoot()
    {
        var program = new DirectoryInfo(AppRoot());
        var descriptor = Path.Combine(program.FullName, "abyss-storage-dir.txt");
        if (File.Exists(descriptor))
        {
            var configured = File.ReadAllText(descriptor).Trim();
            if (configured.Length == 0) throw new InvalidDataException("布局根不能为空");
            return Path.GetFullPath(configured, program.FullName);
        }
        return program.Name.Equals("app", StringComparison.OrdinalIgnoreCase)
            ? program.Parent!.FullName : program.FullName;
    }

    public static string AppRoot()
    {
        // 打包目录优先，避免放在源码仓库下时误把仓库当成程序目录。
        var exeDir = Path.GetDirectoryName(Environment.ProcessPath ?? AppContext.BaseDirectory)
            ?? AppContext.BaseDirectory;
        if (File.Exists(Path.Combine(exeDir, "components", "manager", "release.json"))
            || File.Exists(Path.Combine(exeDir, "manager.exe"))) return exeDir;
        if (Bootstrap.Available) return Bootstrap.Root;

        var baseDir = AppContext.BaseDirectory;
        var dir = new DirectoryInfo(baseDir);
        for (int i = 0; i < 8 && dir is not null; i++)
        {
            if (File.Exists(Path.Combine(dir.FullName, "manager_service.py")))
                return dir.FullName;
            dir = dir.Parent;
        }
        // 冻结包：manager.exe 与本程序同目录
        return exeDir;
    }

    public static int ServicePort() => int.TryParse(Environment.GetEnvironmentVariable("ABYSS_MANAGER_PORT"), out var port)
        && port is > 0 and <= 65535 ? port : 49990;

    public static string ServiceBase() => $"http://127.0.0.1:{ServicePort()}";

    public static string TokenFile() => Path.Combine(DataRoot(), "manager", "service_token.txt");

    public static string ServiceToken()
    {
        try { return File.ReadAllText(TokenFile()).Trim(); }
        catch { return ""; }
    }

    /// <summary>管理服务可执行文件：冻结包内的 manager.exe；开发环境由调用方退化为 python。</summary>
    public static string? ServiceExe() =>
        File.Exists(Path.Combine(AppRoot(), "manager.exe"))
            ? Path.Combine(AppRoot(), "manager.exe")
            : null;

    public static System.Diagnostics.ProcessStartInfo ManagerStartInfo()
    {
        var component = Path.Combine(AppRoot(), "components", "manager");
        var source = Path.Combine(component, "source", "manager_service.py");
        if (File.Exists(source))
        {
            var start = new System.Diagnostics.ProcessStartInfo(Path.Combine(component, "runtime", "python.exe"));
            start.ArgumentList.Add("-u");
            start.ArgumentList.Add(source);
            start.ArgumentList.Add("--port");
            start.ArgumentList.Add(ServicePort().ToString());
            return start;
        }
        var exe = ServiceExe();
        return exe is not null ? new(exe, $"--port {ServicePort()}")
            : new("python", $"\"{Path.Combine(AppRoot(), "manager_service.py")}\" --port {ServicePort()}");
    }
}
