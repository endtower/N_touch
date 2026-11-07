using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Threading;
using Microsoft.Win32;

namespace AbyssLauncher;

public partial class MainWindow
{
    private DispatcherTimer? _updateCheck, _updatePoll;
    private int _checkingUpdates;
    private bool _updatesBusy, _updatesRefreshing, _updateSourcesLoaded;

    /// <summary>组件展开区里的更新面板。行表头只保留状态与主按钮，
    /// 版本下拉、下载/安装、更新源都收在展开区里，避免一行上挤三四个控件。</summary>
    private IEnumerable<ComponentUpdateControl> UpdateControls => new[]
    {
        CoreUpdate, SnowUpdate, IngressUpdate, ManagerUpdate, LauncherUpdate,
    };

    private ComponentUpdateControl? UpdateControl(string component) =>
        UpdateControls.FirstOrDefault(control => control.Component == component);

    /// <summary>组件当前版本号显示在行表头，展开区里不再重复一遍。</summary>
    private TextBlock? VersionChip(string component) => component switch
    {
        "core" => CoreVersion, "snowluma" => SnowVersion, "ingress" => IngressVersion,
        "manager" => ManagerVersion, "launcher" => LauncherVersion, _ => null,
    };

    private void StartUpdateChecks()
    {
        foreach (var control in UpdateControls)
            control.UpdateRequested += ComponentUpdateRequested;
        _updatePoll = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
        _updatePoll.Tick += async (_, _) => await RefreshUpdates();
        _updatePoll.Start();
        _updateCheck = new DispatcherTimer { Interval = TimeSpan.FromHours(6) };
        _updateCheck.Tick += async (_, _) => await CheckUpdatesAutomatically();
        _updateCheck.Start();
        _ = CheckUpdatesAutomatically();
        Closed += (_, _) => { _updatePoll?.Stop(); _updateCheck?.Stop(); };
    }

    /// <summary>展开某个组件的详情（含更新区）。整行只有一个展开状态，由 Startup 那边统一管。</summary>
    private void ExpandUpdate(string component) => ExpandRow(component);

    private static string UpdateText(JsonElement item, string key) =>
        item.TryGetProperty(key, out var child) && child.ValueKind == JsonValueKind.String ? child.GetString() ?? "" : "";

    private async Task RefreshUpdates()
    {
        if (_updatesRefreshing) return;
        _updatesRefreshing = true;
        try
        {
            var status = await _client.GetAsync("/v1/updates");
            var active = status.GetProperty("active").GetBoolean();
            var recover = false;
            var available = 0;
            foreach (var row in status.GetProperty("components").EnumerateArray())
            {
                var component = UpdateText(row, "component");
                if (UpdateControl(component) is { } control) control.Refresh(row, _updatesBusy, active);
                var current = UpdateText(row, "current");
                if (VersionChip(component) is { } chip) chip.Text = current.Length > 0 ? "v" + current : "";
                var state = UpdateText(row, "state");
                if (state == "available") available++;
                recover |= state == "recovery_required";
            }
            // 检查结果放页脚，不跟页头的运行状态抢同一块地方
            UpdateSummary.Text = recover ? "有安装中断的任务，可点右上角「恢复中断的安装」"
                : available > 0 ? $"发现 {available} 个新版本；点开对应程序可查看并安装"
                : "组件独立更新，可选择历史版本并降级安装";
            RecoverUpdateButton.Visibility = recover ? Visibility.Visible : Visibility.Collapsed;
            RecoverUpdateButton.IsEnabled = !_updatesBusy;
            if (!_updateSourcesLoaded)
            {
                var config = status.GetProperty("config");
                AutoCheckUpdates.IsChecked = config.GetProperty("auto_check").GetBoolean();
                var sources = config.GetProperty("sources");
                foreach (var control in UpdateControls) control.SetSource(UpdateText(sources, control.Component));
                _updateSourcesLoaded = true;
            }
        }
        catch (Exception error)
        {
            UpdateOperation.Text = "无法读取版本信息: " + error.Message;
        }
        finally { _updatesRefreshing = false; }
    }

    private async Task CheckUpdatesAutomatically()
    {
        if (_updatesBusy || Interlocked.Exchange(ref _checkingUpdates, 1) == 1) return;
        try
        {
            var status = await _client.GetAsync("/v1/updates");
            if (status.GetProperty("config").GetProperty("auto_check").GetBoolean())
            {
                status = await _client.UpdateAsync("check", new { });
                var available = status.GetProperty("components").EnumerateArray().Count(row => UpdateText(row, "state") == "available");
                UpdateSummary.Text = available > 0 ? $"发现 {available} 个新版本；Core 下载与安装均需确认" : "仅自动检查版本；Core 不自动下载或安装";
            }
        }
        catch { UpdateSummary.Text = "可在各程序展开区里刷新版本，或导入本地包"; }
        finally { Interlocked.Exchange(ref _checkingUpdates, 0); await RefreshUpdates(); }
    }

    private async Task RunUpdate(string message, Func<Task> action)
    {
        if (_updatesBusy) return;
        _updatesBusy = true;
        UpdateOperation.Text = StatusBar.Text = message;
        await RefreshUpdates();
        try { await action(); UpdateOperation.Text = StatusBar.Text = "操作完成"; }
        catch (OperationCanceledException error) { UpdateOperation.Text = StatusBar.Text = error.Message; }
        catch (Exception error) { UpdateOperation.Text = StatusBar.Text = error.Message; }
        finally { _updatesBusy = false; await RefreshUpdates(); }
    }

    private async void ComponentUpdateRequested(ComponentUpdateControl control, string action) =>
        await ApplyComponentAction(control, action);

    private async Task ApplyComponentAction(ComponentUpdateControl control, string action)
    {
        if (_updatesBusy) return;
        var version = control.SelectedVersion;
        if (action == "import")
        {
            var dialog = new OpenFileDialog { Filter = "组件更新包 (*.zip)|*.zip", Title = "选择更新包（需要同名 JSON 清单）" };
            if (dialog.ShowDialog(this) != true) return;
            await RunUpdate("正在校验本地更新包…", async () =>
            {
                var result = await _client.UpdateAsync("import", new { component = control.Component, path = dialog.FileName });
                var row = result.GetProperty("components").EnumerateArray().First(r => UpdateText(r, "component") == control.Component);
                var staged = UpdateText(row, "staged_version");
                control.SelectVersion(staged.Length > 0 ? staged : UpdateText(row, "latest"));
            });
        }
        else if (action == "check") await RunUpdate("正在获取可选版本…", async () =>
        { await _client.UpdateAsync("check", new { component = control.Component, history = true }); });
        else if (action == "download") await RunUpdate("正在准备版本 " + version + "，其他组件可以继续运行…", async () =>
        {
            var confirmation = control.Component == "core" ? await ConfirmCoreUpdate("download", version) : null;
            await _client.UpdateAsync("download", new { component = control.Component, version, confirmation_token = confirmation });
        });
        else if (action == "apply") await RunUpdate("正在安装所选版本 " + version + "…", async () =>
        {
            var confirmation = control.Component == "core" ? await ConfirmCoreUpdate("install", version) : null;
            var result = await _client.UpdateAsync("apply", new { component = control.Component, launcher_pid = Environment.ProcessId, version, confirmation_token = confirmation });
            if (result.GetProperty("exit_launcher").GetBoolean()) ExitForUpdate();
        });
        else if (action == "save_source") await SaveUpdateConfig(control.Component);
        else if (action == "source_changes") await RunUpdate("正在检查源码改动…", async () =>
        {
            var info = await _client.UpdateAsync("source", new { component = "core" });
            var changes = info.GetProperty("local_changes").EnumerateArray().Select(x => x.GetString()).ToArray();
            MessageBox.Show(this, changes.Length == 0 ? "未检测到相对安装基线的改动。"
                : string.Join("\n", changes.Take(100)) + (changes.Length > 100 ? $"\n另有 {changes.Length - 100} 项" : ""),
                "Core 源码改动", MessageBoxButton.OK, MessageBoxImage.Information);
        });
        else if (action == "source_backup") await RunUpdate("正在保留当前源码…", async () =>
        {
            var result = await _client.UpdateAsync("source-backup", new { component = "core" });
            MessageBox.Show(this, "源码备份已保存：\n" + UpdateText(result, "path")
                + "\n\n当前程序与个人数据保持原状。", "源码备份", MessageBoxButton.OK, MessageBoxImage.Information);
        });
    }

    private async Task<string> ConfirmCoreUpdate(string operation, string version)
    {
        var review = await _client.UpdateAsync("review", new { component = "core", operation, version });
        var dialog = new CoreUpdateDialog(review) { Owner = this };
        if (dialog.ShowDialog() != true) throw new OperationCanceledException("已取消，当前 Core 保持不变");
        return UpdateText(review, "confirmation_token");
    }

    private async void CheckAllUpdates_Click(object sender, RoutedEventArgs e) =>
        await RunUpdate("正在获取各组件的可选版本…", async () => { await _client.UpdateAsync("check", new { history = true }); });

    private async void RecoverUpdate_Click(object sender, RoutedEventArgs e) => await RunUpdate("正在恢复中断的安装…", async () =>
    {
        var status = await _client.GetAsync("/v1/updates");
        string? confirmation = null;
        foreach (var component in status.GetProperty("components").EnumerateArray())
            if (UpdateText(component, "component") == "core" && component.TryGetProperty("result", out var recovery)
                && recovery.TryGetProperty("data_restore_required", out var required) && required.ValueKind == JsonValueKind.True)
                confirmation = await ConfirmCoreUpdate("recover", UpdateText(component, "current"));
        var result = await _client.UpdateAsync("recover", new { launcher_pid = Environment.ProcessId, confirmation_token = confirmation });
        if (result.GetProperty("exit_launcher").GetBoolean()) ExitForUpdate();
    });

    /// <summary>更新源散在各组件的展开区里，但服务端是整份覆盖写入的，
    /// 所以任何一处保存都要把全部组件当前的源一起提交，否则会把别处清空。</summary>
    private async Task SaveUpdateConfig(string? savedComponent = null)
    {
        if (_updatesBusy) return;
        _updatesBusy = true;
        var label = savedComponent is null ? "更新设置" : $"{savedComponent} 更新源";
        UpdateOperation.Text = StatusBar.Text = $"正在保存{label}…";
        try
        {
            await _client.UpdateAsync("config", new
            {
                auto_check = AutoCheckUpdates.IsChecked == true,
                sources = UpdateControls.ToDictionary(control => control.Component, control => control.Source),
            });
            UpdateOperation.Text = StatusBar.Text = $"{label}已保存";
        }
        catch (Exception error) { UpdateOperation.Text = StatusBar.Text = error.Message; }
        finally { _updatesBusy = false; }
    }

    private async void AutoCheck_Changed(object sender, RoutedEventArgs e) => await SaveUpdateConfig();

    private void ExitForUpdate()
    {
        _exitConfirmed = true;
        _poll?.Stop(); _updateCheck?.Stop(); _updatePoll?.Stop(); _startupPoll?.Stop();
        Web.Dispose();
        ((App)Application.Current).ReallyExit();
    }

    private async Task SaveUpdateSnapshotIfRequested()
    {
        var path = Environment.GetEnvironmentVariable("ABYSS_UPDATES_SNAPSHOT");
        if (string.IsNullOrWhiteSpace(path)) return;
        await RefreshUpdates();
        var component = Environment.GetEnvironmentVariable("ABYSS_UPDATES_SNAPSHOT_COMPONENT");
        var control = UpdateControls.FirstOrDefault(c => c.Component == component);
        if (control is not null)
        {
            ExpandUpdate(component ?? "");   // 更新区默认收起，抓图前要先展开该组件
            control.SelectVersion(Environment.GetEnvironmentVariable("ABYSS_UPDATES_SNAPSHOT_VERSION") ?? control.SelectedVersion);
            control.BringIntoView();
        }
        UpdateLayout();
        var bitmap = new System.Windows.Media.Imaging.RenderTargetBitmap((int)ActualWidth, (int)ActualHeight, 96, 96, System.Windows.Media.PixelFormats.Pbgra32);
        bitmap.Render(this);
        var encoder = new System.Windows.Media.Imaging.PngBitmapEncoder();
        encoder.Frames.Add(System.Windows.Media.Imaging.BitmapFrame.Create(bitmap));
        using (var stream = System.IO.File.Create(path)) encoder.Save(stream);
        if (Environment.GetEnvironmentVariable("ABYSS_UPDATES_EXIT_AFTER_SNAPSHOT") == "1") ExitForUpdate();
    }
}
