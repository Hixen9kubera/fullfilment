"""Ingesta de ventas diarias y stock desde ML + Odoo a Supabase.

Uso rápido (últimos 7 días):
    cd /Users/je/dev/kubera
    uv run python -m fulfillment.restock.ingest --days 7

Backfill histórico completo:
    uv run python -m fulfillment.restock.ingest --days 90
"""

import argparse
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# Cargar .env antes de cualquier import de config
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from ..config import CUENTAS, get_ml_manager  # noqa: E402
from ..api.ml_stock import fetch_stock_for_account  # noqa: E402
from ..api.odoo_stock import fetch_odoo_stock_by_sku  # noqa: E402
from .db import upsert_daily_sales, upsert_daily_stock  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Nodos de ML que corresponden a Full (centros de distribución)
_FULL_NODE_PREFIXES = ("MXCD", "BRCX", "ARCX", "COCX")  # MX + otros países por si acaso


def _is_full_node(node_id: str | None) -> bool:
    return node_id is not None


def _fetch_orders(cuenta: str, days: int) -> list[dict]:
    ml = get_ml_manager(cuenta)
    user_id = ml.get("/users/me")["id"]
    date_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT00:00:00.000-00:00"
    )
    orders: list[dict] = []
    offset, limit = 0, 50
    while True:
        data = ml.get(
            "/orders/search",
            params={
                "seller": user_id,
                "order.date_created.from": date_from,
                "order.status": "paid",
                "limit": limit,
                "offset": offset,
            },
        )
        batch = data.get("results", [])
        orders.extend(batch)
        total = data.get("paging", {}).get("total", 0)
        offset += limit
        if offset >= total or not batch:
            break
    logger.info(f"  [{cuenta}] {len(orders)} órdenes de los últimos {days} días")
    return orders


def _aggregate_sales(cuenta: str, orders: list[dict]) -> list[dict]:
    """Agrupa órdenes por (date, item_id) y devuelve filas para daily_sales."""
    # key: (date_str, item_id)
    agg: dict[tuple, dict] = defaultdict(lambda: {
        "units_sold": 0,
        "revenue": 0.0,
        "gross_revenue": 0.0,
        "sale_fee": 0.0,
        "sku": None,
        "title": None,
        "is_full": None,
        "node_id": None,
    })

    for order in orders:
        raw_date = order.get("date_created", "")
        try:
            dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            day = dt.astimezone(timezone.utc).date().isoformat()
        except (ValueError, TypeError):
            continue

        for oi in order.get("order_items", []):
            item = oi.get("item", {})
            item_id = item.get("id")
            if not item_id:
                continue

            qty = oi.get("quantity") or 0
            unit_price = oi.get("unit_price") or 0.0
            gross_price = oi.get("gross_price") or 0.0
            fee = oi.get("sale_fee") or 0.0
            node_id = (oi.get("stock") or {}).get("node_id")

            key = (day, item_id)
            row = agg[key]
            row["units_sold"] += qty
            row["revenue"] += qty * unit_price
            row["gross_revenue"] += qty * gross_price
            row["sale_fee"] += fee

            if item.get("seller_sku"):
                row["sku"] = item["seller_sku"]
            if item.get("title"):
                row["title"] = item["title"]
            if node_id and row["node_id"] is None:
                row["node_id"] = node_id

    rows = []
    for (day, item_id), data in agg.items():
        rows.append({
            "date": day,
            "cuenta": cuenta,
            "item_id": item_id,
            "sku": data["sku"],
            "title": data["title"],
            "node_id": data["node_id"],
            "is_full": _is_full_node(data["node_id"]),
            "units_sold": data["units_sold"],
            "revenue": round(data["revenue"], 2),
            "gross_revenue": round(data["gross_revenue"], 2),
            "sale_fee": round(data["sale_fee"], 2),
        })
    return rows


def _build_stock_rows(cuenta: str, odoo_stock: dict[str, float]) -> list[dict]:
    """Snapshot de stock actual para daily_stock (fecha = hoy)."""
    today = date.today().isoformat()
    stock_data = fetch_stock_for_account(cuenta)
    rows = []
    for p in stock_data.get("products", []):
        item_id = p["item_id"]
        sku = p.get("seller_sku") or "—"
        odoo_qty = int(odoo_stock.get(sku, 0)) if sku != "—" else 0
        rows.append({
            "date": today,
            "cuenta": cuenta,
            "item_id": item_id,
            "sku": sku if sku != "—" else None,
            "title": p.get("title"),
            "status": p.get("status"),
            "logistic_type": p.get("logistic_type"),
            "stock_full": p.get("stock_full") or 0,
            "stock_odoo": odoo_qty,
            "price": float(p["price"]) if p.get("price") is not None else None,
            "size_category": p.get("size_category"),
            "dimensions": p.get("dimensions"),
            "start_time": p.get("start_time"),
            "warehouse": p.get("warehouse"),
        })
    return rows


def run_ingest(days: int | None = None, date_from: str | None = None) -> None:
    if date_from:
        start = date.fromisoformat(date_from)
        days = (date.today() - start).days
        logger.info(f"=== Ingest restock: desde {date_from} ({days} días) ===")
    else:
        days = days or 7
        logger.info(f"=== Ingest restock: últimos {days} días ===")

    logger.info("Obteniendo stock Odoo...")
    odoo_stock = fetch_odoo_stock_by_sku()

    for cuenta in CUENTAS:
        logger.info(f"--- {cuenta} ---")

        orders = _fetch_orders(cuenta, days)
        sales_rows = _aggregate_sales(cuenta, orders)
        n_sales = upsert_daily_sales(sales_rows)
        logger.info(f"  [{cuenta}] daily_sales: {n_sales} filas upserted")

        stock_rows = _build_stock_rows(cuenta, odoo_stock)
        n_stock = upsert_daily_stock(stock_rows)
        logger.info(f"  [{cuenta}] daily_stock: {n_stock} filas upserted")

    logger.info("=== Ingest completado ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest ventas ML → Supabase")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, help="Días hacia atrás (ej: 7, 90)")
    group.add_argument("--date-from", dest="date_from", help="Fecha inicio YYYY-MM-DD (ej: 2025-12-01)")
    args = parser.parse_args()
    run_ingest(days=args.days, date_from=args.date_from)
