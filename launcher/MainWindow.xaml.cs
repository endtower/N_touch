using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media.Animation;
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
    private DateTime _consoleSessionRenewAt = DateTime.MinValue;
    private double? _managerStartedAt;

    public MainWindow()
    {
        InitializeComponent();
        ThemeManager.ThemeChanged += OnThemeChanged;
        UpdateThemeIcon();
        UpdateMaximizeGlyph();
        StateChanged += (_, _) => UpdateMaximizeGlyph();
        // 窗口句柄要等 SourceInitialized 之后才有。WindowStyle=None 之后已经没有系统标题栏，
        // 这个调用留着是为了窗口外框/阴影也能跟主题走。
        SourceInitialized += (_, _) => ApplyTitleBarTheme();
        Loaded += async (_, _) =>
        {
            // 诊断用：ABYSS_LAUNCHER_SECTION 可指定启动停在哪个区块（默认 overview）。
            // 配合 ABYSS_LAUNCHER_SNAPSHOT 就能对每个区块单独出一张图来目检。
            var startSection = Environment.GetEnvironmentVariable("ABYSS_LAUNCHER_SECTION");
            ShowSection(string.IsNullOrWhiteSpace(startSection) ? "overview" : startSection);
            UpdateLayout();               // 强制完成一次布局，保证快照内容完整
            ApplyCloseBehavior();         // 把设置里的"× 行为"反映到关闭键提示上
            await SaveSnapshotIfRequestedAsync();
            await EnsureServiceAsync();
            await InitWebView();
            StartPoll();
            StartLogPoll();
        };
        Closed += (_, _) => ThemeManager.ThemeChanged -= OnThemeChanged;
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
                ServiceStateText.Text = "管理服务运行中";
                SetTheme(ServiceStateText, TextBlock.ForegroundProperty, "BrushTagSuccess");
                return;
            }
            StatusBar.Text = "管理服务未运行，正在拉起…";
            var exe = AppPaths.ServiceExe();
            var psi = exe is not null
                ? new System.Diagnostics.ProcessStartInfo(exe) { WorkingDirectory = AppPaths.DataRoot() }
                : new System.Diagnostics.ProcessStartInfo("python",
                    $"\"{Path.Combine(AppPaths.AppRoot(), "manager_service.py")}\"")
                { WorkingDirectory = AppPaths.DataRoot() };
            psi.CreateNoWindow = true; psi.UseShellExecute = false;
            psi.Environment["ABYSS_DATA_DIR"] = AppPaths.DataRoot();
            System.Diagnostics.Process.Start(psi);
            for (int i = 0; i < 20; i++)
            {
                await Task.Delay(500);
                info = await _client.TryInfo();
                if (info is not null)
                {
                    ServiceStateText.Text = "管理服务运行中（本轮自动拉起）";
                    SetTheme(ServiceStateText, TextBlock.ForegroundProperty, "BrushTagSuccess");
                    return;
                }
            }
            ServiceStateText.Text = "管理服务拉起失败";
            SetTheme(ServiceStateText, TextBlock.ForegroundProperty, "BrushTagDanger");
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
            if (root.TryGetProperty("service_started_at", out var started))
            {
                var startedAt = started.GetDouble();
                if (_managerStartedAt != startedAt)
                {
                    _managerStartedAt = startedAt;
                    _consoleSessionRenewAt = DateTime.MinValue;
                    _loadedWebUrl = "";
                    _navigationError = "";
                }
            }
            ServiceStateText.Text = "管理服务运行中";
            SetTheme(ServiceStateText, TextBlock.ForegroundProperty, "BrushTagSuccess");
            ManagerClient.Target? core = null, snow = null, ingress = null;
            foreach (var t in root.GetProperty("targets").EnumerateArray())
            {
                var parsed = ManagerClient.ParseTarget(t);
                if (_targets.TryGetValue(parsed.Name, out var previous)
                    && previous.Pid != parsed.Pid && _webSource == parsed.Name && parsed.Name != "core")
                {
                    _loadedWebUrl = "";
                    _navigationError = "";
                }
                _targets[parsed.Name] = parsed;
                if (parsed.Name == "core") core = parsed;
                if (parsed.Name == "snowluma") snow = parsed;
                if (parsed.Name == "ingress") ingress = parsed;
            }
            if (core is not null)
            {
                CoreStateText.Text = core.State;
                SetTheme(CoreStateText, TextBlock.ForegroundProperty,
                    core.State == "running" ? "BrushTagSuccess" : "BrushTagDanger");
                CoreDetail.Text = $"pid {core.Pid?.ToString() ?? "—"} · 端口 {string.Join("/", core.Ports)} · "
                    + $"运行 {core.UptimeSec?.ToString("0") ?? "—"}s · "
                    + $"业务{(core.Ready ? "就绪" : "未就绪")}"
                    + (string.IsNullOrEmpty(core.LastError) ? "" : $"\n{core.LastError}");
            }
            if (ingress is not null)
            {
                IngressStateText.Text = ingress.State;
                SetTheme(IngressStateText, TextBlock.ForegroundProperty,
                    ingress.Ready ? "BrushTagSuccess" : "BrushTagDanger");
                var connection = ingress.OneBotConnected is true ? "SnowLuma 已连接"
                    : ingress.OneBotConnected is false ? "等待 SnowLuma 连接" : "连接状态未知";
                IngressDetail.Text = $"{ingress.State} · pid {ingress.Pid?.ToString() ?? "—"} · "
                    + $"端口 {string.Join("/", ingress.Ports)} · {connection}"
                    + (string.IsNullOrEmpty(ingress.LastError) ? "" : $"\n{ingress.LastError}");
            }
            else
            {
                IngressStateText.Text = "管理服务版本过旧";
                IngressDetail.Text = "请从旧启动器选择退出全部，再打开新版；旧服务没有独立消息接入。";
            }
            _serviceConnected = true;
            if (Web.CoreWebView2 is not null && DateTime.UtcNow >= _consoleSessionRenewAt)
            {
                var session = await _client.GetAsync("/v1/console-session");
                var cookie = Web.CoreWebView2.CookieManager.CreateCookie(
                    "abyss_console_session", session.GetProperty("session").GetString()!, "127.0.0.1", "/");
                cookie.IsHttpOnly = true;
                cookie.SameSite = CoreWebView2CookieSameSiteKind.Strict;
                cookie.Expires = DateTime.UtcNow.AddSeconds(session.GetProperty("max_age").GetInt32());
                Web.CoreWebView2.CookieManager.AddOrUpdateCookie(cookie);
                _consoleSessionRenewAt = DateTime.UtcNow.AddHours(8);
            }
            UpdateWebPage();
            if (snow is not null)
            {
                SnowlumaStateText.Text = snow.State;
                SetTheme(SnowlumaStateText, TextBlock.ForegroundProperty,
                    snow.State == "running" ? "BrushTagSuccess" : "BrushTagDanger");
                SnowlumaDetail.Text = $"pid {snow.Pid?.ToString() ?? "—"} · 端口 {string.Join("/", snow.Ports)}"
                    + (string.IsNullOrEmpty(snow.LastError) ? "" : $"\n{snow.LastError}");
            }
        }
        catch (Exception)
        {
            _serviceConnected = false;
            _consoleSessionRenewAt = DateTime.MinValue;
            ServiceStateText.Text = "与服务失去连接";
            SetTheme(ServiceStateText, TextBlock.ForegroundProperty, "BrushTagDanger");
            UpdateWebPage();
        }
        finally { Interlocked.Exchange(ref _targetRefresh, 0); }
    }

    /// <summary>把 LogPresentation 给的十六进制色（ANSI 日志内容色）转成画刷。</summary>
    private static System.Windows.Media.SolidColorBrush Brush(string hex) =>
        (System.Windows.Media.SolidColorBrush)new System.Windows.Media.BrushConverter().ConvertFromString(hex)!;

    /// <summary>
    /// 给控件挂"主题资源引用"，等价于 XAML 里的 DynamicResource。
    /// 运行时换主题会自动重绘；直接赋 Brush 会停在旧主题的颜色上。
    /// 元素与文档内容（Run / Paragraph）都支持，所以按类型分派。
    /// </summary>
    private static void SetTheme(DependencyObject target, DependencyProperty property, string key)
    {
        switch (target)
        {
            case FrameworkElement element:
                element.SetResourceReference(property, key);
                break;
            case FrameworkContentElement content:
                content.SetResourceReference(property, key);
                break;
        }
    }

    // ---------------- 导航 ----------------

    private void Nav_Click(object sender, RoutedEventArgs e)
    {
        // sender 现在是图标轨道上的 RadioButton，不再一定是 Button
        var tag = (sender as FrameworkElement)?.Tag?.ToString();
        ShowSection(tag ?? "overview");
    }

    private void ShowSection(string tag)
    {
        var isWeb = tag is "coreweb" or "snowluma";
        OverviewPanel.Visibility = tag == "overview" ? Visibility.Visible : Visibility.Collapsed;
        WebPanel.Visibility = isWeb ? Visibility.Visible : Visibility.Collapsed;
        LogsPanel.Visibility = tag == "logs" ? Visibility.Visible : Visibility.Collapsed;

        // 只有「总览」和「日志」用得到二级侧栏。
        // 页面区块把整列收成 0，宽度全让给网页 —— 页面信息与刷新改放主区自己那条窄栏。
        SideColumn.Width = isWeb ? new GridLength(0) : new GridLength(236);
        SideStatus.Visibility = tag == "overview" ? Visibility.Visible : Visibility.Collapsed;
        SideLogs.Visibility = tag == "logs" ? Visibility.Visible : Visibility.Collapsed;

        // 页面名与状态在状态栏里，非网页区块要清掉，免得留着上一次的内容
        if (!isWeb)
        {
            WebPageTitle.Text = "";
            WebPageState.Text = "";
        }

        // 标题栏的刷新键只对网页区块有意义，其余区块置灰
        BtnRefreshWeb.IsEnabled = isWeb;
        BtnRefreshWeb.ToolTip = isWeb ? "刷新当前网页" : "刷新（切到 Core / SnowLuma 页面后可用）";

        // 选中态交给 IsChecked 的触发器，颜色不在代码里写死
        foreach (var b in new[] { NavOverview, NavCoreWeb, NavSnowluma, NavLogs })
            b.IsChecked = b.Tag?.ToString() == tag;

        if (tag == "coreweb") ShowWeb("core");
        if (tag == "snowluma") ShowWeb("snowluma");
        if (tag == "logs") _ = RefreshLogs();
    }

    // ---------------- 自绘标题栏 ----------------

    private void Minimize_Click(object sender, RoutedEventArgs e) => WindowState = WindowState.Minimized;

    private void MaximizeRestore_Click(object sender, RoutedEventArgs e) =>
        WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;

    /// <summary>关闭键走窗口既有的关闭流程：按任务书 §6，默认隐藏到托盘、后台继续运行。</summary>
    private void Close_Click(object sender, RoutedEventArgs e) => Close();

    private void UpdateMaximizeGlyph()
    {
        if (TryFindResource(WindowState == WindowState.Maximized ? "GlyphRestore" : "GlyphMaximize")
            is System.Windows.Media.Geometry glyph)
            MaximizeGlyph.Data = glyph;
        BtnMaximize.ToolTip = WindowState == WindowState.Maximized ? "还原" : "最大化";
    }

    /// <summary>
    /// 诊断用截图：设置 ABYSS_LAUNCHER_SNAPSHOT=&lt;png 路径&gt; 后启动，
    /// 窗口完成首次布局后会把自身用 WPF 合成一张 PNG 写到该路径（程序继续运行，由调用方结束）。
    /// 另可用 ABYSS_LAUNCHER_SNAPSHOT_DELAY（毫秒）延后抓图 ——
    /// 用来验证悬停这类需要外部先操作、后观察的状态。
    ///
    /// 为什么需要它：系统截屏 / PrintWindow 抓这个窗口不可靠 ——
    /// 实测内嵌 WebView2 的区域会抓成错乱内容，轨道底部的按钮会整块丢失，
    /// 看起来像"控件根本没渲染"，容易把人带到错误的排查方向。
    /// RenderTargetBitmap 拿到的是 WPF 真实合成结果，是唯一可信的目检方式。
    ///
    /// 注意时机：必须在 WebView2 初始化**之前**调用（调用方在这里 await，
    /// 就是为了保证延时不会把它推到 WebView2 之后）。WebView2 起来之后再
    /// RenderTargetBitmap 会抛异常（WebView2 的合成不参与 WPF 渲染管线）。
    /// </summary>
    private async Task SaveSnapshotIfRequestedAsync()
    {
        var path = Environment.GetEnvironmentVariable("ABYSS_LAUNCHER_SNAPSHOT");
        if (string.IsNullOrWhiteSpace(path)) return;

        if (int.TryParse(Environment.GetEnvironmentVariable("ABYSS_LAUNCHER_SNAPSHOT_DELAY"), out var delay)
            && delay > 0)
        {
            await Task.Delay(delay);
        }

        try
        {
            var rtb = new System.Windows.Media.Imaging.RenderTargetBitmap(
                (int)ActualWidth, (int)ActualHeight, 96, 96, System.Windows.Media.PixelFormats.Pbgra32);
            rtb.Render(this);
            var encoder = new System.Windows.Media.Imaging.PngBitmapEncoder();
            encoder.Frames.Add(System.Windows.Media.Imaging.BitmapFrame.Create(rtb));
            using var stream = File.Create(path);
            encoder.Save(stream);
        }
        catch (Exception ex)
        {
            // 诊断失败不影响启动，但要把原因写出来 —— 静默失败会让人误以为"没渲染"
            try { File.WriteAllText(path + ".err", ex.ToString()); } catch { }
        }
    }

    // ---------------- 主题 ----------------

    private void ThemeToggle_Click(object sender, RoutedEventArgs e) => ThemeManager.Toggle();

    private void OnThemeChanged(string theme)
    {
        UpdateThemeIcon();
        ApplyTitleBarTheme();   // 原生标题栏也要跟着变，否则深色界面顶着一条白条
        PushThemeToWeb();       // 主题变了要把新值推给内嵌的自家页面
        RenderLogs();           // 日志正文的默认字色来自主题，要重排一次
    }

    // ---------------- 标题栏跟随主题 ----------------

    [System.Runtime.InteropServices.DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

    /// <summary>
    /// 让原生标题栏跟随主题（Win10 1809+）。
    /// 属性号在新版是 20，1809~1903 是 19；先试 20，不认再退回 19。
    /// </summary>
    private void ApplyTitleBarTheme()
    {
        var handle = new System.Windows.Interop.WindowInteropHelper(this).Handle;
        if (handle == IntPtr.Zero) return;
        int dark = ThemeManager.Current == ThemeManager.Dark ? 1 : 0;
        if (DwmSetWindowAttribute(handle, 20, ref dark, sizeof(int)) != 0)
            DwmSetWindowAttribute(handle, 19, ref dark, sizeof(int));
    }

    private void UpdateThemeIcon()
    {
        var dark = ThemeManager.Current == ThemeManager.Dark;
        // 图标显示"点了会切到哪"：暗色时显示太阳。
        // 用 TryFindResource 而不是 FindResource：构造期资源链可能还没通，
        // 取不到就保留 XAML 里给的默认图标，不要让按钮变成隐形的。
        if (TryFindResource(dark ? "IconSun" : "IconMoon") is System.Windows.Media.Geometry icon)
            ThemeIcon.Data = icon;
        ThemeToggle.ToolTip = dark ? "切换到浅色" : "切换到深色";
    }

    /// <summary>
    /// 把当前主题推给内嵌网页。
    ///
    /// 只对自家 Core 页开放，且消息体里只有一个主题名 ——
    /// 不含管理接口、访问令牌或任何执行能力，符合任务书 §7
    /// 「不向第三方网页注入管理接口、访问令牌或任意宿主执行能力」。
    /// SnowLuma 是第三方页面，_webSource 会把它挡在门外。
    /// </summary>
    private void PushThemeToWeb()
    {
        if (Web.CoreWebView2 is null || _webSource != "core") return;
        try
        {
            Web.CoreWebView2.PostWebMessageAsJson(
                $"{{\"type\":\"abyss-theme\",\"value\":\"{ThemeManager.Current}\"}}");
        }
        catch
        {
            // 页面还没准备好接收就跳过；导航完成时会再推一次
        }
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
                    return;
                }
                PushThemeToWeb();   // 导航完成后再推一次，避免页面加载前丢消息
            };

            // 主题联动的回程：只认一条白名单消息，其余一律忽略。
            // 只在自家 Core 页生效；SnowLuma 是第三方页，任务书 §7 不许给它挂宿主桥接。
            Web.CoreWebView2.WebMessageReceived += (_, e) =>
            {
                if (_webSource != "core") return;
                try
                {
                    using var doc = System.Text.Json.JsonDocument.Parse(e.WebMessageAsJson);
                    var root = doc.RootElement;
                    if (root.TryGetProperty("type", out var t) && t.GetString() == "abyss-theme"
                        && root.TryGetProperty("value", out var v)
                        && v.GetString() is string theme
                        && (theme == ThemeManager.Dark || theme == ThemeManager.Light))
                    {
                        ThemeManager.Apply(theme);
                    }
                }
                catch
                {
                    // 非白名单或畸形消息：忽略，不影响宿主
                }
            };
            // 管理令牌只在原生 HTTP 客户端使用；网页获得独立的 HttpOnly 会话。
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
        PushThemeToWeb();   // 从 SnowLuma 切回 Core 时也要把主题补上
    }

    private void UpdateWebPage(bool force = false)
    {
        if (string.IsNullOrEmpty(_webSource)) return;
        var isConsole = _webSource == "core";
        var name = isConsole ? "管理" : "SnowLuma";
        WebPageTitle.Text = name + " 页面";
        _targets.TryGetValue(_webSource, out var target);
        var ready = _serviceConnected && (isConsole || (target?.State == "running" && target.Ready));
        WebPageState.Text = !_serviceConnected ? "管理服务未连接" : isConsole
            ? (target?.Ready == true ? "管理页面可用 · Core 运行中" : "管理页面可用 · Core 未运行") : target?.State switch
        {
            "running" => "运行中", "starting" => "正在启动", "stopping" => "正在停止",
            "failed" => "运行失败", "conflict" => "端口被占用", _ => "尚未启动"
        };
        WebStartButton.Visibility = !isConsole && _serviceConnected && target is not null
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
        var port = target?.Ports.FirstOrDefault() ?? 5099;
        if (port <= 0) port = 5099;
        var url = isConsole ? AppPaths.ServiceBase() + "/" : $"http://127.0.0.1:{port}/";
        WebPageTitle.ToolTip = url;
        if (force || _loadedWebUrl != url)
        {
            _loadedWebUrl = url;
            Web.CoreWebView2.Navigate(url);
        }
    }

    // ---------------- 刷新网页 ----------------

    /// <summary>
    /// 刷新当前正在显示的网页。标题栏那个刷新按钮直接调它 ——
    /// 目标就是 <see cref="_webSource"/>，不需要任何额外状态。
    /// </summary>
    private async void RefreshWeb_Click(object sender, RoutedEventArgs e)
    {
        var source = _webSource;
        if (string.IsNullOrEmpty(source)) return;

        var button = sender as Button;
        if (button is not null) button.IsEnabled = false;
        try
        {
            await RefreshTargets();
            if (source != _webSource) return;   // 期间切走了就不刷了
            if (Web.CoreWebView2 is not null && Web.Visibility == Visibility.Visible)
            {
                // 绕开 HTTP 缓存重载当前页；保留 cookie 与登录态
                await Web.CoreWebView2.CallDevToolsProtocolMethodAsync("Page.reload", "{\"ignoreCache\":true}");
                StatusBar.Text = "已强制刷新页面";
            }
            else UpdateWebPage(force: true);
        }
        catch (Exception ex) { StatusBar.Text = "刷新失败：" + ex.Message; }
        finally { if (button is not null) button.IsEnabled = true; }
    }

    // ---------------- 设置 ----------------

    private void Settings_Click(object sender, RoutedEventArgs e) =>
        SettingsPopup.IsOpen = !SettingsPopup.IsOpen;

    private void CloseBehavior_Changed(object sender, RoutedEventArgs e)
    {
        // 窗口加载过程中初始化勾选也会触发，挡掉那一次
        if (!IsLoaded) return;
        UiSettings.CloseToTray = SetCloseToTray.IsChecked == true;
        UiSettings.Save();
        ApplyCloseBehavior();
    }

    /// <summary>把设置里的"× 行为"反映到界面上（关闭键的提示文案）。</summary>
    private void ApplyCloseBehavior()
    {
        SetCloseToTray.IsChecked = UiSettings.CloseToTray;
        SetCloseExit.IsChecked = !UiSettings.CloseToTray;
        BtnClose.ToolTip = UiSettings.CloseToTray
            ? "关闭（隐藏到托盘，后台继续运行）"
            : "关闭（退出程序，停止 Core 与 SnowLuma）";
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
        if (_targets.Count > 0 && !_targets.ContainsKey("ingress")
            && (path.EndsWith("/start") || path.EndsWith("/restart")))
        {
            StatusBar.Text = "旧管理服务不支持独立接入。请先从旧启动器退出全部，再打开新版。";
            return;
        }
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
    private void IngressStart_Click(object sender, RoutedEventArgs e) => _ = Send("/v1/targets/ingress/start");
    private void IngressStop_Click(object sender, RoutedEventArgs e) => _ = Send("/v1/targets/ingress/stop");
    private void IngressRestart_Click(object sender, RoutedEventArgs e) => _ = Send("/v1/targets/ingress/restart");
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
        await Send("/v1/targets/core/stop");
        await Send("/v1/targets/snowluma/stop");
        await Send("/v1/targets/ingress/stop");
    }

    // ---------------- 生命周期 ----------------

    private void Window_Closing(object sender, System.ComponentModel.CancelEventArgs e)
    {
        if (!UiSettings.CloseToTray)
        {
            // 设置成"退出程序"：走既有的停止全部并退出流程。
            // 先取消本次关闭，让 RequestExit 去弹确认并收尾；确认后 ReallyExit → Shutdown
            // 会再次触发 Closing，那时 _exitConfirmed 为 true，下面的分支会放行。
            if (!_exitConfirmed)
            {
                e.Cancel = true;
                RequestExit(confirm: false);   // 设置里已明确选了"退出程序"，不再二次确认
            }
            return;
        }

        // 默认行为：隐藏到托盘，受管目标继续运行（任务书 §6）
        e.Cancel = true;
        HideToTray();
    }

    internal void HideToTray() => Visibility = Visibility.Hidden;

    private bool _exitConfirmed;

    /// <param name="confirm">
    /// 是否先弹确认框。托盘菜单那条路要确认；
    /// 标题栏 × 在"设置为退出程序"时不确认 —— 用户已经在设置里明确选了，
    /// 再弹一次就是每次关窗都被拦。
    /// </param>
    internal void RequestExit(bool confirm = true)
    {
        if (_exitConfirmed) { ((App)Application.Current).ReallyExit(); return; }
        _exitConfirmed = true;

        if (confirm)
        {
            var answer = MessageBox.Show(
                "退出将停止 Core、SnowLuma 与消息接入服务。确认停止全部并退出？",
                "退出确认", MessageBoxButton.OKCancel, MessageBoxImage.Question);
            if (answer != MessageBoxResult.OK)
            {
                _exitConfirmed = false;
                return;
            }
        }

        _ = Task.Run(async () =>
        {
            try { await _client.PostAsync("/v1/shutdown"); } catch { }
            Dispatcher.Invoke(() => ((App)Application.Current).ReallyExit());
        });
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
