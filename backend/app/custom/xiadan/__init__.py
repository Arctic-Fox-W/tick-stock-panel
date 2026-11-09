"""同花顺「网上股票交易系统」持仓窗口读取扩展。

通过 Win32 枚举标题为「网上股票交易系统5.0」的交易窗口, PrintWindow 截图后走
Tesseract OCR 抽取持仓行, 经 REST 接口暴露给前端, 并作为只读工具供 AI 助手调用。

只做截图 + OCR 抽取, 不做自动下单等任何写操作。
"""
from __future__ import annotations

from app.extensions import (
    BACKEND_EXTENSION_API_VERSION,
    BackendExtensionRegistrar,
    ExtensionContext,
)

EXTENSION_ID = "xiadan.holdings"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

# startup 阶段注入的服务实例, routes 与 assistant 工具共享。
_state: dict = {}


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import build_router

    registrar.include_router(build_router())


def startup(context: ExtensionContext) -> None:
    from .service import XiadanService

    _state["service"] = XiadanService(data_dir=context.data_dir)