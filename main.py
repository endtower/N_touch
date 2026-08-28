#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Abyss Core - 消息处理核心服务1

这个服务作为NoneBot插件的后端处理系统，主要功能包括：
1. 接收来自NoneBot插件的用户消息
2. 将消息送入SystemCore进行处理和存储
3. 实现简单的发送消息功能作为备用
4. 提供系统状态查询API

运行环境：
- Python 3.7+
- FastAPI
- aiohttp

启动命令：
- python main.py
- 开发环境：uvicorn main:app --host 127.0.0.1 --port 5000 --reload
"""

import sys
import uvicorn
from pathlib import Path
from fastapi import FastAPI
from typing import Dict, Any, AsyncIterator 
from dotenv import load_dotenv
load_dotenv('.env.dev')
# 添加项目根目录到系统路径以便导入Abyss_Core包
project_root = str(Path(__file__).resolve().parent.parent)  # 项目根目录
sys.path.append(project_root)  # 添加项目根目录

# 导入日志模块
from utils.logger import setup_logging

# 导入系统核心协调器
from core.system_core import SystemCore

# 导入工具模块
from utils import setup_exception_handlers

# 初始化日志记录器
logger = setup_logging('main')

# 使用FastAPI推荐的lifespan事件处理器
# 保持现有的lifespan机制，但增加更明确的初始化顺序
async def lifespan(app: FastAPI) -> AsyncIterator[Dict[str, Any]]:
    """FastAPI应用生命周期管理"""
    logger.info("开始初始化系统核心组件...")
    
    # 初始化SystemCore（核心组件）
    async with SystemCore() as system_core:
        # 明确将核心组件绑定到app.state
        app.state.system_core = system_core
        logger.info("SystemCore初始化完成并绑定到应用状态")
        
        # 让应用运行
        yield {}

# 创建FastAPI应用并配置lifespan
app = FastAPI(title="Abyss Core", description="消息处理核心服务",
             version="1.0", lifespan=lifespan)

# CORS：放开给本地 Vite 开发服（5173）调用
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 设置异常处理器
setup_exception_handlers(app)

logger.info("应用初始化完成")

# ======================================
# 主程序入口
# ======================================

def main():
    """主函数：启动FastAPI服务"""
    try:
        # 配置服务参数
        host = "127.0.0.1"
        port = 5000

        # 使用uvicorn启动FastAPI服务（开发环境开启reload，文件修改自动重启）
        uvicorn.run("main:app", host=host, port=port, reload=True)

    except KeyboardInterrupt:
        logger.info("接收到中断信号，正在停止服务...")
    except Exception as e:
        logger.error(f"启动服务时发生错误: {e}")


if __name__ == "__main__":
    main()