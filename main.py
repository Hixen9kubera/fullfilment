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
from .api.odoo_stock import fetch_odoo_oldest_in_date_by_sku, fetch_odoo_stock_by_sku, fetch_odoo_weight_by_sku
from .api.ml_stock import router as ml_stock_router
from .api.ml_ventas import router as ml_ventas_router
from .api.odoo_stock import router as odoo_router
from .routers.recomendador import router as recomendador_router
from .routers.reports import router as reports_router
from .restock.router import router as restock_router
from .restock.supabase_queries import (
    fetch_dashboard_stock,
    fetch_dashboard_monthly_sales,
    fetch_estrella_data,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Kubera Fulfillment")
app.include_router(ml_stock_router)
app.include_router(ml_ventas_router)
app.include_router(odoo_router)
app.include_router(recomendador_router)
app.include_router(reports_router)
app.include_router(restock_router)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


# ======================================================================
# Dashboard – combined stock + monthly sales table (Supabase-backed)
# ======================================================================

def _build_dashboard_data(
    cuenta: str,
    stock_rows: list[dict],
    sales_rows: list[dict],
    odoo_stock: dict[str, float],
    odoo_days: dict[str, int],
    odoo_weight: dict[str, float] | None = None,
) -> dict:
    # Build sales lookup maps for this account
    sales_map: dict[str, int] = {}
    revenue_map: dict[str, float] = {}
    sku_from_sales: dict[str, str] = {}
    for s in sales_rows:
        if s["cuenta"] != cuenta:
            continue
        iid = s["item_id"]
        sales_map[iid] = sales_map.get(iid, 0) + (s.get("units_sold") or 0)
        revenue_map[iid] = revenue_map.get(iid, 0) + float(s.get("revenue") or 0)
        if s.get("sku"):
            sku_from_sales[iid] = s["sku"]

    now = datetime.now(timezone.utc)
    rows = []

    for p in stock_rows:
        if p["cuenta"] != cuenta:
            continue

        item_id = p["item_id"]
        sku = p.get("sku") or sku_from_sales.get(item_id, "—")

        start_time = p.get("start_time")
        days_published = None
        if start_time:
            try:
                dt = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
                days_published = (now - dt.astimezone(timezone.utc)).days
            except (ValueError, TypeError):
                pass

        odoo_qty = odoo_stock.get(sku, 0) if sku != "—" else 0
        odoo_age = odoo_days.get(sku) if sku != "—" else None
        odoo_kg = (odoo_weight or {}).get(sku) if sku != "—" else None

        rows.append({
            "item_id": item_id,
            "sku": sku,
            "title": p.get("title") or "—",
            "status": p.get("status"),
            "logistic_type": p.get("logistic_type"),
            "warehouse": p.get("warehouse") or "—",
            "start_time": start_time,
            "days_published": days_published,
            "monthly_sales": sales_map.get(item_id, 0),
            "monthly_revenue": revenue_map.get(item_id, 0),
            "stock_full": p.get("stock_full") or 0,
            "stock_odoo": odoo_qty,
            "odoo_days": odoo_age,
            "price": p.get("price"),
            "dimensions": p.get("dimensions") or "—",
            "weight_odoo": odoo_kg,
            "size_category": p.get("size_category") or "unknown",
        })

    rows.sort(key=lambda r: r["stock_full"], reverse=True)

    return {
        "cuenta": cuenta,
        "seller_id": None,
        "total_items": len(rows),
        "rows": rows,
    }


def _build_combined_rows(accounts: list[dict]) -> list[dict]:
    """Merge rows across accounts by SKU. Items with SKU='—' are kept separate by item_id."""
    merged: dict[str, dict] = {}
    for acct in accounts:
        for r in acct["rows"]:
            key = r["sku"] if r["sku"] != "—" else f"__noSku__{r['item_id']}"
            if key not in merged:
                merged[key] = {
                    "sku": r["sku"],
                    "title": r["title"],
                    "size_category": r["size_category"],
                    "status": r["status"],
                    "logistic_type": r["logistic_type"],
                    "stock_full": 0,
                    "stock_odoo": r["stock_odoo"],  # Odoo is shared, not summed
                    "odoo_days": r["odoo_days"],
                    "weight_odoo": r.get("weight_odoo"),
                    "monthly_sales": 0,
                    "monthly_revenue": 0,
                    "price": r["price"],
                    "dimensions": r["dimensions"],
                    "cuentas": [],
                    "items": [],
                }
            m = merged[key]
            m["stock_full"] += r["stock_full"]
            m["monthly_sales"] += r["monthly_sales"]
            m["monthly_revenue"] += r["monthly_revenue"]
            m["cuentas"].append(acct["cuenta"])
            m["items"].append(
                {
                    "cuenta": acct["cuenta"],
                    "item_id": r["item_id"],
                    "status": r["status"],
                    "logistic_type": r["logistic_type"],
                    "stock_full": r["stock_full"],
                    "monthly_sales": r["monthly_sales"],
                }
            )
            # Promote to "active" if any of the accounts has it active
            if r["status"] == "active":
                m["status"] = "active"
            # Promote to fulfillment if any account has it on Full
            if r["logistic_type"] == "fulfillment":
                m["logistic_type"] = "fulfillment"

    rows = list(merged.values())
    for r in rows:
        r["cuentas"] = sorted(set(r["cuentas"]))
    rows.sort(key=lambda r: r["stock_full"], reverse=True)
    return rows


@app.get("/api/dashboard/ml")
async def dashboard_data():
    loop = asyncio.get_event_loop()
    try:
        # Odoo (3 bulk calls, fast) + Supabase queries — all in parallel
        odoo_stock, odoo_days, odoo_weight, stock_rows, sales_rows = await asyncio.gather(
            loop.run_in_executor(None, fetch_odoo_stock_by_sku),
            loop.run_in_executor(None, fetch_odoo_oldest_in_date_by_sku),
            loop.run_in_executor(None, fetch_odoo_weight_by_sku),
            loop.run_in_executor(None, fetch_dashboard_stock),
            loop.run_in_executor(None, fetch_dashboard_monthly_sales),
        )
    except Exception as exc:
        logger.exception("Error fetching dashboard data")
        raise HTTPException(status_code=502, detail=str(exc))

    results = [
        _build_dashboard_data(c, stock_rows, sales_rows, odoo_stock, odoo_days, odoo_weight)
        for c in CUENTAS
    ]

    combined_rows = _build_combined_rows(results)
    combined = {
        "cuenta": "AMBAS",
        "seller_id": None,
        "total_items": len(combined_rows),
        "rows": combined_rows,
    }

    return {
        "fecha_consulta": datetime.now(timezone.utc).isoformat(),
        "cuentas": results + [combined],
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="dashboard.html")


# ======================================================================
# Productos Estrella – all-time sales analysis (Supabase-backed)
# ======================================================================

def _analyze_estrella(cuenta: str, rows: list[dict]) -> dict:
    """Build estrella analysis from pre-aggregated Supabase rows."""
    now = datetime.now(timezone.utc).date()

    total_units = sum(r.get("units_sold") or 0 for r in rows)
    total_revenue = sum(float(r.get("revenue") or 0) for r in rows)

    products = []
    for r in rows:
        units = r.get("units_sold") or 0
        revenue = float(r.get("revenue") or 0)
        first = r.get("first_sale")
        if first:
            if isinstance(first, str):
                first = datetime.fromisoformat(first).date()
            months_active = max((now - first).days / 30.0, 1.0)
        else:
            months_active = 1.0

        products.append({
            "item_id": r["item_id"],
            "sku": r.get("sku") or "—",
            "title": r.get("title") or "—",
            "units_sold": units,
            "revenue": revenue,
            "pct_units": round(units / total_units * 100, 2) if total_units else 0,
            "pct_revenue": round(revenue / total_revenue * 100, 2) if total_revenue else 0,
            "avg_monthly_units": round(units / months_active, 1),
            "avg_monthly_revenue": round(revenue / months_active, 0),
            "months_active": round(months_active, 1),
        })

    products.sort(key=lambda p: p["units_sold"], reverse=True)

    cum_u = 0.0
    cum_r = 0.0
    for p in products:
        cum_u += p["pct_units"]
        cum_r += p["pct_revenue"]
        p["cum_pct_units"] = round(cum_u, 1)
        p["cum_pct_revenue"] = round(cum_r, 1)

    return {
        "cuenta": cuenta,
        "total_orders": 0,
        "total_units": total_units,
        "total_revenue": total_revenue,
        "unique_products": len(products),
        "products": products,
    }


@app.get("/api/dashboard/estrella")
async def estrella_data():
    loop = asyncio.get_event_loop()
    try:
        all_rows = await loop.run_in_executor(None, fetch_estrella_data)
    except Exception as exc:
        logger.exception("Error building estrella")
        raise HTTPException(status_code=502, detail=str(exc))

    results = [
        _analyze_estrella(c, [r for r in all_rows if r["cuenta"] == c])
        for c in CUENTAS
    ]

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


@app.get("/recomendador", response_class=HTMLResponse)
async def recomendador_page(request: Request):
    return templates.TemplateResponse(request=request, name="recomendador.html")


@app.get("/restock", response_class=HTMLResponse)
async def restock_page(request: Request):
    return templates.TemplateResponse(request=request, name="restock_page.html")
