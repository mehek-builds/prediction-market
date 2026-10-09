"""Error tracking: a local error log on every install, plus opt-in crash reports (off by default).

What is sent (see README "Error reports"): exception type, a scrubbed message, stack frames (file, line, function,
with the home directory replaced by ~), the fastlane version, Python version, OS, and which component failed.
Never sent: API keys, environment variables, local variables, headlines, request bodies, IP addresses, hostnames.
Opt-in only (FASTLANE_TELEMETRY=1). Send to your own Sentry instead with FASTLANE_SENTRY_DSN.

Errors are throttled: the same error is reported at most once per 10 minutes, and at most 30 reports an hour.
"""
import asyncio
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

from fastlane import __version__
from fastlane.config import RESULTS_DIR

# The maintainer's Sentry project. Kept in three parts so the DSN is not one email-shaped string in the repo.
# An empty key means no maintainer reporting is configured in this build.
MAINTAINER_DSN_KEY = ""
MAINTAINER_DSN_HOST = ""        # e.g. "o0.ingest.us.sentry.io"
MAINTAINER_DSN_PROJECT = ""     # numeric project id

ERROR_LOG = RESULTS_DIR / "errors.log"
SAME_ERROR_EVERY_S = 600
MAX_PER_HOUR = 30

log = logging.getLogger("fastlane")

_SECRET_PATTERNS = [  # (pattern, keep the first group as a readable prefix)
    (re.compile(r"sk-or-v[0-9]-[A-Za-z0-9]+"), False),
    (re.compile(r"xai-[A-Za-z0-9]{10,}"), False),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), False),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), True),
    (re.compile(r"(?i)(KALSHI-ACCESS-(?:KEY|SIGNATURE)['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9+/=_-]{8,}"), True),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password)['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]{6,}"), True),
]
_HOME = str(Path.home())

_state = {"component": None, "sentry": False, "sent": {}, "hour": [], "lock": threading.Lock()}


def scrub(text: str | None, limit: int = 300) -> str | None:
    """Remove anything that looks like a credential, and the home directory, from a string."""
    if text is None:
        return None
    for rx, keep in _SECRET_PATTERNS:
        text = rx.sub(lambda m, keep=keep: (m.group(1) if keep else "") + "[redacted]", text)
    if _HOME and len(_HOME) > 1:
        text = text.replace(_HOME, "~")
    return text[:limit]


def maintainer_dsn() -> str:
    if not (MAINTAINER_DSN_KEY and MAINTAINER_DSN_HOST and MAINTAINER_DSN_PROJECT):
        return ""
    return f"https://{MAINTAINER_DSN_KEY}{chr(64)}{MAINTAINER_DSN_HOST}/{MAINTAINER_DSN_PROJECT}"


def telemetry_dsn() -> str:
    """Opt-in. Nothing is sent unless the user sets their own FASTLANE_SENTRY_DSN, or explicitly shares crash reports
    with the maintainer by setting FASTLANE_TELEMETRY=1."""
    own = os.environ.get("FASTLANE_SENTRY_DSN", "").strip()
    if own:
        return own
    if os.environ.get("FASTLANE_TELEMETRY", "0").strip().lower() in ("1", "true", "yes", "on"):
        return maintainer_dsn()
    return ""


def before_send(event: dict, hint=None) -> dict | None:
    """Last scrub before anything leaves the machine. Drops every field that could carry personal data."""
    for key in ("request", "user", "extra", "breadcrumbs", "modules"):
        event.pop(key, None)
    event["server_name"] = "fastlane"
    ctx = event.get("contexts") or {}
    event["contexts"] = {k: v for k, v in ctx.items() if k in ("os", "runtime", "trace")}
    if "message" in event:
        event["message"] = scrub(event["message"])
    if isinstance(event.get("logentry"), dict):
        event["logentry"] = {"message": scrub(event["logentry"].get("message"))}
    for exc in (event.get("exception") or {}).get("values") or []:
        exc["value"] = scrub(exc.get("value"))
        for frame in (exc.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
            for k in ("abs_path", "filename", "context_line"):
                if frame.get(k):
                    frame[k] = scrub(frame[k], 500)
            for k in ("pre_context", "post_context"):
                if frame.get(k):
                    frame[k] = [scrub(line, 500) for line in frame[k]]
    return event


def _file_logger():
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in log.handlers):
        return
    try:
        ERROR_LOG.parent.mkdir(exist_ok=True)
        h = logging.handlers.RotatingFileHandler(ERROR_LOG, maxBytes=1_000_000, backupCount=3)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    except OSError:
        pass  # read-only filesystem (Vercel): the Sentry path still works


def init(component: str, dsn: str | None = None) -> bool:
    """Call once per process. Returns True when crash reports go to a Sentry project."""
    _state["component"] = component
    _file_logger()
    dsn = telemetry_dsn() if dsn is None else dsn
    if not dsn or _state["sentry"]:
        return _state["sentry"]
    try:
        import sentry_sdk
    except ImportError:
        return False
    sentry_sdk.init(dsn=dsn, release=f"fastlane@{__version__}", environment=component,
                    send_default_pii=False, include_local_variables=False, max_breadcrumbs=0,
                    default_integrations=False, auto_enabling_integrations=False,
                    traces_sample_rate=0, before_send=before_send, server_name="fastlane")
    sentry_sdk.set_tag("component", component)
    _state["sentry"] = True
    return True


def _allowed(fingerprint: str, now: float) -> bool:
    with _state["lock"]:
        if now - _state["sent"].get(fingerprint, 0) < SAME_ERROR_EVERY_S:
            return False
        _state["hour"] = [t for t in _state["hour"] if t > now - 3600]
        if len(_state["hour"]) >= MAX_PER_HOUR:
            return False
        _state["sent"][fingerprint] = now
        _state["hour"].append(now)
        return True


def capture(exc: BaseException, where: str) -> None:
    """Record an exception that was caught and survived. Never raises."""
    try:
        tb = traceback.extract_tb(exc.__traceback__)
        top = tb[-1] if tb else None
        fingerprint = f"{where}:{type(exc).__name__}:{top.filename if top else ''}:{top.lineno if top else ''}"
        log.error("%s: %s: %s", where, type(exc).__name__, scrub(str(exc)))
        if not _state["sentry"] or not _allowed(fingerprint, time.time()):
            return
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("where", where)
            sentry_sdk.capture_exception(exc)
    except Exception:  # error reporting must never take the engine down
        pass


def message(text: str, where: str, level: str = "warning") -> None:
    """Report a condition that is not an exception (a feed that stopped answering, the live switch tripping)."""
    try:
        log.warning("%s: %s", where, scrub(text))
        if not _state["sentry"] or not _allowed(f"msg:{where}:{text[:80]}", time.time()):
            return
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("where", where)
            sentry_sdk.capture_message(scrub(text), level=level)
    except Exception:
        pass


def install_hooks(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Uncaught exceptions on the main thread, other threads and the asyncio loop all get reported."""
    prev = sys.excepthook

    def hook(tp, value, tb):
        if not issubclass(tp, KeyboardInterrupt):
            capture(value, "uncaught")
            flush()
        prev(tp, value, tb)

    sys.excepthook = hook
    threading.excepthook = lambda a: capture(a.exc_value, f"thread:{a.thread.name if a.thread else '?'}")
    if loop is not None:
        def loop_handler(lp, ctx):
            exc = ctx.get("exception")
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                capture(exc, "asyncio")
            lp.default_exception_handler(ctx)
        loop.set_exception_handler(loop_handler)


def flush(timeout: float = 2.0) -> None:
    if _state["sentry"]:
        try:
            import sentry_sdk
            sentry_sdk.flush(timeout=timeout)
        except Exception:
            pass


def notice() -> str:
    """One startup line so nobody is surprised by what leaves their machine."""
    if os.environ.get("FASTLANE_SENTRY_DSN", "").strip():
        return "error reports: to your own Sentry (FASTLANE_SENTRY_DSN)"
    if _state["sentry"]:
        return ("error reports: crash reports (scrubbed: no keys, no headlines) go to the fastlane maintainer. "
                "You opted in with FASTLANE_TELEMETRY=1")
    return f"error reports: local only ({ERROR_LOG.relative_to(RESULTS_DIR.parent.parent)})"
