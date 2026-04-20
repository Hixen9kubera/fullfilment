"""
Fulfillment API – MercadoLibre & Odoo endpoints + interactive dashboards.

Run:
    cd /Users/je/dev/kubera
    uv run uvicorn fulfillment.main:app --reload --port 8001
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .config import CUENTAS
from .ml_stock import fetch_stock_for_account
from .ml_ventas import fetch_all_orders_history, fetch_monthly_sales_by_item
from .odoo_stock import fetch_odoo_stock_by_sku
from .ml_stock import router as ml_stock_router
from .ml_ventas import router as ml_ventas_router
from .odoo_stock import router as odoo_router

logger = logging.getLogger(__name__)

app = FastAPI(title="Kubera Fulfillment")
app.include_router(ml_stock_router)
app.include_router(ml_ventas_router)
app.include_router(odoo_router)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


# ======================================================================
# Dashboard – combined stock + monthly sales table
# ======================================================================

def _build_dashboard_data(cuenta: str, odoo_stock: dict[str, float]) -> dict:
    stock_data = fetch_stock_for_account(cuenta)
    monthly = fetch_monthly_sales_by_item(cuenta)
    sales_map = monthly["sales"]
    revenue_map = monthly["revenue"]
    sku_map = monthly["skus"]

    now = datetime.now(timezone.utc)
    rows = []

    for product in stock_data["products"]:
        item_id = product["item_id"]
        start_time = product.get("start_time")
        days_published = None
        if start_time:
            try:
                dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                days_published = (now - dt).days
            except (ValueError, TypeError):
                pass

        sku = sku_map.get(item_id, "—")
        odoo_qty = odoo_stock.get(sku, 0) if sku != "—" else 0

        rows.append(
            {
                "item_id": item_id,
                "sku": sku,
                "title": product["title"],
                "status": product["status"],
                "logistic_type": product["logistic_type"],
                "warehouse": product["warehouse"],
                "start_time": start_time,
                "days_published": days_published,
                "monthly_sales": sales_map.get(item_id, 0),
                "monthly_revenue": revenue_map.get(item_id, 0),
                "stock_full": product["stock_full"],
                "stock_odoo": odoo_qty,
                "price": product["price"],
                "dimensions": product.get("dimensions") or "—",
            }
        )

    rows.sort(key=lambda r: r["stock_full"], reverse=True)

    return {
        "cuenta": cuenta,
        "seller_id": stock_data["seller_id"],
        "total_items": len(rows),
        "rows": rows,
    }


@app.get("/api/dashboard/ml")
async def dashboard_data():
    loop = asyncio.get_event_loop()
    odoo_stock = await loop.run_in_executor(None, fetch_odoo_stock_by_sku)
    tasks = [
        loop.run_in_executor(None, _build_dashboard_data, c, odoo_stock)
        for c in CUENTAS
    ]
    try:
        results = await asyncio.gather(*tasks)
    except Exception as exc:
        logger.exception("Error building dashboard")
        raise HTTPException(status_code=502, detail=str(exc))

    return {"fecha_consulta": datetime.now(timezone.utc).isoformat(), "cuentas": results}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html")


# ======================================================================
# Productos Estrella – all-time sales analysis
# ======================================================================

def _analyze_estrella(cuenta: str) -> dict:
    """Analyze all historical orders for one account. No item detail calls."""
    orders = fetch_all_orders_history(cuenta)

    item_units: dict[str, int] = defaultdict(int)
    item_revenue: dict[str, float] = defaultdict(float)
    item_skus: dict[str, str] = {}
    item_titles: dict[str, str] = {}
    item_first_sale: dict[str, datetime] = {}

    for order in orders:
        order_date = order.get("date_created", "")
        try:
            dt = datetime.fromisoformat(order_date)
        except (ValueError, TypeError):
            dt = None

        for oi in order.get("order_items", []):
            item = oi.get("item", {})
            item_id = item.get("id")
            if not item_id:
                continue
            qty = oi.get("quantity", 0)
            price = oi.get("unit_price", 0)
            item_units[item_id] += qty
            item_revenue[item_id] += qty * price
            sku = item.get("seller_sku")
            if sku:
                item_skus[item_id] = sku
            title = item.get("title")
            if title:
                item_titles[item_id] = title
            if dt and (item_id not in item_first_sale or dt < item_first_sale[item_id]):
                item_first_sale[item_id] = dt

    now = datetime.now(timezone.utc)
    total_units = sum(item_units.values())
    total_revenue = sum(item_revenue.values())

    products = []
    for item_id in item_units:
        units = item_units[item_id]
        revenue = item_revenue[item_id]
        first = item_first_sale.get(item_id)
        months_active = max((now - first.astimezone(timezone.utc)).days / 30.0, 1.0) if first else 1.0

        products.append(
            {
                "item_id": item_id,
                "sku": item_skus.get(item_id, "—"),
                "title": item_titles.get(item_id, "—"),
                "units_sold": units,
                "revenue": revenue,
                "pct_units": round(units / total_units * 100, 2) if total_units else 0,
                "pct_revenue": round(revenue / total_revenue * 100, 2) if total_revenue else 0,
                "avg_monthly_units": round(units / months_active, 1),
                "avg_monthly_revenue": round(revenue / months_active, 0),
                "months_active": round(months_active, 1),
            }
        )

    products.sort(key=lambda p: p["units_sold"], reverse=True)

    # Cumulative percentages
    cum_u = 0.0
    cum_r = 0.0
    for p in products:
        cum_u += p["pct_units"]
        cum_r += p["pct_revenue"]
        p["cum_pct_units"] = round(cum_u, 1)
        p["cum_pct_revenue"] = round(cum_r, 1)

    return {
        "cuenta": cuenta,
        "total_orders": len(orders),
        "total_units": total_units,
        "total_revenue": total_revenue,
        "unique_products": len(products),
        "products": products,
    }


@app.get("/api/dashboard/estrella")
async def estrella_data():
    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(None, _analyze_estrella, c) for c in CUENTAS]
    try:
        results = await asyncio.gather(*tasks)
    except Exception as exc:
        logger.exception("Error building estrella")
        raise HTTPException(status_code=502, detail=str(exc))

    # Build combined view (merge by SKU)
    sku_data: dict[str, dict] = {}
    for acct in results:
        for p in acct["products"]:
            key = p["sku"] if p["sku"] != "—" else p["item_id"]
            if key not in sku_data:
                sku_data[key] = {
                    "sku": p["sku"],
                    "title": p["title"],
                    "units_sold": 0,
                    "revenue": 0.0,
                    "avg_monthly_units": 0.0,
                    "avg_monthly_revenue": 0.0,
                    "cuentas": [],
                }
            sku_data[key]["units_sold"] += p["units_sold"]
            sku_data[key]["revenue"] += p["revenue"]
            sku_data[key]["avg_monthly_units"] += p["avg_monthly_units"]
            sku_data[key]["avg_monthly_revenue"] += p["avg_monthly_revenue"]
            sku_data[key]["cuentas"].append(acct["cuenta"])

    combined = sorted(sku_data.values(), key=lambda x: x["units_sold"], reverse=True)
    total_units = sum(p["units_sold"] for p in combined)
    total_revenue = sum(p["revenue"] for p in combined)

    cum_u = 0.0
    cum_r = 0.0
    for p in combined:
        p["pct_units"] = round(p["units_sold"] / total_units * 100, 2) if total_units else 0
        p["pct_revenue"] = round(p["revenue"] / total_revenue * 100, 2) if total_revenue else 0
        cum_u += p["pct_units"]
        cum_r += p["pct_revenue"]
        p["cum_pct_units"] = round(cum_u, 1)
        p["cum_pct_revenue"] = round(cum_r, 1)

    grand_orders = sum(r["total_orders"] for r in results)

    return {
        "fecha_consulta": datetime.now(timezone.utc).isoformat(),
        "consolidado": {
            "total_orders": grand_orders,
            "total_units": total_units,
            "total_revenue": total_revenue,
            "unique_products": len(combined),
            "products": combined,
        },
        "cuentas": results,
    }


@app.get("/estrella", response_class=HTMLResponse)
async def estrella_page(request: Request):
    return templates.TemplateResponse(request=request, name="estrella.html")
