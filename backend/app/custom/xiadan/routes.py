"""同花顺持仓扩展的 REST 接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import _state


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/custom/xiadan", tags=["custom-xiadan"])

    def require_service():
        service = _state.get("service")
        if service is None:
            raise HTTPException(503, "同花顺持仓扩展未就绪, 请检查后端日志")
        return service

    @router.get("/holdings")
    def holdings() -> dict:
        try:
            return require_service().snapshot()
        except (ValueError, OSError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    return router