"""The dashboard as deployed to Vercel by `python3 -m fastlane.deploy`: read-only, password protected.

It serves the same routes as fastlane.api over a snapshot of the user's ledger bundled into the deployment. There
is no engine on Vercel, and the paper / real switch is disabled here (POST /control/mode returns 404): real trading
can only be switched on from the dashboard on the user's own machine.

The password is checked with HTTP Basic auth (any username). Only a salted PBKDF2 hash ships in the bundle
(fastlane/hosted_auth.json); a missing or unreadable hash file fails closed with HTTP 503.
"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

os.environ["FASTLANE_HOSTED"] = "1"
os.environ["FASTLANE_ALLOWED_HOSTS"] = ",".join(filter(None, [
    "*.vercel.app", os.environ.get("FASTLANE_ALLOWED_HOSTS", "")]))

from starlette.responses import PlainTextResponse  # noqa: E402

from fastlane import errors  # noqa: E402
from fastlane.passwords import check_password  # noqa: E402
from fastlane.api import app as inner  # noqa: E402
from fastlane.ratelimit import RateLimitMiddleware  # noqa: E402

AUTH_FILE = Path(__file__).resolve().parent / "hosted_auth.json"


class BasicAuth:
    """Pure ASGI. Successful headers are remembered (as a keyed hash) so PBKDF2 runs once per instance, not per poll."""

    def __init__(self, app, auth_file: Path = AUTH_FILE):
        self.app = app
        try:
            self.rec = json.loads(auth_file.read_text())
        except (OSError, ValueError):
            self.rec = None
        self._ok: set[bytes] = set()
        self._key = os.urandom(16)

    def _authorized(self, header: str) -> bool:
        if not header.lower().startswith("basic "):
            return False
        tag = hmac.new(self._key, header.encode(), hashlib.sha256).digest()
        if tag in self._ok:
            return True
        try:
            _, _, password = base64.b64decode(header[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        if check_password(password, self.rec):
            if len(self._ok) > 100:
                self._ok.clear()
            self._ok.add(tag)
            return True
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if not self.rec:
            return await PlainTextResponse("dashboard password not configured; re-run python3 -m fastlane.deploy",
                                           status_code=503)(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        if not self._authorized(headers.get(b"authorization", b"").decode("latin-1")):
            return await PlainTextResponse("password required", status_code=401, headers={
                "WWW-Authenticate": 'Basic realm="fastlane", charset="UTF-8"'})(scope, receive, send)
        return await self.app(scope, receive, send)


if os.environ.get("VERCEL"):  # serverless runtimes may skip ASGI lifespan, so start error reporting here
    # .env is not deployed, so the user's choice (opt out, own DSN, default) was recorded at deploy time.
    try:
        _dsn = json.loads((Path(__file__).resolve().parent / "hosted_meta.json").read_text()).get("telemetry_dsn", "")
    except (OSError, ValueError):
        _dsn = ""
    errors.init("hosted", dsn=_dsn)

# Rate limit outside the password check too, so the password cannot be brute forced at full speed.
app = RateLimitMiddleware(BasicAuth(inner))
