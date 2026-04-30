"""Busca SKUs en todas las cuentas ML y muestra su tamaño."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fulfillment.config import CUENTAS, get_ml_manager
from fulfillment.api.ml_stock import _extract_dim_numeric, _extract_dimensions, _classify_size

SKUS_BUSCADOS = [
    "TEC-0769-AZL-NEG",
    "TEC-1315-NEG",
    "ORG-0280-BLN",
    "PAS-0022-ROS",
    "TEC-0660-MUL",
    "BEB-0034-BLN",
    "TEC-0603-NEG",
    "TEC-0976-NEG-LED",
    "MUE-0083-GRI-ROS-VER",
    "TEC-0293-NEG",
    "CAM-0017-BLN",
    "TEC-0434-AZL",
    "TEC-0551-PLU",
    "TEC-0475-NEG",
    "TEC-0492-MUL",
]


def fetch_items_by_sku(cuenta: str, skus: set[str]) -> list[dict]:
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

    results = []
    for i in range(0, len(all_ids), 20):
        batch = all_ids[i : i + 20]
        data = ml.get("/items", params={"ids": ",".join(batch)})
        for entry in data:
            body = entry.get("body")
            if not body:
                continue
            attrs = {a["id"]: a.get("value_name") for a in body.get("attributes", [])}
            seller_sku = attrs.get("SELLER_SKU", "")
            if seller_sku in skus:
                dims = _extract_dim_numeric(body)
                results.append({
                    "cuenta": cuenta,
                    "item_id": body.get("id"),
                    "title": body.get("title"),
                    "sku": seller_sku,
                    "status": body.get("status"),
                    "dimensions": _extract_dimensions(body) or "—",
                    "size_category": _classify_size(dims),
                    "dim_numeric": dims,
                })
    return results


def main():
    skus_set = set(SKUS_BUSCADOS)
    encontrados: dict[str, list[dict]] = {}

    for cuenta in CUENTAS:
        print(f"Buscando en {cuenta}...", flush=True)
        items = fetch_items_by_sku(cuenta, skus_set)
        for item in items:
            sku = item["sku"]
            if sku not in encontrados:
                encontrados[sku] = []
            encontrados[sku].append(item)

    print("\n" + "=" * 80)
    print(f"{'SKU':<25} {'Cuenta':<16} {'Tamaño':<8} {'Dimensiones':<35} {'Estado'}")
    print("=" * 80)

    for sku in SKUS_BUSCADOS:
        items = encontrados.get(sku, [])
        if not items:
            print(f"{sku:<25} {'—':<16} {'—':<8} {'No encontrado':<35}")
        else:
            for item in items:
                dims = item["dim_numeric"]
                dim_str = item["dimensions"]
                size = item["size_category"]
                print(f"{sku:<25} {item['cuenta']:<16} {size:<8} {dim_str:<35} {item['status']}")

    # Resumen de no encontrados
    no_encontrados = [s for s in SKUS_BUSCADOS if s not in encontrados]
    if no_encontrados:
        print(f"\nSKUs no encontrados en ninguna cuenta:")
        for s in no_encontrados:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
