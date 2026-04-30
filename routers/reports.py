"""Sales/inventory CSV reports."""

import asyncio
import csv
import io
import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..config import CUENTAS
from ..api.ml_stock import fetch_stock_for_account
from ..api.ml_ventas import fetch_all_orders_history, fetch_monthly_sales_by_item
from ..api.odoo_stock import fetch_odoo_oldest_in_date_by_sku, fetch_odoo_stock_by_sku

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reports", tags=["Reports"])


def _index_history(orders: list[dict]) -> dict[str, dict]:
    """{item_id: {units, revenue, first, last, sku, title}}."""
    by_item: dict[str, dict] = defaultdict(
        lambda: {"units": 0, "revenue": 0.0, "first": None, "last": None, "sku": None, "title": None}
    )
    for order in orders:
        raw = order.get("date_created", "")
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            dt = None
        for oi in order.get("order_items", []):
            item = oi.get("item", {})
            item_id = item.get("id")
            if not item_id:
                continue
            qty = oi.get("quantity", 0)
            price = oi.get("unit_price", 0) or 0
            rec = by_item[item_id]
            rec["units"] += qty
            rec["revenue"] += qty * price
            if item.get("seller_sku"):
                rec["sku"] = item["seller_sku"]
            if item.get("title"):
                rec["title"] = item["title"]
            if dt:
                if rec["first"] is None or dt < rec["first"]:
                    rec["first"] = dt
                if rec["last"] is None or dt > rec["last"]:
                    rec["last"] = dt
    return by_item


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        s = raw.replace("Z", "+00:00") if isinstance(raw, str) else raw
        dt = datetime.fromisoformat(s) if isinstance(s, str) else s
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _days_to(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, (now - value).days)


def _size_label(s: str) -> str:
    return {"P": "Pequeño", "M": "Mediano", "G": "Grande", "XG": "Extragrande"}.get(s, "Sin clasificar")


async def _gather_account(cuenta: str) -> dict:
    loop = asyncio.get_event_loop()
    stock_fut = loop.run_in_executor(None, fetch_stock_for_account, cuenta)
    monthly_fut = loop.run_in_executor(None, fetch_monthly_sales_by_item, cuenta)
    history_fut = loop.run_in_executor(None, fetch_all_orders_history, cuenta)
    stock_data, monthly, orders = await asyncio.gather(stock_fut, monthly_fut, history_fut)
    return {
        "cuenta": cuenta,
        "stock": stock_data,
        "monthly": monthly,
        "history": _index_history(orders),
    }


@router.get("/ventas.csv")
async def ventas_csv():
    """Unified per-SKU sales & inventory report across both ML accounts."""
    loop = asyncio.get_event_loop()
    odoo_stock_fut = loop.run_in_executor(None, fetch_odoo_stock_by_sku)
    odoo_days_fut = loop.run_in_executor(None, fetch_odoo_oldest_in_date_by_sku)
    try:
        accounts = await asyncio.gather(*(_gather_account(c) for c in CUENTAS))
        odoo_stock = await odoo_stock_fut
        odoo_days = await odoo_days_fut
    except Exception as exc:
        logger.exception("Error building report")
        raise HTTPException(status_code=502, detail=str(exc))

    now = datetime.now(timezone.utc)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Tienda",
        "SKU",
        "Item ID",
        "Título",
        "Estado",
        "Fecha pausa (aprox)",
        "Tipo logístico",
        "En Full",
        "Tamaño",
        "Bodega",
        "Precio",
        "Fecha publicación",
        "Días publicado",
        "Fecha primera venta",
        "Fecha última venta",
        "Stock Full",
        "Stock Odoo",
        "Días en Odoo (más antiguo)",
        "Ventas totales (uds)",
        "$Venta total",
        "Ventas 30d (uds)",
        "$Venta 30d",
        "Venta diaria prom (histórico)",
        "Venta diaria prom (últimos 30d)",
        "Días agotar stock Odoo",
        "Días agotar stock Odoo+Full",
        "Dimensiones",
    ])

    for acct in accounts:
        cuenta = acct["cuenta"]
        sales_map = acct["monthly"]["sales"]
        revenue_map = acct["monthly"]["revenue"]
        sku_map = acct["monthly"]["skus"]
        history = acct["history"]

        for p in acct["stock"]["products"]:
            item_id = p["item_id"]
            sku = p.get("seller_sku") or sku_map.get(item_id) or history.get(item_id, {}).get("sku") or "—"
            status = p.get("status", "unknown")
            logistic = p.get("logistic_type", "unknown")
            is_full = "sí" if logistic == "fulfillment" else "no"
            size = _size_label(p.get("size_category", "unknown"))
            warehouse = p.get("warehouse", "—")
            price = p.get("price")

            start_dt = _parse_dt(p.get("start_time"))
            days_pub = _days_to(start_dt, now)

            pause_dt_raw = p.get("last_updated") if status == "paused" else None
            pause_dt = _parse_dt(pause_dt_raw)

            # Historical aggregate
            hist = history.get(item_id, {})
            total_units = hist.get("units", 0)
            total_revenue = hist.get("revenue", 0.0)
            first_sale = hist.get("first")
            last_sale = hist.get("last")

            # 30-day
            units_30 = sales_map.get(item_id, 0)
            revenue_30 = revenue_map.get(item_id, 0.0)

            # Daily averages
            if start_dt and days_pub and days_pub > 0:
                daily_hist = total_units / days_pub
            elif first_sale:
                span_days = max(1, (now - first_sale).days)
                daily_hist = total_units / span_days
            else:
                daily_hist = 0.0
            daily_30 = units_30 / 30.0

            stock_full = p.get("stock_full", 0) or 0
            stock_odoo = odoo_stock.get(sku, 0) if sku and sku != "—" else 0
            odoo_age = odoo_days.get(sku) if sku and sku != "—" else None

            def _days_to_deplete(stock_available):
                if daily_30 <= 0 or stock_available <= 0:
                    return ""
                return f"{stock_available / daily_30:.1f}"

            writer.writerow([
                cuenta,
                sku,
                item_id,
                p.get("title") or "",
                status,
                pause_dt.isoformat() if pause_dt else "",
                logistic,
                is_full,
                size,
                warehouse,
                f"{price:.2f}" if price is not None else "",
                start_dt.date().isoformat() if start_dt else "",
                days_pub if days_pub is not None else "",
                first_sale.date().isoformat() if first_sale else "",
                last_sale.date().isoformat() if last_sale else "",
                stock_full,
                stock_odoo,
                odoo_age if odoo_age is not None else "",
                total_units,
                f"{total_revenue:.2f}",
                units_30,
                f"{revenue_30:.2f}",
                f"{daily_hist:.3f}",
                f"{daily_30:.3f}",
                _days_to_deplete(stock_odoo),
                _days_to_deplete(stock_odoo + stock_full),
                p.get("dimensions") or "",
            ])

    csv_data = buf.getvalue()
    filename = f"kubera_ventas_{now.strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
