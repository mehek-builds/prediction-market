import asyncio

import httpx

from fastlane.jev_client import JevClient


def test_non_json_200_returns_error_dict():
    async def go():
        c = JevClient(api_key="test-key")
        await c._http.aclose()
        c._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="<html>oops</html>")))
        try:
            return await c.decide({}, {})
        finally:
            await c.aclose()

    res = asyncio.run(go())
    assert res["error"] == "bad_json"
