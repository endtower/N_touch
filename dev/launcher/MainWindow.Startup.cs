using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Threading;

namespace AbyssLauncher;

/// <summary>
/// 总览页的「程序清单」：每一行都是一个真实运行的程序，
/// 启动链路的进度并进同一行，不再单独列一遍链路卡片
/// （旧版把「主程序 / SnowLuma」既画成链路卡片、又画成组件行，看起来像两套东西）。
/// </summary>
public partial class MainWindow
{
    private DispatcherTimer? _startupPoll;
    private bool _startupRefreshing, _startupBusy;

    /// <summary>服务端启动链路快照，按链路 id 索引。</summary>
    private readonly Dictionary<string, ChainState> _chains = new();
    /// <summary>每行主操作此刻是什么含义：start / continue / stop / none。</summary>
    private readonly Dictionary<string, string> _rowActions = new();

    private sealed class ChainState
    {
        public string State = "idle";
        public string Title = "";
        public string Message = "";
        public string Steps = "";
        public bool Enabled = true, Required, CanContinue, CanSkip;
    }

    /// <summary>一行 = 一个程序。Chain 为空的行走的是目标自身启停（消息接入 / 管理服务 / 启动器）。</summary>
    private sealed record RowUi(
        string Id, string Chain, string Target, string Name,
        System.Windows.Shapes.Ellipse Dot, Border Pill, TextBlock StateText, Button Action,
        StackPanel Panel, ToggleButton Toggle, TextBlock? Steps, TextBlock? Message,
        CheckBox? Participate, Button? Continue, Button? Skip);

    private List<RowUi>? _rows;

    private List<RowUi> Rows => _rows ??= new()
    {
        new RowUi("core", "core", "core", "弱链接 Core",
            CoreDot, CorePill, CoreState, CoreAction, CorePanel, CoreToggle,
            CoreSteps, CoreMessage, CoreParticipate, CoreContinue, CoreSkip),
        new RowUi("snowluma", "snowluma", "snowluma", "SnowLuma",
            SnowDot, SnowPill, SnowState, SnowAction, SnowPanel, SnowToggle,
            SnowSteps, SnowMessage, SnowParticipate, SnowContinue, SnowSkip),
        new RowUi("pi", "pi", "tunnel", "Pi 桥接",
            PiDot, PiPill, PiState, PiAction, PiPanel, PiToggle,
            PiSteps, PiMessage, PiParticipate, PiContinue, PiSkip),
        new RowUi("ingress", "", "ingress", "消息接入",
            IngressDot, IngressPill, IngressState, IngressAction, IngressPanel, IngressToggle,
            null, null, null, null, null),
        new RowUi("manager", "", "", "管理服务",
            ManagerDot, ManagerPill, ManagerState, null!, ManagerPanel, ManagerToggle,
            null, null, null, null, null),
        new RowUi("launcher", "", "", "桌面启动器",
            LauncherDot, LauncherPill, LauncherState, null!, LauncherPanel, LauncherToggle,
            null, null, null, null, null),
    };

    // ---------------- 状态文案 ----------------

    private static string ChainText(string state) => state switch
    {
        "pending" => "排队中",
        "running" => "启动中",
        "blocked" => "等待前置",
        "waiting" => "等待操作",
        "failed" => "需要处理",
        "done" => "已就绪",
        "skipped" => "已跳过",
        "cancelled" => "已暂停",
        _ => "未启动",
    };

    private static string TargetText(string state) => state switch
    {
        "running" => "运行中",
        "starting" => "启动中",
        "stopping" => "停止中",
        "failed" => "运行失败",
        "conflict" => "端口占用",
        _ => "已停止",
    };

    private static string SummaryText(string state) => state switch
    {
        "running" => "正在按顺序启动…",
        "attention" => "有程序需要处理，其余可以继续",
        "paused" => "已暂停；已经运行的程序保持现状",
        "partial" => "本轮有程序被跳过，可稍后单独启动",
        "done" => "已全部就绪",
        _ => "还没有执行过一键启动",
    };

    /// <summary>状态色只分四档：正常 / 进行中 / 需要处理 / 中性。
    /// 「正常」只用中性描边 —— 一切正常时整页不该有一排在喊的彩色胶囊，颜色留给要人管的那几行。</summary>
    private void SetPill(Border pill, TextBlock text, string label, string tone)
    {
        var (border, foreground) = tone switch
        {
            "warning" => ("BrushTagWarning", "BrushTagWarning"),
            "danger" => ("BrushTagDanger", "BrushTagDanger"),
            // 常态也要看得清：浅色主题下的三级字太淡，胶囊会糊成一片
            _ => ("BrushBorderStrong", "BrushTextSecondary"),
        };
        SetTheme(pill, Border.BorderBrushProperty, border);
        SetTheme(text, TextBlock.ForegroundProperty, foreground);
        text.Text = label;
    }

    // ---------------- 轮询 ----------------

    private void StartStartupPoll()
    {
        _startupPoll = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
        _startupPoll.Tick += async (_, _) => await RefreshStartup();
        _startupPoll.Start();
        Closed += (_, _) => _startupPoll.Stop();
        _ = RefreshStartup();
    }

    private void RenderStartup(JsonElement status)
    {
        if (!status.TryGetProperty("chains", out var chains)) return;
        _chains.Clear();
        foreach (var chain in chains.EnumerateArray())
        {
            var id = chain.GetProperty("id").GetString() ?? "";
            var steps = chain.GetProperty("steps").EnumerateArray().Select((step, index) =>
            {
                var progress = step.GetProperty("state").GetString();
                var mark = progress == "done" ? "✓" : progress == "failed" ? "✕"
                    : progress is "running" or "waiting" ? "◔" : "○";
                return $"{index + 1} {mark} {step.GetProperty("title").GetString()}";
            });
            _chains[id] = new ChainState
            {
                State = chain.GetProperty("state").GetString() ?? "idle",
                Title = chain.GetProperty("title").GetString() ?? "",
                Message = chain.GetProperty("message").GetString() ?? "",
                Steps = string.Join("   ", steps),
                Enabled = chain.GetProperty("enabled").GetBoolean(),
                Required = chain.GetProperty("required").GetBoolean(),
                CanContinue = chain.GetProperty("can_continue").GetBoolean(),
                CanSkip = chain.GetProperty("can_skip").GetBoolean(),
            };
        }
        StartupSummary.Text = SummaryText(status.TryGetProperty("state", out var state)
            ? state.GetString() ?? "idle" : "idle");
        RenderRows();
    }

    private async Task RefreshStartup()
    {
        if (_startupRefreshing || _startupBusy) return;
        _startupRefreshing = true;
        try { RenderStartup(await _client.GetAsync("/v1/startup")); }
        catch { StartupSummary.Text = "启动流程暂时无法连接，请确认管理服务已运行"; }
        finally { _startupRefreshing = false; }
    }

    private async Task RunStartup(string action, string? chain = null, bool? enabled = null)
    {
        if (_startupBusy) return;
        _startupBusy = true;
        try
        {
            RenderStartup(await _client.StartupAsync(action, new { chain, enabled }));
            StatusBar.Text = action switch
            {
                "stop" => "已停止受管程序",
                "continue" => "正在复查这一步…",
                "skip" => "本轮已跳过",
                "enable" => enabled == true ? "已加入一键启动" : "已从一键启动移除",
                _ => "启动流程已更新",
            };
        }
        catch (Exception error) { StatusBar.Text = error.Message; }
        finally { _startupBusy = false; await RefreshStartup(); }
    }

    // ---------------- 行的上下文菜单式操作 ----------------

    /// <summary>点行表头：展开/收起该行的详情，同一时刻只开一行，避免又要滚动。</summary>
    private void RowHeader_Click(object sender, System.Windows.Input.MouseButtonEventArgs e)
    {
        var id = (sender as FrameworkElement)?.Tag?.ToString() ?? "";
        if (id.Length == 0) return;
        if (Rows.FirstOrDefault(row => row.Id == id) is not { } target) return;
        if (target.Panel.Visibility == Visibility.Visible) CollapseRows();
        else ExpandRow(id);
    }

    private void ExpandRow(string id)
    {
        CollapseRows();
        if (Rows.FirstOrDefault(row => row.Id == id) is not { } target) return;
        target.Panel.Visibility = Visibility.Visible;
        target.Toggle.IsChecked = true;
    }

    private void CollapseRows()
    {
        foreach (var row in Rows)
        {
            row.Panel.Visibility = Visibility.Collapsed;
            row.Toggle.IsChecked = false;
        }
    }

    /// <summary>行表头的主按钮：同一个位置按状态分别承担启动 / 继续 / 停止。
    /// 只有链路行（Core / SnowLuma / Pi）走这里；消息接入是纯目标启停，走 IngressAction_Click。</summary>
    private async void ChainRowAction_Click(object sender, RoutedEventArgs e)
    {
        var id = (sender as FrameworkElement)?.Tag?.ToString() ?? "";
        if (id.Length == 0 || !_rowActions.TryGetValue(id, out var action)) return;
        switch (action)
        {
            case "start":
                if (id == "core" && !await EnsureCoreSetup()) return;
                await RunStartup("start", ChainOf(id));
                break;
            case "continue":
                if (id == "core" && !await EnsureCoreSetup()) return;
                await RunStartup("continue", ChainOf(id));
                break;
            case "stop":
                await Send($"/v1/targets/{TargetOf(id)}/stop");
                break;
        }
    }

    private static string ChainOf(string id) => id switch
    {
        "snowluma" => "snowluma", "pi" => "pi", _ => "core",
    };

    private string TargetOf(string id) => id switch
    {
        "pi" => "tunnel", "ingress" => "ingress", "snowluma" => "snowluma", _ => "core",
    };

    private async void StartupContinue_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.Tag is string chain && await EnsureCoreSetup())
            await RunStartup("continue", chain);
    }

    private async void StartupSkip_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.Tag is string chain) await RunStartup("skip", chain);
    }

    private async void StartupEnable_Click(object sender, RoutedEventArgs e)
    {
        if (!_startupRendering && sender is CheckBox check && check.Tag is string chain)
            await RunStartup("enable", chain, check.IsChecked == true);
    }

    private bool _startupRendering;

    private async void StartupPause_Click(object sender, RoutedEventArgs e) => await RunStartup("cancel");

    // ---------------- 渲染 ----------------

    /// <summary>把「链路状态 + 目标状态」合成一行的显示。目标轮询与链路轮询都调它。</summary>
    private void RenderRows()
    {
        if (!IsLoaded) return;
        var tunnel = _targets.TryGetValue("tunnel", out var piTarget) ? piTarget : null;
        var hasPi = tunnel is not null;
        PiRow.Visibility = hasPi ? Visibility.Visible : Visibility.Collapsed;
        PiDivider.Visibility = hasPi ? Visibility.Visible : Visibility.Collapsed;

        _startupRendering = true;
        try
        {
            foreach (var row in Rows)
            {
                if (row.Id == "pi" && !hasPi) continue;
                _chains.TryGetValue(row.Chain ?? "", out var chain);
                _targets.TryGetValue(row.Target ?? "", out var target);
                var running = target is not null && IsRunning(target);
                var live = chain is not null && chain.State != "idle";

                // —— 状态：链路在跑就以链路为主，否则看进程本身 ——
                string label, tone;
                if (live && chain!.State is "pending" or "running" or "blocked" or "waiting" or "failed"
                        or "skipped" or "cancelled")
                {
                    label = ChainText(chain.State);
                    tone = chain.State switch
                    {
                        "failed" => "danger",
                        "waiting" or "blocked" => "warning",
                        "skipped" or "cancelled" => "",
                        _ => "warning",
                    };
                }
                else if (row.Id == "manager") { label = _serviceConnected ? "运行中" : "未连接"; tone = _serviceConnected ? "success" : "danger"; }
                else if (row.Id == "launcher") { label = "运行中"; tone = "success"; }
                else if (!_serviceConnected && target is null) { label = "状态未知"; tone = ""; }
                else if (running) { label = target!.State == "running" ? "运行中" : TargetText(target.State); tone = "success"; }
                else if (target is null) { label = row.Id == "pi" ? "未配置" : "未安装"; tone = ""; }
                else { label = TargetText(target.State); tone = RowTone(target); }
                if (chain is { State: "done" } && running) { label = "已就绪"; tone = "success"; }

                // —— 主按钮：一处入口，三种含义 ——
                string action, text, tip = "";
                if (row.Action is null)
                {
                    action = "none"; text = "";
                }
                else if (!_serviceConnected && target is null)
                {
                    action = "none"; text = "启动";
                    tip = "后台服务未连接，暂时不能启动";
                }
                else if (live && chain!.State is "pending" or "running")
                {
                    action = "none"; text = "启动中"; tip = "正在按顺序启动，稍候即可";
                }
                else if (live && chain!.State is "failed" or "waiting" or "blocked")
                {
                    action = "continue"; text = "继续"; tip = "按当前进度继续；也可以直接在下方点「继续」复查";
                }
                else if (running)
                {
                    action = "stop"; text = "停止"; tip = "停止这个程序，不影响其它程序";
                }
                else if (row.Id == "ingress" && target is not null)
                {
                    action = "start"; text = "启动"; tip = "启动消息接入";
                }
                else if (string.IsNullOrEmpty(row.Chain))
                {
                    action = "none"; text = "";
                }
                else
                {
                    action = "start"; text = "启动"; tip = "启动这个程序（连同它需要的前置）";
                }
                _rowActions[row.Id] = action;
                if (row.Action is not null)
                {
                    row.Action.Content = text;
                    row.Action.Visibility = text.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
                    row.Action.IsEnabled = action != "none" && !_startupBusy;
                    row.Action.ToolTip = tip;
                }

                ApplyDot(row.Dot, DotStateFor(row, target, running, chain));
                SetPill(row.Pill, row.StateText, label, tone);

                // —— 详情一行：只在有信息时写，避免满屏破折号 ——
                var detail = DetailFor(row, target, chain, running);
                if (row.Id == "core") { CoreDetail.Text = detail; CoreDetail.ToolTip = detail; }
                else if (row.Id == "snowluma") { SnowDetail.Text = detail; SnowDetail.ToolTip = detail; }
                else if (row.Id == "pi") { PiDetail.Text = detail; PiDetail.ToolTip = detail; }
                else if (row.Id == "ingress") { IngressDetail.Text = detail; IngressDetail.ToolTip = detail; }
                else if (row.Id == "manager") { ManagerDetail.Text = detail; ManagerDetail.ToolTip = detail; }
                else if (row.Id == "launcher") { LauncherDetail.Text = detail; LauncherDetail.ToolTip = detail; }

                // —— 展开区：链路步骤、提示、勾选、继续/跳过 ——
                if (row.Steps is not null)
                    row.Steps.Text = chain is null ? "—" : chain.Steps;
                if (row.Message is not null)
                {
                    // 空闲态的那句「尚未启动」跟右边的状态胶囊重复，只在真有事要说的时候才写
                    var message = chain is { State: not "idle" } ? chain.Message : "";
                    row.Message.Text = message.Length > 0 ? message
                        : !_serviceConnected ? "正在连接后台服务…"
                        : row.Target.Length > 0 && target is null ? "还没检测到这个程序：先在下方选择版本并安装。"
                        : "";
                    row.Message.Visibility = row.Message.Text.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
                }
                if (row.Participate is not null)
                {
                    row.Participate.IsChecked = chain?.Enabled ?? false;
                    row.Participate.IsEnabled = chain is { Required: false };
                    row.Participate.ToolTip = chain is { Required: true }
                        ? "基础程序必须参与一键启动"
                        : "勾选后「一键启动」会带上它";
                }
                if (row.Continue is not null)
                    row.Continue.Visibility = chain is { CanContinue: true } ? Visibility.Visible : Visibility.Collapsed;
                if (row.Skip is not null)
                    row.Skip.Visibility = chain is { CanSkip: true } ? Visibility.Visible : Visibility.Collapsed;
            }
        }
        finally { _startupRendering = false; }

        UpdateOverviewHeadline();
    }

    private static string RowTone(ManagerClient.Target target) => target.State switch
    {
        "failed" or "conflict" => "danger",
        "starting" or "stopping" => "warning",
        _ => "",
    };

    /// <summary>状态灯跟状态胶囊同源：链路失败了就亮红，等人在网页里完成一步就呼吸，
    /// 否则才看进程本身。不然会出现「绿点 + 需要处理」这种自相矛盾的一行。</summary>
    private static string DotStateFor(RowUi row, ManagerClient.Target? target, bool running, ChainState? chain)
    {
        if (row.Id is "manager" or "launcher") return "running";
        if (chain is not null)
        {
            if (chain.State == "failed") return "failed";
            if (chain.State is "waiting" or "blocked") return "starting";
        }
        if (target is null) return "stopped";
        if (target.State is "starting" or "stopping") return target.State;
        return running ? "running" : target.State == "failed" ? "failed" : "stopped";
    }

    private string DetailFor(RowUi row, ManagerClient.Target? target, ChainState? chain, bool running)
    {
        if (row.Id == "launcher") return "就是当前这个窗口 · 构建 " + BuildStamp;
        if (row.Id == "manager")
            return !_serviceConnected ? "正在连后台服务…"
                : target is null ? "随启动器自启" : $"pid {target.Pid} · 端口 {string.Join("/", target.Ports)}";
        if (target is null)
            return !_serviceConnected ? "正在连后台服务…"
                : row.Id == "pi" ? "尚未配置 Pi 桥接" : "尚未安装，先展开这一行选择版本安装";

        var parts = new List<string>();
        if (running)
        {
            if (target.Pid is int pid) parts.Add("pid " + pid);
            if (target.Ports.Length > 0) parts.Add("端口 " + string.Join("/", target.Ports));
            if (target.UptimeSec is double uptime) parts.Add(Describe(uptime));
        }
        else if (target.Ports.Length > 0) parts.Add("端口 " + string.Join("/", target.Ports));

        if (row.Id == "core" && running) parts.Add(target.Ready ? "业务就绪" : "业务未就绪");
        if (row.Id == "pi" && running) parts.Add(TunnelHealthy(target) ? "已连通" : "未连通");
        if (row.Id == "ingress" && running)
            parts.Add(target.OneBotConnected is true ? "SnowLuma 已连接"
                : target.OneBotConnected is false ? "等待 SnowLuma 连接" : "连接状态未知");
        if (!running && target.State == "failed" && target.LastError.Length > 0) return target.LastError;
        if (chain is { State: not "idle" and not "done" } && chain.Message.Length > 0 && !running)
            return chain.Message;
        return parts.Count == 0 ? "尚未运行" : string.Join(" · ", parts);
    }

    private static string Describe(double seconds) => seconds < 90 ? $"{seconds:0} 秒"
        : seconds < 5400 ? $"{seconds / 60:0} 分钟"
        : $"{seconds / 3600:0.0} 小时";

    /// <summary>页头那句「现在是什么状况」：优先说需要人处理的事，其次说在跑什么。</summary>
    private void UpdateOverviewHeadline()
    {
        var names = new List<string> { "core", "snowluma", "ingress" };
        if (_targets.ContainsKey("tunnel")) names.Add("tunnel");
        var running = names.Count(TargetRunning);

        string headline;
        var attention = _chains.Values.FirstOrDefault(chain => chain.State == "failed");
        var waiting = _chains.Values.FirstOrDefault(chain => chain.State == "waiting");
        if (!_serviceConnected)
            headline = "管理服务未连接，正在重试…";
        else if (attention is not null)
            headline = $"「{attention.Title}」需要处理：{attention.Message}";
        else if (waiting is not null)
            headline = $"「{waiting.Title}」在等你完成一步：{waiting.Message}";
        else if (running == 0)
            headline = "现在没有程序在运行。点「一键启动」按顺序启动已勾选的程序。";
        else
        {
            var titles = new List<string>();
            if (TargetRunning("core")) titles.Add("弱链接 Core");
            if (TargetRunning("ingress")) titles.Add("消息接入");
            if (TargetRunning("snowluma")) titles.Add("SnowLuma");
            if (_targets.ContainsKey("tunnel") && TargetRunning("tunnel")) titles.Add("Pi 桥接");
            headline = "正在运行：" + string.Join("、", titles)
                + (running == names.Count ? "。全部就绪。" : "。");
        }
        OverviewHeadline.Text = headline;

        // 胶囊只说"要不要你动手"，不再复述运行数量 —— 那是行本身就能看懂的事
        if (!_serviceConnected) SetPill(OverviewChip, OverviewChipText, "服务未连接", "danger");
        else if (attention is not null) SetPill(OverviewChip, OverviewChipText, "需要处理", "danger");
        else if (waiting is not null) SetPill(OverviewChip, OverviewChipText, "等待操作", "warning");
        else if (running == names.Count) SetPill(OverviewChip, OverviewChipText, "全部就绪", "success");
        else if (running > 0) SetPill(OverviewChip, OverviewChipText, "部分运行", "");
        else SetPill(OverviewChip, OverviewChipText, "全部停止", "");
    }

    private string BuildStamp { get; set; } = "";
}
