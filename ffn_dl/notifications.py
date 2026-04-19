"""Notification dispatchers for the watchlist feature.

Three channels are supported:

* **Pushover** — mobile push via https://api.pushover.net/. The user
  configures an application token (per-app secret) and a user/group key
  (per-account id) in the GUI.
* **Discord** — incoming webhook URL pasted from a channel's integration
  settings. No credentials needed beyond the webhook itself.
* **Email** — re-uses the SMTP config from :mod:`ffn_dl.mailer` that
  ``--send-to-kindle`` already relies on, so the user only configures
  SMTP once.

The module is deliberately self-contained: it uses only the standard
library's :mod:`urllib.request` for HTTP, so we don't drag the scraper's
``curl_cffi`` dependency into the notification path. Notification volume
is low (a few requests per poll), so the extra features of curl_cffi
would be wasted here.

The public entry point is :func:`dispatch`, which fans a single
:class:`Notification` out to every requested channel and collects
per-channel failures so the caller — the watchlist poll runner — can
keep going when one channel is misconfigured. :class:`NotificationError`
is raised by the per-channel ``send_*`` helpers on failure; ``dispatch``
catches those and never re-raises.
"""
from __future__ import annotations

import json
import logging
import smtplib
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from . import __version__, mailer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Channel identifiers — used both in CLI args and in stored watch entries.
# ---------------------------------------------------------------------------

CHANNEL_PUSHOVER = "pushover"
CHANNEL_DISCORD = "discord"
CHANNEL_EMAIL = "email"
ALL_CHANNELS: tuple[str, ...] = (CHANNEL_PUSHOVER, CHANNEL_DISCORD, CHANNEL_EMAIL)

# ---------------------------------------------------------------------------
# Timeouts and limits. Hoisted as named constants so the watch runner
# (and tests) never rely on inline magic numbers.
# ---------------------------------------------------------------------------

# Per-request HTTP timeout for remote notification APIs, in seconds.
# Both Pushover and Discord respond well under a second in normal
# operation; ten seconds is a generous ceiling that still lets the poll
# loop make progress if a remote is hung.
HTTP_TIMEOUT_S = 10

# Pushover endpoint (HTTPS, form-encoded POST).
PUSHOVER_ENDPOINT = "https://api.pushover.net/1/messages.json"

# Pushover-enforced payload caps (from Pushover's public API docs).
PUSHOVER_TITLE_LIMIT = 250
PUSHOVER_MESSAGE_LIMIT = 1024

# Discord webhook ``content`` field is hard-capped at 2000 characters;
# we stay under that with a safety margin to leave room for the ``...``
# truncation marker plus any formatting we add.
DISCORD_CONTENT_LIMIT = 1900

# User-Agent identifies ffn-dl to the remote side for diagnostics.
USER_AGENT = f"ffn-dl/{__version__} (+watchlist)"

# HTTP status codes < 400 are considered success; >= 400 is an error.
HTTP_ERROR_THRESHOLD = 400


class NotificationError(RuntimeError):
    """Raised when a single channel fails to deliver a notification.

    The message is user-facing — it gets logged and (for ``--watch-test``)
    printed on the CLI, so it should describe the failure without a
    traceback.
    """


@dataclass
class Notification:
    """A channel-agnostic notification payload.

    Attributes:
        title: Short headline. Used as push title, email subject, or
            bolded line on Discord. Channel-specific length caps apply
            at send time; callers don't need to pre-truncate.
        message: Longer body. Plain text — no Markdown or HTML — so all
            three channels render it consistently.
        url: Optional link to the thing being announced (story page,
            author page, etc.). Rendered as a tappable "Open" link on
            Pushover, appended to the body on Discord/email.
    """

    title: str
    message: str
    url: Optional[str] = None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post(endpoint: str, data: bytes, content_type: str, timeout: float) -> None:
    """POST ``data`` to ``endpoint`` using stdlib urllib.

    Why stdlib instead of curl_cffi / requests: notification POSTs are
    low-volume, non-scraping plain HTTP — stdlib is enough and keeps
    this module importable even on a minimal CLI-only install.

    Raises :class:`NotificationError` on any HTTP or transport failure
    so callers can treat network issues uniformly.
    """
    req = urlrequest.Request(
        endpoint,
        data=data,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": content_type,
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status >= HTTP_ERROR_THRESHOLD:
                raise NotificationError(f"HTTP {status} from {endpoint}")
    except urlerror.HTTPError as exc:
        raise NotificationError(
            f"HTTP {exc.code} from {endpoint}: {exc.reason}"
        ) from exc
    except urlerror.URLError as exc:
        raise NotificationError(
            f"Network error contacting {endpoint}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise NotificationError(
            f"Timed out contacting {endpoint} after {timeout}s"
        ) from exc


def _post_form(endpoint: str, fields: dict, timeout: float = HTTP_TIMEOUT_S) -> None:
    """POST form-encoded fields (application/x-www-form-urlencoded)."""
    _post(
        endpoint,
        data=urlparse.urlencode(fields).encode("utf-8"),
        content_type="application/x-www-form-urlencoded",
        timeout=timeout,
    )


def _post_json(endpoint: str, payload: dict, timeout: float = HTTP_TIMEOUT_S) -> None:
    """POST a JSON body (application/json)."""
    _post(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        content_type="application/json",
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Per-channel senders
# ---------------------------------------------------------------------------


def send_pushover(token: str, user: str, notification: Notification) -> None:
    """Deliver ``notification`` via Pushover.

    ``token`` is the Pushover application token; ``user`` is the user
    (or group) key. Both come from the user's Pushover dashboard and
    are stored in prefs.

    Raises :class:`NotificationError` on missing config or HTTP failure.
    """
    if not token or not user:
        raise NotificationError("Pushover requires both application token and user key")
    fields = {
        "token": token,
        "user": user,
        "title": notification.title[:PUSHOVER_TITLE_LIMIT],
        "message": notification.message[:PUSHOVER_MESSAGE_LIMIT],
    }
    if notification.url:
        fields["url"] = notification.url
        fields["url_title"] = "Open"
    _post_form(PUSHOVER_ENDPOINT, fields)
    logger.info("Pushover notification delivered: %s", notification.title)


def send_discord(webhook_url: str, notification: Notification) -> None:
    """Deliver ``notification`` via a Discord incoming webhook.

    ``webhook_url`` is pasted from a Discord channel's integrations
    settings. Raises :class:`NotificationError` on missing config or
    HTTP failure.
    """
    if not webhook_url:
        raise NotificationError("Discord webhook URL is not configured")
    content = f"**{notification.title}**\n{notification.message}"
    if notification.url:
        content = f"{content}\n{notification.url}"
    if len(content) > DISCORD_CONTENT_LIMIT:
        content = content[: DISCORD_CONTENT_LIMIT - 3] + "..."
    _post_json(webhook_url, {"content": content})
    logger.info("Discord notification delivered: %s", notification.title)


def send_email(to_addr: str, notification: Notification, prefs=None) -> None:
    """Deliver ``notification`` via SMTP using the shared mailer config.

    Translates ``mailer.SMTPConfigError`` and ``smtplib.SMTPException``
    into :class:`NotificationError` so the dispatcher can treat all
    channels uniformly.
    """
    if not to_addr:
        raise NotificationError("Email notification recipient is not configured")
    body = notification.message
    if notification.url:
        body = f"{body}\n\n{notification.url}"
    try:
        mailer.send_text(to_addr, notification.title, body, prefs=prefs)
    except mailer.SMTPConfigError as exc:
        raise NotificationError(str(exc)) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise NotificationError(f"SMTP send failed: {exc}") from exc
    logger.info("Email notification delivered to %s: %s", to_addr, notification.title)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch(
    channels: Iterable[str],
    notification: Notification,
    prefs,
) -> Tuple[list[str], list[Tuple[str, str]]]:
    """Fan ``notification`` out to every channel in ``channels``.

    Returns ``(delivered, failures)`` where ``delivered`` is the list of
    channel identifiers that accepted the notification and ``failures``
    is a list of ``(channel, error_message)`` tuples. Never raises —
    per-channel failures are captured so one broken webhook doesn't
    silence the rest, and so the watch poll loop keeps running even if
    every channel is misconfigured.

    ``prefs`` is the :class:`ffn_dl.prefs.Prefs` instance; credential
    keys are read lazily so the caller never has to assemble a config
    dict.
    """
    # Late import breaks what would otherwise be a circular dependency
    # (prefs.py imports portable.py, portable.py has no dependencies,
    # but importing prefs at module load forces wx which CLI-only
    # installs don't have).
    from .prefs import (
        KEY_DISCORD_WEBHOOK,
        KEY_NOTIFY_EMAIL,
        KEY_PUSHOVER_TOKEN,
        KEY_PUSHOVER_USER,
    )

    delivered: list[str] = []
    failures: list[Tuple[str, str]] = []
    for channel in channels:
        try:
            if channel == CHANNEL_PUSHOVER:
                send_pushover(
                    prefs.get(KEY_PUSHOVER_TOKEN, "") or "",
                    prefs.get(KEY_PUSHOVER_USER, "") or "",
                    notification,
                )
            elif channel == CHANNEL_DISCORD:
                send_discord(prefs.get(KEY_DISCORD_WEBHOOK, "") or "", notification)
            elif channel == CHANNEL_EMAIL:
                send_email(
                    prefs.get(KEY_NOTIFY_EMAIL, "") or "",
                    notification,
                    prefs=prefs,
                )
            else:
                raise NotificationError(f"Unknown notification channel: {channel!r}")
        except NotificationError as exc:
            logger.warning("Notification channel %s failed: %s", channel, exc)
            failures.append((channel, str(exc)))
        else:
            delivered.append(channel)
    return delivered, failures
