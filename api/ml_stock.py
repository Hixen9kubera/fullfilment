"""MercadoLibre stock router – fetches inventory per account."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from ..config import CUENTAS, get_ml_manager

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

    length = attrs.get("SELLER_PACKAGE_LENGTH")
    width = attrs.get("SELLER_PACKAGE_WIDTH")
    height = attrs.get("SELLER_PACKAGE_HEIGHT")
    weight = attrs.get("SELLER_PACKAGE_WEIGHT")

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


def _parse_dim_value(raw: str | None, is_weight: bool = False) -> float | None:
    """Parse '15 cm' / '500 g' / '1.2 kg' → value in cm (length) or kg (weight)."""
    if not raw:
        return None
    try:
        parts = str(raw).strip().lower().split()
        num = float(parts[0].replace(",", "."))
    except (ValueError, IndexError):
        return None
    unit = parts[1] if len(parts) > 1 else ("g" if is_weight else "cm")
    if is_weight:
        if unit in ("kg", "kgs"):
            return num
        if unit in ("g", "gr", "grs"):
            return num / 1000.0
        return num / 1000.0  # assume grams
    else:
        if unit == "mm":
            return num / 10.0
        if unit in ("m", "mt"):
            return num * 100.0
        return num  # assume cm


def _extract_dim_numeric(body: dict) -> dict:
    """Return {length_cm, width_cm, height_cm, weight_kg} or Nones."""
    attrs_val = {a["id"]: a.get("value_name") for a in body.get("attributes", [])}
    attrs_struct = {a["id"]: a.get("value_struct") for a in body.get("attributes", [])}

    def _get(*keys, is_weight=False):
        # Prefer value_struct if present (more reliable)
        for k in keys:
            st = attrs_struct.get(k)
            if st and isinstance(st, dict) and st.get("number") is not None:
                num = float(st["number"])
                unit = (st.get("unit") or "").lower()
                if is_weight:
                    if unit in ("kg", "kgs"):
                        return num
                    if unit in ("g", "gr"):
                        return num / 1000.0
                    return num / 1000.0
                else:
                    if unit == "mm":
                        return num / 10.0
                    if unit in ("m", "mt"):
                        return num * 100.0
                    return num
        for k in keys:
            parsed = _parse_dim_value(attrs_val.get(k), is_weight=is_weight)
            if parsed is not None:
                return parsed
        return None

    return {
        "length_cm": _get("SELLER_PACKAGE_LENGTH"),
        "width_cm": _get("SELLER_PACKAGE_WIDTH"),
        "height_cm": _get("SELLER_PACKAGE_HEIGHT"),
        "weight_kg": _get("SELLER_PACKAGE_WEIGHT", is_weight=True),
    }


def _classify_size(dims: dict) -> str:
    """Return 'P', 'M', 'G', 'XG' per ML official Full tiers, or 'unknown'.
    Tiers (empaque primario, lados ordenados):
      Pequeño:     ≤ 12×15×25 cm, ≤ 18 kg
      Mediano:     ≤ 28×36×51 cm, ≤ 18 kg
      Grande:      ≤ 50×60×60 cm, ≤ 18 kg
      Extragrande: > 50×60×60 cm o > 18 kg
    Missing any dimension → 'unknown'."""
    l, w, h, kg = dims["length_cm"], dims["width_cm"], dims["height_cm"], dims["weight_kg"]
    if None in (l, w, h, kg):
        return "unknown"
    s = sorted([l, w, h])
    if kg > 18:
        return "XG"
    if s[0] <= 12 and s[1] <= 15 and s[2] <= 25:
        return "P"
    if s[0] <= 28 and s[1] <= 36 and s[2] <= 51:
        return "M"
    if s[0] <= 50 and s[1] <= 60 and s[2] <= 60:
        return "G"
    return "XG"


def _fetch_fulfillment_stock(cuenta: str, inventory_ids: list[str]) -> dict[str, int]:
    """Query /inventories/{id}/stock/fulfillment for a list of inventory_ids.
    Parallelizes with 10 concurrent requests. Returns {inventory_id: available_quantity}."""
    if not inventory_ids:
        return {}
    ml = get_ml_manager(cuenta)

    def _one(inv_id: str) -> tuple[str, int]:
        try:
            data = ml.get(f"/inventories/{inv_id}/stock/fulfillment")
            return inv_id, data.get("available_quantity", 0)
        except Exception:
            return inv_id, 0

    stock_map: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        for inv_id, qty in pool.map(_one, inventory_ids):
            stock_map[inv_id] = qty
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
    max_shipments = 10  # Check at most 10 shipments (was 30; warehouses repeat heavily)

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

        dim_numeric = _extract_dim_numeric(body)
        # Extract seller_sku from attributes
        attrs = {a["id"]: a.get("value_name") for a in body.get("attributes", [])}
        seller_sku = attrs.get("SELLER_SKU")

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
                "last_updated": body.get("last_updated"),
                "date_created": body.get("date_created"),
                "seller_sku": seller_sku,
                "dimensions": _extract_dimensions(body),
                "dim_numeric": dim_numeric,
                "size_category": _classify_size(dim_numeric),
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
