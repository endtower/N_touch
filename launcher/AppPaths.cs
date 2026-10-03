using System.IO;

namespace AbyssLauncher;

/// <summary>
/// 路径与服务地址解析。数据根规则与 Python paths.py 对齐：
/// 便携包 = exe 目录；源码开发 = 向上找 manager_service.py 所在的仓库根。
/// ABYSS_DATA_DIR 环境变量可显式覆盖。
/// </summary>
public static class AppPaths
{
    public static string DataRoot()
    {
        var env = Environment.GetEnvironmentVariable("ABYSS_DATA_DIR");
        if (!string.IsNullOrWhiteSpace(env)) return env;

        var baseDir = AppContext.BaseDirectory;
        var dir = new DirectoryInfo(baseDir);
        for (int i = 0; i < 8 && dir is not null; i++)
        {
            if (File.Exists(Path.Combine(dir.FullName, "manager_service.py")))
                return dir.FullName;
            dir = dir.Parent;
        }
        // 冻结包：manager.exe 与本程序同目录
        var exeDir = Path.GetDirectoryName(Environment.ProcessPath ?? baseDir);
        return exeDir ?? baseDir;
    }

    public static string ServiceBase() => "http://127.0.0.1:49990";

    public static string TokenFile() => Path.Combine(DataRoot(), "manager", "service_token.txt");

    public static string ServiceToken()
    {
        try { return File.ReadAllText(TokenFile()).Trim(); }
        catch { return ""; }
    }

    /// <summary>管理服务可执行文件：冻结包内的 manager.exe；开发环境由调用方退化为 python。</summary>
    public static string? ServiceExe() =>
        File.Exists(Path.Combine(DataRoot(), "manager.exe"))
            ? Path.Combine(DataRoot(), "manager.exe")
            : null;
}
