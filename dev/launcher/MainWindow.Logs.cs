using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Threading;

namespace AbyssLauncher;

public partial class MainWindow
{
    private DispatcherTimer? _logPoll;
    private string _logSource = "core";
    private string _logText = "";
    private string _logFiles = "";
    private string _copyText = "";
    private bool _logReading;
    private bool _logRefreshPending;

    /// <summary>日志页左边那排来源标签，顺序与 data\launcher\logs.json 一致。</summary>
    private readonly List<RadioButton> _logTabs = [];

    /// <summary>
    /// 按 logs.json 建来源标签。文件改了要重开启动器才生效 —— 这里只在启动时跑一次，
    /// 不做热加载：日志来源是低频配置，为它加一套文件监视不划算。
    /// </summary>
    private void BuildLogTabs()
    {
        if (LogSourceTabs is null) return;
        LogSourceTabs.Children.Clear();
        _logTabs.Clear();

        foreach (var source in LogSources.Current)
        {
            var button = new RadioButton
            {
                GroupName = "LogSource",
                Style = (Style)FindResource("SegmentedTab"),
                Tag = source.Id,
                Content = source.Title,
                Visibility = Visibility.Visible,
            };
            button.Click += LogTab_Click;
            LogSourceTabs.Children.Add(button);
            _logTabs.Add(button);
        }

        // 诊断用：ABYSS_LAUNCHER_LOG_SOURCE 指定进日志页时先停在哪个来源。
        // 和 ABYSS_LAUNCHER_SECTION / ABYSS_LAUNCHER_EXPAND 一样，只给无头截图验证用。
        if (Environment.GetEnvironmentVariable("ABYSS_LAUNCHER_LOG_SOURCE") is { Length: > 0 } initial
            && LogSources.Find(initial) is not null)
            _logSource = initial;

        // 进来的来源可能已经被删掉（用户改了 logs.json），退回第一个可用的
        if (LogSources.Find(_logSource) is null)
            _logSource = LogSources.Current.FirstOrDefault()?.Id ?? "core";
    }

    /// <summary>
    /// 某个来源现在该不该显示：绑定了组件行的话，跟着那一行的显示与否走。
    ///
    /// 这里查的是「用户有没有在 ui.json 里关掉那一行」，不是 IsRowShown —— 后者还带一条
    /// 内置条件（Pi 行要有 tunnel 目标才显示）。日志不该跟着这个条件走：tunnel 没配置时
    /// <c>logs\manager\tunnel.out.log</c> 可能仍然有内容，那正是用户翻日志的时候；
    /// 而且 targets 还没拉回来时 IsRowShown 会短暂为 false，会让已经选中的来源被踢走。
    /// </summary>
    private bool IsLogSourceShown(LogSources.Source source) =>
        source.Row.Length == 0 || !IsRowHiddenByUser(source.Row);

    private void StartLogPoll()
    {
        BuildLogTabs();
        UpdateLogTabs();
        _logPoll = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _logPoll.Tick += async (_, _) =>
        {
            if (IsVisible && LogsPanel.Visibility == Visibility.Visible && LogAuto.IsChecked == true)
                await RefreshLogs();
        };
        _logPoll.Start();
    }

    private async Task RefreshLogs()
    {
        if (_logReading) { _logRefreshPending = true; return; }
        _logReading = true;
        var source = _logSource;
        try
        {
            var root = AppPaths.DataRoot();
            var snapshot = await Task.Run(() => LogSources.Read(root, source));
            if (source != _logSource) return;
            var changed = _logText != snapshot.Text || _logFiles != snapshot.Files;
            _logText = snapshot.Text;
            _logFiles = snapshot.Files;
            if (changed || LogBox.Document.Blocks.Count == 0) RenderLogs();
            LogInfo.Text = $"{_logFiles} · 最近最多 1000 行 · 更新于 {DateTime.Now:HH:mm:ss}";
        }
        catch (Exception ex) { LogInfo.Text = "读取日志失败：" + ex.Message; }
        finally
        {
            _logReading = false;
            if (_logRefreshPending) { _logRefreshPending = false; _ = RefreshLogs(); }
        }
    }

    private void RenderLogs()
    {
        if (LogBox is null || LogSearch is null) return;
        var scroll = LogBox.VerticalOffset;
        var filter = LogSearch.Text.Trim();
        var lines = _logText.Replace("\r\n", "\n").Split('\n')
            .Where(line => filter.Length == 0 || LogPresentation.Plain(line).Contains(filter, StringComparison.OrdinalIgnoreCase))
            .ToArray();
        _copyText = string.Join(Environment.NewLine, lines.Select(LogPresentation.Plain));

        // 日志的"中性前景色"跟主题走；级别色与 ANSI 色属于日志内容语义，保持固定
        var defaultColour = FindResource("BrushTextSecondary") is System.Windows.Media.SolidColorBrush themeBrush
            ? themeBrush.Color.ToString()
            : "#C9C9C9";

        var doc = new FlowDocument { PagePadding = new Thickness(8), FontFamily = LogBox.FontFamily,
            FontSize = LogBox.FontSize,
            PageWidth = Math.Max(Math.Max(300, LogBox.ActualWidth - 24), lines.Select(x => LogPresentation.Plain(x).Length).DefaultIfEmpty(0).Max() * 7.3 + 24) };
        SetTheme(doc, FlowDocument.BackgroundProperty, "BrushBgSunken");

        foreach (var line in lines)
        {
            var paragraph = new Paragraph { Margin = new Thickness(0), LineHeight = 18 };
            foreach (var segment in LogPresentation.Colourise(line, defaultColour))
            {
                var run = new Run(segment.Text) { Foreground = Brush(segment.Colour) };
                if (segment.Background is not null) run.Background = Brush(segment.Background);
                if (segment.Underline) run.TextDecorations = TextDecorations.Underline;
                paragraph.Inlines.Add(run);
            }
            doc.Blocks.Add(paragraph);
        }
        if (string.IsNullOrWhiteSpace(_logText))
            doc.Blocks.Add(ThemedParagraph("暂无日志。程序启动后会在这里显示输出。"));
        else if (lines.Length == 0)
            doc.Blocks.Add(ThemedParagraph("没有匹配的日志。"));
        LogBox.Document = doc;
        if (LogFollow.IsChecked == true) LogBox.ScrollToEnd();
        else LogBox.ScrollToVerticalOffset(scroll);
    }

    /// <summary>灰色提示段落，颜色挂主题引用（换主题会自动重绘）。</summary>
    private Paragraph ThemedParagraph(string text)
    {
        var paragraph = new Paragraph(new Run(text));
        SetTheme(paragraph, TextElement.ForegroundProperty, "BrushTextTertiary");
        return paragraph;
    }

    private void UpdateLogTabs()
    {
        // 选中态交给 SegmentedTab 的 IsChecked 触发器，颜色不在代码里写死
        foreach (var tab in _logTabs)
        {
            var source = LogSources.Find(tab.Tag?.ToString() ?? "");
            tab.Visibility = source is null || IsLogSourceShown(source) ? Visibility.Visible : Visibility.Collapsed;
            tab.IsChecked = tab.Tag?.ToString() == _logSource;
        }
    }

    /// <summary>
    /// 某个日志来源被隐藏后，如果当前正停在它上面，就退回第一个可见来源。
    /// 否则会停在一个看不见的 tab 上，日志区还照着它刷，用户完全不明白在显示什么。
    /// </summary>
    private void EnsureLogSourceVisible()
    {
        var current = LogSources.Find(_logSource);
        if (current is not null && IsLogSourceShown(current)) return;

        _logSource = LogSources.Current.FirstOrDefault(IsLogSourceShown)?.Id ?? _logSource;
        _logText = ""; _logFiles = "";
        UpdateLogTabs();
        RenderLogs();
    }

    private async void LogTab_Click(object sender, RoutedEventArgs e)
    {
        _logSource = (sender as FrameworkElement)?.Tag?.ToString() ?? "core";
        _logText = ""; _logFiles = "";
        UpdateLogTabs();
        RenderLogs();
        await RefreshLogs();
    }

    private void LogSearch_Changed(object sender, TextChangedEventArgs e) => RenderLogs();
    private async void LogRefresh_Click(object sender, RoutedEventArgs e) => await RefreshLogs();
    private void LogCopy_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            var text = string.IsNullOrEmpty(LogBox.Selection.Text) ? _copyText : LogBox.Selection.Text;
            if (text.Length == 0) { LogInfo.Text = "没有可复制的日志。"; return; }
            System.Windows.Clipboard.SetText(text);
            LogInfo.Text = "已复制日志（选中内容优先）。";
        }
        catch (Exception ex) { LogInfo.Text = "复制失败：" + ex.Message; }
    }

    private void LogFolder_Click(object sender, RoutedEventArgs e)
    {
        try
        {
            // 打开的是当前来源第一条实际存在的日志所在目录 —— 用户点「打开日志目录」
            // 想看的显然是他正在看的那份，不是笼统的 logs 根
            var folder = LogSources.FolderFor(AppPaths.DataRoot(), _logSource);
            Directory.CreateDirectory(folder);
            Process.Start(new ProcessStartInfo(folder) { UseShellExecute = true });
        }
        catch (Exception ex) { LogInfo.Text = "打开目录失败：" + ex.Message; }
    }
}
