"""
健康检查路由。
"""

from fastapi import APIRouter

from ..time import now_readable

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "timestamp": now_readable(),
    }
