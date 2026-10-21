# 组件独立更新

## 单 EXE 入口与 Core 手动确认（0.4.0 起）

公开下载只提供 `AbyssLauncher.exe`。它首次释放管理服务和更新器到
`%LOCALAPPDATA%/Abyss/app`，用户数据在相邻的 `data` 目录。
启动器也安装在该程序目录；以后双击原下载入口会打开已安装版本，组件更新后仍使用新版。
Core、SnowLuma 在启动器中按需安装；Core 的发布包固定版本，不拉取开发分支 HEAD。
Core 源码、匹配的已构建前端及 Python 依赖一起交付到
`components/core/source` 与 `components/core/runtime`。消息接入与该 Core 版本共用源码，
通过 Core 更新；现有旧版 EXE 组件仍可被识别和保留。

自动检查只请求版本清单。Core 的下载与安装必须分别打开确认窗口：
`review(operation=download)` → 用户确认 → `download`；
`review(operation=install)` → 用户确认 → `apply`。
后端确认令牌有效期十分钟、单次使用，绑定操作、版本、发布说明和当前文件。
版本或文件变化后必须重新确认。关闭窗口或取消不会执行后续动作。
本地源码改动（含新增、删除）默认阻止安装，不自动合并或覆盖。
自行修改过 Core 时，先保留改动副本或补丁，还原官方安装基线后再确认更新；随后在副本中把改动合入新版并验证。也可以在独立安装目录试用新版。更新器不会替用户判断结构改动是否兼容。

安装确认显示 `notes`、`breaking_changes`、最低组件版本 `requires`、数据策略
`data_policy` 和迁移/恢复说明 `migration_notes`。兼容要求不满足时不能安装。
新源码在隔离数据目录验证依赖、模块导入及 PRE 会话创建后，才停止相关服务。
源码组件安装会停止 Core 与消息接入，保留 SnowLuma 登录。
替换前备份程序及相应数据库、配置、人格到更新任务目录；备份仅留在本机。
新版本启动失败时恢复程序；声明涉及迁移或未声明数据兼容时，保持服务停止，
等用户按备份说明处理数据并单独确认恢复旧版。不会自动用旧数据库覆盖新消息。

公开构建（输出必须是新目录）：

```powershell
python scripts/build_public_release.py --output dist/public-0.4.0 --feed-base-url https://github.com/endtower/N_touch/releases/latest/download --package-base-url https://github.com/endtower/N_touch/releases/download/v0.4.0 --metadata releases/core-0.4.0.json
```

`download/AbyssLauncher.exe` 是唯一用户下载入口；`packages/` 中的 ZIP、同名 JSON、
latest 和 versions 清单需由发布者上传到指定地址，构建脚本不会自动发布。
后续构建加 `--previous-releases 上次的packages目录` 保留历史清单，历史 ZIP 保留在原永久地址。
当前默认更新源使用 GitHub 的 latest release：每次发布都应携带所有自有组件的 latest 与 versions 清单，先上传完整版本包，最后更新清单，避免其他组件的更新入口丢失。
启动器更新 ZIP 同时包含外部更新器；对用户的首次下载入口仍只有一个 EXE。
源码运行环境从构建机的 Windows Python 和已安装依赖生成，并记录准确依赖锁文件；
安装前的隔离检查会拒绝缺依赖或不能创建关键会话的包。
公共默认设置和初始化模块在 `release_defaults/`；不复制本机账号、允许列表、
群组、经验、人格 JSON、登录数据或 `.env.dev`。

从旧版升级顺序：先 manager 0.9.0，再 launcher 0.6.0，最后手动确认 Core 0.4.0。
源码仓库运行模式始终保留原代码，不能被组件安装覆盖。

以下为旧二进制组件及通用安装器的补充说明；Core 源码包按上述流程处理。

Core、SnowLuma、manager、launcher 分别维护版本、更新源和程序包。Core 安装会检查发布清单声明的最低组件版本；源码模式下 ingress 随 Core 维护，旧二进制模式保留独立 ingress 包。

更新选项位于启动器「总览与控制」，直接放在各组件启动、停止按钮下方。默认启动时及每 6 小时自动检查；检查不会自动下载安装。组件可选择版本，准备后升级、降级或重装，也可导入本地 ZIP。Core 仍需逐次确认，并通过兼容与本地修改检查。下载期间组件继续运行；安装按影响范围暂停相关服务。

点击「刷新版本」加载历史发布；SnowLuma 列出最近 100 个可校验的官方版本，也可直接输入更早的已发布版本号。自有组件使用发布的版本列表。下载、导入的安装包长期保留，本机更新前的程序备份也会列入版本选项，允许离线退回旧版本。所有旧包仍须通过完整性校验，用户配置与记忆不随程序版本回退。

## 安装边界

| 组件 | 更新的程序文件 | 保留的用户数据 |
| --- | --- | --- |
| Core | 新版 `components/core/`；旧版 `core.exe`、`core_internal/` | 配置、人格、数据库、媒体、记忆、日志 |
| Ingress | `ingress.exe`、`ingress_internal/` | 消息库、配置、令牌 |
| SnowLuma | JS、Node、native、client/webui、发行文档 | `config/`、`data/`、`logs/`，以及外部 QQ 用户数据 |
| Manager | `manager.exe`、`manager_internal/` | `manager/` 中的令牌、更新设置和安装记录 |
| Launcher | WPF 文件、.NET 运行依赖、WebView2 库、`updater/AbyssUpdater.exe` | 数据根下 `launcher/` 的设置和浏览器资料 |

管理服务是后台进程的唯一控制者。因此更新 **manager 本身**会暂时停止它持有的后台进程，换好 manager 后恢复此前运行的进程；其他组件版本不变。更新 launcher 会关闭并重新打开桌面窗口，后台服务继续运行。更新 ingress 会短暂断开 OneBot，Core/SnowLuma 自行重连。

正式管理服务优先使用受管源码组件内的 Python 启动 Core 与 ingress；旧布局继续使用 `core.exe` 和 `ingress.exe`。仓库源码管理服务保留开发启动方式，禁止组件安装覆盖仓库；冻结管理服务可通过 `ABYSS_RUN_MODE=source` 显式进入仓库源码模式。

SnowLuma 更新器与进程控制器使用相同路径：`ABYSS_SNOWLUMA_DIR`（若指定）、已有 `<data_root>/SnowLuma`、否则 `<app_root>/SnowLuma`。为保留旧版登录与配置，本次不迁移其数据，也不擅自修改第三方路径机制。即使程序和数据在同一目录，更新也只替换允许的程序文件。若 native DLL 被 QQ 占用，安装可能失败并恢复程序文件；关闭占用程序后重试。

## 更新源与版本

初始组件版本在仓库根 `component_versions.json` 中定义。构建正式发行包时按实际变更修改对应组件的版本，launcher 的项目 Version、manager 的 SERVICE_VERSION 也应保持一致。安装后的版本和该组件拥有的文件记录在 `<app_root>/.versions/<component>.json`，优先于初始版本。

SnowLuma 默认检查官方稳定发布接口：

`https://api.github.com/repos/SnowLuma/SnowLuma/releases/latest`

仅选择 `win-x64` 完整版 ZIP 或 tar.gz，要求 GitHub 提供该 asset 的 SHA256 digest；没有可信校验值时会显示检查失败，不跳过校验。公开构建已预置自有组件的 HTTPS 清单地址；用户也可以修改更新源。未配置时仍可导入本地更新包。

配置保存在 `<data_root>/manager/update_sources.json`。不在客户端保存发布平台的上传凭据。

自有组件清单示例：

```json
{
  "schema": 1,
  "component": "core",
  "version": "0.2.0",
  "url": "core-0.2.0.zip",
  "size": 123456,
  "sha256": "这里填写实际压缩包的64位SHA256",
  "notes": "版本说明"
}
```

URL 可相对清单地址。使用语义化版本比较（包括预发布版本），不会把 `1.10.0` 判成比 `1.9.0` 更旧。在线检查只提示更高版本；导入本地包允许明确选择旧版本。程序降级不恢复数据库或第三方配置格式，请自行保留所需数据备份。

## 构建与发布

在 Windows 上、项目根目录执行：

```powershell
python scripts/build_release.py
# 也可以只构建某些组件
python scripts/build_release.py --components launcher manager
```

输出 `dist/independent-updates/Abyss/` 为验收用程序文件，`dist/independent-updates/packages/` 为独立更新包。构建不会改动日常运行的 `dist/Abyss/`。本机验收目录的描述文件指向仓库数据根；对外分发时按实际部署设置数据目录，不沿用本机的相对关系。

首次从旧版部署到既有 `dist/Abyss/` 可在验收通过后执行 `python scripts/deploy_release.py`。它先校验全部程序包，再通过现有管理服务停止已持有的目标，关闭旧启动器，以同一个 C# 安装事务替换程序文件，保留描述文件和所有运行数据，并重启原有界面和此前运行的目标。SnowLuma 不在本次本地部署范围内；其程序版本和登录数据保持原状。程序备份位于数据根 `manager/updates/jobs/bootstrap-*/`。

生成单组件包（同时生成历史版本列表）：

```powershell
python scripts/package_component.py core --version 0.2.0 --input dist/independent-updates/components/core
python scripts/package_component.py snowluma --version 1.12.1 --input SnowLuma
```

该脚本按组件白名单生成 `<component>-<version>.zip`、同名 JSON、`<component>-latest.json` 和 `<component>-versions.json`；不包含其他组件或本地运行数据。版本列表汇总输出目录已有的同组件清单，latest 清单通过 `versions_url` 指向该列表。在线发布时先上传 ZIP 与 versions JSON，最后更新 latest JSON。可通过 `--base-url` 指定压缩包的完整发布目录地址。

自有更新源也可直接使用版本列表：`{"schema":1,"component":"core","releases":[...]}`，其中每个条目包含 `version`、`url`、`size`、`sha256`，可带 `notes`。所有版本包和清单应保留，以便用户选择或降级。

本地导入需要 ZIP 旁有同名 JSON 清单。清单中的 size、sha256 由打包脚本生成；不要手工填示例值。

## 安装与恢复

1. 下载到数据根的 `manager/updates/<component>/`，校验大小和 SHA256，再解压到 payload。
2. 包路径必须留在组件范围内，拒绝穿越路径、符号链接、Windows 设备名、重复路径和用户数据文件。
3. 管理服务锁定目标的启停入口，停止目标，并保存此前运行状态。
4. 把自包含 C# 更新器复制到独立任务目录。更新器重新验证每个文件的 hash，先备份旧程序文件，再写安装日志，最后替换目标文件。
5. 成功后写组件 receipt，解除维护锁并恢复此前运行状态。文件安装成功但组件启动失败会明确显示，保留新程序供排查；不会因外部 QQ 离线回滚。
6. 文件替换失败时恢复该任务备份。若更新器被中断，下次更新中心显示「恢复中断的安装」；恢复根据安装日志选择保留已提交的安装或恢复未提交的替换。

备份保留在 `manager/updates/jobs/<job-id>/backup/`。这只是程序文件备份，未声称包含用户数据库或数据迁移回滚。当前安装采用有恢复日志的逐文件替换，不是运行中热更新。应用中断的任务未恢复之前，目标不会被启动。

HTTPS 更新源是程序发布的信任来源，SHA256 用于完整性校验。本次未实现发布签名密钥体系。

## 验证

```powershell
python scripts/acceptance_component_updates.py
python scripts/acceptance_controller_race.py
python scripts/acceptance_ingress.py
.tools/dotnet/dotnet.exe run --project updater-tests/UpdaterTests.csproj -c Release -- tmp
python scripts/acceptance_update_exe.py
python scripts/acceptance_launcher_updates.py
python design/check_theme_keys.py
```

测试使用独立临时目录，验证更新源、离线导入、组件隔离、失败恢复、下载完整性、路径限制和正式版启动路径，不操作真实配置或登录数据。

## 管理接口

所有接口仅在本机管理服务上提供，需要 `X-Manager-Token`；网页 cookie 无权调用更新接口。

| 方法与路径 | 内容 |
| --- | --- |
| `GET /v1/updates` | 组件当前/最新版本、可选 versions、staged_version、下载/安装状态、更新设置、最近安装结果 |
| `POST /v1/updates/check` | 可选 component；省略时并行检查所有独立源。history=true 时加载历史发布 |
| `POST /v1/updates/download` | component、可选 version；按所选版本下载或准备本地包/备份，允许旧版与重装 |
| `POST /v1/updates/review` | Core 的 operation=download/install/recover、version；返回说明、阻止原因和本次确认令牌 |
| `POST /v1/updates/import` | component、path；校验本地 ZIP 与同名 JSON |
| `POST /v1/updates/apply` | component、可选 version（必须与已准备版本相同）；launcher 更新还需要 launcher_pid |
| `POST /v1/updates/config` | auto_check、sources（组件到清单地址的映射） |
| `POST /v1/updates/recover` | 启动中断任务的恢复器；launcher 恢复还需当前 launcher_pid |
| `POST /v1/updates/finish` | 内部更新器用 id 通知安装结束并恢复目标运行状态 |
