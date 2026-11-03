using System.IO;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;

namespace AbyssLauncher;

public partial class MainWindow
{
    private ConnectionCatalog? _connections;
    private ConnectionEntry? _editingConnection;
    private StackPanel _connectionCards = new();
    private Border _connectionEditor = new();
    private TextBox _connectionName = new();
    private TextBox _connectionUrl = new();
    private TextBlock _connectionError = new();

    private void InitializeConnections()
    {
        try
        {
            _connections = ConnectionCatalog.Load(Path.Combine(AppPaths.DataRoot(), "launcher", "connections.json"));
        }
        catch (Exception error)
        {
            ConnectionsContent.Children.Add(ConnectionText("连接配置无法读取：" + error.Message, "TextValue"));
            ConnectionsContent.Children.Add(ConnectionText("请修复 launcher/connections.json 后重新打开启动器，原文件未修改。", "TextValue"));
            return;
        }
        var header = new DockPanel { Margin = new Thickness(0, 0, 0, 12) };
        var add = ConnectionButton("＋ 添加接入器", () => EditConnection(null));
        DockPanel.SetDock(add, Dock.Right);
        header.Children.Add(add);
        header.Children.Add(ConnectionText("消息连接", "TextTitle"));
        ConnectionsContent.Children.Add(header);
        var help = ConnectionText("保存后会自动加入左侧栏，点一下即可打开配置、扫码或查看画面。", "TextValue");
        help.Margin = new Thickness(0, 0, 0, 20);
        ConnectionsContent.Children.Add(help);

        var form = new StackPanel();
        form.Children.Add(ConnectionText("接入器名称", "TextSection"));
        _connectionName = ConnectionInput("例如：Pi 微信");
        form.Children.Add(_connectionName);
        form.Children.Add(ConnectionText("控制网页／画面地址", "TextSection"));
        _connectionUrl = ConnectionInput("http://设备地址:端口/页面");
        form.Children.Add(_connectionUrl);
        var note = ConnectionText("SnowLuma 可留空以跟随实际端口。这里配置网页入口；消息收发使用接入器自身已配置的接口。", "TextValue");
        note.Margin = new Thickness(0, 0, 0, 8);
        form.Children.Add(note);
        _connectionError = ConnectionText("", "TextValue");
        _connectionError.SetResourceReference(TextBlock.ForegroundProperty, "BrushAccent");
        form.Children.Add(_connectionError);
        var actions = new WrapPanel { Margin = new Thickness(0, 8, 0, 0) };
        actions.Children.Add(ConnectionButton("保存", SaveConnection));
        actions.Children.Add(ConnectionButton("取消", () => _connectionEditor.Visibility = Visibility.Collapsed));
        form.Children.Add(actions);
        _connectionEditor = ConnectionCard(form);
        _connectionEditor.Visibility = Visibility.Collapsed;
        ConnectionsContent.Children.Add(_connectionEditor);
        _connectionCards = new StackPanel();
        ConnectionsContent.Children.Add(_connectionCards);
        RefreshConnections();
    }

    private TextBlock ConnectionText(string text, string style) => new()
    {
        Text = text, Style = (Style)FindResource(style), TextWrapping = TextWrapping.Wrap
    };

    private TextBox ConnectionInput(string hint)
    {
        var input = new TextBox { Margin = new Thickness(0, 8, 0, 16), Padding = new Thickness(10, 8, 10, 8), ToolTip = hint };
        input.SetResourceReference(Control.BackgroundProperty, "BrushBgSunken");
        input.SetResourceReference(Control.ForegroundProperty, "BrushTextPrimary");
        input.SetResourceReference(Control.BorderBrushProperty, "BrushBorderSubtle");
        return input;
    }

    private Button ConnectionButton(string text, Action action)
    {
        var button = new Button { Content = text, Style = (Style)FindResource("GhostButton") };
        button.Click += (_, _) => action();
        return button;
    }

    private Border ConnectionCard(UIElement content)
    {
        var card = new Border { Child = content, Padding = new Thickness(20), Margin = new Thickness(0, 0, 0, 12), BorderThickness = new Thickness(1), CornerRadius = new CornerRadius(8) };
        card.SetResourceReference(Border.BackgroundProperty, "BrushBgSurface");
        card.SetResourceReference(Border.BorderBrushProperty, "BrushBorderSubtle");
        return card;
    }

    private void RefreshConnections()
    {
        if (_connections is null) return;
        ConnectionNav.Children.Clear();
        _connectionCards.Children.Clear();
        foreach (var entry in _connections.Entries)
        {
            var tag = "connection:" + entry.Id;
            var button = new RadioButton
            {
                GroupName = "Rail", Style = (Style)FindResource("RailNavButton"), Tag = tag,
                ToolTip = entry.Name, IsChecked = _webSource == tag && WebPanel.Visibility == Visibility.Visible,
                Content = new TextBlock { Text = entry.Name[..Math.Min(2, entry.Name.Length)], FontSize = 13, FontWeight = FontWeights.SemiBold }
            };
            AutomationProperties.SetName(button, entry.Name);
            button.Click += Nav_Click;
            ConnectionNav.Children.Add(button);

            var card = new StackPanel();
            card.Children.Add(ConnectionText(entry.Name, "TextSection"));
            var address = ConnectionText(entry.Url.Length > 0 ? entry.Url : "使用 SnowLuma 当前控制页端口", "TextValue");
            address.Margin = new Thickness(0, 8, 0, 12);
            card.Children.Add(address);
            var actions = new WrapPanel();
            actions.Children.Add(ConnectionButton("打开页面", () => ShowSection(tag)));
            actions.Children.Add(ConnectionButton("编辑", () => EditConnection(entry)));
            var index = _connections.Entries.ToList().FindIndex(value => value.Id == entry.Id);
            var up = ConnectionButton("上移", () => ChangeConnection(() => _connections.Move(entry.Id, -1)));
            up.IsEnabled = index > 0;
            actions.Children.Add(up);
            var down = ConnectionButton("下移", () => ChangeConnection(() => _connections.Move(entry.Id, 1)));
            down.IsEnabled = index < _connections.Entries.Count - 1;
            actions.Children.Add(down);
            actions.Children.Add(ConnectionButton("移除入口", () => ChangeConnection(() => _connections.Remove(entry.Id))));
            card.Children.Add(actions);
            _connectionCards.Children.Add(ConnectionCard(card));
        }
        if (_connections.Entries.Count == 0)
            _connectionCards.Children.Add(ConnectionText("还没有入口，点击“添加接入器”开始。", "TextValue"));
    }

    private void EditConnection(ConnectionEntry? entry)
    {
        if (_connections is null) return;
        ShowSection("connections");
        _editingConnection = entry;
        _connectionName.Text = entry?.Name ?? "";
        _connectionUrl.Text = entry?.Url ?? "";
        _connectionError.Text = "";
        _connectionEditor.Visibility = Visibility.Visible;
        _connectionName.Focus();
        _connectionEditor.BringIntoView();
    }

    private void SaveConnection()
    {
        if (_connections is null) return;
        try
        {
            var entry = (_editingConnection ?? new ConnectionEntry { Id = Guid.NewGuid().ToString("N") }) with
            {
                Name = _connectionName.Text, Url = _connectionUrl.Text
            };
            _connections.Save(entry);
            _connectionEditor.Visibility = Visibility.Collapsed;
            RefreshConnections();
            _loadedWebUrl = "";
            StatusBar.Text = "已保存，左侧入口已更新";
        }
        catch (Exception error) { _connectionError.Text = error.Message; }
    }

    private void ChangeConnection(Action change)
    {
        try
        {
            change();
            if (_editingConnection is not null && !_connections!.Entries.Any(entry => entry.Id == _editingConnection.Id))
                _connectionEditor.Visibility = Visibility.Collapsed;
            RefreshConnections();
            StatusBar.Text = "已保存连接列表";
        }
        catch (Exception error) { StatusBar.Text = "保存失败：" + error.Message; }
    }

    private void AddConnection_Click(object sender, RoutedEventArgs e) => EditConnection(null);

    private ConnectionEntry? CurrentConnection() => _connections?.Entries.FirstOrDefault(entry => _webSource == "connection:" + entry.Id);
    private string CurrentManagedTarget() => CurrentConnection()?.ManagedTarget ?? _webSource;

    private void UpdateConnectionWebPage(bool force)
    {
        var entry = CurrentConnection();
        WebStartButton.Visibility = Visibility.Collapsed;
        WebPageTitle.Text = entry?.Name ?? "消息连接";
        Web.Visibility = Visibility.Collapsed;
        WebUnavailable.Visibility = Visibility.Visible;
        if (force) _navigationError = "";
        UpdateTunnelBar(entry?.ManagedTarget == "tunnel" && _targets.TryGetValue("tunnel", out var tunnel)
            ? tunnel : null);
        if (entry is null)
        {
            WebPageState.Text = "入口已移除";
            WebUnavailableTitle.Text = "入口已移除";
            WebUnavailableDetail.Text = "可以在消息连接页面重新添加。";
            return;
        }
        _targets.TryGetValue(entry.ManagedTarget, out var target);
        var managed = entry.ManagedTarget == "snowluma";
        var ready = !managed || target is { State: "running", Ready: true };
        WebPageState.Text = managed ? target?.State switch
        {
            "running" => "运行中", "starting" => "正在启动", "failed" => "运行失败", _ => "尚未启动"
        } : "外部控制页面";
        if (managed && _serviceConnected && target is not null && target.State is "stopped" or "failed")
        {
            WebStartButton.Visibility = Visibility.Visible;
            WebStartButton.Content = "启动 " + entry.Name;
        }
        if (!ready || Web.CoreWebView2 is null || _navigationError.Length > 0)
        {
            WebUnavailableTitle.Text = _navigationError.Length > 0 ? "页面暂时无法打开" : ready ? "正在加载页面" : entry.Name + " 尚未就绪";
            WebUnavailableDetail.Text = _navigationError.Length > 0
                ? "请检查设备、服务和页面地址，然后点击标题栏刷新。也可以在消息连接中编辑地址。"
                : managed ? target?.LastError ?? "请先启动接入器。" : "正在初始化页面组件。";
            return;
        }
        var port = target?.Ports.FirstOrDefault() ?? 15080;
        var url = entry.Url.Length > 0 ? entry.Url : $"http://127.0.0.1:{port}/";
        WebPageTitle.ToolTip = url;
        Web.Visibility = Visibility.Visible;
        WebUnavailable.Visibility = Visibility.Collapsed;
        if (force || _loadedWebUrl != url)
        {
            _loadedWebUrl = url;
            Web.CoreWebView2.Navigate(url);
        }
    }
}
