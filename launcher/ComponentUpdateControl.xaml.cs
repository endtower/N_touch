using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace AbyssLauncher;

/// <summary>
/// 组件行的下半截：行下方那条状态细线（更新状态 + 下载进度），
/// 以及点整行后拉开的更新源行。
/// 版本选择器是常驻在行表头右侧的独立控件（VersionSelectorControl），装配时由主窗口挂进 Selector。
/// </summary>
public partial class ComponentUpdateControl : UserControl
{
    public string Component { get; set; } = "";

    /// <summary>行表头右侧那个常驻的版本选择器。跨 namescope 绑不了 ElementName，只能由主窗口赋进来。</summary>
    public VersionSelectorControl? Selector { get; set; }

    public string SelectedVersion => Selector?.SelectedVersion ?? "";

    /// <summary>该组件的在线更新源地址（留空表示关闭在线检查）。</summary>
    public string Source => SourceBox.Text.Trim();

    public void SetSource(string? url) => SourceBox.Text = url ?? "";

    /// <summary>行里下载图标是否可点、点了走哪条腿、提示语是什么 —— 都由 Refresh 依服务端返回的 row 算出。</summary>
    public bool CanDownload { get; private set; }
    public string DownloadAction { get; private set; } = "download";
    public string DownloadTip { get; private set; } = "下载所选版本，下载完成后另行确认安装";

    public event Action<ComponentUpdateControl, string>? UpdateRequested;

    private JsonElement _row;
    private bool _busy, _active;
    private HashSet<string> _availableVersions = new();

    public ComponentUpdateControl() => InitializeComponent();

    /// <summary>行内展开区（改更新源 / 导入本地包）是否拉开。</summary>
    public bool Expanded
    {
        get => SourceRow.Visibility == Visibility.Visible;
        set => SourceRow.Visibility = value ? Visibility.Visible : Visibility.Collapsed;
    }

    private static string Text(JsonElement item, string name, string fallback = "") =>
        item.ValueKind == JsonValueKind.Object && item.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? fallback : fallback;

    public void Refresh(JsonElement row, bool busy, bool active)
    {
        _row = row.Clone(); _busy = busy; _active = active;
        var versions = row.TryGetProperty("versions", out var list)
            ? list.EnumerateArray().Select(v => Text(v, "version")).ToArray() : Array.Empty<string>();
        if (versions.Length == 0 && Text(row, "latest") is var latest && latest.Length > 0)
            versions = new[] { latest };   // 管理服务降级后仍要能用本地包安装
        _availableVersions = versions.ToHashSet();
        var current = Text(row, "current");
        if (Version.TryParse(current.Split('-', '+')[0], out _) && !versions.Contains(current))
            versions = new[] { current }.Concat(versions).ToArray();

        if (Selector is not null)
        {
            // 列表变了才重铺，免得每 2 秒的轮询把正在输入的下拉打断；重铺时保留已选项
            var keep = Selector.SelectedVersion;
            if (Selector.SetVersions(versions)) Selector.SetSelectedVersion(keep);
            if (Selector.SelectedVersion.Length == 0) Selector.SetSelectedVersion(Preferred(row, versions, current));
        }
        RefreshStatus();
    }

    /// <summary>没选过版本时默认落在"已准备好 / 最新 / 当前"上，都不成立才退到列表头一条。</summary>
    private static string Preferred(JsonElement row, string[] versions, string current)
    {
        var preferred = Text(row, "staged_version");
        if (preferred.Length == 0) preferred = Text(row, "latest");
        if (preferred.Length == 0) preferred = current;
        return preferred.Length > 0 ? preferred : versions.FirstOrDefault() ?? "";
    }

    /// <summary>行里的版本下拉换了选项：重算下载图标的状态与那句状态提示。</summary>
    public void NotifySelectionChanged() => RefreshStatus();

    public void SelectVersion(string version) { Selector?.SetSelectedVersion(version); RefreshStatus(); }

    private void RefreshStatus()
    {
        if (_row.ValueKind != JsonValueKind.Object) return;
        var version = SelectedVersion;
        var current = Text(_row, "current");
        var state = Text(_row, "state");
        var staged = Text(_row, "staged_version");
        if (staged.Length == 0 && state == "ready") staged = Text(_row, "latest");
        var installable = _row.TryGetProperty("installable", out var flag) && flag.GetBoolean();
        // 所选版本已经准备好（staged）且服务端说可装：下载图标这一步就直接跳到"安装"
        var ready = version.Length > 0 && state == "ready" && staged == version && installable;
        var local = _row.TryGetProperty("versions", out var list) && list.EnumerateArray()
            .Any(v => Text(v, "version") == version && v.GetProperty("local").GetBoolean());
        var older = CompareVersions(version, current) < 0;
        var idle = !_busy && !_active;

        Selector?.SetEnabled(idle);
        SourceBox.IsEnabled = SaveSourceButton.IsEnabled = ImportButton.IsEnabled = idle;
        CanDownload = idle && version.Length > 0 && (version != current || _availableVersions.Contains(version));
        DownloadAction = ready ? "apply" : "download";
        DownloadTip = ready
            ? older ? $"降级安装 {version}，配置与记忆保留"
                : version == current ? $"重新安装 {version}" : $"安装 {version}"
            : local ? $"准备本地包 {version}" : $"下载 {version}，完成后另行安装";

        var message = Text(_row, "message", "选择版本后手动下载，再确认安装");
        if (_row.TryGetProperty("result", out var result) && state is "" or "current" or "unconfigured") message = Text(result, "message", message);
        if (state == "installing") message = "正在安装，请稍候…";
        else if (staged.Length > 0) message = $"{staged} 已准备好" + (staged == version ? "，可以安装" : $"；当前选择 {version}");
        else if (older) message = $"将退回到 {version}，配置与记忆保留";
        if (Component == "core" && state is "available" or "current") message += "；仅检查新版，下载与安装均需确认";
        if (Text(_row, "managed_by") == "core") { CanDownload = false; message = "随 Core 源码组件更新"; }
        Message.Text = message;
        DownloadProgress.Visibility = state == "downloading" ? Visibility.Visible : Visibility.Collapsed;
        DownloadProgress.Value = _row.TryGetProperty("progress", out var progress) ? progress.GetInt32() : 0;
    }

    private static int CompareVersions(string left, string right)
    {
        static Version? Parse(string value) => Version.TryParse(value.Split('-', '+')[0], out var parsed) ? parsed : null;
        var a = Parse(left); var b = Parse(right);
        if (a is null || b is null) return 0;
        var comparison = a.CompareTo(b);
        if (comparison != 0) return comparison;
        static string[] Pre(string value) => value.Split('+')[0].Split('-', 2) is var parts && parts.Length == 2 ? parts[1].Split('.') : Array.Empty<string>();
        var aa = Pre(left); var bb = Pre(right);
        if (aa.Length == 0 || bb.Length == 0) return (aa.Length == 0).CompareTo(bb.Length == 0);
        for (var i = 0; i < Math.Min(aa.Length, bb.Length); i++)
        {
            var an = long.TryParse(aa[i], out var av); var bn = long.TryParse(bb[i], out var bv);
            comparison = an && bn ? av.CompareTo(bv) : an != bn ? (an ? -1 : 1) : string.CompareOrdinal(aa[i], bb[i]);
            if (comparison != 0) return comparison;
        }
        return aa.Length.CompareTo(bb.Length);
    }

    private void Import_Click(object sender, RoutedEventArgs e) => UpdateRequested?.Invoke(this, "import");
    private void SaveSource_Click(object sender, RoutedEventArgs e) => UpdateRequested?.Invoke(this, "save_source");
}
