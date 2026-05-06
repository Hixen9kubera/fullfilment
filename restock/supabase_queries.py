"""Supabase-backed data fetchers that replace live ML API calls."""

from ..config import CUENTAS
from .db import get_client


def _rpc_per_account(rpc_name: str, cuenta: str | None) -> list[dict]:
    """Call an RPC scoped by p_cuenta. When cuenta is None, fan out one call
    per account to avoid PostgREST's 1000-row response cap that silently
    truncates multi-account results."""
    db = get_client()
    if cuenta:
        return db.rpc(rpc_name, {"p_cuenta": cuenta}).execute().data or []
    rows: list[dict] = []
    for c in CUENTAS:
        rows.extend(db.rpc(rpc_name, {"p_cuenta": c}).execute().data or [])
    return rows


def fetch_dashboard_stock(cuenta: str | None = None) -> list[dict]:
    """Latest stock snapshot per item from daily_stock (via get_dashboard_stock RPC)."""
    return _rpc_per_account("get_dashboard_stock", cuenta)


def fetch_dashboard_monthly_sales(cuenta: str | None = None) -> list[dict]:
    """Last 30d sales per item from daily_sales (via get_dashboard_monthly_sales RPC)."""
    return _rpc_per_account("get_dashboard_monthly_sales", cuenta)


def fetch_estrella_data(cuenta: str | None = None) -> list[dict]:
    """All-time sales aggregated per item from daily_sales (via get_estrella_data RPC)."""
    return _rpc_per_account("get_estrella_data", cuenta)
