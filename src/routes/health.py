"""
健康检查路由 — 对应 TypeScript 版 `routes/health.ts`。
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health():
    from datetime import datetime, timezone
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("T", " ")[:19],
    }
