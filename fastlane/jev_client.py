"""Thin async client for the OpenRouter Decisions API (Jev)."""
import os
import time

import httpx

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"


class JevClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.model = model or os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is empty. Paste it into .env first.")
        # One pooled HTTP/2 connection: avoids a TLS handshake on every decision.
        self._http = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    async def decide(self, state, questions: dict) -> dict:
        """Returns the API response plus `latency_ms` measured around the HTTP call."""
        body = {
            "model": self.model,
            "state": state,
            "questions": questions,
            "provider": {"allow_fallbacks": False},
        }
        t0 = time.perf_counter()
        resp = await self._http.post(DECISIONS_URL, json=body)
        latency_ms = (time.perf_counter() - t0) * 1000
        if resp.status_code != 200:
            return {"error": resp.status_code, "detail": resp.text[:500], "latency_ms": latency_ms}
        try:
            out = resp.json()
        except ValueError:
            return {"error": "bad_json", "detail": resp.text[:200], "latency_ms": latency_ms}
        if not isinstance(out, dict):
            return {"error": "bad_json", "detail": "not an object", "latency_ms": latency_ms}
        out["latency_ms"] = latency_ms
        return out

    async def keepalive(self):
        """Free request that keeps the pooled connection open. OpenRouter drops idle connections after ~5 s,
        and reconnecting adds ~400 ms (TCP + TLS) to the next decision."""
        await self._http.get("https://openrouter.ai/api/v1/key")

    async def aclose(self):
        await self._http.aclose()
