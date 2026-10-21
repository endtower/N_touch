using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace AbyssLauncher;

/// <summary>只对当前版本、当前发布说明和当前本地文件作一次明确确认。</summary>
public sealed class CoreUpdateDialog : Window
{
    public CoreUpdateDialog(JsonElement review)
    {
        static string Text(JsonElement value, string key, string fallback = "") =>
            value.TryGetProperty(key, out var child) && child.ValueKind == JsonValueKind.String ? child.GetString() ?? fallback : fallback;
        static IEnumerable<string> List(JsonElement value, string key) =>
            value.TryGetProperty(key, out var child) && child.ValueKind == JsonValueKind.Array
                ? child.EnumerateArray().Select(x => x.GetString() ?? "") : Array.Empty<string>();
        var install = Text(review, "operation") == "install";
        var recover = Text(review, "operation") == "recover";
        var permitted = review.GetProperty("can_proceed").GetBoolean();
        Title = recover ? "人工确认 Core 数据恢复" : install ? "确认安装 Core" : "确认下载 Core";
        Width = 620; Height = 620; MinWidth = 400; MinHeight = 360;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        SetResourceReference(BackgroundProperty, "BrushBgSurface");
        SetResourceReference(ForegroundProperty, "BrushTextPrimary");
        var panel = new DockPanel { Margin = new Thickness(24) };
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 16, 0, 0) };
        var cancel = new Button { Content = install ? "取消，保留当前版本" : "取消", IsCancel = true, MinWidth = 110, Padding = new Thickness(12, 6, 12, 6) };
        buttons.Children.Add(cancel);
        if (permitted)
        {
            var accept = new Button { Content = recover ? "我已处理数据，启动旧版" : install ? "备份后安装此版本" : "只下载此版本", Margin = new Thickness(12, 0, 0, 0), Padding = new Thickness(12, 6, 12, 6), MinWidth = 120 };
            accept.Click += (_, _) => DialogResult = true;
            buttons.Children.Add(accept);
        }
        DockPanel.SetDock(buttons, Dock.Bottom); panel.Children.Add(buttons);
        var release = review.GetProperty("release");
        var content = new StringBuilder();
        content.AppendLine($"当前版本：{Text(review, "current")}    所选版本：{Text(review, "version")}");
        content.AppendLine(recover ? "仅在你已按备份与迁移说明处理好数据后确认。本操作只启动旧版服务，不会替你恢复数据库，也不会自动覆盖更新后产生的数据。" : install ? "此操作会停止相应服务，备份后替换程序。" : "此操作只下载并准备更新包，不会安装、停机或切换版本。");
        content.AppendLine("\n版本说明\n" + Text(release, "notes", "发布方未提供版本说明"));
        var breaking = List(release, "breaking_changes").ToArray();
        content.AppendLine("\n破坏性改动\n" + (breaking.Length > 0 ? string.Join("\n", breaking.Select(x => "• " + x)) : "发布方未列出破坏性改动，请结合版本说明判断。"));
        content.AppendLine("\n兼容要求");
        var requires = release.GetProperty("requires").EnumerateObject().ToArray();
        content.AppendLine(requires.Length == 0 ? "发布方未声明最低组件版本。" : string.Join("\n", requires.Select(x => $"{x.Name} ≥ {x.Value.GetString()}")));
        content.AppendLine("\n数据与恢复\n" + (Text(release, "data_policy") == "compatible" ? "发布方声明数据格式兼容。" : Text(release, "data_policy") == "migration" ? "此版本涉及数据迁移。" : "发布方未声明数据格式兼容性。"));
        content.AppendLine(Text(release, "migration_notes"));
        content.AppendLine(Text(review, "backup_note"));
        if (install) content.AppendLine("暂停的服务：" + string.Join("、", List(review, "affected")));
        var changes = List(review, "local_changes").ToArray();
        content.AppendLine("\n本地修改\n" + (changes.Length == 0 ? "未检测到相对安装基线的程序改动。" : string.Join("\n", changes.Take(50)) + (changes.Length > 50 ? $"\n另有 {changes.Length - 50} 项" : "")));
        var blockers = List(review, "blockers").ToArray();
        if (blockers.Length > 0) content.AppendLine("\n当前不能安装\n" + string.Join("\n", blockers));
        var body = new TextBox { Text = content.ToString(), IsReadOnly = true, TextWrapping = TextWrapping.Wrap, VerticalScrollBarVisibility = ScrollBarVisibility.Auto, BorderThickness = new Thickness(0), FontSize = 14, Padding = new Thickness(4) };
        body.SetResourceReference(BackgroundProperty, "BrushBgSurface");
        body.SetResourceReference(ForegroundProperty, "BrushTextPrimary");
        panel.Children.Add(body); Content = panel;
        Loaded += (_, _) => cancel.Focus();
    }
}
