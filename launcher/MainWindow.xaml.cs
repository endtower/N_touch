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
    private string _webSource = "";      // "" | "core" | "snowluma"
    private readonly Dictionary<string, ManagerClient.Target> _targets = new();
    private bool _serviceConnected;
    private int _targetRefresh;
    private string _loadedWebUrl = "";
    private string _navigationError = "";
    private ulong _webNavigationId;
    private int _serviceEnsuring;

    public MainWindow()
    {
        InitializeComponent();
        Loaded += async (_, _) =>
        {
            await EnsureServiceAsync();
            await InitWebView();
            StartPoll();
            StartLogPoll();
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
        catch (Exception ex)
        {
            ServiceStateText.Text = "管理服务启动失败";
            StatusBar.Text = ex.Message;
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
        if (Interlocked.Exchange(ref _targetRefresh, 1) == 1) return;
        try
        {
            var root = await _client.GetAsync("/v1/targets");
            ServiceStateText.Text = "管理服务运行中"; ServiceStateText.Foreground = Brush("#34D399");
            ManagerClient.Target? core = null, snow = null;
            foreach (var t in root.GetProperty("targets").EnumerateArray())
            {
                var parsed = ManagerClient.ParseTarget(t);
                if (_targets.TryGetValue(parsed.Name, out var previous)
                    && previous.Pid != parsed.Pid && _webSource == parsed.Name)
                {
                    _loadedWebUrl = "";
                    _navigationError = "";
                }
                _targets[parsed.Name] = parsed;
                if (parsed.Name == "core") core = parsed;
                if (parsed.Name == "snowluma") snow = parsed;
            }
            if (core is not null)
            {
                CoreStateText.Text = core.State;
                CoreStateText.Foreground = Brush(core.State == "running" ? "#34D399" : "#F87171");
                CoreDetail.Text = $"pid {core.Pid?.ToString() ?? "—"} · 端口 {string.Join("/", core.Ports)} · "
                    + $"运行 {core.UptimeSec?.ToString("0") ?? "—"}s · "
                    + $"业务{(core.Ready ? "就绪" : "未就绪")}"
                    + (string.IsNullOrEmpty(core.LastError) ? "" : $"\n{core.LastError}");
            }
            _serviceConnected = true;
            UpdateWebPage();
            if (snow is not null)
            {
                SnowlumaStateText.Text = snow.State;
                SnowlumaStateText.Foreground = Brush(snow.State == "running" ? "#34D399" : "#F87171");
                SnowlumaDetail.Text = $"pid {snow.Pid?.ToString() ?? "—"} · 端口 {string.Join("/", snow.Ports)}"
                    + (string.IsNullOrEmpty(snow.LastError) ? "" : $"\n{snow.LastError}");
            }
        }
        catch (Exception)
        {
            _serviceConnected = false;
            ServiceStateText.Text = "与服务失去连接"; ServiceStateText.Foreground = Brush("#F87171");
            UpdateWebPage();
        }
        finally { Interlocked.Exchange(ref _targetRefresh, 0); }
    }

    private static System.Windows.Media.SolidColorBrush Brush(string hex) =>
        (System.Windows.Media.SolidColorBrush)new System.Windows.Media.BrushConverter().ConvertFromString(hex)!;

    // ---------------- 导航 ----------------

    private void Nav_Click(object sender, RoutedEventArgs e)
    {
        var tag = ((Button)sender).Tag.ToString();
        ShowSection(tag ?? "overview");
    }

    private void ShowSection(string tag)
    {
        OverviewPanel.Visibility = tag == "overview" ? Visibility.Visible : Visibility.Collapsed;
        WebPanel.Visibility = (tag == "coreweb" || tag == "snowluma") ? Visibility.Visible : Visibility.Collapsed;
        LogsPanel.Visibility = tag == "logs" ? Visibility.Visible : Visibility.Collapsed;
        foreach (var b in new[] { NavOverview, NavCoreWeb, NavSnowluma, NavLogs })
            b.Foreground = b.Tag.ToString() == tag ? System.Windows.Media.Brushes.White : new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromArgb(0xCB, 0xCB, 0xD5, 0xE1));
        if (tag == "coreweb") ShowWeb("core");
        if (tag == "snowluma") ShowWeb("snowluma");
        if (tag == "logs") _ = RefreshLogs();
    }

    // ---------------- WebView2 ----------------

    private async Task InitWebView()
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
            Web.CoreWebView2.NavigationStarting += (_, e) => _webNavigationId = e.NavigationId;
            Web.CoreWebView2.NavigationCompleted += (_, e) =>
            {
                if (e.NavigationId != _webNavigationId) return;
                if (!e.IsSuccess)
                {
                    _navigationError = "页面暂时无法连接，请查看程序状态或点击刷新页面。";
                    UpdateWebPage();
                }
            };
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
        if (_webSource != source) _navigationError = "";
        _webSource = source;
        UpdateWebPage();
    }

    private void UpdateWebPage(bool force = false)
    {
        if (string.IsNullOrEmpty(_webSource)) return;
        var name = _webSource == "core" ? "Core" : "SnowLuma";
        WebPageTitle.Text = name + " 页面";
        _targets.TryGetValue(_webSource, out var target);
        var ready = _serviceConnected && target?.State == "running" && target.Ready;
        WebPageState.Text = !_serviceConnected ? "管理服务未连接" : target?.State switch
        {
            "running" => "运行中", "starting" => "正在启动", "stopping" => "正在停止",
            "failed" => "运行失败", "conflict" => "端口被占用", _ => "尚未启动"
        };
        WebStartButton.Visibility = _serviceConnected && target is not null
            && (target.State is "stopped" or "failed") ? Visibility.Visible : Visibility.Collapsed;
        WebStartButton.Content = target?.Pid is not null ? "重新启动 " + name : "启动 " + name;
        if (force) _navigationError = "";
        if (!ready || !string.IsNullOrEmpty(_navigationError) || Web.CoreWebView2 is null)
        {
            Web.Visibility = Visibility.Collapsed;
            WebUnavailable.Visibility = Visibility.Visible;
            WebUnavailableTitle.Text = !string.IsNullOrEmpty(_navigationError) ? "页面暂时无法打开"
                : !_serviceConnected ? "管理服务未连接" : target?.State is "starting" or "stopping"
                ? "请稍候" : ready ? "正在加载页面" : target?.State == "failed" ? name + " 运行失败" : name + " 尚未启动";
            WebUnavailableDetail.Text = !string.IsNullOrEmpty(_navigationError) ? _navigationError
                : !_serviceConnected ? "可以在总览检查管理服务，或在日志页查看启动输出。"
                : !string.IsNullOrEmpty(target?.LastError) ? target.LastError
                : ready ? "正在初始化页面组件。" : "启动程序后，页面会自动显示。切换页面不会关闭后台程序。";
            return;
        }
        WebUnavailable.Visibility = Visibility.Collapsed;
        Web.Visibility = Visibility.Visible;
        var port = target!.Ports.FirstOrDefault();
        if (port <= 0) port = _webSource == "core" ? 5002 : 5099;
        var url = $"http://127.0.0.1:{port}/";
        WebPageTitle.ToolTip = url;
        if (force || _loadedWebUrl != url)
        {
            _loadedWebUrl = url;
            Web.CoreWebView2.Navigate(url);
        }
    }

    private async void WebRefresh_Click(object sender, RoutedEventArgs e)
    {
        var button = (Button)sender;
        var source = _webSource;
        button.IsEnabled = false;
        try
        {
            await RefreshTargets();
            if (source != _webSource) return;
            if (Web.CoreWebView2 is not null && Web.Visibility == Visibility.Visible)
            {
                // Reload the current page while bypassing HTTP cache; keep cookies and login state.
                await Web.CoreWebView2.CallDevToolsProtocolMethodAsync("Page.reload", "{\"ignoreCache\":true}");
                StatusBar.Text = "已强制刷新页面";
            }
            else UpdateWebPage(force: true);
        }
        catch (Exception ex) { StatusBar.Text = "刷新失败：" + ex.Message; }
        finally { button.IsEnabled = true; }
    }

    private async void WebStart_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrEmpty(_webSource)) return;
        var source = _webSource;
        var action = _targets.TryGetValue(source, out var target) && target.Pid is not null ? "restart" : "start";
        await Send($"/v1/targets/{source}/{action}");
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
    private void SnowlumaWebui_Click(object sender, RoutedEventArgs e) => ShowSection("snowluma");

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

    // ---------------- 生命周期 ----------------

    private void Window_Closing(object sender, System.ComponentModel.CancelEventArgs e)
    {
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
