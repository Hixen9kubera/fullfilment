"""MercadoLibre stock router – fetches inventory per account."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from .config import CUENTAS, get_ml_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ml/stock", tags=["ML Stock"])


def _fetch_all_items(cuenta: str) -> list[dict]:
    """Return all items with full detail for a ML account."""
    ml = get_ml_manager(cuenta)
    user_id = ml.get("/users/me")["id"]

    all_ids: list[str] = []
    offset = 0
    while True:
        search = ml.get(
            f"/users/{user_id}/items/search",
            params={"offset": offset, "limit": 100},
        )
        all_ids.extend(search.get("results", []))
        total = search.get("paging", {}).get("total", 0)
        offset += 100
        if offset >= total:
            break

    items: list[dict] = []
    for i in range(0, len(all_ids), 20):
        batch = all_ids[i : i + 20]
        data = ml.get("/items", params={"ids": ",".join(batch)})
        for entry in data:
            body = entry.get("body")
            if body:
                items.append(body)

    return items


def _extract_dimensions(body: dict) -> str | None:
    """Extract package dimensions from item attributes."""
    attrs = {a["id"]: a.get("value_name") for a in body.get("attributes", [])}

    length = attrs.get("SELLER_PACKAGE_LENGTH") or attrs.get("LENGTH") or attrs.get("TOTAL_LENGTH")
    width = attrs.get("SELLER_PACKAGE_WIDTH") or attrs.get("WIDTH") or attrs.get("TOTAL_WIDTH")
    height = attrs.get("SELLER_PACKAGE_HEIGHT") or attrs.get("HEIGHT") or attrs.get("TOTAL_HEIGHT")
    weight = attrs.get("SELLER_PACKAGE_WEIGHT") or attrs.get("WEIGHT")

    parts = []
    if length:
        parts.append(f"L:{length}")
    if width:
        parts.append(f"A:{width}")
    if height:
        parts.append(f"Al:{height}")
    if weight:
        parts.append(f"P:{weight}")

    return " | ".join(parts) if parts else None


def _fetch_fulfillment_stock(cuenta: str, inventory_ids: list[str]) -> dict[str, int]:
    """Query /inventories/{id}/stock/fulfillment for a list of inventory_ids.
    Returns {inventory_id: available_quantity}."""
    if not inventory_ids:
        return {}
    ml = get_ml_manager(cuenta)
    stock_map: dict[str, int] = {}
    for inv_id in inventory_ids:
        try:
            data = ml.get(f"/inventories/{inv_id}/stock/fulfillment")
            stock_map[inv_id] = data.get("available_quantity", 0)
        except Exception:
            stock_map[inv_id] = 0
    return stock_map


def _discover_warehouses(cuenta: str, fulfillment_item_ids: set[str]) -> tuple[dict[str, str], str]:
    """Find warehouse for fulfillment items by sampling recent shipments.
    Only checks enough shipments to map known fulfillment items.
    Returns ({item_id: logistic_center_id}, default_warehouse)."""
    if not fulfillment_item_ids:
        return {}, "—"

    ml = get_ml_manager(cuenta)
    user_id = ml.get("/users/me")["id"]

    date_from = (datetime.now(timezone.utc) - timedelta(days=90)).strftime(
        "%Y-%m-%dT00:00:00.000-00:00"
    )

    item_warehouse: dict[str, str] = {}
    default_warehouse: str | None = None
    checked_shipments = 0
    max_shipments = 30  # Check at most 30 shipments

    offset = 0
    while checked_shipments < max_shipments:
        data = ml.get(
            "/orders/search",
            params={
                "seller": user_id,
                "order.date_created.from": date_from,
                "order.status": "paid",
                "limit": 50,
                "offset": offset,
            },
        )
        orders = data.get("results", [])
        if not orders:
            break
        offset += 50

        for order in orders:
            if checked_shipments >= max_shipments:
                break

            # Only check orders that contain fulfillment items
            order_item_ids = [
                oi.get("item", {}).get("id")
                for oi in order.get("order_items", [])
                if oi.get("item", {}).get("id") in fulfillment_item_ids
            ]
            if not order_item_ids:
                continue

            ship_id = order.get("shipping", {}).get("id")
            if not ship_id:
                continue

            checked_shipments += 1
            try:
                ship = ml.get(f"/shipments/{ship_id}")
            except Exception:
                continue

            if ship.get("logistic_type") != "fulfillment":
                continue

            node = ship.get("sender_address", {}).get("node", {})
            warehouse = node.get("logistic_center_id")
            if not warehouse:
                continue

            if default_warehouse is None:
                default_warehouse = warehouse

            for iid in order_item_ids:
                item_warehouse[iid] = warehouse

    return item_warehouse, default_warehouse or "—"


def fetch_stock_for_account(cuenta: str) -> dict:
    """Full stock data for one account with status, logistic type, fulfillment stock, and warehouse."""
    items = _fetch_all_items(cuenta)
    ml = get_ml_manager(cuenta)
    user_id = ml.get("/users/me")["id"]

    # Collect all inventory_ids from fulfillment items
    # inventory_id can be at item level OR inside each variation
    inv_id_to_item: dict[str, str] = {}  # inventory_id -> item_id
    for body in items:
        if body.get("shipping", {}).get("logistic_type") == "fulfillment":
            # Item-level inventory_id
            inv_id = body.get("inventory_id")
            if inv_id:
                inv_id_to_item[inv_id] = body["id"]
            # Variation-level inventory_ids
            for var in body.get("variations", []):
                inv_id = var.get("inventory_id")
                if inv_id:
                    inv_id_to_item[inv_id] = body["id"]

    # Batch-fetch fulfillment stock
    unique_inv_ids = list(inv_id_to_item.keys())
    full_stock_map = _fetch_fulfillment_stock(cuenta, unique_inv_ids)

    # Aggregate fulfillment stock per item_id
    item_full_stock: dict[str, int] = {}
    for inv_id, qty in full_stock_map.items():
        item_id = inv_id_to_item[inv_id]
        item_full_stock[item_id] = item_full_stock.get(item_id, 0) + qty

    # Discover warehouses from shipments (only for fulfillment items)
    fulfillment_item_ids = {
        body["id"] for body in items
        if body.get("shipping", {}).get("logistic_type") == "fulfillment"
    }
    item_warehouse, default_warehouse = _discover_warehouses(cuenta, fulfillment_item_ids)

    products = []
    for body in items:
        item_id = body.get("id")
        logistic_type = body.get("shipping", {}).get("logistic_type", "unknown")
        status = body.get("status", "unknown")

        if logistic_type == "fulfillment":
            stock_full = item_full_stock.get(item_id, 0)
            warehouse = item_warehouse.get(item_id, default_warehouse)
        else:
            stock_full = 0
            warehouse = "Seller"

        products.append(
            {
                "item_id": item_id,
                "title": body.get("title"),
                "status": status,
                "logistic_type": logistic_type,
                "stock_full": stock_full,
                "warehouse": warehouse,
                "available_quantity": body.get("available_quantity", 0),
                "price": body.get("price"),
                "currency_id": body.get("currency_id"),
                "start_time": body.get("start_time"),
                "dimensions": _extract_dimensions(body),
            }
        )

    products.sort(key=lambda p: p["stock_full"], reverse=True)

    return {
        "cuenta": cuenta,
        "seller_id": user_id,
        "total_items": len(products),
        "products": products,
    }


@router.get("")
async def stock_ml():
    """Stock ranking per ML account."""
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(None, fetch_stock_for_account, c) for c in CUENTAS
    ]
    try:
        results = await asyncio.gather(*tasks)
    except Exception as exc:
        logger.exception("Error fetching ML stock")
        raise HTTPException(status_code=502, detail=str(exc))

    return {"fecha_consulta": datetime.now(timezone.utc).isoformat(), "cuentas": results}
