"""Imprime 2 items de ejemplo de la API de ML."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fulfillment.config import get_ml_manager

ml = get_ml_manager("BEKURA")
user_id = ml.get("/users/me")["id"]
ids = ml.get(f"/users/{user_id}/items/search", params={"offset": 0, "limit": 2})["results"]
data = ml.get("/items", params={"ids": ",".join(ids)})
for entry in data:
    print(json.dumps(entry.get("body"), indent=2, ensure_ascii=False))
    print("\n" + "="*80 + "\n")
