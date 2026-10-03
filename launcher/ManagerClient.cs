using System.Net.Http;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;

namespace AbyssLauncher;

/// <summary>管理服务 /v1 接口客户端（GUI 适配模块：不发控制逻辑，只发请求转提示）。</summary>
public class ManagerClient
{
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(60) };

    private static HttpRequestMessage Request(string method, string path, object? body = null)
    {
        var req = new HttpRequestMessage(new HttpMethod(method), AppPaths.ServiceBase() + path);
        req.Headers.Add("X-Manager-Token", AppPaths.ServiceToken());
        if (body is not null)
            req.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
        return req;
    }

    public async Task<JsonElement> GetAsync(string path)
    {
        using var resp = await Http.SendAsync(Request("GET", path));
        var text = await resp.Content.ReadAsStringAsync();
        using var doc = JsonDocument.Parse(string.IsNullOrWhiteSpace(text) ? "{}" : text);
        return doc.RootElement.Clone();
    }

    public async Task<JsonElement> PostAsync(string path)
    {
        using var resp = await Http.SendAsync(Request("POST", path, new { }));
        var text = await resp.Content.ReadAsStringAsync();
        using var doc = JsonDocument.Parse(string.IsNullOrWhiteSpace(text) ? "{}" : text);
        return doc.RootElement.Clone();
    }

    public async Task<JsonElement> PutAsync(string path, object body)
    {
        using var resp = await Http.SendAsync(Request("PUT", path, body));
        var text = await resp.Content.ReadAsStringAsync();
        using var doc = JsonDocument.Parse(string.IsNullOrWhiteSpace(text) ? "{}" : text);
        return doc.RootElement.Clone();
    }

    public async Task<JsonElement?> TryInfo()
    {
        try { return await GetAsync("/v1/info"); }
        catch { return null; }
    }

    public class Target
    {
        public string Name { get; set; } = "";
        public string State { get; set; } = "";
        public string Desired { get; set; } = "";
        public int? Pid { get; set; }
        public int[] Ports { get; set; } = Array.Empty<int>();
        public bool Ready { get; set; }
        public bool Managed { get; set; }
        public int Restarts { get; set; }
        public string LastError { get; set; } = "";
        public double? UptimeSec { get; set; }
    }

    public static Target ParseTarget(JsonElement el)
    {
        int? pid = el.TryGetProperty("pid", out var p) && p.ValueKind == JsonValueKind.Number ? p.GetInt32() : null;
        double? up = el.TryGetProperty("uptime_sec", out var u) && u.ValueKind == JsonValueKind.Number
            ? u.GetDouble() : null;
        var ports = new List<int>();
        if (el.TryGetProperty("ports", out var pa) && pa.ValueKind == JsonValueKind.Array)
            foreach (var x in pa.EnumerateArray()) ports.Add(x.GetInt32());
        return new Target
        {
            Name = el.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "",
            State = el.TryGetProperty("state", out var s) ? s.GetString() ?? "" : "",
            Desired = el.TryGetProperty("desired", out var d) ? d.GetString() ?? "" : "",
            Pid = pid,
            Ports = ports.ToArray(),
            Ready = el.TryGetProperty("ready", out var r) && r.GetBoolean(),
            Managed = el.TryGetProperty("managed", out var m) && m.GetBoolean(),
            Restarts = el.TryGetProperty("restarts", out var rs) ? rs.GetInt32() : 0,
            LastError = el.TryGetProperty("last_error", out var le) ? le.GetString() ?? "" : "",
            UptimeSec = up,
        };
    }
}
