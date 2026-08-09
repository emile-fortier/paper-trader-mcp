from __future__ import annotations

import json
import time
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ALLOWED = {
    ("data.alpaca.markets", "/v2/stocks/quotes"),
    ("paper-api.alpaca.markets", "/v2/calendar"),
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AlpacaError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        self.status = status
        super().__init__(message)


class AlpacaHTTP:
    def __init__(self, key: str, secret: str, timeout: float = 10, max_bytes: int = 8_000_000):
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
        self.timeout, self.max_bytes = timeout, max_bytes
        self.opener = build_opener(NoRedirect)

    def get(self, host: str, path: str, params: dict[str, str]) -> object:
        if (host, path) not in ALLOWED:
            raise ValueError("HTTP destination is not allowlisted")
        url = f"https://{host}{path}?{urlencode(params)}"
        req = Request(url, headers=self.headers, method="GET")
        for attempt in range(3):
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    if response.status != 200 or response.geturl() != url:
                        raise AlpacaError(response.status, "unexpected provider status or redirect")
                    raw = response.read(self.max_bytes + 1)
                    if len(raw) > self.max_bytes:
                        raise AlpacaError(response.status, "provider response too large")
                try:
                    return json.loads(raw, parse_float=Decimal)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise AlpacaError(200, "provider returned invalid JSON") from exc
            except HTTPError as exc:
                body = exc.read(16_384)
                try:
                    detail = json.loads(body).get("message", body.decode("utf-8", "replace"))
                except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                    detail = body.decode("utf-8", "replace")
                if exc.code in {429, 502, 503, 504} and attempt < 2:
                    retry_after = exc.headers.get("Retry-After")
                    delay = min(float(retry_after), 5.0) if retry_after and retry_after.isdigit() else 0.25 * (2**attempt)
                    time.sleep(delay)
                    continue
                raise AlpacaError(exc.code, f"Alpaca HTTP {exc.code}: {detail[:500]}") from exc
            except (URLError, TimeoutError) as exc:
                if attempt < 2:
                    time.sleep(0.25 * (2**attempt))
                    continue
                raise AlpacaError(None, "Alpaca request failed after retries") from exc
        raise AlpacaError(None, "Alpaca request failed")

    def quotes(self, symbol: str, start: str, end: str) -> list[dict]:
        result, token, pages, items, seen = [], None, 0, 0, set()
        while True:
            params = {"symbols": symbol, "start": start, "end": end, "feed": "sip", "limit": "10000", "sort": "asc"}
            if token: params["page_token"] = token
            body = self.get("data.alpaca.markets", "/v2/stocks/quotes", params)
            if not isinstance(body, dict) or not isinstance(body.get("quotes"), dict):
                raise RuntimeError("invalid quotes response")
            rows = body["quotes"].get(symbol, [])
            if not isinstance(rows, list) or len(rows) > 10000:
                raise RuntimeError("invalid quotes page")
            result.extend(rows); items += len(rows); pages += 1
            if pages > 100 or items > 1_000_000: raise RuntimeError("pagination bound exceeded")
            token = body.get("next_page_token")
            if token is None: break
            if not isinstance(token, str) or not token: raise RuntimeError("invalid page token")
            if token in seen: raise RuntimeError("repeated page token")
            seen.add(token)
        return result

    def calendar(self, start: str, end: str) -> list[dict]:
        body = self.get("paper-api.alpaca.markets", "/v2/calendar", {"start": start, "end": end})
        if not isinstance(body, list) or len(body) > 400:
            raise RuntimeError("invalid calendar response")
        return body
