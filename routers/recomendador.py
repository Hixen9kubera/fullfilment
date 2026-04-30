"""Recomendador IA – scoring + Claude executive summary.

Input:  current dashboard data + per-account capacity (PM/GXG).
Output: ranked reposition plan per account & bucket + narrative summary.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/recomendador", tags=["Recomendador"])

_BUCKET_SIZES: dict[str, set[str]] = {
    "PM": {"P", "M"},
    "GXG": {"G", "XG"},
}

TARGET_MIN = 0.85   # Mínimo de ocupación que queremos mantener
TARGET_IDEAL = 0.90  # Objetivo de llenado
DAYS_COVER = 60
URGENCY_DAYS = 50
URGENCY_MULT = 1.3
PAUSED_PENALTY = 0.3  # Paused items score lower since we don't know if they'd resell


class CapacityInput(BaseModel):
    cuenta: str
    capacity_pm: int
    capacity_gxg: int


class RecomendadorRequest(BaseModel):
    capacities: list[CapacityInput]
    include_summary: bool = False  # Desactivado — consumía tokens de Claude; usar CSV + chat


def _score_candidate(row: dict) -> tuple[float, int, str]:
    """Return (score, recommended_qty, reason)."""
    monthly_sales = row.get("monthly_sales") or 0
    monthly_revenue = row.get("monthly_revenue") or 0
    stock_full = row.get("stock_full") or 0
    stock_odoo = row.get("stock_odoo") or 0
    odoo_days = row.get("odoo_days")
    status = row.get("status")

    if stock_odoo <= 0 or row.get("size_category") == "unknown":
        return (0.0, 0, "sin candidatura")

    # Target: keep DAYS_COVER/30 months of cover based on velocity
    target_stock = int(monthly_sales * (DAYS_COVER / 30.0))
    deficit = max(0, target_stock - stock_full)
    qty = min(deficit, stock_odoo)
    if qty <= 0 and monthly_sales == 0 and status == "paused":
        # Paused items with stock in Odoo: suggest small test batch if bucket has room
        qty = min(stock_odoo, 5)

    # Base score: expected monthly revenue from this item
    base = monthly_revenue if monthly_revenue > 0 else (monthly_sales * 100)  # rough proxy
    if status == "paused":
        base *= PAUSED_PENALTY

    urgency = URGENCY_MULT if (odoo_days is not None and odoo_days >= URGENCY_DAYS) else 1.0

    reason_parts = []
    if monthly_sales > 0:
        reason_parts.append(f"{monthly_sales} u./mes")
    if odoo_days is not None and odoo_days >= URGENCY_DAYS:
        reason_parts.append(f"{odoo_days}d en Odoo (urgente)")
    if status == "paused":
        reason_parts.append("pausado")
    if stock_full == 0 and monthly_sales > 0:
        reason_parts.append("sin stock Full")
    reason = " · ".join(reason_parts) or "candidato"

    return (base * urgency, qty, reason)


def _expected_monthly_revenue(row: dict, qty_to_send: int) -> float:
    """Revenue that this item is expected to generate per month after reposition."""
    monthly_sales = row.get("monthly_sales") or 0
    monthly_revenue = row.get("monthly_revenue") or 0
    if monthly_sales > 0 and monthly_revenue > 0:
        # Baseline: historical velocity × price = current monthly_revenue
        return float(monthly_revenue)
    # Paused or no sales: conservative expectation = 0 until we reactivate
    return 0.0


def _build_plan_for_bucket(
    rows: list[dict],
    bucket: str,
    capacity: int,
    current_stock: int,
) -> dict:
    """Greedy fill up to TARGET_IDEAL of capacity (with TARGET_MIN as the reference bar)."""
    target_min = int(capacity * TARGET_MIN)
    target_ideal = int(capacity * TARGET_IDEAL)
    available_space = max(0, target_ideal - current_stock)

    bucket_sizes = _BUCKET_SIZES.get(bucket, {bucket})
    candidates = []
    for r in rows:
        if r.get("size_category") not in bucket_sizes:
            continue
        score, qty, reason = _score_candidate(r)
        if score <= 0 or qty <= 0:
            continue
        candidates.append(
            {
                "sku": r.get("sku"),
                "item_id": r.get("item_id"),
                "title": r.get("title"),
                "status": r.get("status"),
                "monthly_sales": r.get("monthly_sales") or 0,
                "monthly_revenue": r.get("monthly_revenue") or 0,
                "stock_full": r.get("stock_full") or 0,
                "stock_odoo": r.get("stock_odoo") or 0,
                "odoo_days": r.get("odoo_days"),
                "price": r.get("price"),
                "score": round(score, 2),
                "recommended_qty": qty,
                "reason": reason,
                "_row": r,  # for revenue calc later
            }
        )

    candidates.sort(key=lambda c: c["score"], reverse=True)

    plan = []
    remaining = available_space
    for c in candidates:
        if remaining <= 0:
            break
        take = min(c["recommended_qty"], remaining)
        if take <= 0:
            continue
        row = c.pop("_row")
        expected_rev = _expected_monthly_revenue(row, take)
        # pct del target mínimo (85%) que este producto ocupa
        pct_of_min = (take / target_min) if target_min > 0 else 0
        c2 = {
            **c,
            "recommended_qty": take,
            "expected_monthly_revenue": round(expected_rev, 2),
            "pct_of_target_min": round(pct_of_min, 4),
        }
        plan.append(c2)
        remaining -= take

    # Strip _row from any unused candidates
    for c in candidates:
        c.pop("_row", None)

    total_planned = sum(p["recommended_qty"] for p in plan)
    total_expected_revenue = sum(p["expected_monthly_revenue"] for p in plan)
    projected_occupancy = current_stock + total_planned
    projected_pct = (projected_occupancy / capacity) if capacity > 0 else 0

    # Porcentaje actual del target mínimo (lo que ya está + lo planeado)
    fill_vs_min = (projected_occupancy / target_min) if target_min > 0 else 0

    # Stock actual por cuántos % del min ocupa
    current_pct_of_min = (current_stock / target_min) if target_min > 0 else 0

    return {
        "bucket": bucket,
        "capacity": capacity,
        "current_stock": current_stock,
        "target_min": target_min,
        "target_ideal": target_ideal,
        "planned_units": total_planned,
        "planned_expected_monthly_revenue": round(total_expected_revenue, 2),
        "projected_occupancy": projected_occupancy,
        "projected_pct": round(projected_pct, 4),
        "fill_vs_min_pct": round(fill_vs_min, 4),  # >=1.0 means we hit the 85% floor
        "current_pct_of_min": round(current_pct_of_min, 4),
        "remaining_space": max(0, available_space - total_planned),
        "plan": plan,
        "candidates_considered": len(candidates),
    }


def _account_occupancy(rows: list[dict]) -> dict[str, int]:
    occ = {"PM": 0, "GXG": 0, "unknown": 0}
    for r in rows:
        if r.get("logistic_type") != "fulfillment":
            continue
        cat = r.get("size_category") or "unknown"
        qty = r.get("stock_full") or 0
        if cat in _BUCKET_SIZES["PM"]:
            occ["PM"] += qty
        elif cat in _BUCKET_SIZES["GXG"]:
            occ["GXG"] += qty
        else:
            occ["unknown"] += qty
    return occ


def build_recommendation(
    accounts: list[dict],
    capacities: list[CapacityInput],
) -> dict:
    cap_map = {c.cuenta: c for c in capacities}
    per_account = {}
    for acct in accounts:
        name = acct["cuenta"]
        if name not in cap_map:
            continue
        cap = cap_map[name]
        occ = _account_occupancy(acct["rows"])
        pm = _build_plan_for_bucket(acct["rows"], "PM", cap.capacity_pm, occ["PM"])
        gxg = _build_plan_for_bucket(acct["rows"], "GXG", cap.capacity_gxg, occ["GXG"])
        total_rev = pm["planned_expected_monthly_revenue"] + gxg["planned_expected_monthly_revenue"]
        total_units = pm["planned_units"] + gxg["planned_units"]
        per_account[name] = {
            "cuenta": name,
            "PM": pm,
            "GXG": gxg,
            "totals": {
                "planned_units": total_units,
                "planned_expected_monthly_revenue": round(total_rev, 2),
                "planned_expected_annual_revenue": round(total_rev * 12, 2),
            },
            "unknown_stock": occ["unknown"],
        }
    return per_account


def _summary_payload(per_account: dict) -> dict:
    """Trim the plan down to what Claude needs for the summary."""

    def _bucket_summary(b: dict) -> dict:
        return {
            "capacity": b["capacity"],
            "target_85_pct": b["target_min"],
            "target_90_pct": b["target_ideal"],
            "current_stock": b["current_stock"],
            "current_pct_of_min_85": round(b["current_pct_of_min"] * 100, 1),
            "planned_units": b["planned_units"],
            "planned_expected_monthly_revenue": b["planned_expected_monthly_revenue"],
            "projected_pct_of_capacity": round(b["projected_pct"] * 100, 1),
            "fill_vs_min_85_pct": round(b["fill_vs_min_pct"] * 100, 1),
            "remaining_space": b["remaining_space"],
            "top_items": [
                {
                    "sku": p["sku"],
                    "title": (p["title"] or "")[:60],
                    "status": p["status"],
                    "qty": p["recommended_qty"],
                    "pct_of_min_85": round(p["pct_of_target_min"] * 100, 1),
                    "monthly_sales": p["monthly_sales"],
                    "expected_monthly_revenue": p["expected_monthly_revenue"],
                    "odoo_days": p["odoo_days"],
                    "reason": p["reason"],
                }
                for p in b["plan"][:10]
            ],
        }

    out = {}
    for name, data in per_account.items():
        out[name] = {
            "totals": data["totals"],
            "PM": _bucket_summary(data["PM"]),
            "GXG": _bucket_summary(data["GXG"]),
            "unknown_stock": data["unknown_stock"],
        }
    return out


SYSTEM_PROMPT = """Eres un asesor ejecutivo de fulfillment para un retailer mexicano que opera en MercadoLibre con dos cuentas (BEKURA y SANCORFASHION). Analizás un plan de reposición algorítmico de stock a bodegas Full y escribís recomendaciones accionables por tienda, con foco en ROI.

REGLAS DEL NEGOCIO
- Productos en Odoo pueden estar hasta 60 días; ≥50 días son urgentes (hay que sacarlos antes de que expire el stock).
- Target de ocupación: 85% mínimo / 90% ideal. Cada unidad vacía arriba del 85% es dinero que no estamos facturando.
- La métrica primaria es **ingreso mensual esperado** (`planned_expected_monthly_revenue`).
- `pct_of_min_85` de cada item dice cuánto del 85% de capacidad llena ese SKU.
- `fill_vs_min_85_pct` ≥ 100 significa que YA llegamos al piso del 85% con el plan.
- `status: paused` → item pausado en ML. Si está pausado pero tiene stock Odoo, vale evaluar reactivarlo, especialmente si es G/XG donde a veces falta catálogo.

FORMATO DE SALIDA (en español, markdown simple, directo)

## Lectura rápida
Dos líneas máximo: "Plan propone enviar X unidades que agregan $Y/mes" + una observación estratégica del conjunto.

## BEKURA
**Diagnóstico P/M**: 1-2 oraciones con situación (actual % vs target, qué tan bien se llena).
**Diagnóstico G/XG**: idem.
**Acciones recomendadas** (bullet list, 3-5 items priorizados):
- SKU → qty a enviar → ingreso mensual esperado → razón concreta (velocidad, días Odoo, si está pausado, si desbloquea algo).
**Lo que queda en la mesa**: si el plan no llena al 85%, por qué (falta catálogo, falta stock Odoo en esa categoría, items sin clasificar). Si llena >100%, menciona que hay margen para incluso más agresividad.

## SANCORFASHION
Mismo formato que BEKURA.

## Riesgos & oportunidades
- Urgencias Odoo (items ≥50d): mencionar SKUs específicos si los hay en el plan.
- Items pausados con potencial: si el score los dejó fuera pero el user podría reactivarlos.
- Desbalances entre cuentas o buckets.

Reglas de estilo:
- Máximo 500 palabras total.
- Cifras siempre en contexto ("agrega $12,500/mes" no "$12500").
- No inventes ingresos si `expected_monthly_revenue` es 0 — decí "velocidad desconocida" o "requiere test".
- No repitas el JSON crudo."""


def generate_summary(plan: dict) -> dict:
    """Call Claude to generate an executive summary. Returns {text, model, usage} or {error}."""
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY no configurada en .env"}

    try:
        import anthropic
    except ImportError:
        return {"error": "paquete 'anthropic' no instalado. Correr: uv pip install anthropic"}

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        payload = _summary_payload(plan)
        user_content = (
            "Aquí está el plan de reposición calculado algorítmicamente. "
            "Escribe el resumen ejecutivo según el formato del system prompt.\n\n"
            f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
        )
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=2000,
            output_config={"effort": "medium"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return {
            "text": text,
            "model": response.model,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }
    except Exception as exc:
        logger.exception("Error generating Claude summary")
        return {"error": f"{type(exc).__name__}: {exc}"}


@router.post("")
async def run_recomendador(req: RecomendadorRequest) -> dict[str, Any]:
    # Import here to avoid circular import at module load
    from .main import _build_combined_rows, _build_dashboard_data
    from .config import CUENTAS
    from .odoo_stock import fetch_odoo_oldest_in_date_by_sku, fetch_odoo_stock_by_sku
    import asyncio
    from datetime import datetime, timezone

    loop = asyncio.get_event_loop()
    odoo_stock_fut = loop.run_in_executor(None, fetch_odoo_stock_by_sku)
    odoo_days_fut = loop.run_in_executor(None, fetch_odoo_oldest_in_date_by_sku)
    try:
        odoo_stock = await odoo_stock_fut
        odoo_days = await odoo_days_fut
    except Exception as exc:
        logger.exception("Error fetching Odoo")
        raise HTTPException(status_code=502, detail=str(exc))

    tasks = [
        loop.run_in_executor(None, _build_dashboard_data, c, odoo_stock, odoo_days)
        for c in CUENTAS
    ]
    try:
        accounts = await asyncio.gather(*tasks)
    except Exception as exc:
        logger.exception("Error building account data")
        raise HTTPException(status_code=502, detail=str(exc))

    plan = build_recommendation(accounts, req.capacities)

    # Resumen Claude desactivado — usar /api/reports/ventas.csv + chat de Claude en su lugar
    # summary = None
    # if req.include_summary:
    #     summary = await loop.run_in_executor(None, generate_summary, plan)
    summary = None

    return {
        "fecha": datetime.now(timezone.utc).isoformat(),
        "plan": plan,
        "summary": summary,
    }
