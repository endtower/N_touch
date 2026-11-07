# 视觉体系（CMF）

> 覆盖 `web_frontend/`（网页前端）与 `launcher/`（AbyssLauncher.exe 桌面外壳）。
> 状态：**已落地**。颜色、形态、字体只在**一个源头**定义，两端产物由脚本生成。
> SnowLuma 的 WebUI 是第三方页面，**不在**本体系内（见第六节）。
>
> 本文档位于 `design/`（**受版本控制**）。参考图与运行截图保留在本地 `refs/`，不随源码上传；两端布局与配色以下方令牌和源码为准。

---

## 一、一句话

改颜色**只改** [`tokens.json`](tokens.json)，然后跑生成器。两端（CSS 变量 / WPF 资源字典）自动同步。

```
design/tokens.json                       ← 唯一真值
        │
        ├─ python design/build_tokens.py ─┬─→ web_frontend/src/tokens.css
        │                                 ├─→ launcher/Theme/Shared.xaml   （形态+字体，端无关）
        │                                 ├─→ launcher/Theme/Dark.xaml     （暗色）
        │                                 └─→ launcher/Theme/Light.xaml    （亮色）
        │
        └─ python design/build_tokens.py --check   校验生成物是否与源头一致
           python design/check_theme_keys.py       校验 WPF 资源引用是否都能解析
           python design/check_web_vars.py         校验网页 CSS 变量是否都有定义
```

**生成物不要手改** —— 下次生成会被覆盖。手写的部分只有：

| 文件 | 内容 |
|---|---|
| [`launcher/Theme/Controls.xaml`](../launcher/Theme/Controls.xaml) | 控件样式与模板（Button / CheckBox / TextBox / ScrollBar / ToolTip / 轨道按钮） |
| [`launcher/ChamferBorder.cs`](../launcher/ChamferBorder.cs) | 斜切角容器（WPF 侧形态语言原语） |
| [`launcher/ThemeManager.cs`](../launcher/ThemeManager.cs) | 主题切换、落盘、广播 |
| [`web_frontend/src/style.css`](../web_frontend/src/style.css) | 布局与组件外观 + 旧变量名兼容映射 + 斜切角工具类 |

### 改颜色的正确姿势

```bash
python design/build_tokens.py          # 生成两端产物
python design/build_tokens.py --check  # 生成物是否与源头一致（不一致返回 1，可进 CI）
python design/check_theme_keys.py      # WPF 资源引用是否都能解析
python design/check_web_vars.py        # 网页 CSS 变量是否都有定义
```

> 后两个脚本守的是同一类问题：**引用了不存在的名字，静默失效**。
> WPF 的 `DynamicResource` 找不到键不报错、CSS 的 `var()` 找不到变量也不报错 ——
> 构建全过、只是样式悄悄不生效。并行改样式时这类问题会成倍出现。
> （`check_web_vars.py` 上线第一次跑就抓出 `--shadow-*` 从未生成、以及两个既有死引用。）

---

## 二、色板与来源

配色来自用户提供的 **CMF 印象卡**（`refs/_ref_cmf.png`）：

印象卡原图仅保留本地，以下表格记录实际采用的色值。

| 卡面标注 | 占比 | 我们的用法 |
|---|---|---|
| `#404040` | 70% | 保留为色阶**强调档** `bg-peak` |
| `#FFFFFF` | 15% | 文字主色 |
| `#FA063E` | 10% | 强调色 `accent` |
| 5 个彩点 | ~5% | 标签五色 |

> **两处对卡面的刻意偏离（重要）：**
>
> 1. **卡片上"#404040 / 70%"那块的实测像素是 `#1A1A1A`→`#333333` 的渐变，整张卡里根本没有 `#404040`。**
>    所以实际的中性色阶取自**卡片背景的实测值**（`#181818`→`#333333`），而 `#404040` 按卡面标注
>    保留为色阶顶端的"强调档"（悬停/抬起面），不浪费也不被"70%"绑住。
> 2. **百分比不按字面执行。** 用户明确指示"用这些颜色自己组暗色，不要参考百分比"。
>    所以这三个值是**配色来源**，不是面积约束。

### 中性色阶（暗色，取自卡片背景实测）

```
#141414  bg-sunken    日志底 / 代码块
#181818  bg-app       主内容区
#1E1E1E  bg-surface   面板 / 侧栏
#262626  bg-elevated  二级面
#2C2C2C  bg-raised    浮层 / 弹窗
#333333  bg-hover     悬停 / 激活
#3A3A3A  bg-active    按下
#404040  bg-peak      强调档（卡面标注值）
```

文字阶 `#FFFFFF` / `#C9C9C9` / `#939393` / `#5C5C5C` 全部取自卡片实测像素。
描边 `#262626` / `#333333` / `#4D4D4D`。

### 亮色：同一中性系的翻转

亮色不是另找一套色板，而是**同一套中性色反过来用**：

- 面板阶取自 Codex 亮色截图实测 —— `#FFFFFF` / `#FBFBFC` / `#F3F3F5` / `#EEEFF0` / `#E9EAEA`
- **`#1A1A1A` 在暗色是最深底色，在亮色是正文色** —— 这正是"同一体系翻转"的体现

### 强调与标签

- 强调色 `#FA063E`，**两套主题一致**（品牌色不随主题变）
- 标签五色取自卡片右侧 5 个彩点（像素采样）：`#40B0FF` 信息 · `#7DD043` 成功 · `#FF991C` 警告 · `#FF1B23` 危险 · `#FF7EB3` 特殊
- 亮色下标签色**压暗**，以保证在白底上的对比度达标

### 悬停提示

反相气泡，底/字是独立令牌（`tooltip-bg` / `tooltip-fg`）：暗色用抬高的深灰底，亮色用近黑底，两种主题下都能盖住内容。

---

## 三、形态语言：锐边 / 斜切 / 圆角

CMF 文案里的这三个词是**形态语言**，不只是换色。

| | Web | WPF |
|---|---|---|
| 圆角 | `--radius-xs…pill` | `RadiusXs…RadiusPill`（`CornerRadius`） |
| 斜切 | `.chamfer-tl/tr/bl/br/diag` 工具类（`clip-path`） | [`ChamferBorder`](../launcher/ChamferBorder.cs) 容器 |
| 描边 | `--border-hair/thick` | `BorderHair/Thick`（`Thickness`） |

**坑：`clip-path` 会连描边一起裁掉。** 所以斜切元素不要直接用 `border`，
要用"底色 + inset 内阴影"或在外层再包一层来表现描边。

已应用斜切的位置：网页左侧轨道的品牌标记、启动器品牌标记、启动器的控制卡片。

---

## 四、两端命名映射

生成器按固定规则转换，所以令牌名只需要记住一套：

| 令牌（tokens.json） | Web | WPF |
|---|---|---|
| `bg-surface` | `var(--bg-surface)` | `{DynamicResource BrushBgSurface}` |
| `accent-hover` | `var(--accent-hover)` | `{DynamicResource BrushAccentHover}` |
| `radius-md` | `var(--radius-md)` | `{DynamicResource RadiusMd}` |
| `space-4` | `var(--space-4)` | `{DynamicResource Space4}` / `Pad4` |
| `font-sans` | `var(--font-sans)` | `{DynamicResource FontSans}` |

规则：WPF 侧颜色加 `Brush`/`Color` 前缀、其余按 `-` 分段转 PascalCase。

> **`Space4` 与 `Pad4` 为什么有两个：** `Margin` 要 `Thickness`、但有些地方只想要一个数。
> 更关键的是 **XAML 属性串里不能嵌 markup extension** ——
> `Margin="0,0,{DynamicResource Space2},0"` 是**非法**的，解析期就报错。
> 所以需要 `Padding` 的地方用 `Pad*`（`Thickness`）令牌，带方向的边距直接写字面量。

### 旧变量名兼容映射

`style.css` 与各组件的 scoped 样式里历史上有**数百处** `--text-1` / `--accent-2` 这类旧名字。
在 `style.css` 顶部把它们一次性指向新令牌，避免同一轮改上千处：

```css
:root {
  --text-1: var(--text-primary);
  --accent-2: var(--tag-success);
  --accent-2-soft: color-mix(in srgb, var(--tag-success) 16%, transparent);
  /* … */
}
```

**新写的代码请直接用 tokens.css 里的语义令牌**，不要再引入旧名字。

---

## 五、主题联动（exe ↔ 网页）

启动器用 WebView2 内嵌自家网页，所以主题是**双向**打通的：

```
用户在网页点切换 ─→ localStorage ─→ chrome.webview.postMessage
                                            │
                                            ▼
用户在 exe 点切换 ─→ ThemeManager ─→ 换 ResourceDictionary
                          │                 │
                          └─→ PostWebMessageAsJson ─→ 网页应用主题（不回推，避免回声）
```

- **落盘**：`<数据根>/launcher/ui.json` 的 `theme` 字段。写回时**只动 theme，未知字段原样保留**（与本项目配置约定一致）。
- **源头**：`ThemeManager.Initialize()` 在建窗口前执行，避免先闪一下默认暗色再跳。
- **网页侧**：[`api/hostTheme.ts`](../web_frontend/src/api/hostTheme.ts)。独立浏览器里 `window.chrome?.webview` 不存在，全部退化成空操作，不影响单独运行。

### 安全边界（任务书 §7）

任务书规定：**不向第三方网页注入管理接口、访问令牌或任意宿主执行能力**，
且 SnowLuma 网页不得自动获得宿主桥接。因此：

- 桥接**只对自家的 Core 页**开启，靠 `_webSource == "core"` 拦截，SnowLuma 页不挂；
- 协议**只有一条白名单消息** `{ type: 'abyss-theme', value: 'dark'|'light' }`，其余一律忽略；
- 消息体里**没有**管理接口、令牌或任何执行能力，纯展示用途。

改这块前请先读 [`MainWindow.xaml.cs`](../launcher/MainWindow.xaml.cs) 里 `PushThemeToWeb` 与 `WebMessageReceived` 的注释。

### 实测结果（已跑通双向）

用隔离实例 + 页面内 `ExecuteScriptAsync` 探针实测（宿主初始设为 `light`，网页默认是 `dark`）：

| 步骤 | 观测 | 结论 |
|---|---|---|
| 宿主启动并导航到 Core 页 | 网页 `{"ls":"light","doc":"light","host":true}` | exe → 网页推送生效；`window.chrome.webview` 存在 |
| 页面内 `postMessage({type:'abyss-theme',value:'dark'})` | `ui.json` → `theme: dark`，宿主主题 → `dark` | 网页 → exe 回传生效 |
| 同上，检查其余字段 | `"unknownField": "保留我"` 原样还在 | **写回只动 theme，未知字段保留** |
| 宿主再推回 | 网页变 `{"ls":"dark","doc":"dark"}` | 往返收敛，无回声死循环 |

> 探针只是验证手段，验证完已从代码里移除；结果记录在此备查。

---

## 六、范围

| 皮肤 | 是否在本体系内 |
|---|---|
| Web 前端（Vue3 + Element Plus） | ✅ |
| exe 外壳（WPF `AbyssLauncher`） | ✅ |
| SnowLuma WebUI（第三方 NapCat 系，独立打包） | ❌ 不在范围内，保持原样（任务书 §7） |

---

## 七、已知坑（改之前看这里）

1. **WPF 必须用 `DynamicResource`。** 用 `StaticResource` 取颜色，运行时切主题**不会重绘**。
2. **`DynamicResource` 键不存在时不报错、不崩溃**，只是渲染成 null（字看不见、底色丢失），
   构建也一样过。所以新增/重命名令牌后**必须**跑 `python design/check_theme_keys.py`。
3. **XAML 属性串里不能嵌 markup extension**（见第四节的 `Pad*` 说明）。
4. **`Decorator` 没有 `Padding` 属性**（只有 `Border` 有），`ChamferBorder` 自己实现了一个，
   并把 `Padding` 算进 `MeasureOverride`/`ArrangeOverride`，否则样式里的 `Padding` 会被无声忽略。
5. **csproj 去掉了 `System.Drawing` 与 `System.Windows.Forms` 的隐式 using**
   （`UseWindowsForms` 会带进来，与 WPF 的 `Brush`/`Size`/`Application`/`MessageBox` 撞名，CS0104）。
   托盘图标代码全部用全限定名。
6. **日志配色分两类**：级别色（DEBUG 青 / WARN 黄 / ERROR 红）与 ANSI 色属于**日志内容**语义，
   保持固定；只有"中性前景色"跟主题走（由 `LogPresentation.Colourise` 的 `defaultColour` 参数传入）。
7. **别用系统截屏 / `PrintWindow` 验证这个窗口。** 它抓含 WebView2 的窗口不可靠：
   实测轨道底部的按钮**整块丢失**、WebView2 区域抓成错乱内容 —— 看起来像"控件根本没渲染"，
   极易把人带偏（本次就因此误判了一轮布局）。**用第九节的快照钩子。**
8. **`RenderTargetBitmap` 必须在 WebView2 初始化之前调用**，之后调用会抛异常
   （WebView2 的合成不参与 WPF 渲染管线）。快照钩子已经卡在正确的时机上。
9. **`DockPanel` 里没显式写 `Dock` 的子元素默认按 `Dock=Left` 处理**，
   混用 `Top`/`Bottom` 时落位依赖声明顺序，很容易排到意外位置。
   侧栏/轨道这类固定分区的布局，直接用 `Grid` 行定义，确定性更好。
10. **`WindowStyle="None"` 之后，标题栏是我们自己的 WPF 内容**，
    所以它天然跟主题走（不再依赖 DWM）。但 `WindowChrome` 的 `CaptionHeight` 只管拖动，
    **栏内要接收点击的控件必须标 `WindowChrome.IsHitTestVisibleInChrome="True"`**，
    否则点不动。
11. **改完代码别急着清理构建暂存目录。** 部署要求先把新产物复制到 `bin/Release`，
    而启动器在运行时那一步必然失败 —— 此时若顺手清了暂存目录，新版本就没了。
    **先确认落位成功，再清理**（本次连踩两次）。
12. **`ResourceDictionary` 根元素不能挂事件处理器**（报 MC6024）。`Theme/Controls.xaml` 里
    模板内部的按钮不能写 `Click="..."`。两条出路：改用 `RoutedCommand` + 窗口的
    `CommandBinding`；或者 —— 更省事 —— **把按钮放进窗口自己的 XAML**
    （标题栏的刷新键就是这么做的，根本不进模板，也就没这个限制）。
13. **同一个字典里 `StaticResource` 不能前向引用**：只能引用**写在它前面**的键。
    本次有个样式排在了它依赖的样式之前，运行时直接抛
    `XamlParseException: 无法找到名为"…"的资源`，窗口都建不出来（编译完全不报）。
    跨位置的引用一律用 `DynamicResource`，或把被引用的键挪到前面。
14. **用 `Margin` 把子元素排到父容器外，会被布局压成 0 尺寸、完全不渲染。**
    非 `Stretch` 对齐时 WPF 按"可用尺寸"取小值，父容器只有 42px 宽而左边距 46px →
    可用为负 → 宽度 0。要在容器外浮出元素，用 `Canvas`（按子元素期望尺寸排布、不压缩）
    配合父层 `Panel.ZIndex` 保证画在主区之上。（但即使这样做了也未必可见 —— 见第 16 条。）
15. **GUI 崩溃会留下 `launcher_crash.log`**（写在数据根）。`App.OnStartup` 里挂了
    `DispatcherUnhandledException` 与 `AppDomain.UnhandledException`。
    没有它，故障现象只是"窗口没出来"，无从下手 —— 本次靠它一次定位到 MC6024/静态资源前向引用。
16. **WPF 内容画不到 WebView2 上面（airspace 限制）—— 最容易误判的一条。**
    WebView2 是独立的 HWND 子窗口，走另一套合成。任何 WPF 子元素（哪怕溢出到主区的
    `Canvas`、调高 `Panel.ZIndex`、`Adorner`）都会被它**整个盖住**。
    表现极具迷惑性：**目标运行时（WebView2 可见）浮层不显示，目标停掉时（显示 WPF 占位面板）
    反而正常** —— 很容易误判成"逻辑分支写错了"。
    真要在网页之上浮东西，只有 `Popup`（自带顶层窗口）。但它的内容不在模板视觉树里，
    `TemplateBinding` 与模板内 `TargetName` 触发器都失效，开合与状态得全在 code-behind 里写。
    > **本项目的结论：这类浮层不值得。** 刷新按钮最终直接放进标题栏 ——
    > 永远可见、不涉及 Popup、不涉及层级问题，一次消掉一整类 bug。
    > 以后想"浮在网页上"之前，**先问能不能换个位置**。
17. **`RenderTargetBitmap` 拍不到 Popup**（它只渲染主窗口的视觉树）。
    所以浮层类东西既不能靠快照看，也不能靠外部鼠标点（本会话控制不了光标），
    属于**验证手段够不着**的区域 —— 能不往这类形态上做就不做。
18. **`DoubleAnimation` 只给 `To` 不给 `From` 时，起点取属性当前值 —— 那个值是 `NaN` 就会崩。**
    本次为了量浮层自然宽度把 `Width` 设成 `double.NaN`(auto)，随后启动动画直接抛
    `AnimationException: …无法使用 "NaN" 的默认 origin 值`，进程当场挂掉。
    **凡是可能遇到 auto/NaN 的属性（`Width`/`Height`/`MaxWidth`…）做动画，`From` 一律显式给。**
19. **半透明色放进 Popup 会与本体"叠两次"。** 若浮层要与一个半透明色块无缝相接，
    两者重叠的那几像素会被叠两遍 → **一道明显的竖缝**。
    要么给浮层用**预先算好的不透明等价色**（半透明色与其底下那层色的混合结果），
    要么干脆别让它们重叠。

---

## 九、怎么目检（重要）

启动器有四个**环境变量钩子**，专用于在**不影响正在运行的主实例**的前提下做验证：

| 环境变量 | 作用 |
|---|---|
| `ABYSS_LAUNCHER_MUTEX` | 换一个单实例互斥名。主实例还在跑时，也能起一个隔离实例做冒烟检查，两者不会互相唤醒。 |
| `ABYSS_LAUNCHER_SNAPSHOT` | 启动后把窗口用 WPF `RenderTargetBitmap` 合成一张 PNG 写到该路径。 |
| `ABYSS_LAUNCHER_SNAPSHOT_DELAY` | 延后若干毫秒再抓图，用来观察需要先发生某个状态变化再截图的情形。 |
| `ABYSS_LAUNCHER_SECTION` | 指定启动停在哪个区块（`overview` / `coreweb` / `snowluma` / `logs`，默认 `overview`），配合快照可逐区块出图。 |
| `ABYSS_DATA_DIR` | （既有）换数据根，顺带隔离 WebView2 的数据目录，避免与主实例抢锁。 |

```powershell
$env:ABYSS_LAUNCHER_MUTEX    = 'AbyssLauncher_Smoke'
$env:ABYSS_DATA_DIR          = 'E:\ABYSS\N_Touch\build\smoke_data'
$env:ABYSS_LAUNCHER_SECTION  = 'coreweb'      # 想验哪个区块
$env:ABYSS_LAUNCHER_SNAPSHOT = 'E:\ABYSS\N_Touch\build\coreweb.png'
# 想验浅色就先写 ui.json： {"theme":"light"}
$proc = Start-Process .\launcher\bin\Release\net8.0-windows\AbyssLauncher.exe -PassThru
Start-Sleep 9
Stop-Process -Id $proc.Id -Force      # 用 PID 精确结束，别误杀主实例
```

> 快照失败时会在目标路径旁写一个 `.err` 文件（而不是静默失败）——
> 静默失败正是"看起来没渲染"这类误判的源头。

**快照验不了两样东西**：① 悬停等交互态 —— 需要外部先把鼠标移上去，而这个会话里
`SetCursorPos` 会被环境重置（设到 A，读回来是 B），试过把窗口摆到光标底下也没用，
因为**移动窗口不产生 `WM_MOUSEMOVE`**，WPF 的 `IsMouseOver` 不会更新；
② 任何在 `Popup` 里的内容（快照只渲染主窗口视觉树）。

> 这两条限制合起来说明一件事：**别把关键交互做成"悬停才出现的浮层"**，
> 否则你既看不到它、也点不到它、还验不了它。UI 尽量落在主窗口的常规布局里。

**能测的行为就去测，别只测「编译通过」。** 例：标题栏 × 的行为由设置决定，
这个不靠截图也能验 —— 给窗口发 `WM_CLOSE`，看进程是存活（隐藏到托盘）还是退出：

```powershell
Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;
public static class WC { [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h,uint m,IntPtr w,IntPtr l); }'
[void][WC]::SendMessage($proc.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)   # WM_CLOSE
```

---

## 十、构建、部署与验证

```bash
# 前端（含 vue-tsc 类型检查）
cd web_frontend && npm run build

# 启动器（项目自带便携 SDK 在 .tools/dotnet）
$env:DOTNET_ROOT = "E:\ABYSS\N_Touch\.tools\dotnet"
& .tools/dotnet/dotnet.exe build launcher/AbyssLauncher.csproj -c Release

# 令牌生成物是否与源头同步 + 资源引用是否都能解析
python design/build_tokens.py --check
python design/check_theme_keys.py
```

### 部署到正式位置（有坑，按顺序来）

启动器**运行时会锁住** `bin/Release/.../AbyssLauncher.exe` 和 `.dll`，
直接构建到正式位置会在复制那一步失败（MSB3027/MSB3021），而**编译其实是好的**。
所以顺序必须是：

```powershell
# 1. 构建到暂存目录（总是成功，同时也就验证了编译）
dotnet build launcher/AbyssLauncher.csproj -c Release -o build\launcher_new

# 2. 退出启动器。注意：关窗口没用（默认隐藏到托盘、进程不退、文件照样锁着）。
#    要么托盘「停止全部并退出」（会连 Core/SnowLuma 一起停），
#    要么结束进程（Core/SnowLuma 由管理服务托管，会继续跑）。

# 3. 复制到正式位置
Copy-Item build\launcher_new\* launcher\bin\Release\net8.0-windows\ -Force

# 4. 确认落位成功之后，才清理暂存目录
Remove-Item build\launcher_new -Recurse -Force
```

> 前端没有这个问题：Core 直接伺服 `web_frontend/dist`，`npm run build` 一跑就是最新的。

---

## 十一、并行改风格时的分工与边界

多人（或多 agent）同时改风格时，按**所有权**切，不要按"谁顺手"切。

### 代价分两层，别搞混

| 改什么 | 代价 | 怎么改 |
|---|---|---|
| **令牌层**：颜色、字体、圆角、间距、阴影 | 分钟级 | 改 `tokens.json` → 跑生成器，两端同时生效 |
| **观感层**：形态语言（斜切去留）、布局、组件重做 | 天级 | 设计决策 + 在两端逐个组件落地，**不能只靠改令牌** |

> "换配色很便宜"只对第一层成立。第二层要动 `style.css` 与各 `.vue` 的 scoped 样式、
> WPF 的 `Controls.xaml`，是实打实的改动量。

### 所有权表

| 角色 | 可写 | 只读（生成物，别手改） |
|---|---|---|
| **令牌 owner（只设一个）** | `design/tokens.json` | — |
| **Web agent** | `web_frontend/src/**`（`style.css`、`components/*.vue`） | `web_frontend/src/tokens.css` |
| **exe agent** | `launcher/**`（`Theme/Controls.xaml`、`MainWindow.xaml`、`*.cs`） | `launcher/Theme/{Shared,Dark,Light}.xaml` |
| **设计 / 评审** | 不写文件，只出决策 | 全部 |

Web 与 exe 两个 agent 的文件集**几乎完全不相交**，可以真并行。
唯一串行点是令牌 owner —— 两个人同时改 `tokens.json` 必定互相覆盖。

### 三条硬规则

1. **令牌名是接口，冻结。** 改名会让另一端的生成物一起变，而引用旧名的地方在
   **两端都不会报错**（见第一节末尾）。要改名必须令牌 owner 统一改，两端同时跟进。
2. **别手改生成物**：`web_frontend/src/tokens.css`、
   `launcher/Theme/Shared.xaml`、`Dark.xaml`、`Light.xaml`。下次生成即覆盖。
3. **改完跑三个校验器**（第一条末尾那三条命令），三条全绿才算完成。

### 一个容易忽略的历史债

`web_frontend/src/style.css` 顶部有一段**旧变量名 → 新令牌的兼容映射**
（`--text-1` → `--text-primary`、`--accent-2` → `--tag-success` …）。

它让当初的迁移不必一次改上千处引用，但代价是**同一个视觉属性有两个名字**。
并行改样式时注意：改 `--text-primary` 会影响所有还在用 `--text-1` 的地方。
新写的代码**一律用新令牌名**，别再引入旧名。

---

## 附录：外壳结构与布局

启动器窗口的分区（自外向内）：

运行截图仅保留本地，窗口分区如下：

```
┌──────────────────────────────────────────────────────────┐
│ 标题栏(40px) [◤A] 弱链接  ↻ ⚙ │ − □ ×       │ ← 自绘
├────┬──────────────┬──────────────────────────────────────┤
│ 图 │ 二级侧栏      │ 主区                                  │
│ 标 │ (236px，       │  总览卡片 / 网页 / 日志正文            │
│ 轨 │  仅总览与日志  │                                      │
│ 道 │  展开)         │                                      │
├────┴──────────────┴──────────────────────────────────────┤
│ 状态栏（网页区块在这里显示页面名与运行状态）                  │
└──────────────────────────────────────────────────────────┘
```

**标题栏是自绘的**（`WindowStyle="None"` + `WindowChrome`），不是系统标题栏：

- 左半区（品牌 + 标题）靠 `WindowChrome.CaptionHeight="40"` 自动获得拖动与双击最大化；
- 右侧按钮必须标 `WindowChrome.IsHitTestVisibleInChrome="True"` 才点得到；
- 按钮从左到右：**↻ 刷新当前网页**（只在网页区块可用，其余区块置灰）、
  **⚙ 设置**、分隔线、**− □ ×**；
- − 最小化；□ 最大化/还原（图标随状态切换）；× 的行为**由设置决定**；
- 最外层 1px `Border` 定义窗口边缘 —— `WindowStyle=None` 之后没有系统边框了。

> **为什么刷新键在标题栏，而不是"悬停时从轨道浮出"**：网页区域是 WebView2（独立 HWND），
> WPF 内容画不到它上面；浮层只能用 `Popup`，而 Popup 的内容既不在视觉树里（快照拍不到）、
> 又要自己写开合与动画，连带引出一串 bug（崩溃过一次）。
> 放进标题栏后这些问题一次消失 —— 永远可见、不涉及层级、可以直接截图验证。
> 详见第七节第 16~19 条。

**二级侧栏是条件展开的**：

| 区块 | 侧栏 | 原因 |
|---|---|---|
| 总览 | 236px，显示服务状态 | 状态信息是这一页的主体 |
| 日志 | 236px，显示来源切换与筛选 | 控件多，放主区会挤掉日志正文 |
| Core / SnowLuma 页面 | **收成 0** | 宽度全让给网页；页面名与状态移到状态栏，刷新在标题栏 |

**设置**（标题栏 ⚙）：目前只有一项 —— 点 × 时「隐藏到托盘」还是「退出程序」。
存在 `<数据根>/launcher/ui.json`（与主题同一个文件，未知字段保留；读写统一走 `UiSettings`）。
选「退出程序」时**不再二次确认** —— 用户已经在设置里明确选过了。
