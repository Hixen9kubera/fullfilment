"""Supabase-backed data fetchers that replace live ML API calls."""

from .db import get_client


def fetch_dashboard_stock(cuenta: str | None = None) -> list[dict]:
    """Latest stock snapshot per item from daily_stock (via get_dashboard_stock RPC)."""
    db = get_client()
    params = {"p_cuenta": cuenta} if cuenta else {}
    return db.rpc("get_dashboard_stock", params).execute().data


def fetch_dashboard_monthly_sales(cuenta: str | None = None) -> list[dict]:
    """Last 30d sales per item from daily_sales (via get_dashboard_monthly_sales RPC)."""
    db = get_client()
    params = {"p_cuenta": cuenta} if cuenta else {}
    return db.rpc("get_dashboard_monthly_sales", params).execute().data


def fetch_estrella_data(cuenta: str | None = None) -> list[dict]:
    """All-time sales aggregated per item from daily_sales (via get_estrella_data RPC)."""
    db = get_client()
    params = {"p_cuenta": cuenta} if cuenta else {}
    return db.rpc("get_estrella_data", params).execute().data
