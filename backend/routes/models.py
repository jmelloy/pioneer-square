"""Provider/model catalog endpoint backed by models.dev."""

from __future__ import annotations

from fastapi import APIRouter
from util.models_dev import fetch_providers

router = APIRouter()


@router.get("/api/models")
async def list_models():
    """Return providers and their models sourced from models.dev (cached 1 h)."""
    return await fetch_providers()
