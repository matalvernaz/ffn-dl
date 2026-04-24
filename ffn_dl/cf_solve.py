"""Playwright-backed Cloudflare challenge solver for stubborn 403s.

FFN's Cloudflare deployment occasionally refuses every impersonated
``curl_cffi`` request with a first-contact 403 — browser rotation
and client-hint injection mostly handle it, but a bad-fingerprint day
makes every retry fail and the retry loop just burns time. This
module provides a real-browser fallback: open the URL in Playwright,
let Cloudflare's challenge JavaScript run to completion, and hand the
solved ``cf_clearance`` cookie back to the caller. The curl session
then injects the cookie and retries; subsequent chapters reuse it
from the persisted cookie jar without another Playwright run.

The fallback is strictly opt-in — Playwright ships a ~300MB browser
binary and requires ``playwright install chromium`` before first use.
Most users never need it; the feature exists for the "bad day" case
where the built-in mitigations aren't enough.

Cookies are persisted per-host under the scraper cache dir so a
second invocation of ffn-dl doesn't re-run Playwright for the same
host. The TTL is short (24h, since Cloudflare's cf_clearance rotates
that frequently) to prevent stale tokens from silently causing every
request to 403 after the challenge would have expired anyway.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CF_CHALLENGE_TIMEOUT_S = 30
"""How long to wait for the Cloudflare challenge to resolve. Real
challenges take 3-8s; the rest of the window absorbs Turnstile widget
renders and occasional slow network. Anything beyond this and we're
probably being served a solvable-only-by-human captcha — bail rather
than hang the download."""

COOKIE_CACHE_TTL_S = 24 * 60 * 60
"""How long a persisted cookie set stays authoritative before we
force a fresh Playwright run. Cloudflare cycles cf_clearance roughly
daily; hanging onto a stale cookie past that window just produces a
new round of 403s with no useful output."""


@dataclass
class SolveResult:
    """What the solver hands back after a successful challenge pass.

    The user agent is captured because Cloudflare fingerprints the
    UA/cookie pair — replaying the cookie from curl_cffi with a
    different UA sometimes re-triggers the challenge. The scraper
    uses it to override its impersonation profile's UA for the
    remainder of the session.
    """

    cookies: list[dict]
    user_agent: str
    fetched_at: float


class SolverUnavailable(Exception):
    """Playwright isn't installed or didn't launch. Callers fall back
    to the normal retry path when this fires — solving is opt-in, so
    "unavailable" is an expected outcome, not an error."""


def is_available() -> bool:
    """True when Playwright imports cleanly. Doesn't verify the
    browser binary is installed — that check happens lazily on first
    :func:`solve` call so an ``is_available()`` probe stays cheap."""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def _cookie_cache_dir() -> Path:
    """Resolve the folder where solved cookies land. Piggybacks on the
    scraper cache dir so uninstall is still "delete one folder"."""
    from .scraper import _default_cache_dir
    path = _default_cache_dir() / "cf-cookies"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _host_cache_path(host: str) -> Path:
    # Sanitise the host so we can't accidentally write outside the
    # cache dir if a caller feeds us a malformed value. The regex
    # keeps ASCII letters, digits, dots, and dashes; everything else
    # becomes underscore.
    safe = re.sub(r"[^a-z0-9.\-]", "_", host.lower())
    return _cookie_cache_dir() / f"{safe}.json"


def load_cached(host: str, *, now: Optional[float] = None) -> Optional[SolveResult]:
    """Return the cached solve for ``host`` if it's still within the
    TTL window. Caller is expected to try the cached cookies first
    before invoking :func:`solve` — the TTL keeps a stale cf_clearance
    from hanging the retry loop forever.
    """
    path = _host_cache_path(host)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fetched_at = float(data.get("fetched_at") or 0.0)
    current = time.time() if now is None else now
    if current - fetched_at > COOKIE_CACHE_TTL_S:
        return None
    cookies = data.get("cookies") or []
    ua = str(data.get("user_agent") or "")
    if not cookies or not ua:
        return None
    return SolveResult(
        cookies=[dict(c) for c in cookies],
        user_agent=ua,
        fetched_at=fetched_at,
    )


def persist(host: str, result: SolveResult) -> None:
    """Write the solved cookies for ``host`` to the cache dir. The
    filename is hostname-sanitised (see :func:`_host_cache_path`)
    so an attacker-controlled URL can't traverse out of the cache.
    """
    path = _host_cache_path(host)
    payload = {
        "cookies": result.cookies,
        "user_agent": result.user_agent,
        "fetched_at": result.fetched_at,
    }
    try:
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("cf-cookie persist failed for %s: %s", host, exc)


def solve(
    url: str,
    *,
    timeout_s: float = CF_CHALLENGE_TIMEOUT_S,
    launcher=None,
) -> SolveResult:
    """Run Playwright against ``url`` and return the challenge cookies.

    Raises :class:`SolverUnavailable` when Playwright isn't installed
    or the browser binary failed to launch; callers treat that as
    "fall back to the normal retry path" rather than a hard error.
    All other failures (timeout, network, Cloudflare serving a
    human-only captcha) bubble up as generic exceptions — the caller
    logs and continues with the built-in 403 retries.

    ``launcher`` is a hook that tests inject to avoid launching a
    real browser in unit tests — see :func:`_default_launcher` for
    the production path.
    """
    launch = launcher if launcher is not None else _default_launcher
    try:
        cookies, user_agent = launch(url, timeout_s)
    except ImportError as exc:
        raise SolverUnavailable(str(exc)) from exc
    if not cookies:
        raise RuntimeError(
            "cf-solve: Playwright returned no cookies for {url}; "
            "likely a human-only captcha or a network failure".format(url=url)
        )
    return SolveResult(
        cookies=cookies,
        user_agent=user_agent,
        fetched_at=time.time(),
    )


def _default_launcher(url: str, timeout_s: float) -> tuple[list[dict], str]:
    """Production launcher: opens a real Chromium, waits for the
    challenge to clear, returns (cookies, user_agent).

    Kept at module scope so :func:`solve` can accept a test double
    for unit tests — we don't want CI running a 300MB browser.
    Playwright's ``sync_playwright()`` context manager cleans up the
    browser even if the challenge throws, so there's no orphaned
    process on timeout.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ImportError(
            "cf-solve requires the 'cf-solve' extra: "
            "pip install 'ffn-dl[cf-solve]' then 'playwright install chromium'"
        ) from exc

    timeout_ms = int(timeout_s * 1000)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(url, timeout=timeout_ms)
            # Wait until the DOM is stable — Cloudflare's challenge
            # script replaces the body with the real page content
            # once solved, so ``networkidle`` is a reliable "challenge
            # cleared" signal without us having to detect specific
            # DOM markers.
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            raw_cookies = context.cookies()
            ua = page.evaluate("() => navigator.userAgent")
            cookies: list[dict] = []
            for c in raw_cookies:
                # Keep only the fields curl_cffi cares about — the
                # rest (sameSite, priority) would just be dropped and
                # add noise to the on-disk cookie cache.
                cookies.append({
                    "name": c.get("name"),
                    "value": c.get("value"),
                    "domain": c.get("domain"),
                    "path": c.get("path") or "/",
                    "secure": bool(c.get("secure")),
                })
            return cookies, str(ua)
        finally:
            browser.close()


def inject_into_session(session, result: SolveResult) -> None:
    """Apply ``result`` to a curl_cffi session in-place.

    The cookie jar acquires every cookie returned by Playwright (not
    just cf_clearance — some Cloudflare deployments also set __cf_bm
    and cf_chl_rc_i that need to travel together for the clearance to
    validate). The User-Agent header is also pinned, because
    Cloudflare fingerprints the UA + cookie pair together.
    """
    for c in result.cookies:
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain") or ""
        if not name or value is None:
            continue
        try:
            session.cookies.set(
                name=name,
                value=value,
                domain=domain,
                path=c.get("path") or "/",
            )
        except Exception:  # pragma: no cover — curl_cffi internal
            logger.debug(
                "cf-solve: skipped cookie %s@%s (rejected by jar)",
                name, domain,
            )
    if result.user_agent:
        session.headers.update({"User-Agent": result.user_agent})
