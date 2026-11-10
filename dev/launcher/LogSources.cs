using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace AbyssLauncher;

/// <summary>
/// 日志来源定义，落在 &lt;数据根&gt;/launcher/logs.json。
///
/// 内置默认值写在 <see cref="Defaults"/> 里，用户文件存在就整个覆盖它。所以：
/// - 想让日志页多一个来源（比如自己另起的程序），改这个文件即可，不用重新编译 EXE；
/// - 删掉文件，下次启动会重新落一份内置默认。
///
/// 每个来源 = 一组「流」，一个流 = 一组文件模式 + 取法。
/// 取法两种：newest 取最新匹配的文件，all 把匹配到的全部按时间顺序拼起来。
/// 这样「Core 的 stdout 会换落点、要取最新的那个」和「管理服务 stdout/stderr 两份都要」
/// 这两种完全不同的情况用同一套描述就能表达。
/// </summary>
public static class LogSources
{
    /// <summary>一条日志流：一组文件模式，以及从里面取哪几个。</summary>
    public sealed class Stream
    {
        /// <summary>相对数据根的路径模式，支持 * 和 ?。也可写绝对路径。轮转分片（xxx.log.1）自动忽略。</summary>
        public List<string> Files = [];

        /// <summary>newest = 取最新的一个；all = 全部按时间顺序拼起来。</summary>
        public string Pick = "newest";

        /// <summary>拼起来时在它上面标一句 [label]。默认 stderr。</summary>
        public string Label = "stderr";
    }

    /// <summary>日志页左边的一个来源。</summary>
    public sealed class Source
    {
        public string Id = "";
        public string Title = "";

        /// <summary>绑到哪个组件行：那行被隐藏时，这个来源也一起隐藏。空 = 不绑定、始终显示。</summary>
        public string Row = "";

        public List<Stream> Streams = [];
    }

    public record Snapshot(string Text, string Files);

    private static List<Source> _sources = [];

    /// <summary>当前生效的来源列表。第一次访问前会先 Load。</summary>
    public static IReadOnlyList<Source> Current
    {
        get
        {
            if (_sources.Count == 0) Load();
            return _sources;
        }
    }

    private static string SettingsPath() => Path.Combine(AppPaths.DataRoot(), "launcher", "logs.json");

    public static Source? Find(string id) =>
        Current.FirstOrDefault(source => string.Equals(source.Id, id, StringComparison.OrdinalIgnoreCase));

    public static void Load()
    {
        var path = SettingsPath();
        if (!File.Exists(path))
        {
            // 第一次运行：把内置默认落成文件，用户和他的 Agent 才有东西可改
            _sources = Defaults();
            Save();
            return;
        }

        try
        {
            var root = JsonNode.Parse(File.ReadAllText(path)) as JsonObject;
            var parsed = new List<Source>();
            if (root?["sources"] is JsonArray entries)
            {
                foreach (var entry in entries)
                {
                    if (entry is not JsonObject item) continue;
                    var source = new Source
                    {
                        Id = Text(item, "id"),
                        Title = Text(item, "title"),
                        Row = Text(item, "row"),
                    };
                    if (source.Id.Length == 0) source.Id = source.Title;
                    if (source.Id.Length == 0) continue;
                    if (source.Title.Length == 0) source.Title = source.Id;

                    if (item["streams"] is JsonArray streams)
                    {
                        foreach (var streamEntry in streams)
                        {
                            if (streamEntry is not JsonObject streamItem) continue;
                            var stream = new Stream
                            {
                                Pick = Text(streamItem, "pick") == "all" ? "all" : "newest",
                                Label = Text(streamItem, "label"),
                            };
                            if (stream.Label.Length == 0) stream.Label = "stderr";
                            if (streamItem["files"] is JsonArray files)
                                foreach (var file in files)
                                {
                                    var pattern = file?.GetValue<string>() ?? "";
                                    if (pattern.Length > 0) stream.Files.Add(pattern);
                                }
                            if (stream.Files.Count > 0) source.Streams.Add(stream);
                        }
                    }
                    if (source.Streams.Count > 0) parsed.Add(source);
                }
            }
            // 一条都没解析出来，说明文件写坏了 —— 按默认跑，并且**不覆盖**用户文件，
            // 这样他还能把改坏的地方修回来
            _sources = parsed.Count > 0 ? parsed : Defaults();
        }
        catch
        {
            _sources = Defaults();
        }
    }

    private static string Text(JsonObject node, string key)
    {
        try { return node[key]?.GetValue<string>() ?? ""; }
        catch { return ""; }
    }

    /// <summary>
    /// 内置来源。顺序就是日志页里标签的顺序。
    /// 改这里等于改「全新安装时默认有什么」，已有用户的 logs.json 不受影响。
    /// </summary>
    private static List<Source> Defaults() =>
    [
        new()
        {
            Id = "core", Title = "Core", Row = "core",
            Streams =
            [
                // Core 的 stdout 落点随启动方式变化：manager 托管时写 manager/core.out.log，
                // 冻结版 core.exe 写 logs/core_stdout.log，root logger 写 logs/app_*.log。
                // 只认死其中一个，进程切换落点后会长期显示旧文件，所以取最新的那个。
                new() { Pick = "newest", Files = ["logs/manager/core.out.log", "logs/core_stdout.log", "logs/app_*.log"] },
            ],
        },
        new()
        {
            Id = "snowluma", Title = "SnowLuma", Row = "snowluma",
            Streams = [new() { Pick = "newest", Files = ["logs/manager/snowluma.out.log"] }],
        },
        new()
        {
            Id = "ingress", Title = "消息接入", Row = "ingress",
            Streams = [new() { Pick = "newest", Files = ["logs/manager/ingress.out.log"] }],
        },
        // Pi 隧道：此前界面上有这个标签，但读取端没有对应分支，点了必然抛异常。
        new()
        {
            Id = "tunnel", Title = "Pi 隧道", Row = "pi",
            Streams = [new() { Pick = "newest", Files = ["logs/manager/tunnel.out.log"] }],
        },
        new()
        {
            Id = "service", Title = "管理服务", Row = "manager",
            // stdout 与 stderr 是同一个进程的两份，各自取最新的那份再拼起来
            Streams =
            [
                new() { Pick = "newest", Files = ["logs/manager/service*.out.log"] },
                new() { Pick = "newest", Files = ["logs/manager/service*.err.log"], Label = "stderr" },
            ],
        },
    ];

    public static void Save()
    {
        try
        {
            var path = SettingsPath();
            var dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);

            var entries = new JsonArray();
            foreach (var source in _sources)
            {
                var streams = new JsonArray();
                foreach (var stream in source.Streams)
                {
                    var files = new JsonArray();
                    foreach (var pattern in stream.Files) files.Add(pattern);
                    streams.Add(new JsonObject
                    {
                        ["files"] = files,
                        ["pick"] = stream.Pick,
                        ["label"] = stream.Label,
                    });
                }
                entries.Add(new JsonObject
                {
                    ["id"] = source.Id,
                    ["title"] = source.Title,
                    ["row"] = source.Row,
                    ["streams"] = streams,
                });
            }

            var root = new JsonObject
            {
                ["_readme"] = new JsonArray(
                    "日志页的来源定义。改完保存、重开启动器生效；删掉这个文件会重新生成默认内容。",
                    "详见同目录 README.md。",
                    "",
                    "每个来源：id（唯一）、title（标签上显示的名字）、row（绑哪个组件行，那行隐藏时它也跟着隐藏，留空=始终显示）、streams。",
                    "每条 stream：files（相对数据根的路径模式，支持 * 和 ?）、pick（newest 取最新一份 / all 全部按时间拼起来）、label（拼起来时显示的标记）。",
                    "轮转分片（xxx.log.1）自动忽略。"),
                ["sources"] = entries,
            };

            JsonFile.Write(path, root);
        }
        catch (Exception error)
        {
            // 落盘失败不影响已经加载的定义；但要把原因留在程序目录里，
            // 否则"文件没生成"会变成查不出来的静默失败
            try { File.WriteAllText(Path.Combine(Path.GetDirectoryName(Environment.ProcessPath) ?? ".", "logs-json-error.txt"), error.ToString()); } catch { }
        }
    }

    /// <summary>读出某个来源的日志正文。来源不存在或没有文件时返回空快照，不抛异常。</summary>
    public static Snapshot Read(string dataRoot, string sourceId)
    {
        var source = Find(sourceId);
        if (source is null) return new("", "");

        var text = new StringBuilder();
        var names = new List<string>();
        foreach (var stream in source.Streams)
        {
            foreach (var file in Resolve(dataRoot, stream))
            {
                var tail = LogPresentation.Tail(file);
                if (tail.Length == 0) continue;
                if (text.Length > 0) text.AppendLine().AppendLine("[" + stream.Label + "]");
                text.Append(tail);
                names.Add(Path.GetFileName(file));
            }
        }
        return new(text.ToString(), string.Join(" · ", names));
    }

    /// <summary>把一个流解析成具体文件列表。</summary>
    private static IEnumerable<string> Resolve(string dataRoot, Stream stream)
    {
        var matches = new List<string>();
        foreach (var pattern in stream.Files)
        {
            var full = Path.IsPathRooted(pattern)
                ? pattern
                : Path.Combine(dataRoot, pattern.Replace('/', Path.DirectorySeparatorChar));
            var dir = Path.GetDirectoryName(full);
            var name = Path.GetFileName(full);
            if (string.IsNullOrEmpty(dir) || string.IsNullOrEmpty(name) || !Directory.Exists(dir)) continue;
            try
            {
                matches.AddRange(Directory.GetFiles(dir, name)
                    // 排除轮转分片：Windows 的 *.log 通配会连带命中 app_x.log.1，那是同一份日志的旧片段
                    .Where(file => !Regex.IsMatch(Path.GetFileName(file), @"\.\d+$")));
            }
            catch
            {
                // 单个目录读不了不影响其它流
            }
        }

        var distinct = matches.Distinct().Where(File.Exists).ToList();
        if (stream.Pick == "all")
            return distinct.OrderBy(File.GetLastWriteTimeUtc).ToList();
        var newest = distinct.OrderByDescending(File.GetLastWriteTimeUtc).FirstOrDefault();
        return newest is null ? [] : [newest];
    }

    /// <summary>某个来源第一条实际存在的日志所在目录，给「打开日志目录」用；找不到就退回 logs。</summary>
    public static string FolderFor(string dataRoot, string sourceId)
    {
        var source = Find(sourceId);
        if (source is not null)
            foreach (var stream in source.Streams)
                foreach (var file in Resolve(dataRoot, stream))
                    if (Path.GetDirectoryName(file) is { Length: > 0 } folder) return folder;
        return Path.Combine(dataRoot, "logs");
    }
}
