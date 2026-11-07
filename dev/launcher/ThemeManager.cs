using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Windows;

namespace AbyssLauncher;

/// <summary>
/// 主题（暗 / 亮）切换与落盘。
///
/// 颜色令牌本体在 Theme/Dark.xaml 与 Theme/Light.xaml（由 design/build_tokens.py
/// 从 design/tokens.json 生成）。这里只负责把合并字典里的"那一本颜色"换掉，
/// 并广播给订阅者（主窗口据此把主题推给内嵌网页）。
///
/// 之所以能运行时切换，是因为所有控件样式都引用 DynamicResource；
/// 用 StaticResource 的话换字典不会重绘。
/// </summary>
public static class ThemeManager
{
    public const string Dark = "dark";
    public const string Light = "light";

    public static string Current { get; private set; } = Dark;

    /// <summary>主题变化事件，参数为新主题名（"dark" / "light"）。</summary>
    public static event Action<string>? ThemeChanged;

    public static void Initialize()
    {
        UiSettings.Load();
        Apply(UiSettings.Theme, persist: false);
    }

    public static string Toggle() => Apply(Current == Dark ? Light : Dark, persist: true);

    /// <summary>
    /// 主题名 → 资源文件名。
    /// 注意大小写：BAML 是编译进程序集的资源，名字按原文件名生成，
    /// 用 "Theme/dark.xaml" 去取 "Theme/Dark.xaml" 在 Windows 上也不保证成功。
    /// </summary>
    private static string FileNameFor(string theme) => theme == Light ? "Light" : "Dark";

    public static string Apply(string theme, bool persist = true)
    {
        theme = theme == Light ? Light : Dark;

        var merged = Application.Current?.Resources.MergedDictionaries;
        if (merged is not null)
        {
            var wanted = new ResourceDictionary { Source = new Uri($"Theme/{FileNameFor(theme)}.xaml", UriKind.Relative) };

            // 找到当前挂着的那本颜色字典，原地替换；找不到就补一本
            int index = -1;
            for (int i = 0; i < merged.Count; i++)
            {
                var src = merged[i].Source?.OriginalString ?? "";
                if (src.EndsWith("Dark.xaml", StringComparison.OrdinalIgnoreCase)
                    || src.EndsWith("Light.xaml", StringComparison.OrdinalIgnoreCase))
                {
                    index = i;
                    break;
                }
            }
            if (index >= 0) merged[index] = wanted;
            else merged.Add(wanted);
        }

        Current = theme;
        if (persist)
        {
            // 落盘统一走 UiSettings（ui.json 的唯一读写入口），避免两处互相覆盖
            UiSettings.Theme = theme;
            UiSettings.Save();
        }
        ThemeChanged?.Invoke(theme);
        return theme;
    }
}
