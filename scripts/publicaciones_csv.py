"""
Reporte CSV: publicaciones agrupadas por SKU con stock y conteo de activas/pausadas por tienda.

Run:
    cd /Users/je/dev/kubera
    uv run python -m fulfillment.scripts.publicaciones_csv
"""

import asyncio
import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from ..config import CUENTAS
from ..api.ml_stock import fetch_stock_for_account


def _empty_acct() -> dict:
    return {"pubs": 0, "activas": 0, "pausadas": 0, "stock_full": 0, "stock_pub": 0, "items": []}


async def main() -> Path:
    loop = asyncio.get_event_loop()
    results = await asyncio.gather(
        *(loop.run_in_executor(None, fetch_stock_for_account, c) for c in CUENTAS)
    )

    # SKU → {"title": ..., "by_acct": {cuenta: {...}}}
    grouped: dict[str, dict] = defaultdict(lambda: {"title": "", "by_acct": defaultdict(_empty_acct)})

    for acct in results:
        cuenta = acct["cuenta"]
        for p in acct["products"]:
            sku = p.get("seller_sku") or f"SIN_SKU::{p['item_id']}"
            entry = grouped[sku]
            if not entry["title"] and p.get("title"):
                entry["title"] = p["title"]
            row = entry["by_acct"][cuenta]
            row["pubs"] += 1
            if p.get("status") == "active":
                row["activas"] += 1
            elif p.get("status") == "paused":
                row["pausadas"] += 1
            row["stock_full"] += p.get("stock_full", 0) or 0
            row["stock_pub"] += p.get("available_quantity", 0) or 0
            row["items"].append(p["item_id"])

    now = datetime.now(timezone.utc)
    out_path = Path(__file__).resolve().parent / f"publicaciones_por_sku_{now.strftime('%Y%m%d_%H%M')}.csv"

    header = ["SKU", "Título"]
    for c in CUENTAS:
        header += [f"{c} Pubs", f"{c} Activas", f"{c} Pausadas", f"{c} Stock Full", f"{c} Stock pub", f"{c} Item IDs"]
    header += ["Total Pubs", "Total Activas", "Total Pausadas", "Total Stock Full"]

    rows = []
    for sku, entry in grouped.items():
        row = [sku if not sku.startswith("SIN_SKU::") else "—", entry["title"]]
        tot_pubs = tot_act = tot_pau = tot_full = 0
        for c in CUENTAS:
            a = entry["by_acct"].get(c, _empty_acct())
            row += [a["pubs"], a["activas"], a["pausadas"], a["stock_full"], a["stock_pub"], " ".join(a["items"])]
            tot_pubs += a["pubs"]
            tot_act += a["activas"]
            tot_pau += a["pausadas"]
            tot_full += a["stock_full"]
        row += [tot_pubs, tot_act, tot_pau, tot_full]
        rows.append(row)

    rows.sort(key=lambda r: r[-1], reverse=True)  # by Total Stock Full desc

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

    skus_con_sku = sum(1 for s in grouped if not s.startswith("SIN_SKU::"))
    skus_sin = len(grouped) - skus_con_sku
    print(f"OK · {len(grouped)} SKUs ({skus_con_sku} con SKU, {skus_sin} sin SKU) · {out_path}")
    for a in results:
        active = sum(1 for p in a["products"] if p.get("status") == "active")
        paused = sum(1 for p in a["products"] if p.get("status") == "paused")
        print(f"  {a['cuenta']}: {len(a['products'])} pubs (activas: {active}, pausadas: {paused})")
    return out_path


if __name__ == "__main__":
    asyncio.run(main())
