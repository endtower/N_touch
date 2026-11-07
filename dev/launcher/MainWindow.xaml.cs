using System.IO;
using System.Reflection;
using System.Text.Json;
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
    private string _webSource = "";      // "" | "core" | "connection:<id>"
    private readonly Dictionary<string, ManagerClient.Target> _targets = new();
    private bool _serviceConnected;
    private int _targetRefresh;
    private string _loadedWebUrl = "";
    private string _navigationError = "";
    private ulong _webNavigationId;
    private int _serviceEnsuring;
    private DateTime _consoleSessionRenewAt = DateTime.MinValue;
    private double? _managerStartedAt;
    /// <summary>上一次看到的隧道状态：只在「正常 → 断开」时提醒一次，避免反复弹。</summary>
    private bool? _tunnelHealthy;

    public MainWindow()
    {
        InitializeComponent();
        InitializeConnections();
        // 构建时间戳水印：publish 时由 csproj 写入程序集元数据，
        // 存在多份同名 EXE 时据此辨认当前运行的是哪一次构建。
        var buildStamp = typeof(MainWindow).Assembly
            .GetCustomAttributes<AssemblyMetadataAttribute>()
            .FirstOrDefault(attr => attr.Key == "BuildStamp")?.Value ?? "";
        if (buildStamp.Length > 0)
        {
            BuildStamp = buildStamp;
            BuildStampText.Text = "构建 " + buildStamp;
            Title = "Abyss 桌面启动管理器 · 构建 " + buildStamp;
        }
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
            ShowSection(string.IsNullOrWhiteSpace(startSection) || startSection == "updates" ? "overview" : startSection);
            UpdateLayout();               // 强制完成一次布局，保证快照内容完整
            ApplyCloseBehavior();         // 把设置里的"× 行为"反映到关闭键提示上
            if (IsSnapshotRun)
            {
                // 截图流程：先把数据拉齐（不初始化 WebView2 —— 它一旦合成，RenderTargetBitmap 就抓不到窗口内容），
                // 也不拉起管理服务（截图不该有副作用），出图后直接退出。
                StartPoll();
                StartLogPoll();
                StartUpdateChecks();
                StartStartupPoll();
                if (int.TryParse(Environment.GetEnvironmentVariable("ABYSS_LAUNCHER_SNAPSHOT_DELAY"), out var delay))
                    await Task.Delay(Math.Max(delay, 400));
                else
                    await Task.Delay(1500);
                // 诊断用：ABYSS_LAUNCHER_EXPAND 指定先展开哪一行的详情再出图
                if (Environment.GetEnvironmentVariable("ABYSS_LAUNCHER_EXPAND") is { Length: > 0 } expand)
                    ExpandRow(expand);
                UpdateLayout();
                await SaveSnapshotIfRequestedAsync();
                await SaveUpdateSnapshotIfRequested();
                if (Environment.GetEnvironmentVariable("ABYSS_UPDATES_EXIT_AFTER_SNAPSHOT") == "1") ExitForUpdate();
                return;
            }
            await EnsureServiceAsync();
            await InitWebView();
            StartPoll();
            StartLogPoll();
            StartUpdateChecks();
            StartStartupPoll();
            await SaveUpdateSnapshotIfRequested();
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
            if (info is null)
            {
                StatusBar.Text = "管理服务未运行，正在拉起…";
                System.IO.Directory.CreateDirectory(AppPaths.DataRoot());
                var psi = AppPaths.ManagerStartInfo();
                psi.WorkingDirectory = AppPaths.DataRoot();
                psi.CreateNoWindow = true; psi.UseShellExecute = false;
                psi.Environment["ABYSS_DATA_DIR"] = AppPaths.DataRoot();
                System.Diagnostics.Process.Start(psi);
                for (int i = 0; i < 20 && info is null; i++)
                {
                    await Task.Delay(500);
                    info = await _client.TryInfo();
                }
                if (info is null)
                {
                    StatusBar.Text = "管理服务拉起失败，可在日志里查看原因";
                    return;
                }
            }
            await EnsureIngressAsync();
        }
        catch (Exception ex)
        {
            StatusBar.Text = "管理服务启动失败：" + ex.Message;
        }
        finally
        {
            Interlocked.Exchange(ref _serviceEnsuring, 0);
        }
    }

    /// <summary>消息接入是常驻服务：启动器一起来就把它拉起来，Core 没运行时也持续收件。
    /// 旧管理服务没有独立接入目标时静默跳过（总览那一行会给出"版本过旧"的提示）。
    /// 截图/验收走的是只读流程，不做这个副作用。</summary>
    private async Task EnsureIngressAsync()
    {
        if (IsSnapshotRun) return;
        if (Bootstrap.Available && !File.Exists(Path.Combine(AppPaths.AppRoot(), "components", "core", "release.json")))
        {
            StatusBar.Text = "请选择 Core 和 SnowLuma 版本，确认下载并安装后即可启动";
            return;
        }
        var root = await _client.GetAsync("/v1/targets");
        var ingress = root.GetProperty("targets").EnumerateArray()
            .Select(ManagerClient.ParseTarget).FirstOrDefault(t => t.Name == "ingress");
        if (ingress is null || IsRunning(ingress)) return;
        var result = await _client.PostAsync("/v1/targets/ingress/start");
        if (result.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.False)
            StatusBar.Text = "消息接入未能常驻："
                + (result.TryGetProperty("message", out var message) ? message.GetString() : "未知原因");
    }

    private static bool IsSnapshotRun =>
        !string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("ABYSS_UPDATES_SNAPSHOT"))
        || !string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("ABYSS_LAUNCHER_SNAPSHOT"));

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
            ManagerClient.Target? core = null, snow = null, ingress = null;
            foreach (var t in root.GetProperty("targets").EnumerateArray())
            {
                var parsed = ManagerClient.ParseTarget(t);
                if (_targets.TryGetValue(parsed.Name, out var previous)
                    && previous.Pid != parsed.Pid && CurrentManagedTarget() == parsed.Name && parsed.Name != "core")
                {
                    _loadedWebUrl = "";
                    _navigationError = "";
                }
                _targets[parsed.Name] = parsed;
                if (parsed.Name == "core") core = parsed;
                if (parsed.Name == "snowluma") snow = parsed;
                if (parsed.Name == "ingress") ingress = parsed;
            }
            _ = core; _ = snow; _ = ingress;   // 三者的显示合并进 RenderRows，这里只保证字典已更新
            ApplyTunnelState(_targets.TryGetValue("tunnel", out var tunnel) ? tunnel : null);
            _serviceConnected = true;
            RenderRows();
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
        }
        catch (Exception)
        {
            _serviceConnected = false;
            _consoleSessionRenewAt = DateTime.MinValue;
            RenderRows();
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
        if (tag == "snowluma") tag = "connection:snowluma";
        var isWeb = tag == "coreweb" || tag.StartsWith("connection:", StringComparison.Ordinal);
        OverviewPanel.Visibility = tag == "overview" ? Visibility.Visible : Visibility.Collapsed;
        WebPanel.Visibility = isWeb ? Visibility.Visible : Visibility.Collapsed;
        LogsPanel.Visibility = tag == "logs" ? Visibility.Visible : Visibility.Collapsed;
        ConnectionsPanel.Visibility = tag == "connections" ? Visibility.Visible : Visibility.Collapsed;

        // 只有「日志」还用得到二级侧栏；总览的服务状态已并进组件清单，整列收 0 把宽度让给内容。
        SideColumn.Width = tag == "logs" ? new GridLength(236) : new GridLength(0);
        SideLogs.Visibility = tag == "logs" ? Visibility.Visible : Visibility.Collapsed;

        // 页面名与状态在状态栏里，非网页区块要清掉，免得留着上一次的内容
        if (!isWeb)
        {
            WebPageTitle.Text = "";
            WebPageState.Text = "";
        }

        // 标题栏的刷新键只对网页区块有意义，其余区块置灰
        BtnRefreshWeb.IsEnabled = isWeb;
        BtnRefreshWeb.ToolTip = isWeb ? "刷新当前网页" : "刷新（切到网页后可用）";

        // 选中态交给 IsChecked 的触发器，颜色不在代码里写死
        foreach (var b in new[] { NavOverview, NavCoreWeb, NavConnections, NavLogs }
            .Concat(ConnectionNav.Children.OfType<RadioButton>()))
            b.IsChecked = b.Tag?.ToString() == tag;

        if (tag == "coreweb") ShowWeb("core");
        if (tag.StartsWith("connection:", StringComparison.Ordinal)) ShowWeb(tag);
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
        if (WebPanel.Visibility != Visibility.Visible) return;
        if (_webSource.StartsWith("connection:", StringComparison.Ordinal))
        {
            UpdateConnectionWebPage(force);
            return;
        }
        var isConsole = _webSource == "core";
        var name = isConsole ? "管理" : "SnowLuma";
        WebPageTitle.Text = name + " 页面";
        _targets.TryGetValue(_webSource, out var target);
        var ready = _serviceConnected && (isConsole || (target?.State == "running" && target.Ready));
        WebPageState.Text = !_serviceConnected ? "管理服务未连接" : isConsole
            ? (target?.Ready == true ? "管理页面可用 · Core 运行中"
                : target?.State == "starting" ? "管理页面可用 · Core 正在启动"
                : target?.State == "stopping" ? "管理页面可用 · Core 正在停止"
                : target?.Pid is not null ? "管理页面可用 · Core 已启动，尚未就绪"
                : "管理页面可用 · Core 已停止") : target?.State switch
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
        var source = CurrentManagedTarget();
        if (string.IsNullOrEmpty(source)) return;
        if (source == "core" && !await EnsureCoreSetup()) return;
        var action = _targets.TryGetValue(source, out var target) && target.Pid is not null ? "restart" : "start";
        await Send($"/v1/targets/{source}/{action}");
    }

    // ---------------- 控制动作 ----------------

    /// <summary>受管目标是否处于"已起来"的状态。</summary>
    private static bool IsRunning(ManagerClient.Target target) => target.Pid is not null || target.State is "running" or "starting";

    // ---------------- Pi 隧道 ----------------

    /// <summary>隧道是否端到端可用：进程在跑、端口在听、Pi 端桥接也响应。</summary>
    private static bool TunnelHealthy(ManagerClient.Target? tunnel) =>
        tunnel is not null && tunnel.State == "running" && tunnel.Ready;

    /// <summary>刷新总览里的 Pi 隧道行与网页区状态条；状态由「通」变「断」时弹一次托盘气泡。
    /// 行的显示细节由 RenderRows 统一画，这里只负责隧道自身的登录态与断线提醒。</summary>
    private void ApplyTunnelState(ManagerClient.Target? tunnel)
    {
        if (tunnel is null)
        {
            _tunnelHealthy = null;
            UpdateTunnelBar(null);
            return;
        }

        var healthy = TunnelHealthy(tunnel);
        if (_tunnelHealthy == true && !healthy)
            App.Notify("Pi 隧道已断开", "微信收发会失效。打开启动器后点「重新启动」，或在 Pi 页面顶部重连。");
        _tunnelHealthy = healthy;

        UpdateTunnelBar(tunnel);
    }

    /// <summary>网页区顶部的隧道状态条：只在当前入口是 tunnel 时出现。</summary>
    private void UpdateTunnelBar(ManagerClient.Target? tunnel)
    {
        var entry = CurrentConnection();
        var isTunnel = entry?.ManagedTarget == "tunnel";
        TunnelBar.Visibility = isTunnel ? Visibility.Visible : Visibility.Collapsed;
        if (!isTunnel) return;

        if (tunnel is null)
        {
            SetTheme(TunnelBarDot, System.Windows.Shapes.Shape.FillProperty, "BrushTagDanger");
            TunnelBarText.Text = "未检测到隧道配置：请在系统设置里填写 Pi 地址、用户名与密钥路径，并重启管理服务。";
            TunnelBarAction.Content = "查看说明";
            TunnelBarAction.Tag = "";
            TunnelBarAction.IsEnabled = true;
            return;
        }

        var healthy = TunnelHealthy(tunnel);
        SetTheme(TunnelBarDot, System.Windows.Shapes.Shape.FillProperty,
            healthy ? "BrushTagSuccess" : tunnel.State == "starting" ? "BrushTagWarning" : "BrushTagDanger");
        TunnelBarAction.Tag = "tunnel";
        TunnelBarAction.IsEnabled = true;
        if (healthy)
        {
            TunnelBarText.Text = "隧道已连接。画面显示的是 Pi 桌面，与消息通道是两条不同的链路。";
            TunnelBarAction.Content = "断开";
        }
        else if (tunnel.State == "starting")
        {
            TunnelBarText.Text = "正在建立隧道…";
            TunnelBarAction.Content = "连接中";
            TunnelBarAction.IsEnabled = false;
        }
        else
        {
            TunnelBarText.Text = "隧道未连接，微信消息发不出去。"
                + (string.IsNullOrEmpty(tunnel.LastError) ? "" : "原因：" + tunnel.LastError);
            TunnelBarAction.Content = tunnel.State == "running" ? "重新连接" : "连接 Pi";
        }
    }

    private async void TunnelBarAction_Click(object sender, RoutedEventArgs e)
    {
        if (TunnelBarAction.Tag?.ToString() != "tunnel")
        {
            StatusBar.Text = "请先在系统设置的平台接入里配置 Pi 隧道";
            return;
        }
        var action = TunnelHealthy(_targets.TryGetValue("tunnel", out var t) ? t : null) ? "stop" : "restart";
        await Send($"/v1/targets/tunnel/{action}");
    }

    // ---------------- 状态灯 ----------------

    private readonly HashSet<System.Windows.Shapes.Ellipse> _pulsingDots = new();

    /// <summary>状态色：运行绿，过渡态（启动中/停止中）橙，失败红；未安装/已停止用中性灰，
    /// 免得整页都是红点、看起来像所有东西都坏了。</summary>
    private static string DotBrush(string state) => state switch
    {
        "running" => "BrushTagSuccess",
        "starting" or "stopping" => "BrushTagWarning",
        "failed" or "conflict" => "BrushTagDanger",
        _ => "BrushTextDisabled",
    };

    /// <summary>给状态灯上色；过渡态额外做呼吸灯，提示"正在开启/正在关闭"这类事件还在进行。</summary>
    private void ApplyDot(System.Windows.Shapes.Ellipse dot, string state)
    {
        SetTheme(dot, System.Windows.Shapes.Shape.FillProperty, DotBrush(state));
        if (state is "starting" or "stopping") StartPulse(dot);
        else StopPulse(dot);
    }

    private void StartPulse(System.Windows.Shapes.Ellipse dot)
    {
        if (!_pulsingDots.Add(dot)) return;   // 已经在呼吸就不要重启动画，免得每轮轮询都跳一下
        dot.BeginAnimation(OpacityProperty, new DoubleAnimation(1, 0.2, TimeSpan.FromMilliseconds(620))
        {
            AutoReverse = true,
            RepeatBehavior = RepeatBehavior.Forever,
            EasingFunction = new SineEase { EasingMode = EasingMode.EaseInOut },
        });
    }

    private void StopPulse(System.Windows.Shapes.Ellipse dot)
    {
        if (!_pulsingDots.Remove(dot)) return;
        dot.BeginAnimation(OpacityProperty, null);
        dot.Opacity = 1;
    }

    private bool TargetRunning(string name) => _targets.TryGetValue(name, out var target) && IsRunning(target);

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

    /// <summary>行右侧操作区：内部按钮/下拉已处理的点击到这里就停了；
    /// 点操作区空白处也不该触发整行的展开/收起。</summary>
    private void RowActions_Click(object sender, MouseButtonEventArgs e) => e.Handled = true;

    private async Task<bool> EnsureCoreSetup()
    {
        if (!File.Exists(Path.Combine(AppPaths.AppRoot(), "components", "core", "release.json"))) return true;
        try
        {
            var config = await _client.GetAsync("/v1/config/core");
            var fields = config.GetProperty("fields");
            var ownerSet = fields.TryGetProperty("MASTER_ID", out var owner) && long.TryParse(owner.GetString(), out var number) && number > 0;
            var keySet = fields.TryGetProperty("FLASH_API_KEY_SET", out var configured) && configured.GetBoolean();
            if (ownerSet && keySet) return true;
            var dialog = new CoreSetupDialog(config) { Owner = this };
            if (dialog.ShowDialog() != true) return false;
            var saved = await _client.PutAsync("/v1/config/core", new { fields = dialog.Fields });
            if (!saved.TryGetProperty("saved", out var ok) || !ok.GetBoolean())
                throw new InvalidOperationException(saved.TryGetProperty("message", out var message) ? message.GetString() : "配置未保存");
            return true;
        }
        catch (Exception error) { StatusBar.Text = "首次设置未完成：" + error.Message; return false; }
    }

    private void TargetStop_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.Tag?.ToString() is string name && name.Length > 0)
            _ = Send($"/v1/targets/{name}/stop");
    }

    private async void StartAll_Click(object sender, RoutedEventArgs e)
    {
        if (!await EnsureCoreSetup()) return;
        await RunStartup("start");
    }

    /// <summary>消息接入没有启动链路，只有目标自身的启停；按钮文案随状态在 RenderRows 里算。</summary>
    private async void IngressAction_Click(object sender, RoutedEventArgs e)
    {
        var running = TargetRunning("ingress");
        await Send($"/v1/targets/ingress/{(running ? "stop" : "start")}");
    }

    private async void StopAll_Click(object sender, RoutedEventArgs e)
    {
        await RunStartup("stop");
    }

    // ---------------- 生命周期 ----------------

    private void Window_Closing(object sender, System.ComponentModel.CancelEventArgs e)
    {
        if (_exitConfirmed) return;
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
            try
            {
                var result = await _client.PostAsync("/v1/shutdown");
                if (result.TryGetProperty("ok", out var ok) && ok.ValueKind == System.Text.Json.JsonValueKind.False)
                {
                    Dispatcher.Invoke(() =>
                    {
                        _exitConfirmed = false;
                        StatusBar.Text = result.TryGetProperty("message", out var message) ? message.GetString() : "暂时无法退出";
                    });
                    return;
                }
            }
            catch { }
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
