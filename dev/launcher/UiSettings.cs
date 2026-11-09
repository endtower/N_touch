using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace AbyssLauncher;

/// <summary>
/// 启动器的界面设置，落在 &lt;数据根&gt;/launcher/ui.json。
///
/// 只认自己关心的键，**写回时未知字段原样保留**（与本项目配置约定一致）。
/// 所以这里是这个文件的唯一读写入口 —— 别再在别处直接写它，否则会互相覆盖。
///
/// 这个文件是**给用户和用户的 Agent 改的**：想让某个组件行不显示、换个叫法，
/// 改这里即可，不需要重新编译 EXE。
/// </summary>
public static class UiSettings
{
    /// <summary>点标题栏 × 时：true = 隐藏到托盘、后台继续跑；false = 退出整个程序。</summary>
    public static bool CloseToTray { get; set; } = true;

    /// <summary>主题名（<see cref="ThemeManager.Dark"/> / <see cref="ThemeManager.Light"/>）。</summary>
    public static string Theme { get; set; } = ThemeManager.Dark;

    /// <summary>某个组件行的外观覆盖。键是组件 id（core / snowluma / pi / ingress / manager / launcher）。</summary>
    public sealed class RowConfig
    {
        /// <summary>false = 这一行连同分隔线一起隐藏。默认 true。</summary>
        public bool Visible = true;

        /// <summary>改这一行的标题文字。空 = 用内置名字。</summary>
        public string Title = "";
    }

    private static readonly Dictionary<string, RowConfig> _rows = new(StringComparer.OrdinalIgnoreCase);

    private static JsonObject _root = new();

    private static string SettingsPath() => Path.Combine(AppPaths.DataRoot(), "launcher", "ui.json");

    public static void Load()
    {
        _rows.Clear();
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

        // rows: { "snowluma": { "visible": false, "title": "QQ 接入" } }
        try
        {
            if (_root["rows"] is JsonObject rows)
            {
                foreach (var pair in rows)
                {
                    if (pair.Value is not JsonObject entry) continue;
                    var config = new RowConfig();
                    try { config.Visible = entry["visible"]?.GetValue<bool>() ?? true; }
                    catch { config.Visible = true; }
                    try { config.Title = entry["title"]?.GetValue<string>() ?? ""; }
                    catch { config.Title = ""; }
                    _rows[pair.Key] = config;
                }
            }
        }
        catch
        {
            // 单项写坏了就整体按默认走，不影响其它设置
            _rows.Clear();
        }

        SeedReadme();
    }

    /// <summary>
    /// 在 ui.json 旁边放一份说明，让用户（或他请的 Agent）知道这个文件能改什么。
    /// 只在说明文件不存在时写一次，用户改了不会被覆盖。
    /// </summary>
    private static void SeedReadme()
    {
        try
        {
            var dir = Path.GetDirectoryName(SettingsPath());
            if (string.IsNullOrEmpty(dir)) return;
            Directory.CreateDirectory(dir);
            var readme = Path.Combine(dir, "README.md");
            if (File.Exists(readme)) return;
            File.WriteAllText(readme, """
# 启动器界面设置（ui.json）

这个文件控制「弱链接」启动器的外观，改完保存、重开启动器生效，**不需要重新编译**。
它属于用户数据，组件更新不会覆盖。

## 能改什么

```json
{
  "theme": "dark",
  "closeToTray": false,
  "rows": {
    "snowluma": { "visible": false },
    "core": { "title": "大脑" }
  }
}
```

| 键 | 作用 |
|---|---|
| `theme` | `"dark"` 或 `"light"` |
| `closeToTray` | `true` = 点 × 收进托盘继续跑；`false` = 停止全部并退出 |
| `rows` | 按组件单独设置这一行 |

`rows` 里每个组件的 id 是 `core` / `snowluma` / `pi` / `ingress` / `manager` / `launcher`：

| 字段 | 作用 |
|---|---|
| `visible` | `false` 隐藏这一行（连同分隔线与对应日志来源）。默认 `true` |
| `title` | 换掉这一行的名字；不填用内置名字 |

## 隐藏一行会怎样

- 不看这一行，也不把它算进页头的「正在运行」和右上角状态。
- 它的启动链路会被自动取消「参与一键启动」，点「一键启动」不会再去拉它，
  它失败也不会再在页头喊「需要处理」。
- 想恢复：把 `visible` 改回 `true` 或删掉那一项，重开启动器，需要的话再勾上「参与一键启动」。

## 注意

- 文件用 UTF-8（不带 BOM）保存。
- JSON 写坏不会拦着启动，程序按默认值跑；改不动就删掉这个文件，重开启动器会重新生成。
- 只认上面这几个键，其它键原样保留。
""");
        }
        catch
        {
            // 说明文件写不出来不影响程序运行
        }
    }

    /// <summary>取某个组件行的覆盖配置；没配过就是默认值（显示、用内置标题）。</summary>
    public static RowConfig Row(string id) =>
        _rows.TryGetValue(id, out var config) ? config : new RowConfig();

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
            File.WriteAllText(path, _root.ToJsonString(new JsonSerializerOptions
            {
                WriteIndented = true,
                // 中文标题不该被转义成 \uXXXX，用户要直接读得懂
                Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
            }));
        }
        catch
        {
            // 落盘失败不影响本次操作
        }
    }
}
