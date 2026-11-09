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

    private void StartLogPoll()
    {
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
            var snapshot = await Task.Run(() => LogPresentation.Read(root, source));
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
        foreach (var tab in new[] { LogTabCore, LogTabSnowluma, LogTabIngress, LogTabTunnel, LogTabService })
            tab.IsChecked = tab.Tag?.ToString() == _logSource;
    }

    /// <summary>
    /// 某个日志来源被隐藏后，如果当前正停在它上面，就退回 Core。
    /// 否则会停在一个看不见的 tab 上，日志区还照着它刷，用户完全不明白在显示什么。
    /// </summary>
    private void EnsureLogSourceVisible()
    {
        var hidden = _logSource switch
        {
            "snowluma" => !IsRowShown("snowluma"),
            "tunnel" => !IsRowShown("pi"),
            _ => false,
        };
        if (!hidden) return;
        _logSource = "core";
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
            var folder = Path.Combine(AppPaths.DataRoot(), "logs");
            Directory.CreateDirectory(folder);
            Process.Start(new ProcessStartInfo(folder) { UseShellExecute = true });
        }
        catch (Exception ex) { LogInfo.Text = "打开目录失败：" + ex.Message; }
    }
}
