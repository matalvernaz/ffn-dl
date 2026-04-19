"""Email an exported file to a Kindle (or any) address via SMTP.

Amazon killed MOBI-by-email in 2022, but kindle.com addresses still
accept EPUB attachments. Sender address must be on the user's Amazon
"Approved Personal Document E-mail List" or the message silently
drops.

Config comes from env vars so CLI users can set them up once in their
shell profile — the GUI can override via prefs later without changing
this module's interface.
"""

import logging
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger(__name__)

# SMTP connection timeout, seconds. Long enough to absorb a slow DNS
# lookup on the first send, short enough that a hung relay can't wedge
# the watchlist poll loop for minutes.
SMTP_TIMEOUT_S = 30

# Implicit-TLS port. Everything else uses STARTTLS.
SMTP_SSL_PORT = 465


class SMTPConfigError(RuntimeError):
    """Raised when required SMTP settings aren't available."""


def _config(prefs=None):
    """Read SMTP config. Prefs override env; env is the fallback.

    Returns a dict; raises SMTPConfigError if required keys are missing.
    """
    def _read(pref_key, env_key):
        if prefs is not None:
            value = prefs.get(pref_key)
            if value:
                return value
        return os.environ.get(env_key, "").strip()

    cfg = {
        "host": _read("smtp_host", "SMTP_HOST"),
        "port": _read("smtp_port", "SMTP_PORT") or "587",
        "user": _read("smtp_user", "SMTP_USER"),
        "password": _read("smtp_password", "SMTP_PASSWORD"),
        "from_addr": _read("smtp_from", "SMTP_FROM"),
    }
    missing = [k for k in ("host", "user", "password") if not cfg[k]]
    if missing:
        raise SMTPConfigError(
            "Missing SMTP settings: " + ", ".join(missing) + ". "
            "Set SMTP_HOST / SMTP_USER / SMTP_PASSWORD (and optionally "
            "SMTP_PORT / SMTP_FROM) in your environment, or configure "
            "them in the GUI preferences."
        )
    if not cfg["from_addr"]:
        cfg["from_addr"] = cfg["user"]
    try:
        cfg["port"] = int(cfg["port"])
    except ValueError:
        raise SMTPConfigError(f"SMTP_PORT must be numeric, got {cfg['port']!r}")
    return cfg


def send_text(to_addr: str, subject: str, body: str, prefs=None) -> None:
    """Send a plain-text email using the same SMTP config as ``send_file``.

    Used by the watchlist notification dispatcher. Kept here rather than
    in a new module so all SMTP plumbing — STARTTLS vs SSL port handling,
    credential resolution, From-address defaulting — lives in one place.
    """
    cfg = _config(prefs=prefs)

    msg = EmailMessage()
    msg["From"] = cfg["from_addr"]
    msg["To"] = to_addr
    msg["Subject"] = subject or "(no subject)"
    msg.set_content(body or "")

    logger.info(
        "Sending notification email to %s via %s:%d",
        to_addr, cfg["host"], cfg["port"],
    )

    # Port 465 is the implicit-TLS ("SSL") port; everything else is
    # STARTTLS (explicit TLS upgrade). Matches send_file's logic.
    if cfg["port"] == SMTP_SSL_PORT:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT_S) as smtp:
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT_S) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)


def send_file(to_addr: str, attachment_path, subject=None, body="", prefs=None):
    """Email `attachment_path` to `to_addr` using configured SMTP.

    Standard Amazon deliver-to-Kindle flow: any plain-text subject works
    ("convert" in the subject forces format conversion, which you don't
    want for EPUB). Body is optional — Amazon ignores it.
    """
    cfg = _config(prefs=prefs)
    path = Path(attachment_path)
    if not path.is_file():
        raise FileNotFoundError(f"Attachment not found: {path}")

    msg = EmailMessage()
    msg["From"] = cfg["from_addr"]
    msg["To"] = to_addr
    msg["Subject"] = subject or path.stem
    msg.set_content(body or f"{path.name}")

    ctype, _ = mimetypes.guess_type(str(path))
    if not ctype:
        ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)
    msg.add_attachment(
        path.read_bytes(),
        maintype=maintype,
        subtype=subtype,
        filename=path.name,
    )

    logger.info(
        "Sending %s (%d bytes) to %s via %s:%d",
        path.name, path.stat().st_size, to_addr, cfg["host"], cfg["port"],
    )

    if cfg["port"] == SMTP_SSL_PORT:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT_S) as smtp:
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT_S) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
