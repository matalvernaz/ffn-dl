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

    if cfg["port"] == 465:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30) as smtp:
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
