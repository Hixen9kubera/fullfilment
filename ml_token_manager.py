"""
ml_token_manager.py

Manages MercadoLibre OAuth tokens stored (Fernet-encrypted) in MySQL.
Handles automatic token refresh and persistence back to the DB so tokens
survive process restarts.

Required environment variables (load via python-dotenv in your app):
    DB_ENCRYPTION_KEY  - Fernet key (base64url, 44 chars)
    DB_USER            - MySQL username
    DB_PASSWORD        - MySQL password
    DB_HOST            - MySQL host   (default: srv1249.hstgr.io)
    DB_PORT            - MySQL port   (default: 3306)
    DB_NAME            - MySQL database (default: u531713409_kubera_ml)

Usage:
    from shared.ml_token_manager import MLTokenManager

    ml = MLTokenManager(table='ml_tokens_dashboard')

    # Direct token access
    token = ml.access_token

    # Authenticated ML API calls (auto-refresh on 401)
    data = ml.get('/users/me')
    result = ml.post('/items', json={...})
"""

import logging
import os

import mysql.connector
import requests
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

ML_API_BASE = "https://api.mercadolibre.com"
ML_TOKEN_URL = f"{ML_API_BASE}/oauth/token"

ENCRYPTED_FIELDS = ("access_token", "refresh_token", "client_secret")


class MLTokenManager:
    """
    MercadoLibre token manager backed by an encrypted MySQL table.

    Args:
        table: DB table name.
               - 'ml_tokens_dashboard' for the sales dashboard
               - 'ml_tokens'           for kubera-mcp and other services
    """

    def __init__(self, table: str = "ml_tokens_dashboard", cuenta: str | None = None):
        self.table = table
        self.cuenta = cuenta

        key = os.environ.get("DB_ENCRYPTION_KEY", "")
        if not key:
            raise ValueError("DB_ENCRYPTION_KEY is not set in the environment")
        self._fernet = Fernet(key.encode())

        self._db_config = {
            "host": os.environ.get("DB_HOST", "srv1249.hstgr.io"),
            "port": int(os.environ.get("DB_PORT", 3306)),
            "user": os.environ["DB_USER"],
            "password": os.environ["DB_PASSWORD"],
            "database": os.environ.get("DB_NAME", "u531713409_kubera_ml"),
        }

        self._tokens: dict | None = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _decrypt(self, value: str) -> str:
        if not value:
            return value
        return self._fernet.decrypt(value.encode()).decode()

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def _connect(self):
        return mysql.connector.connect(**self._db_config)

    def _load_from_db(self) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor(dictionary=True)
            if self.cuenta:
                cursor.execute(
                    f"SELECT * FROM `{self.table}` WHERE cuenta = %s LIMIT 1",
                    (self.cuenta,),
                )
            else:
                cursor.execute(f"SELECT * FROM `{self.table}` LIMIT 1")
            row = cursor.fetchone()
            cursor.close()
        finally:
            conn.close()

        if not row:
            label = f"cuenta='{self.cuenta}'" if self.cuenta else ""
            raise ValueError(
                f"No token record found in table `{self.table}` {label}"
            )

        self._tokens = dict(row)
        for field in ENCRYPTED_FIELDS:
            if field in self._tokens and self._tokens[field]:
                self._tokens[field] = self._decrypt(str(self._tokens[field]))

    def _persist_tokens(self, access_token: str, refresh_token: str) -> None:
        """Write updated tokens (Fernet-encrypted) back to the database."""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            if self.cuenta:
                cursor.execute(
                    f"UPDATE `{self.table}` SET access_token = %s, refresh_token = %s "
                    f"WHERE cuenta = %s LIMIT 1",
                    (self._encrypt(access_token), self._encrypt(refresh_token), self.cuenta),
                )
            else:
                cursor.execute(
                    f"UPDATE `{self.table}` SET access_token = %s, refresh_token = %s LIMIT 1",
                    (self._encrypt(access_token), self._encrypt(refresh_token)),
                )
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_tokens(self, force_reload: bool = False) -> dict:
        """
        Return a dict with all token fields (sensitive fields decrypted).
        Loads from DB on first call; use force_reload=True to re-read.
        """
        if self._tokens is None or force_reload:
            self._load_from_db()
        return self._tokens  # type: ignore[return-value]

    @property
    def access_token(self) -> str:
        return self.get_tokens()["access_token"]

    def refresh(self) -> dict:
        """
        Call the ML OAuth refresh endpoint, update in-memory state,
        and persist the new tokens (encrypted) to the DB.
        Returns the updated token dict.
        """
        tokens = self.get_tokens()
        logger.info("Refreshing ML tokens (table=%s, cuenta=%s)", self.table, self.cuenta)

        response = requests.post(
            ML_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": tokens["app_id"],
                "client_secret": tokens["client_secret"],
                "refresh_token": tokens["refresh_token"],
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        new_access = data["access_token"]
        new_refresh = data.get("refresh_token", tokens["refresh_token"])

        self._tokens["access_token"] = new_access
        self._tokens["refresh_token"] = new_refresh

        self._persist_tokens(new_access, new_refresh)
        logger.info("Tokens refreshed and persisted to DB")
        return self._tokens

    # ------------------------------------------------------------------
    # HTTP helpers with automatic refresh on 401
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """
        Authenticated request to the ML API.
        On 401 the token is refreshed and the request is retried once.
        """
        url = path if path.startswith("http") else f"{ML_API_BASE}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.access_token}"

        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

        if resp.status_code == 401:
            logger.warning("401 received – refreshing tokens and retrying")
            self.refresh()
            headers["Authorization"] = f"Bearer {self.access_token}"
            resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

        resp.raise_for_status()
        return resp

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET {ML_API_BASE}{path} and return the JSON body."""
        return self._request("GET", path, params=params).json()

    def post(self, path: str, json: dict | None = None, data: dict | None = None) -> dict:
        """POST {ML_API_BASE}{path} and return the JSON body."""
        return self._request("POST", path, json=json, data=data).json()

    def put(self, path: str, json: dict | None = None) -> dict:
        """PUT {ML_API_BASE}{path} and return the JSON body."""
        return self._request("PUT", path, json=json).json()
