using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using Microsoft.Web.WebView2.Core;

namespace AbyssLauncher;

/// <summary>主窗口：导航 / 总览控制 / WebView2 页面区域 / 日志 / 服务自启。</summary>
public partial class MainWindow : Window
{
    private readonly ManagerClient _client = new();
    private DispatcherTimer? _poll;
    private bool _exiting;
    private string _webSource = "";      // "" | "core" | "snowluma"
    private bool _coreWebNavigated;
    private bool _snowlumaNavigated;
    private int _serviceEnsuring;

    public MainWindow()
    {
        InitializeComponent();
        Loaded += async (_, _) =>
        {
            await EnsureServiceAsync();
            InitWebView();
            StartPoll();
        };
    }

    // ---------------- 服务自启 ----------------

    private async Task EnsureServiceAsync()
    {
        if (Interlocked.Exchange(ref _serviceEnsuring, 1) == 1) return;
        try
        {
            var info = await _client.TryInfo();
            if (info is not null)
            {
                ServiceStateText.Text = "管理服务运行中"; ServiceStateText.Foreground = Brush("#34D399");
                return;
            }
            StatusBar.Text = "管理服务未运行，正在拉起…";
            var exe = AppPaths.ServiceExe();
            var psi = exe is not null
                ? new System.Diagnostics.ProcessStartInfo(exe) { WorkingDirectory = AppPaths.DataRoot() }
                : new System.Diagnostics.ProcessStartInfo("python",
                    $"\"{Path.Combine(AppPaths.DataRoot(), "manager_service.py")}\"")
                { WorkingDirectory = AppPaths.DataRoot() };
            psi.CreateNoWindow = true; psi.UseShellExecute = false;
            System.Diagnostics.Process.Start(psi);
            for (int i = 0; i < 20; i++)
            {
                await Task.Delay(500);
                info = await _client.TryInfo();
                if (info is not null)
                {
                    ServiceStateText.Text = "管理服务运行中（本轮自动拉起）";
                    ServiceStateText.Foreground = Brush("#34D399");
                    return;
                }
            }
            ServiceStateText.Text = "管理服务拉起失败"; ServiceStateText.Foreground = Brush("#F87171");
        }
        finally
        {
            Interlocked.Exchange(ref _serviceEnsuring, 0);
        }
    }

    // ---------------- 轮询 ----------------

    private void StartPoll()
    {
        _poll = new DispatcherTimer { Interval = TimeSpan.FromSeconds(5) };
        _poll.Tick += async (_, _) => await RefreshTargets();
        _poll.Start();
        _ = RefreshTargets();
    }

    private async Task RefreshTargets()
    {
        try
        {
            var root = await _client.GetAsync("/v1/targets");
            ServiceStateText.Text = "管理服务运行中"; ServiceStateText.Foreground = Brush("#34D399");
            ManagerClient.Target? core = null, snow = null;
            foreach (var t in root.GetProperty("targets").EnumerateArray())
            {
                var parsed = ManagerClient.ParseTarget(t);
                if (parsed.Name == "core") core = parsed;
                if (parsed.Name == "snowluma") snow = parsed;
            }
            if (core is not null)
            {
                CoreStateText.Text = $"{core.State}（期望 {core.Desired}）";
                CoreStateText.Foreground = Brush(core.State == "running" ? "#34D399" : "#F87171");
                CoreDetail.Text = $"pid {core.Pid?.ToString() ?? "—"} · 端口 {string.Join("/", core.Ports)} · "
                    + $"运行 {core.UptimeSec?.ToString("0") ?? "—"}s · 重启 {core.Restarts} 次 · "
                    + $"业务{(core.Ready ? "就绪" : "未就绪")}"
                    + (string.IsNullOrEmpty(core.LastError) ? "" : $"\n{core.LastError}");
                UpdateWebOverlay(core.State);
            }
            if (snow is not null)
            {
                SnowlumaStateText.Text = $"{snow.State}（期望 {snow.Desired}）";
                SnowlumaStateText.Foreground = Brush(snow.State == "running" ? "#34D399" : "#F87171");
                SnowlumaDetail.Text = $"pid {snow.Pid?.ToString() ?? "—"} · 端口 {string.Join("/", snow.Ports)}"
                    + (string.IsNullOrEmpty(snow.LastError) ? "" : $"\n{snow.LastError}");
            }
        }
        catch (Exception)
        {
            ServiceStateText.Text = "与服务失去连接"; ServiceStateText.Foreground = Brush("#F87171");
        }
    }

    private static System.Windows.Media.SolidColorBrush Brush(string hex) =>
        (System.Windows.Media.SolidColorBrush)new System.Windows.Media.BrushConverter().ConvertFromString(hex)!;

    // ---------------- 导航 ----------------

    private void Nav_Click(object sender, RoutedEventArgs e)
    {
        var tag = ((Button)sender).Tag.ToString();
        OverviewPanel.Visibility = tag == "overview" ? Visibility.Visible : Visibility.Collapsed;
        WebPanel.Visibility = (tag == "coreweb" || tag == "snowluma") ? Visibility.Visible : Visibility.Collapsed;
        LogsPanel.Visibility = tag == "logs" ? Visibility.Visible : Visibility.Collapsed;
        UpdatePanel.Visibility = tag == "update" ? Visibility.Visible : Visibility.Collapsed;
        foreach (var b in new[] { NavOverview, NavCoreWeb, NavSnowluma, NavLogs, NavUpdate })
            b.Foreground = b.Tag.ToString() == tag ? System.Windows.Media.Brushes.White : new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromArgb(0xCB, 0xCB, 0xD5, 0xE1));
        if (tag == "coreweb") ShowWeb("core");
        if (tag == "snowluma") ShowWeb("snowluma");
        if (tag == "logs") _ = RefreshLogs();
    }

    // ---------------- WebView2 ----------------

    private async void InitWebView()
    {
        try
        {
            var userData = Path.Combine(AppPaths.DataRoot(), "launcher", "webview");
            Directory.CreateDirectory(userData);
            var env = await CoreWebView2Environment.CreateAsync(
                userDataFolder: userData);
            await Web.EnsureCoreWebView2Async(env);
            Web.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
            Web.CoreWebView2.Settings.AreDevToolsEnabled = false;
            // 不向网页注入管理令牌或宿主桥接（任务书 §7）
        }
        catch (WebView2RuntimeNotFoundException)
        {
            MessageBox.Show("未检测到 WebView2 运行时。\n请安装 WebView2 Runtime（Evergreen Bootstrapper）后重试。",
                "缺少组件", MessageBoxButton.OK, MessageBoxImage.Warning);
        }
        catch (Exception ex)
        {
            MessageBox.Show($"WebView2 初始化失败：{ex.Message}", "错误",
                MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }

    private void ShowWeb(string source)
    {
        _webSource = source;
        int port = source == "core" ? 5002 : 5099;
        var url = $"http://127.0.0.1:{port}/";
        if (Web.CoreWebView2 is null) return;
        if (source == "core" && !_coreWebNavigated) { Web.CoreWebView2.Navigate(url); _coreWebNavigated = true; }
        else if (source == "snowluma" && !_snowlumaNavigated) { Web.CoreWebView2.Navigate(url); _snowlumaNavigated = true; }
        else Web.CoreWebView2.Navigate(url);
        WebOverlayHint.Visibility = source == "core" && CoreStateText.Text.StartsWith("stopped")
            ? Visibility.Visible : Visibility.Collapsed;
    }

    private void WebTabCore_Click(object sender, RoutedEventArgs e) => ShowWeb("core");
    private void WebTabSnowluma_Click(object sender, RoutedEventArgs e) => ShowWeb("snowluma");

    private void UpdateWebOverlay(string coreState)
    {
        if (_webSource == "core")
            WebOverlayHint.Visibility = coreState == "running" ? Visibility.Collapsed : Visibility.Visible;
    }

    // ---------------- 控制动作 ----------------

    private async Task Send(string path)
    {
        StatusBar.Text = "执行中…";
        try
        {
            var r = await _client.PostAsync(path);
            StatusBar.Text = r.TryGetProperty("message", out var m) ? m.GetString() ?? "完成" : "完成";
            if (r.TryGetProperty("error_code", out var ec) && ec.GetString() == "conflict")
                MessageBox.Show(r.TryGetProperty("message", out var mm) ? mm.GetString() : "冲突",
                    "端口冲突", MessageBoxButton.OK, MessageBoxImage.Warning);
            await RefreshTargets();
        }
        catch (Exception ex)
        {
            StatusBar.Text = $"失败：{ex.Message}";
        }
    }

    private void CoreStart_Click(object sender, RoutedEventArgs e) => _ = Send("/v1/targets/core/start");
    private void CoreStop_Click(object sender, RoutedEventArgs e) => _ = Send("/v1/targets/core/stop");
    private void CoreRestart_Click(object sender, RoutedEventArgs e) => _ = Send("/v1/targets/core/restart");
    private void SnowlumaStart_Click(object sender, RoutedEventArgs e) => _ = Send("/v1/targets/snowluma/start");
    private void SnowlumaStop_Click(object sender, RoutedEventArgs e) => _ = Send("/v1/targets/snowluma/stop");
    private void SnowlumaWebui_Click(object sender, RoutedEventArgs e) => ShowWeb("snowluma");

    private async void StartAll_Click(object sender, RoutedEventArgs e)
    {
        await Send("/v1/targets/core/start");
        await Send("/v1/targets/snowluma/start");
    }

    private async void StopAll_Click(object sender, RoutedEventArgs e)
    {
        await Send("/v1/targets/snowluma/stop");
        await Send("/v1/targets/core/stop");
    }

    // ---------------- 日志 ----------------

    private async Task RefreshLogs()
    {
        if (LogSource.SelectedItem is ComboBoxItem item && item.Tag is string source)
        {
            try
            {
                var r = await _client.GetAsync($"/v1/logs/{source}?lines=200");
                LogBox.Text = r.TryGetProperty("lines", out var arr)
                    ? string.Join("\n", arr.EnumerateArray().Select(x => x.GetString())) : "（无日志）";
            }
            catch (Exception ex) { LogBox.Text = $"读取失败：{ex.Message}"; }
        }
    }

    private void LogSource_Changed(object sender, SelectionChangedEventArgs e) => _ = RefreshLogs();
    private void LogRefresh_Click(object sender, RoutedEventArgs e) => _ = RefreshLogs();

    // ---------------- 生命周期 ----------------

    private void Window_Closing(object sender, System.ComponentModel.CancelEventArgs e)
    {
        if (_exiting) return;
        // 关闭窗口默认隐藏到托盘，受管目标继续运行（任务书 §6）
        e.Cancel = true;
        HideToTray();
    }

    internal void HideToTray() => Visibility = Visibility.Hidden;

    private bool _exitConfirmed;

    internal void RequestExit()
    {
        if (_exitConfirmed) { ((App)Application.Current).ReallyExit(); return; }
        _exitConfirmed = true;
        var confirm = MessageBox.Show(
            "退出将停止 Core 与 SnowLuma（数据由 WAL 保护）。确认停止全部并退出？",
            "退出确认", MessageBoxButton.OKCancel, MessageBoxImage.Question);
        if (confirm == MessageBoxResult.OK)
        {
            _ = Task.Run(async () =>
            {
                try { await _client.PostAsync("/v1/shutdown"); } catch { }
                Dispatcher.Invoke(() => ((App)Application.Current).ReallyExit());
            });
        }
        else _exitConfirmed = false;
    }

    internal static void ExitFromTray()
    {
        var w = Application.Current.Windows.OfType<MainWindow>().FirstOrDefault();
        w?.RequestExit();
    }

    internal static void ShowFromTray()
    {
        var w = Application.Current.Windows.OfType<MainWindow>().FirstOrDefault();
        if (w is null) return;
        w.Visibility = Visibility.Visible;
        w.WindowState = WindowState.Normal;
        w.Activate();
    }

    internal void ForceShutdownIfRequested() { /* 占位：托盘退出已走 RequestExit */ }
}
