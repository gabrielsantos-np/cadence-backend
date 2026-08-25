"""Liveness endpoint.

Deliberately dependency-free: it must not touch the database or OpenRouter, so
a deployment health check reports on this process and nothing else.
"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness check")
async def health() -> dict[str, str]:
    return {"status": "ok"}
