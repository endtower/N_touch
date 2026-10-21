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

    /// <summary>组件行里的一组控件：更新面板、版本选择器、折叠角标、下载/启动/停止三个动作图标。
    /// 顺序与清单行一致；manager/launcher 的启动/停止常驻置灰。</summary>
    private sealed record UpdateSlot(
        string Component,
        ComponentUpdateControl Control,
        VersionSelectorControl Selector,
        ToggleButton Toggle,
        Button Download,
        Button Start,
        Button Stop);

    private IEnumerable<UpdateSlot> UpdateSlots => new[]
    {
        new UpdateSlot("core", CoreUpdate, CoreSelector, CoreUpdateToggle, CoreDownload, CoreStart, CoreStop),
        new UpdateSlot("snowluma", SnowUpdate, SnowSelector, SnowUpdateToggle, SnowDownload, SnowStart, SnowStop),
        new UpdateSlot("ingress", IngressUpdate, IngressSelector, IngressUpdateToggle, IngressDownload, IngressStart, IngressStop),
        new UpdateSlot("manager", ManagerUpdate, ManagerSelector, ManagerUpdateToggle, ManagerDownload, ManagerStart, ManagerStop),
        new UpdateSlot("launcher", LauncherUpdate, LauncherSelector, LauncherUpdateToggle, LauncherDownload, LauncherStart, LauncherStop),
    };

    private IEnumerable<ComponentUpdateControl> UpdateControls => UpdateSlots.Select(s => s.Control);

    /// <summary>当前版本号显示在清单行尾，展开的更新区里不再重复一遍。</summary>
    private TextBlock? VersionChip(string component) => component switch
    {
        "core" => CoreVersion, "snowluma" => SnowVersion, "ingress" => IngressVersion,
        "manager" => ManagerVersion, "launcher" => LauncherVersion, _ => null,
    };

    private void StartUpdateChecks()
    {
        foreach (var slot in UpdateSlots)
        {
            slot.Control.Selector = slot.Selector;
            slot.Control.UpdateRequested += ComponentUpdateRequested;
            // 版本选择器在行表头，点它自己的刷新/下拉不会冒泡到"点整行展开"，
            // 但事件要转发到这里才能拿到组件上下文去走 /v1/updates。
            slot.Selector.RefreshRequested += () => _ = ApplyComponentAction(slot.Control, "check");
            slot.Selector.SelectionChanged += slot.Control.NotifySelectionChanged;
        }
        _updatePoll = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
        _updatePoll.Tick += async (_, _) => await RefreshUpdates();
        _updatePoll.Start();
        _updateCheck = new DispatcherTimer { Interval = TimeSpan.FromHours(6) };
        _updateCheck.Tick += async (_, _) => await CheckUpdatesAutomatically();
        _updateCheck.Start();
        _ = CheckUpdatesAutomatically();
        Closed += (_, _) => { _updatePoll.Stop(); _updateCheck.Stop(); };
    }

    /// <summary>点行表头任意位置：展开/收起该组件的更新区。同一时刻只展开一个，免得又要滚动。</summary>
    private void UpdateRow_Click(object sender, MouseButtonEventArgs e)
    {
        var component = (sender as FrameworkElement)?.Tag?.ToString() ?? "";
        var slot = UpdateSlots.FirstOrDefault(s => s.Component == component);
        if (slot is null) return;
        if (slot.Control.Expanded) CloseAllUpdates();
        else ExpandUpdate(component);
    }

    /// <summary>展开指定组件的更新区、收起其余，并把行尾角标翻到"收起"态。</summary>
    private void ExpandUpdate(string component)
    {
        var slot = UpdateSlots.FirstOrDefault(s => s.Component == component);
        if (slot is null) return;
        CloseAllUpdates();
        slot.Control.Expanded = true;
        slot.Toggle.IsChecked = true;
    }

    private void CloseAllUpdates()
    {
        foreach (var slot in UpdateSlots)
        {
            slot.Control.Expanded = false;
            slot.Toggle.IsChecked = false;
        }
    }

    private static string UpdateText(JsonElement item, string key) =>
        item.TryGetProperty(key, out var child) && child.ValueKind == JsonValueKind.String ? child.GetString() ?? "" : "";

    /// <summary>
    /// 卡片标题右上角的状态 chip。底色/边框/字色只分两档：需要处理（强调色）与常态（中性），
    /// 走资源键而不是直接赋画刷，切主题时才会跟着重绘。
    /// </summary>
    private static void SetChip(Border chip, TextBlock text, string label, bool attention)
    {
        SetTheme(chip, Border.BackgroundProperty, attention ? "BrushAccentSoft" : "BrushBgSunken");
        SetTheme(chip, Border.BorderBrushProperty, attention ? "BrushAccent" : "BrushBorderDefault");
        SetTheme(text, TextBlock.ForegroundProperty, attention ? "BrushAccent" : "BrushTextTertiary");
        text.Text = label;
    }

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
                var slot = UpdateSlots.FirstOrDefault(s => s.Component == component);
                if (slot is not null)
                {
                    slot.Control.Refresh(row, _updatesBusy, active);
                    slot.Download.IsEnabled = slot.Control.CanDownload;
                    slot.Download.ToolTip = slot.Control.CanDownload
                        ? slot.Control.DownloadTip
                        : "当前没有可下载或可安装的版本";
                }
                var current = UpdateText(row, "current");
                if (VersionChip(component) is { } chip) chip.Text = current.Length > 0 ? "v" + current : "";
                var state = UpdateText(row, "state");
                if (state == "available") available++;
                recover |= state == "recovery_required";
            }
            SetChip(VersionStatusChip, VersionStatusText,
                recover ? "安装中断" : available > 0 ? $"有新版本 {available}" : "已是最新",
                recover || available > 0);
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
            SetChip(VersionStatusChip, VersionStatusText, "状态未知", false);
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
        catch { UpdateSummary.Text = "可在各组件下刷新版本，或导入本地包"; }
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

    /// <summary>同一图标分两次操作：先下载，准备完成后另行确认安装。</summary>
    private async void ComponentDownload_Click(object sender, RoutedEventArgs e)
    {
        var component = (sender as FrameworkElement)?.Tag?.ToString() ?? "";
        var slot = UpdateSlots.FirstOrDefault(s => s.Component == component);
        if (slot is null) return;
        await ApplyComponentAction(slot.Control, slot.Control.DownloadAction);
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
        _poll?.Stop(); _updateCheck?.Stop(); _updatePoll?.Stop();
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
