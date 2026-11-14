"""Email skill: check, read, search (read-only, never marks mail as read) and send/reply (always asks first).

Uses plain IMAP/SMTP from the Python standard library - no extra packages.
Settings (.env / dashboard > Email): EMAIL_PROVIDER, EMAIL_ADDRESS, EMAIL_APP_PASSWORD,
EMAIL_IMAP_SERVER / EMAIL_SMTP_SERVER (only for provider=custom).
Gmail / Google Workspace needs an *app password* (Google Account > Security > 2-Step Verification > App passwords).
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import os
import re
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage
from html import unescape

from .tools import tool

PROVIDERS = {
    # provider: (imap host, smtp host, smtp port)
    "gmail": ("imap.gmail.com", "smtp.gmail.com", 465),      # also Google Workspace (custom domains on Google)
    "outlook": ("outlook.office365.com", "smtp.office365.com", 587),
    "yahoo": ("imap.mail.yahoo.com", "smtp.mail.yahoo.com", 465),
    "icloud": ("imap.mail.me.com", "smtp.mail.me.com", 587),
}
MAX_BODY_CHARS = 1500


class EmailNotConfigured(Exception):
    pass


def email_config() -> dict:
    """Read email settings from the environment (the .env file is loaded into it by config.load_settings)."""
    g = lambda k, d="": os.environ.get(k, d).strip()
    provider = g("EMAIL_PROVIDER", "gmail").lower()
    address, password = g("EMAIL_ADDRESS"), g("EMAIL_APP_PASSWORD").replace(" ", "")
    imap_host, smtp_host, smtp_port = PROVIDERS.get(provider, ("", "", 465))
    if provider == "custom" or g("EMAIL_IMAP_SERVER"):
        imap_host = g("EMAIL_IMAP_SERVER") or imap_host
        smtp_host = g("EMAIL_SMTP_SERVER") or smtp_host
        try:
            smtp_port = int(g("EMAIL_SMTP_PORT", str(smtp_port)))
        except ValueError:
            pass
    return {"provider": provider, "address": address, "password": password,
            "imap": imap_host, "smtp": smtp_host, "smtp_port": smtp_port}


def _require(cfg: dict) -> None:
    missing = [n for n, k in (("email address", "address"), ("app password", "password"), ("IMAP server", "imap")) if not cfg[k]]
    if missing:
        raise EmailNotConfigured("Email isn't set up yet (missing " + ", ".join(missing) +
                                 "). Open the dashboard > Settings > Email to add it.")


def _connect(cfg: dict) -> imaplib.IMAP4_SSL:
    _require(cfg)
    conn = imaplib.IMAP4_SSL(cfg["imap"], 993, ssl_context=ssl.create_default_context(), timeout=20)
    try:
        conn.login(cfg["address"], cfg["password"])
    except imaplib.IMAP4.error as e:
        conn.logout()
        raise EmailNotConfigured(
            "The email login was rejected. For Gmail / Google Workspace you need an app password, "
            f"not your normal password. ({str(e)[:120]})") from None
    return conn


def _dec(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _short_from(value: str) -> str:
    name, addr = email.utils.parseaddr(_dec(value))
    return name or addr or "unknown sender"


def _body_text(msg: email.message.Message) -> str:
    """Best plain-text version of a message (falls back to stripped HTML)."""
    plain, html = None, None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            payload = part.get_payload(decode=True)
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace") if payload else ""
        except Exception:
            continue
        if part.get_content_type() == "text/plain" and plain is None:
            plain = text
        elif part.get_content_type() == "text/html" and html is None:
            html = text
    if plain is None and html:
        plain = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        plain = unescape(re.sub(r"(?s)<[^>]+>", " ", plain))
    text = re.sub(r"[ \t\r\f\v]+", " ", plain or "")
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fetch_headers(conn, ids: list[bytes]) -> list[dict]:
    out = []
    for uid in ids:
        typ, data = conn.uid("fetch", uid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            continue
        flags = data[0][0].decode(errors="replace")
        msg = email.message_from_bytes(data[0][1])
        out.append({"id": uid.decode(), "from": _short_from(msg.get("From")), "subject": _dec(msg.get("Subject")) or "(no subject)",
                    "date": msg.get("Date", ""), "unread": "\\Seen" not in flags})
    return out


def _format_list(items: list[dict], total: int, what: str) -> str:
    if not items:
        return f"No {what}."
    lines = [f"{total} {what}. Showing {len(items)} newest (id: from - subject):"]
    lines += [f"- id {m['id']}: {m['from']} - {m['subject']}" + (" [unread]" if m["unread"] else "") for m in items]
    lines.append("Use read_email with an id to read one.")
    return "\n".join(lines)


def _search(criteria: list[str], max_results: int, what: str, folder: str = "INBOX") -> str:
    cfg = email_config()
    try:
        conn = _connect(cfg)
    except EmailNotConfigured as e:
        return str(e)
    try:
        typ, _ = conn.select(_quote(folder), readonly=True)  # readonly: never changes read/unread
        if typ != "OK":
            return f"Couldn't open the mail folder '{folder}'."
        typ, data = conn.uid("search", None, *criteria)
        ids = data[0].split() if typ == "OK" and data and data[0] else []
        newest = list(reversed(ids))[: max(1, min(int(max_results or 5), 20))]
        return _format_list(_fetch_headers(conn, newest), len(ids), what)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# --------------------------------------------------------------------------- tools
@tool("check_email", "Check the user's email inbox: how many unread emails and who they are from (read-only).",
      {"type": "object", "properties": {
          "unread_only": {"type": "boolean", "description": "true = only unread (default), false = newest emails"},
          "max_results": {"type": "integer", "description": "How many to list (default 5, max 20)"}}})
def check_email(ctx, unread_only: bool = True, max_results: int = 5):
    return _search(["UNSEEN"] if unread_only else ["ALL"], max_results, "unread emails" if unread_only else "emails in the inbox")


@tool("search_email", "Search the user's email by sender, subject or words (read-only).",
      {"type": "object", "properties": {
          "query": {"type": "string", "description": "Words to look for anywhere in the email"},
          "from_sender": {"type": "string", "description": "Name or address of the sender"},
          "days": {"type": "integer", "description": "Only the last N days"},
          "max_results": {"type": "integer"}}})
def search_email(ctx, query: str = "", from_sender: str = "", days: int = 0, max_results: int = 5):
    from datetime import datetime, timedelta

    crit: list[str] = []
    if from_sender:
        crit += ["FROM", _quote(from_sender)]
    if query:
        crit += ["TEXT", _quote(query)]
    if days and int(days) > 0:
        crit += ["SINCE", (datetime.now() - timedelta(days=int(days))).strftime("%d-%b-%Y")]
    return _search(crit or ["ALL"], max_results, "matching emails")


@tool("read_email", "Read one email by its id (from check_email / search_email). Does not mark it as read.",
      {"type": "object", "properties": {"email_id": {"type": "string"}}, "required": ["email_id"]})
def read_email(ctx, email_id: str):
    cfg = email_config()
    try:
        conn = _connect(cfg)
    except EmailNotConfigured as e:
        return str(e)
    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("fetch", str(email_id).encode(), "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return f"I couldn't find an email with id {email_id}."
        msg = email.message_from_bytes(data[0][1])
        body = _body_text(msg)
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + " ...(cut short)"
        attachments = [_dec(p.get_filename()) for p in msg.walk() if p.get_filename()]
        return (f"From: {_dec(msg.get('From'))}\nSubject: {_dec(msg.get('Subject'))}\nDate: {msg.get('Date', '')}\n"
                + (f"Attachments: {', '.join(attachments)}\n" if attachments else "") + f"\n{body or '(empty email)'}")
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _send(cfg: dict, msg: EmailMessage) -> None:
    _require(cfg)
    if not cfg["smtp"]:
        raise EmailNotConfigured("No SMTP (sending) server set. Add EMAIL_SMTP_SERVER in the dashboard.")
    ctx_ssl = ssl.create_default_context()
    if cfg["smtp_port"] == 465:
        with smtplib.SMTP_SSL(cfg["smtp"], 465, context=ctx_ssl, timeout=20) as s:
            s.login(cfg["address"], cfg["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["smtp"], cfg["smtp_port"], timeout=20) as s:
            s.starttls(context=ctx_ssl)
            s.login(cfg["address"], cfg["password"])
            s.send_message(msg)


@tool("send_email", "Send a new email from the user's account. Always asks the user to confirm first.",
      {"type": "object", "properties": {"to": {"type": "string", "description": "Recipient email address"},
                                        "subject": {"type": "string"}, "body": {"type": "string"}},
       "required": ["to", "subject", "body"]}, dangerous=True, risk="send")
def send_email(ctx, to: str, subject: str, body: str):
    if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", to.strip()):
        return f"Error: '{to}' doesn't look like one valid email address. Ask the user for the exact address."
    cfg = email_config()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["address"], to.strip(), subject
    msg["Date"], msg["Message-ID"] = email.utils.formatdate(localtime=True), email.utils.make_msgid()
    msg.set_content(body)
    try:
        _send(cfg, msg)
    except EmailNotConfigured as e:
        return str(e)
    return f"Email sent to {to}."


@tool("reply_email", "Reply to an email by its id. Always asks the user to confirm first.",
      {"type": "object", "properties": {"email_id": {"type": "string"}, "body": {"type": "string"}},
       "required": ["email_id", "body"]}, dangerous=True, risk="send")
def reply_email(ctx, email_id: str, body: str):
    cfg = email_config()
    try:
        conn = _connect(cfg)
    except EmailNotConfigured as e:
        return str(e)
    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.uid("fetch", str(email_id).encode(),
                             "(BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO SUBJECT MESSAGE-ID REFERENCES)])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return f"I couldn't find an email with id {email_id}."
        orig = email.message_from_bytes(data[0][1])
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    to = email.utils.parseaddr(_dec(orig.get("Reply-To") or orig.get("From")))[1]
    subject = _dec(orig.get("Subject"))
    msg = EmailMessage()
    msg["From"], msg["To"] = cfg["address"], to
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    msg["Date"], msg["Message-ID"] = email.utils.formatdate(localtime=True), email.utils.make_msgid()
    if orig.get("Message-ID"):
        msg["In-Reply-To"] = orig["Message-ID"]
        msg["References"] = (orig.get("References", "") + " " + orig["Message-ID"]).strip()
    msg.set_content(body)
    try:
        _send(cfg, msg)
    except EmailNotConfigured as e:
        return str(e)
    return f"Reply sent to {to}."


def check_connection() -> dict:
    """Used by the dashboard status check: (status, summary)."""
    cfg = email_config()
    if not cfg["address"] or not cfg["password"]:
        return {"status": "info", "summary": "Not set up (optional). Settings > Email."}
    try:
        conn = _connect(cfg)
        typ, data = conn.select("INBOX", readonly=True)
        typ2, unseen = conn.search(None, "UNSEEN")
        conn.logout()
        n = len(unseen[0].split()) if typ2 == "OK" and unseen and unseen[0] else 0
        return {"status": "ok", "summary": f"Connected as {cfg['address']} · {n} unread"}
    except EmailNotConfigured as e:
        return {"status": "error", "summary": str(e)[:160]}
    except Exception as e:
        return {"status": "error", "summary": f"Can't connect to {cfg['imap']} ({type(e).__name__}: {str(e)[:100]})"}
