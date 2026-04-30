"""Bollinger band signals from Supabase."""

from .db import get_client

FLOOR_DAYS = 14
WARN_DAYS = 30
CEILING_DAYS = 50


def _signal(days: float | None) -> str:
    if days is None:
        return "unknown"
    if days < FLOOR_DAYS:
        return "critical"
    if days < WARN_DAYS:
        return "warn"
    return "ok"


def fetch_signals(cuenta: str | None = None, sku: str | None = None) -> list[dict]:
    """
    Calls get_restock_signals() in Supabase and returns structured items.
    cuenta=None → both accounts combined. sku=None → all SKUs.
    """
    db = get_client()
    params: dict = {}
    if cuenta:
        params["p_cuenta"] = cuenta
    if sku:
        params["p_sku"] = sku
    rows = db.rpc("get_restock_signals", params).execute().data

    # Group flat rows into items with a series array
    items: dict[str, dict] = {}
    for r in rows:
        sku = r["sku"]
        if sku not in items:
            days = r["days_coverage"]
            items[sku] = {
                "sku": sku,
                "title": r["title"] or sku,
                "stock_full": r["stock_full"] or 0,
                "stock_odoo": r["stock_odoo"] or 0,
                "size_category": r["size_category"] or "?",
                "days_coverage": days,
                "signal": _signal(days),
                "series": [],
            }
        items[sku]["series"].append({
            "date": r["signal_date"],
            "units_sold": float(r["units_sold"] or 0),
            "mean": float(r["mean"] or 0),
            "lower": float(r["lower_band"] or 0),
            "upper": float(r["upper_band"] or 0),
        })

    return list(items.values())
