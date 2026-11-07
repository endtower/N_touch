"""准备人工安装验收目录；只复制候选与隔离配置，不安装或启动 Core。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import socket
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config_keys import CONFIG_OVERRIDE_KEYS

LAUNCH = r'''param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
try {
    $profile = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'test-profile.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $appRoot = Join-Path $PSScriptRoot 'app'
    $dataRoot = Join-Path $PSScriptRoot 'data'
    $entry = Join-Path $PSScriptRoot 'AbyssLauncher.exe'
    if (!(Test-Path -LiteralPath $entry)) { throw 'Launcher EXE is missing.' }
    $managerRunning = $false
    $tokenPath = Join-Path $dataRoot 'manager/service_token.txt'
    if (Test-Path -LiteralPath $tokenPath) {
        try {
            $token = (Get-Content -LiteralPath $tokenPath -Raw -Encoding UTF8).Trim()
            $info = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $profile.ports.manager + '/v1/info') -Headers @{'X-Manager-Token'=$token} -TimeoutSec 2
            if ([IO.Path]::GetFullPath($info.instance) -ne [IO.Path]::GetFullPath($dataRoot)) { throw 'Different test instance.' }
            $managerRunning = $true
        } catch { }
    }
    if (!$managerRunning) {
        foreach ($property in $profile.ports.PSObject.Properties) {
            $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, [int]$property.Value)
            try { $listener.Start() }
            catch { throw ('Test port is busy: ' + $property.Value + '. Close the previous test instance, then retry.') }
            finally { $listener.Stop() }
        }
    }
    if ($CheckOnly) { Write-Output 'PASS: isolated paths and ports; no application started.'; exit 0 }
    foreach ($key in $profile.clear_env) { [Environment]::SetEnvironmentVariable($key, $null, 'Process') }
    $env:ABYSS_INSTALL_DIR = $appRoot
    $env:ABYSS_DATA_DIR = $dataRoot
    $env:ABYSS_MANAGER_PORT = [string]$profile.ports.manager
    $env:ABYSS_LAUNCHER_MUTEX = $profile.mutex
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $entry
    $start.WorkingDirectory = $PSScriptRoot
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    [Diagnostics.Process]::Start($start) | Out-Null
} catch {
    if ($CheckOnly) { Write-Error $_; exit 1 }
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, 'Abyss installation test') | Out-Null
    exit 1
}
'''

GUIDE = '''# 人工安装验收

请双击「开始安装测试.cmd」，不要直接双击旁边的 EXE。测试入口会把程序、数据、端口与现用实例隔离；不会复制现用账号、模型密钥或记忆。

1. 打开后，Core 应显示「未安装」。这时不应自动下载或安装 Core。
2. 展开 Core 行右侧的箭头，点击「导入本地包」，选择本目录 `组件包/core-0.4.0.zip`。同名 JSON 保持在 ZIP 旁边，不用解压。
3. 点击安装图标，查看版本说明，先取消一次。确认取消后仍未安装，再点击并确认安装。
4. 等待安装完成，再点击 Core 的启动按钮。按向导填写你自己的账号与模型接入。
5. 进入 Core 网页，检查记忆、经验等弹窗的大小与关闭按钮，并创建一条网页会话验证收发。
6. 从托盘选择「停止全部并退出」，再双击测试入口。确认配置与新增数据保留，Core 不会被重新安装。

本轮先测 Core 与 Web；QQ、微信接入初始关闭，其他后台功能保留产品默认值。填写真实模型接入并启动后，后台模块可能正常调用模型。
如果暂时不做聊天测试，可在首次配置时选「稍后设置」，这会取消本次 Core 启动；之后再填写接入并启动。

请记录失败的步骤、提示内容或截图。我可以直接检查本目录 data 下的测试日志来定位，无需你整理程序文件。

## 下一轮：正式在线安装

本目录使用本地 Core 包，自有组件在线源留空，因为发布资源尚未上传。SnowLuma 保留官方更新源，并启用版本自动检查；检查不会自动下载或安装。也可点击 SnowLuma 版本旁的刷新按钮手动检查。
通过本轮后，先上传预发布资源，再使用只有一个 EXE 的干净环境验证在线下载 Core、安装 SnowLuma、登录 QQ、接收消息及更新确认流程。
QQ 登录验收放到关闭旧实例之后进行。

目录说明：app 是这份测试的程序；data 是这份测试的数据。后续仍通过测试入口打开，保持隔离环境。
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    release, output = args.release.resolve(), args.output.resolve()
    if output.exists():
        raise ValueError('请使用新目录，不能覆盖已有测试数据')
    required = ['download/AbyssLauncher.exe', 'packages/core-0.4.0.zip', 'packages/core-0.4.0.json']
    if not all((release / name).is_file() for name in required):
        raise ValueError('候选文件不完整')
    output.mkdir(parents=True)
    packages = output / '组件包'
    packages.mkdir()
    shutil.copy2(release / required[0], output / 'AbyssLauncher.exe')
    for name in required[1:]:
        shutil.copy2(release / name, packages / Path(name).name)
    reservations = []
    ports = {}
    try:
        for name in ('manager', 'ingress', 'web', 'main', 'onebot'):
            sock = socket.socket()
            sock.bind(('127.0.0.1', 0))
            reservations.append(sock)
            ports[name] = sock.getsockname()[1]
    finally:
        for sock in reservations:
            sock.close()
    clear = list(CONFIG_OVERRIDE_KEYS) + [key for key in (
        'ABYSS_RUN_MODE', 'ABYSS_SNOWLUMA_DIR', 'ABYSS_UPDATES_SNAPSHOT', 'ABYSS_UPDATES_EXIT_AFTER_SNAPSHOT',
        'ABYSS_LAUNCHER_SNAPSHOT', 'ABYSS_LAUNCHER_SECTION')]
    (output / 'test-profile.json').write_text(json.dumps({'ports': ports, 'mutex': 'AbyssManualTest_' + uuid.uuid4().hex,
                                                       'clear_env': clear}, indent=2), encoding='utf-8')
    manager = output / 'data/manager'
    manager.mkdir(parents=True)
    (output / 'data/.env.dev').write_text('ONEBOT_ENABLED=false\nBRIDGE_ENABLED=false\n' + ''.join(
        f'{key}={ports[name]}\n' for key, name in (('INGRESS_API_PORT', 'ingress'), ('WEB_WS_PORT', 'web'),
                                                 ('MAIN_PORT', 'main'), ('ONEBOT_WS_PORT', 'onebot'))), encoding='utf-8')
    (manager / 'update_sources.json').write_text(json.dumps({'auto_check': True,
        'sources': {'core': '', 'manager': '', 'launcher': '', 'ingress': ''}}), encoding='utf-8')
    (output / 'launch-isolated.ps1').write_text(LAUNCH, encoding='utf-8-sig')
    (output / '开始安装测试.cmd').write_bytes(b'@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch-isolated.ps1"\r\n')
    (output / '先看这里.md').write_text(GUIDE, encoding='utf-8')
    print(output)


if __name__ == '__main__':
    main()
