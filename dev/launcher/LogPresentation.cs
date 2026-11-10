using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace AbyssLauncher;

// Bounded file tails and colour parsing are independent of the window and process controller.
// 读哪些文件由 <see cref="LogSources"/> 决定，这里只管「怎么读」和「怎么上色」。
public static class LogPresentation
{
    public record Segment(string Text, string Colour, bool Underline = false, string? Background = null);
    private static readonly Regex Escape = new(@"\x1B\[[0-?]*[ -/]*[@-~]|\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)");
    private static readonly Regex Logger = new(@"^(\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]) ([^|]+)(\|.*)$");
    private static readonly string[] Colours = ["#64748B", "#F87171", "#34D399", "#FBBF24", "#60A5FA", "#C084FC", "#22D3EE", "#CBD5E1"];
    public static string Plain(string text) => Escape.Replace(text, "");

    public static string Tail(string path)
    {
        const int limit = 256 * 1024;
        using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        var start = Math.Max(0, file.Length - limit);
        file.Seek(start, SeekOrigin.Begin);
        var bytes = new byte[(int)Math.Min(limit, file.Length - start)];
        var read = file.Read(bytes, 0, bytes.Length);
        using var memory = new MemoryStream(bytes, 0, read);
        using var reader = new StreamReader(memory, Encoding.UTF8, true);
        var text = reader.ReadToEnd();
        if (start > 0)
        {
            var end = text.IndexOf('\n');
            text = end < 0 ? "" : text[(end + 1)..];
        }
        return string.Join("\n", text.Replace("\r\n", "\n").Split('\n').TakeLast(1000));
    }

    public static IReadOnlyList<Segment> Colourise(string line, string defaultColour = "#CBD5E1")
    {
        if (line.Contains('\x1B'))
        {
            var segments = new List<Segment>();
            var colour = defaultColour;
            var underline = false;
            string? background = null;
            var offset = 0;
            foreach (Match escape in Escape.Matches(line))
            {
                if (escape.Index > offset) segments.Add(new(line[offset..escape.Index], colour, underline, background));
                if (escape.Value.StartsWith("\x1B[") && escape.Value.EndsWith('m'))
                {
                    var codes = escape.Value[2..^1].Split(';').Select(x => int.TryParse(x, out var n) ? n : 0).ToArray();
                    for (int i = 0; i < codes.Length; i++)
                    {
                        var c = codes[i];
                        if (c == 0) { colour = defaultColour; underline = false; background = null; }
                        else if (c == 4) underline = true;
                        else if (c == 24) underline = false;
                        else if (c == 39) colour = defaultColour;
                        else if (c == 49) background = null;
                        else if (c is >= 40 and <= 47) background = Colours[c - 40];
                        else if (c is >= 100 and <= 107) background = Colours[c - 100];
                        else if (c is >= 30 and <= 37) colour = Colours[c - 30];
                        else if (c is >= 90 and <= 97) colour = Colours[c - 90];
                        else if (c == 38 && i + 4 < codes.Length && codes[i + 1] == 2)
                        {
                            colour = $"#{Math.Clamp(codes[i + 2], 0, 255):X2}{Math.Clamp(codes[i + 3], 0, 255):X2}{Math.Clamp(codes[i + 4], 0, 255):X2}";
                            i += 4;
                        }
                        else if (c == 38 && i + 2 < codes.Length && codes[i + 1] == 5)
                        {
                            var index = Math.Clamp(codes[i + 2], 0, 255);
                            if (index < 16) colour = Colours[index % 8];
                            else if (index >= 232) { var grey = 8 + (index - 232) * 10; colour = $"#{grey:X2}{grey:X2}{grey:X2}"; }
                            else { var n = index - 16; int Channel(int v) => v == 0 ? 0 : 55 + v * 40;
                                colour = $"#{Channel(n / 36):X2}{Channel(n / 6 % 6):X2}{Channel(n % 6):X2}"; }
                            i += 2;
                        }
                    }
                }
                offset = escape.Index + escape.Length;
            }
            if (offset < line.Length) segments.Add(new(line[offset..], colour, underline, background));
            return segments;
        }
        var match = Logger.Match(line);
        var level = match.Success ? match.Groups[3].Value : "";
        // 级别配色属于"日志内容"语义，保持固定（DEBUG 青 / WARN 黄 / ERROR 红）；
        // 只有中性前景色跟主题走，由调用方通过 defaultColour 传入。
        var levelColour = level switch { "DEBUG" => "#22D3EE", "WARNING" => "#FBBF24", "ERROR" or "CRITICAL" => "#F87171", _ => defaultColour };
        if (match.Success) return [new(match.Groups[1].Value + " ", "#34D399"),
            new(match.Groups[2].Value + " ", level == "CRITICAL" ? "#FFFFFF" : levelColour, false, level == "CRITICAL" ? "#DC2626" : null),
            new(match.Groups[4].Value, "#F87171", true), new(match.Groups[5].Value, levelColour)];
        return [new(line, line.StartsWith("ERROR:") || line.StartsWith("Traceback") ? "#F87171"
            : line.StartsWith("WARNING:") ? "#FBBF24" : defaultColour)];
    }
}
