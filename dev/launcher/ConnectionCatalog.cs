using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace AbyssLauncher;

public sealed record ConnectionEntry
{
    public string Id { get; init; } = "";
    public string Name { get; init; } = "";
    public string Url { get; init; } = "";
    public string ManagedTarget { get; init; } = "";
    [JsonExtensionData] public Dictionary<string, JsonElement>? Extra { get; init; }
}

public sealed record ConnectionDocument
{
    public int Version { get; init; } = 1;
    public List<ConnectionEntry> Connections { get; init; } = new();
    [JsonExtensionData] public Dictionary<string, JsonElement>? Extra { get; init; }
}

/// <summary>Launcher page registrations. These entries do not install or execute adapters.</summary>
public sealed class ConnectionCatalog
{
    private readonly string _path;
    private ConnectionDocument _document;
    private static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        PropertyNameCaseInsensitive = true,
        WriteIndented = true
    };
    public IReadOnlyList<ConnectionEntry> Entries => _document.Connections.AsReadOnly();

    private ConnectionCatalog(string path, ConnectionDocument document)
    {
        _path = path;
        _document = document;
        Validate(document.Connections);
    }

    public static ConnectionCatalog Load(string path) => new(path, File.Exists(path)
        ? JsonSerializer.Deserialize<ConnectionDocument>(File.ReadAllText(path), Json)
            ?? throw new InvalidDataException("连接配置为空")
        : new ConnectionDocument { Connections = new() {
            new() { Id = "snowluma", Name = "SnowLuma", ManagedTarget = "snowluma" }
        }});

    private static void Validate(IEnumerable<ConnectionEntry> entries)
    {
        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var entry in entries)
        {
            if (string.IsNullOrWhiteSpace(entry.Id) || !ids.Add(entry.Id))
                throw new InvalidDataException("连接标识为空或重复");
            if (string.IsNullOrWhiteSpace(entry.Name))
                throw new InvalidDataException("请填写连接名称");
            if (entry.ManagedTarget is not ("" or "snowluma" or "tunnel"))
                throw new InvalidDataException("未知的本机受管接入器");
            if (entry.Url.Length == 0 && entry.ManagedTarget.Length > 0) continue;
            if (!Uri.TryCreate(entry.Url, UriKind.Absolute, out var uri)
                || uri.Scheme is not ("http" or "https") || string.IsNullOrEmpty(uri.Host))
                throw new InvalidDataException("控制页地址应为完整的 http:// 或 https:// 地址");
        }
    }

    private void Commit(List<ConnectionEntry> entries)
    {
        Validate(entries);
        var next = _document with { Connections = entries };
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(_path))!);
        var temporary = _path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(next, Json));
            File.Move(temporary, _path, overwrite: true);
            _document = next;
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }

    public void Save(ConnectionEntry entry)
    {
        entry = entry with { Name = entry.Name.Trim(), Url = entry.Url.Trim() };
        var entries = _document.Connections.ToList();
        var index = entries.FindIndex(value => value.Id == entry.Id);
        if (index < 0) entries.Add(entry); else entries[index] = entry;
        Commit(entries);
    }

    public void Remove(string id) => Commit(_document.Connections.Where(entry => entry.Id != id).ToList());

    public void Move(string id, int delta)
    {
        var entries = _document.Connections.ToList();
        var index = entries.FindIndex(entry => entry.Id == id);
        var destination = index + delta;
        if (index < 0 || destination < 0 || destination >= entries.Count) return;
        (entries[index], entries[destination]) = (entries[destination], entries[index]);
        Commit(entries);
    }
}
