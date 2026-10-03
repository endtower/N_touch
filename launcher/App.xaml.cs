using System.IO;
using System.Windows;

namespace AbyssLauncher;

/// <summary>应用入口：单实例互斥 + WinForms 托盘（WPF 是唯一窗口/托盘所有者）。</summary>
public partial class App : Application
{
    private static Mutex? _single;
    private static System.Windows.Forms.NotifyIcon? _tray;
    private static MainWindow? _main;

    private static EventWaitHandle? _wake;

    protected override void OnStartup(StartupEventArgs e)
    {
        // 单实例 + 唤醒（V01）：命名 EventWaitHandle 兼任互斥与唤醒信号。
        // 重复双击：已有实例收到信号后把窗口带回前台。
        _wake = new EventWaitHandle(false, EventResetMode.AutoReset,
                                    "AbyssLauncher_Wake", out var createdNew);
        if (!createdNew)
        {
            EventWaitHandle.OpenExisting("AbyssLauncher_Wake").Set();
            Shutdown(0);
            return;
        }
        base.OnStartup(e);
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
            g.Clear(System.Drawing.Color.FromArgb(30, 41, 59));
            using var f = new System.Drawing.Font("Segoe UI", 16, System.Drawing.FontStyle.Bold);
            g.DrawString("A", f, System.Drawing.Brushes.MediumPurple, 4, 2);
        }
        return System.Drawing.Icon.FromHandle(bmp.GetHicon());
    }

    public static void ShowMainWindow()
    {
        if (_main is null) return;
        _main.Visibility = Visibility.Visible;
        _main.WindowState = WindowState.Normal;
        _main.Activate();
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
