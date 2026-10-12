#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Abyss Core - 主入口

FastAPI 应用本身零业务路由，职责有两个：
1. lifespan 启动 SystemCore（大脑 + 各通道；Web 业务路由在 adapt/web，端口 5002）
2. 作为将来系统管理 API（supervisor 状态/日志端点）的宿主

启动命令：
    python main.py           # 生产模式（无热重载）
    python main.py --dev     # 开发模式（文件修改自动重启，已排除日志/数据目录）
    可选 --host / --port 覆盖监听地址（默认 127.0.0.1:5000）

运行环境：Python 3.10+
"""

import argparse
import sys
import time
import uvicorn
from pathlib import Path
from fastapi import FastAPI
from typing import Dict, Any, AsyncIterator
from dotenv import load_dotenv

from paths import app_root, data_root, log_root, ensure_cwd

# 项目根：源码运行 = 仓库根；冻结运行 = exe 所在目录（.env.dev 与用户数据都在这）
project_root = app_root()
ensure_cwd()
sys.path.insert(0, str(project_root))

# 配置外置入口（.env.dev 不存在时静默跳过；见 config/ 下的读取方）
_env_file = data_root() / ".env.dev"
load_dotenv(_env_file if _env_file.is_file() else project_root / ".env.dev")

# 冻结（--noconsole）模式下 stdout/stderr 是无效句柄，重定向到文件保住启动输出
# 注意必须是文本模式：logging/uvicorn 写的是 str，二进制句柄会 TypeError
if getattr(sys, "frozen", False):
    _log_dir = log_root()
    _log_dir.mkdir(parents=True, exist_ok=True)
    sys.stdout = open(_log_dir / "core_stdout.log", "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stderr = sys.stdout

# 导入日志模块
from utils.logger import setup_logging

# 导入系统核心协调器
from core.system_core import SystemCore

# 初始化日志记录器
logger = setup_logging('main')


# 使用FastAPI推荐的lifespan事件处理器
async def lifespan(app: FastAPI) -> AsyncIterator[Dict[str, Any]]:
    """FastAPI应用生命周期管理"""
    logger.info("开始初始化系统核心组件...")

    # 初始化SystemCore（核心组件）
    async with SystemCore() as system_core:
        # 明确将核心组件绑定到app.state
        app.state.system_core = system_core
        app.state.started_at = time.time()
        logger.info("SystemCore初始化完成并绑定到应用状态")

        # 让应用运行
        yield {}


# 创建FastAPI应用（零路由：目前只承载 lifespan；将来在此挂管理 API）
app = FastAPI(title="Abyss Core", description="消息处理核心服务",
              version="1.0", lifespan=lifespan)

logger.info("应用初始化完成")

# ======================================
# 主程序入口
# ======================================

def main() -> int:
    """主函数：启动FastAPI服务。返回非零码表示启动失败（GPT 审查-5）。"""
    parser = argparse.ArgumentParser(description="Abyss Core 主服务")
    parser.add_argument("--dev", action="store_true",
                        help="开发模式：开启热重载（生产/打包环境务必关闭）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    try:
        if args.dev:
            # 运行期写入热点必须从热重载监视中排除：服务启动即写日志（logs/），
            # 若被 watchfiles 监视到会触发「写入 → 重启 → 再写入」的无限重启风暴
            reload_excludes = [
                "logs", "logs/*", "*.log",           # 运行日志
                "tests", "tests/*",                   # 测试产物 results/*.json
                "docs", "docs/*",                     # 文档改动无需重启后端
                "scripts", "scripts/*",
                "web_frontend", "web_frontend/*",     # 前端由 Vite 自管
                "__pycache__", "*__pycache__*", "*.pyc",
                "*.db", "*.sqlite*", "*.sqlite3",     # 数据库文件
                "memory", "memory/*",                 # 运行期数据（消息/记忆/统计）
            ]
            uvicorn.run(
                "main:app", host=args.host, port=args.port, reload=True,
                reload_dirs=[str(project_root)],
                reload_excludes=reload_excludes,
            )
        else:
            # 生产模式：直接传 app 对象，无文件监视、无子进程。
            # 进程停止由管理服务统一协调（core 优雅关闭经 /system/core/shutdown）
            config = uvicorn.Config(app, host=args.host, port=args.port)
            server = uvicorn.Server(config)
            from adapt.web.system_api import set_uvicorn_server
            set_uvicorn_server(server)   # 内部直达关闭入口需要 Server 句柄
            server.run()
            # uvicorn 0.30.x：lifespan 启动失败时 run() 正常返回且 started=False，
            # 必须显式转换非零退出码，监督者不能只看进程是否存活
            if not server.started:
                logger.error("Core 启动失败（lifespan 未完成）——以非零退出码结束")
                return 1
            return 0

    except KeyboardInterrupt:
        logger.info("接收到中断信号，正在停止服务...")


if __name__ == "__main__":
    sys.exit(main())
