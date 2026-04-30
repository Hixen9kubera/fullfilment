"""FastAPI router for restock endpoints."""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from .bollinger import fetch_signals
from .db import get_client

router = APIRouter(prefix="/api/restock", tags=["Restock"])


@router.get("/signals")
def restock_signals(cuenta: str | None = None, p_sku: str | None = None):
    """Bollinger band signals (series de 60 días por SKU)."""
    try:
        items = fetch_signals(cuenta, p_sku)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {
        "fecha": datetime.now(timezone.utc).isoformat(),
        "cuenta": cuenta or "AMBAS",
        "items": items,
    }


@router.get("/table")
def restock_table(cuenta: str | None = None):
    """Tabla de restock con semáforo, días para agotar y restock sugerido."""
    try:
        db = get_client()
        params = {"p_cuenta": cuenta} if cuenta else {}
        rows = db.rpc("get_restock_table", params).execute().data
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {
        "fecha": datetime.now(timezone.utc).isoformat(),
        "cuenta": cuenta or "AMBAS",
        "rows": rows,
    }
