"""
Test script – Explore MercadoLibre fulfillment APIs.

Run from project root:
    uv run python fulfillment/test.py
"""

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / "sales-dashboard" / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sales-dashboard"))

from ml_token_manager import MLTokenManager

CUENTA = "BEKURA"
ml = MLTokenManager(table="ml_tokens_dashboard", cuenta=CUENTA)


def pp(label: str, data):
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


# -- Helpers --
user_id = ml.get("/users/me")["id"]
print(f"Cuenta: {CUENTA} | user_id: {user_id}")

# Get a sample item to work with
search = ml.get(f"/users/{user_id}/items/search", params={"limit": 5})
item_ids = search.get("results", [])
print(f"Total items: {search['paging']['total']}, sample: {item_ids[:3]}")


# ── 1. JSON completo de una publicacion ──────────────────────────────
sample_id = item_ids[0]
item_detail = ml.get(f"/items/{sample_id}")
pp(f"1) Detalle de publicacion {sample_id}", item_detail)


# ── 2. GET /users/{{user_id}}/fulfillment/inventory_items ────────────
print("\n\n")
try:
    r2 = ml.get(f"/users/{user_id}/fulfillment/inventory_items")
    pp("2) /users/{user_id}/fulfillment/inventory_items", r2)
except Exception as e:
    pp("2) /users/{user_id}/fulfillment/inventory_items — ERROR", {"error": str(e)})


# ── 3. Inventory ID from item, then /inventories/{{id}}/stock/fulfillment
print("\n\n")
# Try to find an inventory_id from item variations
inventory_id = None
for item_id in item_ids:
    detail = ml.get(f"/items/{item_id}")
    for var in detail.get("variations", []):
        inv = var.get("inventory_id")
        if inv:
            inventory_id = inv
            print(f"Found inventory_id={inv} in item={item_id} variation={var['id']}")
            break
    if inventory_id:
        break

if inventory_id:
    try:
        r3 = ml.get(f"/inventories/{inventory_id}/stock/fulfillment")
        pp(f"3) /inventories/{inventory_id}/stock/fulfillment", r3)
    except Exception as e:
        pp(f"3) /inventories/{inventory_id}/stock/fulfillment — ERROR", {"error": str(e)})
else:
    pp("3) /inventories/{{id}}/stock/fulfillment", {"skip": "No inventory_id found in items"})


# ── 4. GET /users/{{user_id}}/inventory_items?fulfillment=true ────────
print("\n\n")
try:
    r4 = ml.get(f"/users/{user_id}/inventory_items", params={"fulfillment": "true"})
    pp("4) /users/{user_id}/inventory_items?fulfillment=true", r4)
except Exception as e:
    pp("4) /users/{user_id}/inventory_items?fulfillment=true — ERROR", {"error": str(e)})


# ── 5. Same as 3 but with first inventory_id from test 4 if available
print("\n\n")
try:
    r4_data = ml.get(f"/users/{user_id}/inventory_items", params={"fulfillment": "true"})
    results = r4_data.get("results", r4_data) if isinstance(r4_data, dict) else r4_data
    inv_id_5 = None

    if isinstance(results, list) and results:
        inv_id_5 = results[0].get("inventory_id") or results[0].get("id")
    elif isinstance(results, dict):
        for item in results.get("results", results.get("items", [])):
            inv_id_5 = item.get("inventory_id") or item.get("id")
            if inv_id_5:
                break

    if inv_id_5:
        r5 = ml.get(f"/inventories/{inv_id_5}/stock/fulfillment")
        pp(f"5) /inventories/{inv_id_5}/stock/fulfillment (from test 4)", r5)
    else:
        pp("5) /inventories/{{id}}/stock/fulfillment", {"skip": "No inventory_id from test 4"})
except Exception as e:
    pp("5) /inventories/{{id}}/stock/fulfillment — ERROR", {"error": str(e)})
