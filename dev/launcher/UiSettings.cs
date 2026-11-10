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
# 启动器界面设置（ui.json / logs.json）

这两个文件控制「弱链接」启动器的外观，改完保存、重开启动器生效，**不需要重新编译**。
它们属于用户数据，组件更新不会覆盖。

- `ui.json` —— 主题、点 × 的行为、每个组件行的显示与名字
- `logs.json` —— 日志页左边有哪些来源、每个来源读哪些文件

## ui.json

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

## logs.json

日志页左边那排来源（Core / SnowLuma / 消息接入 / Pi 隧道 / 管理服务）不是写死在程序里的，
整份来源表就是这个文件。加一个来源、改名字、换读哪个文件，都不用改程序。

```json
{
  "sources": [
    {
      "id": "core",
      "title": "Core",
      "row": "core",
      "streams": [
        { "files": ["logs/manager/core.out.log", "logs/core_stdout.log", "logs/app_*.log"],
          "pick": "newest" }
      ]
    }
  ]
}
```

| 字段 | 作用 |
|---|---|
| `id` | 唯一标识，程序内部用它记住「上次看的是哪个」。改 id 等于换一个新的 |
| `title` | 标签上显示的名字，随便写 |
| `row` | 绑到哪个组件行（`core` / `snowluma` / `pi` / `ingress` / `manager`）。那一行被隐藏时，这个来源跟着隐藏。留空 = 一直显示 |
| `streams` | 这个来源由几段日志拼成，按顺序排 |

每条 `stream`：

| 字段 | 作用 |
|---|---|
| `files` | 文件路径，相对数据根（`data\`）。支持 `*` 和 `?`；也可以写绝对路径 |
| `pick` | `"newest"` = 只读最新的那一个；`"all"` = 匹配到的全部按时间从旧到新拼起来 |
| `label` | 只有多段拼在一起时才会显示，标出下面这段来自哪里，默认 `stderr` |

- 轮转出来的旧分片（`xxx.log.1`、`xxx.log.2`）会自动跳过，不用写排除规则。
- 什么都不匹配、或者文件是空的，这一格就是空日志，不会报错。
- 常见用法：自己另起了个程序并让它把输出写进 `data\logs\`，加一条
  `{ "id": "mine", "title": "我的脚本", "row": "", "streams": [{ "files": ["logs/mine.log"] }] }`
  就能在日志页直接看，不用去文件夹里翻。
- 「打开日志目录」打开的是当前来源第一份实际存在的日志所在目录。

## 注意

- 两个文件都用 UTF-8（不带 BOM）保存。
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
            // 只在既有对象上改自己关心的键，未知字段自然保留
            _root["theme"] = Theme;
            _root["closeToTray"] = CloseToTray;
            JsonFile.Write(SettingsPath(), _root);
        }
        catch
        {
            // 落盘失败不影响本次操作
        }
    }
}
