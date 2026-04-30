"""MercadoLibre sales router – orders and monthly sales aggregation."""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from ..config import CUENTAS, get_ml_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ml/ventas", tags=["ML Ventas"])


def _fetch_orders(cuenta: str, days: int) -> list[dict]:
    """Fetch all paid orders for the last N days."""
    ml = get_ml_manager(cuenta)
    user_id = ml.get("/users/me")["id"]

    date_from = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT00:00:00.000-00:00"
    )

    orders: list[dict] = []
    offset = 0
    limit = 50
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
        orders.extend(data.get("results", []))
        total = data.get("paging", {}).get("total", 0)
        offset += limit
        if offset >= total:
            break

    return orders


def _build_account_summary(cuenta: str, orders: list[dict]) -> dict:
    total_units = 0
    total_revenue = 0.0
    order_list = []

    for order in orders:
        order_units = sum(
            item.get("quantity", 0) for item in order.get("order_items", [])
        )
        order_amount = order.get("total_amount", 0.0)
        total_units += order_units
        total_revenue += order_amount

        order_list.append(
            {
                "order_id": order.get("id"),
                "date_created": order.get("date_created"),
                "total_amount": order_amount,
                "currency_id": order.get("currency_id"),
                "units": order_units,
                "items": [
                    {
                        "item_id": item.get("item", {}).get("id"),
                        "title": item.get("item", {}).get("title"),
                        "sku": item.get("item", {}).get("seller_sku"),
                        "quantity": item.get("quantity"),
                        "unit_price": item.get("unit_price"),
                    }
                    for item in order.get("order_items", [])
                ],
                "buyer": order.get("buyer", {}).get("nickname"),
            }
        )

    return {
        "cuenta": cuenta,
        "total_units": total_units,
        "total_revenue": total_revenue,
        "total_orders": len(order_list),
        "orders": order_list,
    }


def fetch_ventas_for_account(cuenta: str, days: int = 7) -> dict:
    orders = _fetch_orders(cuenta, days)
    return _build_account_summary(cuenta, orders)


def fetch_monthly_sales_by_item(cuenta: str) -> dict:
    """Return units, revenue, and skus per item for the last 30 days."""
    orders = _fetch_orders(cuenta, 30)
    sales: dict[str, int] = defaultdict(int)
    revenue: dict[str, float] = defaultdict(float)
    skus: dict[str, str] = {}

    for order in orders:
        for oi in order.get("order_items", []):
            item_id = oi.get("item", {}).get("id")
            if item_id:
                qty = oi.get("quantity", 0)
                price = oi.get("unit_price", 0)
                sales[item_id] += qty
                revenue[item_id] += qty * price
                sku = oi.get("item", {}).get("seller_sku")
                if sku:
                    skus[item_id] = sku

    return {"sales": dict(sales), "revenue": dict(revenue), "skus": skus}


def fetch_all_orders_history(cuenta: str) -> list[dict]:
    """Fetch ALL paid orders for an account (full history)."""
    ml = get_ml_manager(cuenta)
    user_id = ml.get("/users/me")["id"]

    orders: list[dict] = []
    offset = 0
    limit = 50
    while True:
        data = ml.get(
            "/orders/search",
            params={
                "seller": user_id,
                "order.status": "paid",
                "sort": "date_desc",
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

    return orders


@router.get("/7dias")
async def ventas_7dias():
    """Sales from the last 7 days for all ML accounts."""
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(None, fetch_ventas_for_account, c, 7) for c in CUENTAS
    ]
    try:
        results = await asyncio.gather(*tasks)
    except Exception as exc:
        logger.exception("Error fetching orders")
        raise HTTPException(status_code=502, detail=str(exc))

    grand_units = sum(r["total_units"] for r in results)
    grand_revenue = sum(r["total_revenue"] for r in results)
    grand_orders = sum(r["total_orders"] for r in results)

    return {
        "periodo": "ultimos_7_dias",
        "fecha_consulta": datetime.now(timezone.utc).isoformat(),
        "totales": {
            "unidades_vendidas": grand_units,
            "ingresos": grand_revenue,
            "ordenes": grand_orders,
        },
        "cuentas": results,
    }
