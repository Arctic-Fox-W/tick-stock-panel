"""五档盘口查询 API。

详情弹窗按需查看任意标的(含 ETF)的买卖五档; 与连板梯队 sealed 修正链路
解耦, 走同一五档能力门禁与数据源路由 (preferences.depth5_data_provider)。
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/depth", tags=["depth"])


def _levels(book: dict, key: str, volumes_key: str) -> tuple[list, list]:
    """取价格档位与数量数组, 缺失时由档位回退推导数量。"""
    levels = book.get(key) or []
    volumes = book.get(volumes_key)
    if not volumes:
        volumes = [level[1] for level in levels if isinstance(level, (list, tuple)) and len(level) >= 2]
    return levels, volumes


@router.get("")
def get_depth(request: Request, symbol: str = Query(..., min_length=1)) -> dict:
    """查询单个标的的五档盘口(买一~买五 / 卖一~卖五)。

    无五档能力或未取到数据时 available=False, 前端据此展示降级提示。
    """
    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol 不能为空")

    ds = getattr(request.app.state, "depth_service", None)
    if ds is None:
        raise HTTPException(status_code=503, detail="五档服务未就绪")

    from app.data_providers.capabilities import capability_gate

    ok, label = capability_gate("depth5")
    if not ok:
        return {"symbol": sym, "available": False, "reason": f"五档盘口不可用 ({label})"}

    try:
        data = ds.get_depth([sym])
    except Exception as e:  # noqa: BLE001
        logger.warning("depth 查询 %s 失败: %s", sym, e)
        return {"symbol": sym, "available": False, "reason": f"盘口查询失败: {e}"}
    # 记录本次取数完成时间 (上游不返回时间戳), 供前端展示数据新鲜度 (epoch 秒)。
    fetched_at = time.time()

    book = data.get(sym) if isinstance(data, dict) else None
    if not book:
        return {"symbol": sym, "available": False, "reason": "未获取到盘口数据"}

    asks, ask_volumes = _levels(book, "asks", "ask_volumes")
    bids, bid_volumes = _levels(book, "bids", "bid_volumes")
    return {
        "symbol": sym,
        "available": True,
        "asof": fetched_at,
        "asks": asks,
        "bids": bids,
        "ask_volumes": ask_volumes,
        "bid_volumes": bid_volumes,
    }
