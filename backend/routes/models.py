"""Provider/model catalog endpoint backed by models.dev + DB persistence."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from database import get_db_dep
from util.models_dev import fetch_providers, get_providers_from_db

router = APIRouter()


@router.get("/api/models")
async def list_models(db: AsyncSession = Depends(get_db_dep)):
    """Return providers and their models.

    Reads from the persisted ``model_catalog`` table first; falls back to a
    live fetch from models.dev (with 1-hour in-memory cache) if the table is
    empty (e.g. on first startup before the catalog has been refreshed).
    """
    providers = await get_providers_from_db(db)
    if providers:
        return providers
    return await fetch_providers()
