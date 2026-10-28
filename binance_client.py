"""Minimal signed client for the Binance Affiliate Performance API."""

import hashlib
import hmac
import time
from typing import Optional
from urllib.parse import urlencode

import requests

BASE_URL = "https://api.binance.com"


class BinanceClient:
    def __init__(self, api_key: str, secret_key: str, base_url: str = BASE_URL):
        if not api_key or not secret_key:
            raise RuntimeError("BK_KEY / BK_SEC not found — check your .env file")
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")

    def _sign(self, params: dict) -> str:
        qs = urlencode({k: v for k, v in params.items() if v is not None})
        return hmac.new(self.secret_key.encode(), qs.encode(), hashlib.sha256).hexdigest()

    def signed_get(self, path: str, params: Optional[dict] = None) -> requests.Response:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        signature = self._sign(params)
        qs = urlencode(params) + "&signature=" + signature
        return requests.get(
            self.base_url + path,
            params=qs,
            headers={"X-MBX-APIKEY": self.api_key},
            timeout=15,
        )

    # ---- Affiliate Performance API ----

    def invitee_performance(self, user_id: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> requests.Response:
        return self.signed_get(
            "/sapi/v1/affiliate/performance/invitee",
            {"userId": user_id, "startDate": start_date, "endDate": end_date},
        )

    def code_performance(self, code: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> requests.Response:
        return self.signed_get(
            "/sapi/v1/affiliate/performance/code",
            {"code": code, "startDate": start_date, "endDate": end_date},
        )
