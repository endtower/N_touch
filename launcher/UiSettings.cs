using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace AbyssLauncher;

/// <summary>
/// 启动器的界面设置，落在 &lt;数据根&gt;/launcher/ui.json。
///
/// 只认自己关心的键，**写回时未知字段原样保留**（与本项目配置约定一致）。
/// 所以这里是这个文件的唯一读写入口 —— 别再在别处直接写它，否则会互相覆盖。
/// </summary>
public static class UiSettings
{
    /// <summary>点标题栏 × 时：true = 隐藏到托盘、后台继续跑；false = 退出整个程序。</summary>
    public static bool CloseToTray { get; set; } = true;

    /// <summary>主题名（<see cref="ThemeManager.Dark"/> / <see cref="ThemeManager.Light"/>）。</summary>
    public static string Theme { get; set; } = ThemeManager.Dark;

    private static JsonObject _root = new();

    private static string SettingsPath() => Path.Combine(AppPaths.DataRoot(), "launcher", "ui.json");

    public static void Load()
    {
        try
        {
            var path = SettingsPath();
            _root = File.Exists(path)
                ? JsonNode.Parse(File.ReadAllText(path)) as JsonObject ?? new JsonObject()
                : new JsonObject();
        }
        catch
        {
            // 配置坏了不该拦住启动，重建即可
            _root = new JsonObject();
        }

        try { CloseToTray = _root["closeToTray"]?.GetValue<bool>() ?? true; }
        catch { CloseToTray = true; }

        try
        {
            var theme = _root["theme"]?.GetValue<string>();
            Theme = theme == ThemeManager.Light ? ThemeManager.Light : ThemeManager.Dark;
        }
        catch { Theme = ThemeManager.Dark; }
    }

    public static void Save()
    {
        try
        {
            var path = SettingsPath();
            var dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);

            // 只在既有对象上改自己关心的键，未知字段自然保留
            _root["theme"] = Theme;
            _root["closeToTray"] = CloseToTray;
            File.WriteAllText(path, _root.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
        }
        catch
        {
            // 落盘失败不影响本次操作
        }
    }
}
