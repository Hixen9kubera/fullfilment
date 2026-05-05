"""
Productos Estrella – Identifica los productos que más venden en ambas cuentas.

Analiza TODAS las ventas históricas de BEKURA y SANCORFASHION.
Para cada producto muestra:
  - Unidades vendidas totales
  - Ingresos totales
  - % del total de ventas (unidades e ingresos)
  - % acumulado (Pareto)
  - Promedio mensual de ventas
  - Estado actual de la publicación

Run:
    cd /Users/je/dev/kubera
    uv run python fulfillment/productos_estrella.py
"""

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fulfillment.ml_token_manager import MLTokenManager

CUENTAS = ["BEKURA", "SANCORFASHION"]


def fetch_all_orders(cuenta: str) -> list[dict]:
    """Fetch ALL paid orders for an account (full history)."""
    ml = MLTokenManager(table="ml_tokens_dashboard", cuenta=cuenta)
    user_id = ml.get("/users/me")["id"]

    orders: list[dict] = []
    offset = 0
    limit = 50
    total = None

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

        if total is None:
            total = data.get("paging", {}).get("total", 0)
            print(f"  {cuenta}: {total} órdenes totales, descargando...", end="", flush=True)

        offset += limit
        if offset >= total or not batch:
            break

        # Progress indicator
        if offset % 500 == 0:
            print(f" {offset}", end="", flush=True)

    print(f" OK ({len(orders)} descargadas)")
    return orders


def fetch_item_details(cuenta: str, item_ids: list[str]) -> dict[str, dict]:
    """Multi-get item details (status, logistic_type, title)."""
    ml = MLTokenManager(table="ml_tokens_dashboard", cuenta=cuenta)
    details: dict[str, dict] = {}

    for i in range(0, len(item_ids), 20):
        batch = item_ids[i : i + 20]
        data = ml.get("/items", params={"ids": ",".join(batch)})
        for entry in data:
            body = entry.get("body", {})
            if body:
                details[body["id"]] = {
                    "title": body.get("title", ""),
                    "status": body.get("status", "unknown"),
                    "logistic_type": body.get("shipping", {}).get("logistic_type", "unknown"),
                    "price": body.get("price", 0),
                }

    return details


def analyze_account(cuenta: str) -> dict:
    """Analyze all orders for one account and return product ranking."""
    orders = fetch_all_orders(cuenta)

    # Aggregate by item
    item_units: dict[str, int] = defaultdict(int)
    item_revenue: dict[str, float] = defaultdict(float)
    item_skus: dict[str, str] = {}
    item_first_sale: dict[str, datetime] = {}
    item_last_sale: dict[str, datetime] = {}

    for order in orders:
        order_date = order.get("date_created", "")
        try:
            dt = datetime.fromisoformat(order_date)
        except (ValueError, TypeError):
            dt = None

        for oi in order.get("order_items", []):
            item_id = oi.get("item", {}).get("id")
            if not item_id:
                continue

            qty = oi.get("quantity", 0)
            unit_price = oi.get("unit_price", 0)
            sku = oi.get("item", {}).get("seller_sku")

            item_units[item_id] += qty
            item_revenue[item_id] += qty * unit_price

            if sku:
                item_skus[item_id] = sku

            if dt:
                if item_id not in item_first_sale or dt < item_first_sale[item_id]:
                    item_first_sale[item_id] = dt
                if item_id not in item_last_sale or dt > item_last_sale[item_id]:
                    item_last_sale[item_id] = dt

    # Fetch item details (status, title, etc.)
    all_item_ids = list(item_units.keys())
    print(f"  Consultando detalles de {len(all_item_ids)} productos...")
    details = fetch_item_details(cuenta, all_item_ids)

    # Build product list
    total_units = sum(item_units.values())
    total_revenue = sum(item_revenue.values())
    now = datetime.now(timezone.utc)

    products = []
    for item_id in all_item_ids:
        units = item_units[item_id]
        revenue = item_revenue[item_id]
        detail = details.get(item_id, {})

        # Calculate months active (from first sale to now)
        first = item_first_sale.get(item_id)
        if first:
            months_active = max((now - first.astimezone(timezone.utc)).days / 30.0, 1.0)
        else:
            months_active = 1.0

        products.append(
            {
                "item_id": item_id,
                "sku": item_skus.get(item_id, "—"),
                "title": detail.get("title", "—"),
                "status": detail.get("status", "unknown"),
                "logistic_type": detail.get("logistic_type", "unknown"),
                "units_sold": units,
                "revenue": revenue,
                "pct_units": (units / total_units * 100) if total_units else 0,
                "pct_revenue": (revenue / total_revenue * 100) if total_revenue else 0,
                "avg_monthly_units": round(units / months_active, 1),
                "avg_monthly_revenue": round(revenue / months_active, 1),
                "months_active": round(months_active, 1),
                "first_sale": first.isoformat() if first else None,
                "last_sale": item_last_sale.get(item_id, first),
            }
        )

    # Sort by units sold descending
    products.sort(key=lambda p: p["units_sold"], reverse=True)

    # Add cumulative percentage (Pareto)
    cum_units = 0.0
    cum_revenue = 0.0
    for p in products:
        cum_units += p["pct_units"]
        cum_revenue += p["pct_revenue"]
        p["cum_pct_units"] = round(cum_units, 1)
        p["cum_pct_revenue"] = round(cum_revenue, 1)

    return {
        "cuenta": cuenta,
        "total_orders": len(orders),
        "total_units": total_units,
        "total_revenue": total_revenue,
        "unique_products": len(products),
        "first_order": orders[-1]["date_created"] if orders else None,
        "last_order": orders[0]["date_created"] if orders else None,
        "products": products,
    }


def print_report(data: dict, top_n: int = 30):
    """Print a formatted report for one account."""
    c = data["cuenta"]
    print(f"\n{'='*110}")
    print(f"  {c}  |  {data['total_orders']} órdenes  |  {data['total_units']:,} unidades  |  ${data['total_revenue']:,.0f} ingresos  |  {data['unique_products']} productos")
    print(f"  Periodo: {data['first_order'][:10] if data['first_order'] else '?'} → {data['last_order'][:10] if data['last_order'] else '?'}")
    print(f"{'='*110}")

    header = (
        f"{'#':>3}  {'SKU':<22} {'Titulo':<30} {'Est':>7} {'Tipo':>6} "
        f"{'Uds':>7} {'%Uds':>6} {'Acum%':>6} {'$/mes':>10} {'Uds/mes':>8}"
    )
    print(header)
    print("-" * 110)

    for i, p in enumerate(data["products"][:top_n], 1):
        status_short = p["status"][:6]
        type_short = "Full" if p["logistic_type"] == "fulfillment" else "Drop"
        title = p["title"][:29] if p["title"] else "—"

        print(
            f"{i:>3}  {p['sku']:<22} {title:<30} {status_short:>7} {type_short:>6} "
            f"{p['units_sold']:>7,} {p['pct_units']:>5.1f}% {p['cum_pct_units']:>5.1f}% "
            f"${p['avg_monthly_revenue']:>9,.0f} {p['avg_monthly_units']:>7.0f}"
        )

    # Pareto insights
    products = data["products"]
    for threshold in [50, 80, 90]:
        count = 0
        for p in products:
            count += 1
            if p["cum_pct_units"] >= threshold:
                break
        print(f"\n  → {threshold}% de las ventas vienen de {count} productos ({count/len(products)*100:.1f}% del catálogo)")


def print_combined_report(accounts: list[dict], top_n: int = 30):
    """Combine both accounts and print unified ranking."""
    # Merge products by SKU
    sku_data: dict[str, dict] = {}

    for acct in accounts:
        for p in acct["products"]:
            sku = p["sku"]
            if sku == "—":
                sku = p["item_id"]  # Use item_id if no SKU

            if sku not in sku_data:
                sku_data[sku] = {
                    "sku": p["sku"],
                    "title": p["title"],
                    "units_sold": 0,
                    "revenue": 0.0,
                    "avg_monthly_units": 0.0,
                    "avg_monthly_revenue": 0.0,
                    "cuentas": [],
                    "statuses": set(),
                }

            sku_data[sku]["units_sold"] += p["units_sold"]
            sku_data[sku]["revenue"] += p["revenue"]
            sku_data[sku]["avg_monthly_units"] += p["avg_monthly_units"]
            sku_data[sku]["avg_monthly_revenue"] += p["avg_monthly_revenue"]
            sku_data[sku]["cuentas"].append(acct["cuenta"])
            sku_data[sku]["statuses"].add(p["status"])

    combined = sorted(sku_data.values(), key=lambda x: x["units_sold"], reverse=True)

    total_units = sum(p["units_sold"] for p in combined)
    total_revenue = sum(p["revenue"] for p in combined)

    # Add percentages
    cum = 0.0
    for p in combined:
        p["pct_units"] = (p["units_sold"] / total_units * 100) if total_units else 0
        cum += p["pct_units"]
        p["cum_pct_units"] = round(cum, 1)

    total_orders = sum(a["total_orders"] for a in accounts)

    print(f"\n{'#'*110}")
    print(f"  CONSOLIDADO AMBAS CUENTAS  |  {total_orders:,} órdenes  |  {total_units:,} unidades  |  ${total_revenue:,.0f} ingresos")
    print(f"  Productos únicos (por SKU): {len(combined)}")
    print(f"{'#'*110}")

    header = (
        f"{'#':>3}  {'SKU':<22} {'Titulo':<30} {'Cuentas':<12} "
        f"{'Uds':>7} {'%Uds':>6} {'Acum%':>6} {'$/mes':>10} {'Uds/mes':>8}"
    )
    print(header)
    print("-" * 110)

    for i, p in enumerate(combined[:top_n], 1):
        title = p["title"][:29] if p["title"] else "—"
        cuentas = ",".join(sorted(set(c[:3] for c in p["cuentas"])))

        print(
            f"{i:>3}  {p['sku']:<22} {title:<30} {cuentas:<12} "
            f"{p['units_sold']:>7,} {p['pct_units']:>5.1f}% {p['cum_pct_units']:>5.1f}% "
            f"${p['avg_monthly_revenue']:>9,.0f} {p['avg_monthly_units']:>7.0f}"
        )

    for threshold in [50, 80, 90]:
        count = 0
        for p in combined:
            count += 1
            if p["cum_pct_units"] >= threshold:
                break
        print(f"\n  → {threshold}% de las ventas vienen de {count} productos ({count/len(combined)*100:.1f}% del catálogo)")


if __name__ == "__main__":
    print("=" * 60)
    print("  ANÁLISIS DE PRODUCTOS ESTRELLA")
    print("=" * 60)

    accounts = []
    for cuenta in CUENTAS:
        print(f"\n📦 Procesando {cuenta}...")
        data = analyze_account(cuenta)
        accounts.append(data)

    # Per-account reports
    for data in accounts:
        print_report(data)

    # Combined report
    print_combined_report(accounts)

    print("\n✅ Análisis completo.")
