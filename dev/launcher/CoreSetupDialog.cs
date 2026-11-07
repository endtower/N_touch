using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;

namespace AbyssLauncher;

/// <summary>全新源码安装先填自己的身份和模型接入，不需要编辑 Python 配置。</summary>
public sealed class CoreSetupDialog : Window
{
    public Dictionary<string, object> Fields { get; } = new();

    public CoreSetupDialog(JsonElement config)
    {
        Title = "首次启动 Core"; Width = 560; Height = 630;
        WindowStartupLocation = WindowStartupLocation.CenterOwner;
        SetResourceReference(BackgroundProperty, "BrushBgSurface");
        SetResourceReference(ForegroundProperty, "BrushTextPrimary");
        var values = config.GetProperty("fields");
        static string Text(JsonElement data, string key, string fallback) => data.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(value.GetString()) ? value.GetString()! : fallback;
        var panel = new StackPanel { Margin = new Thickness(24) };
        panel.Children.Add(new TextBlock { Text = "填写你自己的账号与模型接入。配置保存在用户数据目录，更新 Core 时会保留。", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 12) });
        TextBox Input(string label, string value)
        {
            panel.Children.Add(new TextBlock { Text = label, Margin = new Thickness(0, 8, 0, 4) });
            var input = new TextBox { Text = value, Padding = new Thickness(6), MaxLength = 512 };
            panel.Children.Add(input); return input;
        }
        var master = Input("你的 QQ 号", Text(values, "MASTER_ID", ""));
        var name = Input("你的称呼", Text(values, "MASTER_NAME", "主人"));
        var account = Input("助手登录的 QQ 号", Text(values, "ASSISTANT_QQ", ""));
        var assistant = Input("助手名称", Text(values, "ASSISTANT_NAME", "助手"));
        var defaults = config.GetProperty("defaults").GetProperty("common");
        var endpoint = Input("模型 API 地址", Text(values, "FLASH_BASE_URL", Text(defaults, "base_url", "")));
        var model = Input("模型名称", Text(values, "LLM_FLASH", Text(defaults, "model", "")));
        panel.Children.Add(new TextBlock { Text = "API Key（已有配置时留空保留）", Margin = new Thickness(0, 8, 0, 4) });
        var key = new PasswordBox { Padding = new Thickness(6) }; panel.Children.Add(key);
        EnablePaste(key);
        var error = new TextBlock { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 0) };
        error.SetResourceReference(ForegroundProperty, "BrushAccent"); panel.Children.Add(error);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 16, 0, 0) };
        var cancel = new Button { Content = "稍后设置", IsCancel = true, Padding = new Thickness(12, 6, 12, 6) };
        var save = new Button { Content = "保存并启动", Margin = new Thickness(12, 0, 0, 0), Padding = new Thickness(12, 6, 12, 6) };
        save.Click += (_, _) =>
        {
            if (!long.TryParse(master.Text.Trim(), out var owner) || owner <= 0 || !long.TryParse(account.Text.Trim(), out var bot) || bot <= 0)
            { error.Text = "请填写有效的 QQ 号。"; return; }
            var configured = values.TryGetProperty("FLASH_API_KEY_SET", out var flag) && flag.GetBoolean();
            if (string.IsNullOrWhiteSpace(key.Password) && !configured) { error.Text = "请填写 API Key，也可以稍后再设置。"; return; }
            Fields["MASTER_ID"] = master.Text.Trim(); Fields["MASTER_NAME"] = name.Text.Trim();
            Fields["ASSISTANT_QQ"] = account.Text.Trim(); Fields["ASSISTANT_NAME"] = assistant.Text.Trim();
            Fields["FLASH_BASE_URL"] = endpoint.Text.Trim(); Fields["LLM_FLASH"] = model.Text.Trim();
            if (!string.IsNullOrWhiteSpace(key.Password)) Fields["FLASH_API_KEY"] = key.Password.Trim();
            DialogResult = true;
        };
        buttons.Children.Add(cancel); buttons.Children.Add(save); panel.Children.Add(buttons);
        Content = new ScrollViewer { Content = panel, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
    }

    /// <summary>让密码框支持 Ctrl+V 与右键「粘贴」：整段覆盖，避免残留旧字符。</summary>
    private static void EnablePaste(PasswordBox box)
    {
        static string? ReadClipboard()
        {
            if (!Clipboard.ContainsText()) return null;
            // 从文档里复制的内容常带换行与首尾空白；API Key 不能带这些字符
            var text = Clipboard.GetText().Replace("\r", "").Replace("\n", "").Trim();
            return text.Length == 0 ? null : text;
        }

        void Paste()
        {
            var text = ReadClipboard();
            if (text is not null) box.Password = text;
        }

        var menu = new ContextMenu();
        var item = new MenuItem { Header = "粘贴" };
        item.Click += (_, _) => Paste();
        menu.Items.Add(item);
        box.ContextMenu = menu;
        box.PreviewKeyDown += (_, e) =>
        {
            if (e.Key == Key.V && (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control)
            {
                Paste();
                e.Handled = true;
            }
        };
    }
}
