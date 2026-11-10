# 一键启动与外部 AI 配置

用户打开启动器后，在「总览与控制」点击「一键启动」才执行业务流程。打开 EXE、读取状态或管理服务自启均不启动任何业务链路。

启动脚本分两份，按下面的顺序取用：

| 位置 | 属于 | 更新时 | 谁改 |
|---|---|---|---|
| `data\manager\startup_plan.py` | 用户 | 永不覆盖，也不进版本库 | 用户或他请的 AI |
| `app\components\manager\source\startup_plan.py` | 程序 | 随组件更新被替换 | 官方 |

`data` 里有用户脚本就用它，没有才用程序自带的那份。所以升级管理服务不会影响用户改过的启动流程；不想要自己的修改，删掉 `data\manager\startup_plan.py` 即可回到出厂流程。

两份都是可直接修改的普通源码，不需要扩展目录、插件 SDK 或独立打包。`startup_workflow.py` 提供步骤调度、依赖、等待、跳过与恢复。用户勾选是否参与一键启动；也可以单独启动任意链路。用户脚本旁边的辅助程序放 `data\manager\tools\`，与脚本一起归用户所有。

## 两个现有示例

| 链路 | 当前流程 | 就绪条件 |
|---|---|---|
| 主程序 | 启动 Core，复用现有的消息接入依赖 | 受管服务就绪 |
| SnowLuma · QQ | 启动 SnowLuma → 确认自动注入关闭 → 打开 QQ → 等小号登录 → 只对小号注入 → 等待 OneBot 连接 | 接入服务报告 OneBot 已连接 |
| Pi · 个人桥接 | 建立已有配置的 SSH 隧道，检查 Pi 桥接服务，等待桥接连接 | 接入服务报告桥接 ready 且没有 blocked |

### SnowLuma 的按账号注入

SnowLuma 配置、登录和内部存储由上游管理。它自带的 `hookAutoLoad` 是**全局开关**：打开后会对本机发现的每个 QQ 主进程无条件注入，没有按号码过滤的入口。因此本脚本保持它关闭，改用 `data/manager/tools/snowluma-hook.mjs` 做按账号注入：

1. 用 SnowLuma 自带的 native 组件枚举 QQ 主进程；
2. 账号到进程的对应关系取自「谁的 `<Tencent Files>\<QQ号>\nt_qq\nt_db\nt_msg.db` 正被某个 QQ 进程占用」——同一时刻只有登录该号的进程持有它，SnowLuma 自己认账号也是这套办法；
3. 只有助手号（`ASSISTANT_QQ`）已登录时，才对**那一个** pid 调用 `loadModuleManualAsync`；目标号没登录就一个都不注入，其它 QQ 进程一律不碰；
4. 注入是否成功以进程里出现 `\\.\pipe\mojo.<pid>.control` 为准，重复执行不会重复加载。

「等待小号登录」和「接入 SnowLuma」这两步都带只读 probe：用户自己点开 QQ 窗口登录小号后，链路自动往下走；一直没登录就一直停在等待状态（**暂停等，不判失败**），也可以在界面上点「继续」立即复查。

Pi 示例复用本机已经配置的个人桥接及隧道，不硬编码设备地址、账号、私钥或远端目录。远端守护程序若尚未启动，本例会报告失败供用户处理；外部 AI 可在 `start_pi` 前加入该自研程序的启动命令及健康检查。账号登录仍由用户完成。

两个平台共用主程序，基础服务就绪后独立执行。Pi 失败不阻止 QQ，QQ 等待登录不阻止 Pi。基础服务失败时，依赖它的链路显示等待前置链路；修复基础服务后继续执行。

## 给接手的外部 AI

1. 通过 `GET /v1/storage` 取得实际程序、数据和 `startup_script`（用户脚本）、`startup_example`（出厂最简版）两个路径；不要只改开发工程 `dev`。
2. 调用源码备份接口保存 manager；保留个人数据和第三方内部目录。
3. 安装、登记项目并配置凭据。凭据放在当前 `data/.env.dev`，不要写进脚本、日志或聊天。普通用户无需安装开发工具。
4. 只改 `startup_script` 指向的那一份，也就是 `data\manager\startup_plan.py`。程序目录里那份是出厂样板，改了会被更新覆盖。新增进程管理目标时修改同一管理服务的 `core/supervisor.py`，继续由 SupervisorService 持有进程。
5. 每个步骤使用稳定的 id。`run(ctx)` 必须可重复检查、避免重复创建进程；成功返回 `Outcome()`，需要用户处理返回 `Outcome.waiting("明确操作提示")`，失败抛出有用但脱敏的异常，或返回 `Outcome("failed", "原因")`。
6. 等待步骤可提供无副作用的 `probe(ctx)`。返回 True 后自动继续；没有 probe 时等待用户点击继续。点击继续会重跑当前步骤的检查，不会直接认定成功，也不会重放已完成步骤。
7. 修改脚本后先校验语法；调用 cancel 暂停旧流程，再调用 reload 加载源码，然后单独启动所需链路验证。重新加载不停止已运行程序；新的检查状态从头建立，步骤应复用已有实例。只有配置脚本有此热加载入口，修改管理服务框架或 supervisor 后仍需重启管理服务。

示例：

```python
async def login(ctx):
    if await actual_login_check(ctx):
        return Outcome(message="已登录")
    return Outcome.waiting("请在应用窗口登录，完成后点击继续")

Chain("my_platform", "我的平台", (
    Step("launch", "启动程序", launch_idempotently),
    Step("login", "检查登录", login, probe=actual_login_check),
), dependencies=("core",), enabled=True)
```

启动成功应当表示预期链路已就绪。仅进程存在或 WebUI 可打开不代表账号登录、注入或消息连接成功。验证不用真实对外发送消息。

## 控制与状态

| 接口 | 行为 |
|---|---|
| `GET /v1/startup` | 读取链路、步骤、状态和最近事件，不执行步骤 |
| `POST /v1/startup/start` | 空对象启动已勾选链路；`{"chain":"pi"}` 单独启动 |
| `POST /v1/startup/continue` | `{"chain":"pi"}` 从当前失败/等待步骤继续并复查 |
| `POST /v1/startup/retry` | 同样重试当前步骤 |
| `POST /v1/startup/skip` | `{"chain":"pi"}` 本轮跳过，可稍后继续 |
| `POST /v1/startup/cancel` | 暂停后续步骤，不停止已运行进程 |
| `POST /v1/startup/enable` | `{"chain":"pi","enabled":false}` 设置参与一键启动的选择 |
| `POST /v1/startup/reload` | 无在途步骤时重新读取启动源码，供外部 AI 使用 |
| `POST /v1/startup/stop` | 暂停后续启动，再按受管顺序停止所有受管目标 |

沿用本机 `X-Manager-Token` 鉴权。请求返回当前状态，步骤在后台执行；GUI 轮询进度。链路状态有 idle、pending、running、waiting、failed、blocked、done、skipped、cancelled。成功链路在同一轮重试中保留；单独点击已完成链路的启动会重新检查它，步骤必须幂等。

启用选择存入 `data/manager/startup-settings.json`；脱敏状态快照在 `data/logs/manager/startup-state.json`，最多保留最近 100 条事件。服务重启后重新检查，不将历史“成功”冒充当前就绪。用户脚本在 `data` 下，不受任何组件更新保护逻辑约束；程序目录里的出厂样板仍受原有更新保护，不能重置安装基线来解除保护。

## 界面本身也能改（`data/launcher/ui.json`）

启动器不再把「有哪些程序」写死在界面里。每一行显示与否、叫什么名字，都由 `data/launcher/ui.json` 决定，改完重开启动器生效，**不需要重新编译 EXE**。这个文件同样属于用户数据，组件更新不覆盖；启动器首次运行时会自动在它旁边放一份 `README.md` 说明。

```json
{
  "theme": "dark",
  "closeToTray": false,
  "rows": {
    "snowluma": { "visible": false },
    "core": { "title": "大脑" }
  }
}
```

| 键 | 作用 |
|---|---|
| `theme` | `"dark"` / `"light"`，与界面左下角切换等效 |
| `closeToTray` | 点 × 收进托盘继续跑（`true`）还是停止全部并退出（`false`） |
| `rows` | 按组件 id 覆盖：`visible`（`false` = 隐藏）与 `title`（换名字） |

组件 id 是 `core` / `snowluma` / `pi` / `ingress` / `manager` / `launcher`。

隐藏一行不只是看不见：

- 那一行上面的分隔线一起收掉；隐藏 SnowLuma 或 Pi 时，日志页对应的来源标签也收起来，且如果正停在那个来源上会退回 Core。
- 页头的「正在运行」和右上角状态不再把它算进去；它那条链路的失败/等待也不再在页头喊「需要处理」。
- 它那条链路会被自动取消「参与一键启动」（必需链路 Core 除外）——用户关掉了那一行，点「一键启动」就不该还去拉它。

判断「要不要显示」的唯一入口是 `MainWindow.IsRowShown()`：ui.json 开关 + 内置条件（Pi 需要后端真的有 tunnel 目标）。渲染、页头统计、链路同步都查同一个结论，不要另写一份判断。

**Pitfall**：区分「用户主动隐藏」和「运行时暂时没探测到」。Pi 行在没有 tunnel 目标时也不显示，但那是环境没配好，不是用户不要它。拿 `IsRowShown` 去同步「参与一键启动」会误关没配 Pi 的用户的 Pi 链路，必须用 `IsRowHiddenByUser()`（只看 ui.json）。日志来源的显隐同理用它 —— 后端还没返回 targets 的那一瞬间 `IsRowShown` 为 false，会把用户已经选中的来源踢走。

## 日志页也是配置（`data/launcher/logs.json`）

日志页左边那排来源（Core / SnowLuma / 消息接入 / Pi 隧道 / 管理服务）原本在三个地方各写死一份：XAML 里五个 `RadioButton`、`MainWindow.Logs.cs` 里一个数组、`LogPresentation.Read()` 里一串 `if (source == ...)`。现在整份来源表都来自 `data/launcher/logs.json`，由 `LogSources` 读取；代码里只剩一份**内置默认值**，用于文件不存在时落盘。加来源、改名字、换读哪个文件，都不需要重新编译 EXE。

```json
{
  "sources": [
    {
      "id": "core",
      "title": "Core",
      "row": "core",
      "streams": [
        { "files": ["logs/manager/core.out.log", "logs/core_stdout.log", "logs/app_*.log"],
          "pick": "newest" }
      ]
    }
  ]
}
```

| 字段 | 作用 |
|---|---|
| `id` | 唯一标识；程序用它记住「上次看的是哪个」，改 id 等于换新来源 |
| `title` | 标签文字 |
| `row` | 绑到哪个组件行；那行被隐藏时本来源跟着隐藏。空 = 始终显示 |
| `streams` | 由几段日志拼成，按顺序 |

每条 `stream`：`files`（相对数据根的路径模式，支持 `*` / `?`，也可写绝对路径）、`pick`（`"newest"` 取最新一份 / `"all"` 全部按时间从旧到新拼）、`label`（多段拼接时的分隔标记，默认 `stderr`）。

几处容易踩的地方：

- **路径相对数据根，不是程序目录。** 写 `logs/manager/core.out.log`，不要写 `app\...`。
- **轮转分片自动跳过。** Windows 的 `*.log` 通配会连带命中 `app_x.log.1`，那是一份旧片段；`LogSources` 统一过滤 `\.\d+$`，配置里不用写排除。
- **Core 要写三个候选再让 `newest` 挑。** Core 的 stdout 落点随启动方式变化（manager 托管 / 冻结版 core.exe / root logger 各写一处），只认死其中一个，进程换落点后会长期显示旧文件。
- **JSON 写坏不炸。** 解析失败退回内置默认，且**不覆写**用户文件；只有整份 `sources` 解析不出任何一条时才用默认值跑本期。
- **`sources: []` 等于空日志页。** 想恢复内置五个，删掉文件重开启动器即可重新生成。
- **`IsLogSourceShown()` 走 `IsRowHiddenByUser()`**，理由见上一节结尾。

写 JSON 统一用 `JsonFile.Write()`（`dev/launcher/JsonFile.cs`）。**不要**在别处直接 `node.ToJsonString(new JsonSerializerOptions { WriteIndented = true })` —— .NET 8 里只要节点里有嵌套的 `JsonArray`/`JsonObject` 就会抛 `JsonSerializerOptions instance must specify a TypeInfoResolver setting before being marked as read-only`（扁平对象侥幸不炸，很容易漏测）。`JsonFile.Write` 用 `Utf8JsonWriter` 自己写，不经过那套反射。
