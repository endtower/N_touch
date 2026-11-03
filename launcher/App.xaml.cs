using System.IO;
using System.Windows;
using System.Windows.Threading;

namespace AbyssLauncher;

/// <summary>应用入口：单实例互斥 + WinForms 托盘（WPF 是唯一窗口/托盘所有者）。</summary>
public partial class App : Application
{
    private static System.Windows.Forms.NotifyIcon? _tray;
    private static MainWindow? _main;

    private static EventWaitHandle? _wake;

    protected override void OnStartup(StartupEventArgs e)
    {
        // GUI 程序没有控制台：未处理异常必须落盘，否则现象只是"窗口没出来"，
        // 完全看不到原因。写 <数据根>/launcher_crash.log。
        DispatcherUnhandledException += (_, args) => LogFatal(args.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, args) => LogFatal(args.ExceptionObject as Exception);

        try
        {
            Bootstrap.EnsureInstalled();
            if (Bootstrap.LaunchInstalled(e.Args)) { Shutdown(0); return; }
        }
        catch (Exception error)
        {
            MessageBox.Show("首次安装未完成：" + error.Message, "Abyss", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown(1); return;
        }

        // 单实例 + 唤醒（V01）：命名 EventWaitHandle 兼任互斥与唤醒信号。
        // 重复双击：已有实例收到信号后把窗口带回前台。
        //
        // 互斥名可用 ABYSS_LAUNCHER_MUTEX 覆盖：这样能在正式实例仍在运行时
        // 起一个隔离实例做冒烟检查（配合 ABYSS_DATA_DIR 隔离 WebView2 数据目录），
        // 两个实例不会互相唤醒。
        var wakeName = Environment.GetEnvironmentVariable("ABYSS_LAUNCHER_MUTEX") is { Length: > 0 } custom
            ? custom
            : "AbyssLauncher_Wake";
        _wake = new EventWaitHandle(false, EventResetMode.AutoReset,
                                    wakeName, out var createdNew);
        if (!createdNew)
        {
            EventWaitHandle.OpenExisting(wakeName).Set();
            Shutdown(0);
            return;
        }
        base.OnStartup(e);
        // 主题要在建窗口之前定好：不然会先闪一下默认暗色再跳到用户选的主题
        ThemeManager.Initialize();
        InitTray();
        // 唤醒监听线程：收到信号 → 主线程把窗口带回前台
        var wakeThread = new Thread(() =>
        {
            while (_wake is not null)
            {
                _wake.WaitOne();
                Dispatcher.Invoke(ShowMainWindow);
            }
        });
        wakeThread.IsBackground = true;
        wakeThread.Start();
        _main = new MainWindow();
        _main.Show();
    }

    /// <summary>未处理异常落盘。失败也绝不抛，否则会把原始崩溃盖掉。</summary>
    private static void LogFatal(Exception? ex)
    {
        try
        {
            File.WriteAllText(
                Path.Combine(AppPaths.DataRoot(), "launcher_crash.log"),
                $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}]{Environment.NewLine}{ex}");
        }
        catch
        {
            // 落盘失败无能为力，保持进程原样
        }
    }

    private void InitTray()
    {
        var menu = new System.Windows.Forms.ContextMenuStrip();
        menu.Items.Add("打开窗口", null, (_, _) => ShowMainWindow());
        menu.Items.Add("停止全部并退出", null, (_, _) => AbyssLauncher.MainWindow.ExitFromTray());
        _tray = new System.Windows.Forms.NotifyIcon
        {
            Icon = DrawIcon(),
            Text = "Abyss 管家",
            Visible = true,
            ContextMenuStrip = menu,
        };
        _tray.DoubleClick += (_, _) => ShowMainWindow();
    }

    private static System.Drawing.Icon DrawIcon()
    {
        using var bmp = new System.Drawing.Bitmap(32, 32);
        using (var g = System.Drawing.Graphics.FromImage(bmp))
        {
            g.Clear(System.Drawing.Color.Transparent);
            // 托盘图标跟随品牌强调色（取自主题资源，和两端品牌标记同色）
            var accent = ResolveAccent();
            g.FillRectangle(new System.Drawing.SolidBrush(accent), 0, 0, 32, 32);
            using var f = new System.Drawing.Font("Segoe UI", 16, System.Drawing.FontStyle.Bold);
            g.DrawString("A", f, System.Drawing.Brushes.White, 4, 2);
        }
        return System.Drawing.Icon.FromHandle(bmp.GetHicon());
    }

    /// <summary>取当前主题的强调色；取不到就退回 CMF 的 #FA063E。</summary>
    private static System.Drawing.Color ResolveAccent()
    {
        try
        {
            if (Current?.Resources["ColorAccent"] is System.Windows.Media.Color c)
                return System.Drawing.Color.FromArgb(c.A, c.R, c.G, c.B);
        }
        catch
        {
            // 资源还没建好：用品牌色兜底
        }
        return System.Drawing.Color.FromArgb(0xFA, 0x06, 0x3E);
    }

    public static void ShowMainWindow()
    {
        if (_main is null) return;
        _main.Visibility = Visibility.Visible;
        _main.WindowState = WindowState.Normal;
        _main.Activate();
    }

    /// <summary>托盘气泡提醒：不抢焦点、不弹窗口，窗口最小化到托盘时也能看到。</summary>
    public static void Notify(string title, string message)
    {
        if (_tray is null) return;
        _tray.BalloonTipTitle = title;
        _tray.BalloonTipText = message;
        _tray.BalloonTipIcon = System.Windows.Forms.ToolTipIcon.Warning;
        _tray.ShowBalloonTip(10_000);
    }

    public static void ExitFromTray() => _main?.RequestExit();

    /// <summary>关闭窗口时隐藏到托盘（受管目标继续运行）。</summary>
    internal void HideToTray() => _main?.HideToTray();

    /// <summary>真正退出：移除托盘图标后关闭应用。</summary>
    internal void ReallyExit()
    {
        if (_tray is not null) { _tray.Visible = false; _tray.Dispose(); _tray = null; }
        Shutdown(0);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _tray?.Dispose();
        base.OnExit(e);
    }
}
