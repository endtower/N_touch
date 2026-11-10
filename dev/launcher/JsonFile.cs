using System.IO;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace AbyssLauncher;

/// <summary>
/// 把 JsonNode 写成人类可读的 UTF-8 文件（两格缩进、中文不转义、不带 BOM）。
///
/// 为什么要单独一个入口：<c>node.ToJsonString(new JsonSerializerOptions { WriteIndented = true })</c>
/// 在 .NET 8 里会抛
/// <c>JsonSerializerOptions instance must specify a TypeInfoResolver setting before being marked as read-only</c>
/// —— 节点里一旦有嵌套的 JsonArray/JsonObject 就会踩到（扁平对象侥幸不炸，所以很容易漏测）。
/// 用 Utf8JsonWriter 自己写则完全不经过那套反射，也不会有这个坑。
///
/// 界面配置（ui.json / logs.json）统一走这里，别再各写各的。
/// </summary>
public static class JsonFile
{
    public static void Write(string path, JsonNode root)
    {
        var dir = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(dir)) Directory.CreateDirectory(dir);

        using var buffer = new MemoryStream();
        using (var writer = new Utf8JsonWriter(buffer, new JsonWriterOptions
        {
            Indented = true,
            // 中文标题/说明不该被转义成 \uXXXX，用户和他的 Agent 要直接读得懂
            Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        }))
        {
            root.WriteTo(writer);
        }
        File.WriteAllBytes(path, buffer.ToArray());
    }
}
