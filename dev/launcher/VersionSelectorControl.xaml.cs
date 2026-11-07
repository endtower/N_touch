using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;

namespace AbyssLauncher;

/// <summary>
/// 组件行里常驻的版本选择：窄下拉 + 刷新图标，跟该行的下载/启动/停止排在同一条线上。
/// 只管选择与刷新；选中的版本由外面的 ComponentUpdateControl 读 SelectedVersion。
/// </summary>
public partial class VersionSelectorControl : UserControl
{
    public event Action? SelectionChanged;
    public event Action? RefreshRequested;
    private bool _settingSelection;

    public VersionSelectorControl()
    {
        InitializeComponent();
        Picker.SelectionChanged += (_, _) => { if (!_settingSelection) SelectionChanged?.Invoke(); };
        Picker.AddHandler(TextBoxBase.TextChangedEvent,
            new TextChangedEventHandler((_, _) => { if (!_settingSelection) SelectionChanged?.Invoke(); }));
        Refresh.Click += (_, _) => RefreshRequested?.Invoke();
    }

    public string SelectedVersion => Picker.Text.Trim().TrimStart('v');

    /// <summary>铺版本列表；列表没变就不动，免得每 2 秒的轮询打断正在输入的下拉。返回是否换过列表。</summary>
    public bool SetVersions(IReadOnlyList<string> versions)
    {
        if (Picker.Items.Cast<string>().SequenceEqual(versions)) return false;
        _settingSelection = true;
        Picker.ItemsSource = versions;
        _settingSelection = false;
        return true;
    }

    public void SetSelectedVersion(string version)
    {
        _settingSelection = true;
        Picker.Text = version;
        _settingSelection = false;
    }

    public void SetEnabled(bool enabled) => Picker.IsEnabled = Refresh.IsEnabled = enabled;

    /// <summary>把焦点放到版本下拉上（验收脚本展开某组件后会用到）。</summary>
    public void FocusPicker() => Picker.Focus();
}
