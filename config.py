"""Shared configuration – env vars and ML account setup."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Local .env (also accepts vars set directly in the environment, e.g. DigitalOcean App Platform)
load_dotenv(Path(__file__).resolve().parent / ".env")

from .ml_token_manager import MLTokenManager  # noqa: E402

# -- MercadoLibre --
CUENTAS = ["BEKURA", "SANCORFASHION"]


def get_ml_manager(cuenta: str) -> MLTokenManager:
    return MLTokenManager(table="ml_tokens_dashboard", cuenta=cuenta)


# -- Odoo --
ODOO_URL = os.environ.get("ODOO_URL", "")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_USER = os.environ.get("ODOO_USER", "")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")

# -- Anthropic (recomendador) --
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
