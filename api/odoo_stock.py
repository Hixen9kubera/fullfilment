"""Odoo inventory router."""

import asyncio
import logging
import random
import xmlrpc.client
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..config import ODOO_DB, ODOO_PASSWORD, ODOO_URL, ODOO_USER

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/odoo", tags=["Odoo"])


def _odoo_connect() -> tuple[int, xmlrpc.client.ServerProxy]:
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    if not uid:
        raise ValueError("Odoo authentication failed")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


def fetch_odoo_stock_by_sku() -> dict[str, float]:
    """Return {sku: free_qty} – free to use quantity from product.product
    (equivalent to On Hand - Reserved, excluding scrap locations)."""
    uid, models = _odoo_connect()

    products = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "!=", False], ["free_qty", ">", 0]]],
        {"fields": ["default_code", "free_qty"], "limit": 10000},
    )

    return {p["default_code"]: p["free_qty"] for p in products}


def fetch_odoo_weight_by_sku() -> dict[str, float]:
    """Return {sku: weight_kg} from product.product.weight (Odoo standard field)."""
    uid, models = _odoo_connect()
    products = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "search_read",
        [[["default_code", "!=", False], ["weight", ">", 0]]],
        {"fields": ["default_code", "weight"], "limit": 10000},
    )
    return {p["default_code"]: p["weight"] for p in products}


def fetch_odoo_oldest_in_date_by_sku() -> dict[str, int]:
    """Return {sku: days_since_oldest_in_date} using stock.quant.in_date.
    We pick the MIN in_date per SKU (oldest incoming batch still in bodega) because
    that's the one approaching the 60-day limit."""
    uid, models = _odoo_connect()

    quants = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "stock.quant", "search_read",
        [[
            ["location_id.usage", "=", "internal"],
            ["quantity", ">", 0],
            ["product_id.default_code", "!=", False],
        ]],
        {"fields": ["product_id", "in_date"], "limit": 50000},
    )

    if not quants:
        return {}

    # Need SKU per product_id
    prod_ids = list({q["product_id"][0] for q in quants if q.get("product_id")})
    prods = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "product.product", "read",
        [prod_ids],
        {"fields": ["default_code"]},
    )
    sku_by_id = {p["id"]: p["default_code"] for p in prods if p.get("default_code")}

    oldest: dict[str, datetime] = {}
    for q in quants:
        pid = q["product_id"][0] if q.get("product_id") else None
        sku = sku_by_id.get(pid)
        raw = q.get("in_date")
        if not sku or not raw:
            continue
        try:
            # Odoo returns "2026-02-14 10:30:15" (UTC, naive)
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if sku not in oldest or dt < oldest[sku]:
            oldest[sku] = dt

    now = datetime.now(timezone.utc)
    return {sku: max((now - dt).days, 0) for sku, dt in oldest.items()}


def _fetch_stock_sample() -> dict:
    uid, models = _odoo_connect()
    quants = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "stock.quant", "search_read",
        [[["location_id.usage", "=", "internal"], ["quantity", ">", 0]]],
        {"fields": ["product_id", "quantity"], "limit": 200},
    )
    sample = random.sample(quants, min(10, len(quants)))
    products = [
        {
            "product_id": q["product_id"][0],
            "product_name": q["product_id"][1],
            "quantity": q["quantity"],
        }
        for q in sample
    ]
    return {"total_registros_inventario": len(quants), "muestra": products}


@router.get("/stock/sample")
async def stock_sample():
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _fetch_stock_sample)
    except Exception as exc:
        logger.exception("Error fetching Odoo stock")
        raise HTTPException(status_code=502, detail=str(exc))
    return result
