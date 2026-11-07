using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace AbyssLauncher;

/// <summary>
/// 组件展开区里的更新面板：选版本 → 下载/安装 → 一句状态，右边箭头藏起更新源与源码工具。
///
/// 为什么版本选择放在展开区而不是行表头：行表头要同时回答「它是什么、现在什么状态、
/// 下一步点哪里」，再塞一个下拉和三个图标就成了一排噪声。
/// </summary>
public partial class ComponentUpdateControl : UserControl
{
    public string Component { get; set; } = "";

    /// <summary>该组件的在线更新源地址（留空表示关闭在线检查）。</summary>
    public string Source => SourceBox.Text.Trim();

    public void SetSource(string? url) => SourceBox.Text = url ?? "";

    /// <summary>下载按钮这一下是「下载」还是「安装」——由服务端返回的 row 算出。</summary>
    public bool CanDownload { get; private set; }
    public string DownloadAction { get; private set; } = "download";
    public string DownloadLabel { get; private set; } = "下载";
    public string DownloadTip { get; private set; } = "下载所选版本，下载完成后另行确认安装";

    public string SelectedVersion => VersionSelector.SelectedVersion;

    public event Action<ComponentUpdateControl, string>? UpdateRequested;

    private JsonElement _row;
    private bool _busy, _active;
    private HashSet<string> _availableVersions = new();

    public ComponentUpdateControl()
    {
        InitializeComponent();
        VersionSelector.SelectionChanged += () => { if (!_busy) RefreshStatus(); };
        VersionSelector.RefreshRequested += () => UpdateRequested?.Invoke(this, "check");
    }

    /// <summary>展开区里的「更新源 / 源码工具」那块是否拉开。</summary>
    public bool Expanded
    {
        get => SourceRow.Visibility == Visibility.Visible;
        set
        {
            SourceRow.Visibility = value ? Visibility.Visible : Visibility.Collapsed;
            SourceToggle.IsChecked = value;
        }
    }

    private static string Text(JsonElement item, string name, string fallback = "") =>
        item.ValueKind == JsonValueKind.Object && item.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? fallback : fallback;

    public void Refresh(JsonElement row, bool busy, bool active)
    {
        _row = row.Clone(); _busy = busy; _active = active;
        var versions = row.TryGetProperty("versions", out var list)
            ? list.EnumerateArray().Select(v => Text(v, "version")).Where(v => v.Length > 0).ToArray()
            : Array.Empty<string>();
        var current = Text(row, "current");
        if (versions.Length == 0 && current.Length > 0) versions = new[] { current };
        _availableVersions = versions.ToHashSet();
        if (current.Length > 0 && !versions.Contains(current))
            versions = new[] { current }.Concat(versions).ToArray();

        // 列表没变就不重铺，免得每 2 秒的轮询把正在输入的下拉打断
        var keep = VersionSelector.SelectedVersion;
        if (VersionSelector.SetVersions(versions)) VersionSelector.SetSelectedVersion(keep);
        if (VersionSelector.SelectedVersion.Length == 0)
            VersionSelector.SetSelectedVersion(Preferred(row, versions, current));
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

    /// <summary>用户在下拉里换了选项：重算按钮状态与状态提示。</summary>
    public void NotifySelectionChanged() => RefreshStatus();

    public void SelectVersion(string version) { VersionSelector.SetSelectedVersion(version); RefreshStatus(); }

    /// <summary>把焦点放到版本下拉上；验收脚本展开某组件后要能看到它。</summary>
    public void FocusVersion() => VersionSelector.FocusPicker();

    private void RefreshStatus()
    {
        if (_row.ValueKind != JsonValueKind.Object) return;
        var version = SelectedVersion;
        var current = Text(_row, "current");
        var state = Text(_row, "state");
        var staged = Text(_row, "staged_version");
        var installable = _row.TryGetProperty("installable", out var flag) && flag.GetBoolean();
        // 服务端已经把所选版本准备好（staged）且允许安装：这一步的按钮直接变成"安装"
        var ready = version.Length > 0 && state == "ready" && staged == version && installable;
        var local = _row.TryGetProperty("versions", out var list) && list.EnumerateArray()
            .Any(v => Text(v, "version") == version && v.TryGetProperty("local", out var l) && l.GetBoolean());
        var older = CompareVersions(version, current) < 0;
        var managed = Text(_row, "managed_by") == "core";
        var idle = !_busy && !_active;

        VersionSelector.SetEnabled(idle && !managed);
        SourceBox.IsEnabled = SaveSourceButton.IsEnabled = idle;
        SourceActions.Visibility = Component == "core"
            && _row.TryGetProperty("source_available", out var available) && available.ValueKind == JsonValueKind.True
            ? Visibility.Visible : Visibility.Collapsed;
        SourceChangesButton.IsEnabled = SourceBackupButton.IsEnabled = idle;
        ImportButton.IsEnabled = idle && !managed;

        CanDownload = idle && !managed && version.Length > 0 && (version != current || _availableVersions.Contains(version));
        DownloadAction = ready ? "apply" : "download";
        DownloadLabel = ready ? "安装" : "下载";
        DownloadTip = ready
            ? older ? $"降级安装 {version}，配置与记忆保留"
                : version == current ? $"重新安装 {version}" : $"安装 {version}"
            : local ? $"准备本地包 {version}" : $"下载 {version}，完成后另行安装";
        DownloadButton.Content = DownloadLabel;
        DownloadButton.ToolTip = CanDownload ? DownloadTip : "先选一个和当前不同的版本，或点刷新获取可选版本";
        DownloadButton.IsEnabled = CanDownload;

        string message;
        if (managed) message = Text(_row, "message", "随 Core 源码组件更新");
        else if (state == "downloading")
            message = $"正在下载 {Text(_row, "latest")} …";
        else if (state == "installing") message = "正在安装，请稍候…";
        else if (state == "checking") message = "正在获取可选版本…";
        else if (state == "error") message = Text(_row, "message", "检查失败，可稍后重试");
        else if (ready) message = $"{staged} 已准备好，点「安装」应用";
        else if (staged.Length > 0 && staged != version) message = $"{staged} 已准备好；当前选择 {version}";
        else if (older) message = $"将退回到 {version}，配置与记忆保留";
        else if (version.Length > 0 && version == current)
            message = local ? "当前版本；本机已留备份，可随时装回" : "当前版本，无需更新";
        else if (version.Length > 0) message = local ? $"{version} 已在本机，可直接安装" : $"可下载 {version}";
        else message = Text(_row, "message", "选择版本后手动下载，再确认安装");

        Message.Text = message;
        Message.ToolTip = message;
        DownloadProgress.Visibility = state == "downloading" ? Visibility.Visible : Visibility.Collapsed;
        DownloadProgress.Value = _row.TryGetProperty("progress", out var progress)
            && progress.ValueKind == JsonValueKind.Number ? progress.GetInt32() : 0;
    }

    private static int CompareVersions(string left, string right)
    {
        static Version? Parse(string value) => Version.TryParse(value.Split('-', '+')[0], out var parsed) ? parsed : null;
        var a = Parse(left); var b = Parse(right);
        if (a is null || b is null) return 0;
        var comparison = a.CompareTo(b);
        if (comparison != 0) return comparison;
        static string[] Pre(string value) => value.Split('+')[0].Split('-', 2) is var parts && parts.Length == 2
            ? parts[1].Split('.') : Array.Empty<string>();
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

    private void Download_Click(object sender, RoutedEventArgs e) => UpdateRequested?.Invoke(this, DownloadAction);
    private void Import_Click(object sender, RoutedEventArgs e) => UpdateRequested?.Invoke(this, "import");
    private void SaveSource_Click(object sender, RoutedEventArgs e) => UpdateRequested?.Invoke(this, "save_source");
    private void SourceChanges_Click(object sender, RoutedEventArgs e) => UpdateRequested?.Invoke(this, "source_changes");
    private void SourceBackup_Click(object sender, RoutedEventArgs e) => UpdateRequested?.Invoke(this, "source_backup");
    private void SourceToggle_Click(object sender, RoutedEventArgs e) => Expanded = SourceToggle.IsChecked == true;
}
