"""Supabase client + upsert helpers for restock tables."""

import os
from datetime import date

from supabase import Client, create_client

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL y SUPABASE_KEY deben estar en .env")
        _client = create_client(url, key)
    return _client


def upsert_daily_sales(rows: list[dict]) -> int:
    """Upsert a batch of daily_sales rows. Returns count inserted/updated."""
    if not rows:
        return 0
    db = get_client()
    db.table("daily_sales").upsert(rows, on_conflict="date,cuenta,item_id").execute()
    return len(rows)


def upsert_daily_stock(rows: list[dict]) -> int:
    """Upsert a batch of daily_stock rows. Returns count inserted/updated."""
    if not rows:
        return 0
    db = get_client()
    db.table("daily_stock").upsert(rows, on_conflict="date,cuenta,item_id").execute()
    return len(rows)
