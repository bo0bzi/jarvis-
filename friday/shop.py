"""Shop helpers for Integrity Powersports: job board, "your bike is ready" texts, parts reminders, quote drafts.
Everything is stored locally in data/friday.db. Texts to customers always ask the owner first."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import activity
from .env import data_dir, env, env_float
from .memory_db import _LOCK, connect, index
from .tools import tool

STATUSES = ["booked", "in progress", "waiting for parts", "ready", "picked up", "cancelled"]
OPEN = ("booked", "in progress", "waiting for parts", "ready")


def _db(ctx=None):
    base = getattr(getattr(ctx, "settings", None), "data_dir", None)
    con = connect(base)
    con.execute("""CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, customer TEXT, phone TEXT, vehicle TEXT,
                   work TEXT, status TEXT, quote TEXT, notes TEXT, due TEXT, created TEXT, updated TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS parts (id INTEGER PRIMARY KEY, part TEXT, job_id INTEGER, remind_on TEXT,
                   done INTEGER DEFAULT 0, created TEXT)""")
    return con, base


def _now() -> str:
    return datetime.now().isoformat(timespec="minutes")


def _status(s: str) -> str:
    s = (s or "").lower().strip()
    for st in STATUSES:
        if s and (s == st or s in st or st in s):
            return st
    if s in ("done", "finished", "complete", "completed"):
        return "ready"
    if s in ("parts", "waiting"):
        return "waiting for parts"
    return ""


def _row_text(r) -> str:
    return (f"Job {r['id']}: {r['customer']} - {r['vehicle']} - {r['work']} [{r['status']}]"
            + (f", due {r['due']}" if r["due"] else "") + (f", quote {r['quote']}" if r["quote"] else ""))


def _find(con, job: str):
    job = str(job or "").strip().lstrip("#")
    if job.isdigit():
        return con.execute("SELECT * FROM jobs WHERE id=?", (int(job),)).fetchone()
    like = f"%{job}%"
    return con.execute("SELECT * FROM jobs WHERE (customer LIKE ? OR vehicle LIKE ?) ORDER BY "
                       "CASE WHEN status IN ('picked up','cancelled') THEN 1 ELSE 0 END, id DESC",
                       (like, like)).fetchone()


@tool("add_job", "Add a repair/service job to the shop job board.",
      {"type": "object", "properties": {
          "customer": {"type": "string"}, "vehicle": {"type": "string", "description": "e.g. 2017 Kawasaki ZX6R"},
          "work": {"type": "string", "description": "what needs doing"},
          "phone": {"type": "string"}, "quote": {"type": "string"}, "due": {"type": "string"}},
       "required": ["customer", "vehicle", "work"]})
def add_job(ctx, customer: str, vehicle: str, work: str, phone: str = "", quote: str = "", due: str = ""):
    with _LOCK:
        con, base = _db(ctx)
        with con:
            cur = con.execute("INSERT INTO jobs(customer, phone, vehicle, work, status, quote, notes, due, created, updated)"
                              " VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (customer.strip(), phone.strip(), vehicle.strip(), work.strip(), "booked", quote, "",
                               due, _now(), _now()))
        r = con.execute("SELECT * FROM jobs WHERE id=?", (cur.lastrowid,)).fetchone()
        con.close()
    index("job", _row_text(r) + (f" phone {phone}" if phone else ""), ref=str(r["id"]), base=base)
    activity.log("shop", f"New job {r['id']}: {customer} {vehicle}")
    return f"Added job {r['id']}: {customer}'s {vehicle} - {work}."


@tool("update_job", "Change a job's status (booked, in progress, waiting for parts, ready, picked up, cancelled) "
      "and/or add a note. 'job' can be the job number or the customer's name / bike.",
      {"type": "object", "properties": {"job": {"type": "string"}, "status": {"type": "string"},
                                        "note": {"type": "string"}, "quote": {"type": "string"},
                                        "phone": {"type": "string"}},
       "required": ["job"]})
def update_job(ctx, job: str, status: str = "", note: str = "", quote: str = "", phone: str = ""):
    with _LOCK:
        con, base = _db(ctx)
        r = _find(con, job)
        if r is None:
            con.close()
            return f"I couldn't find a job for '{job}'."
        st = _status(status) if status else r["status"]
        if status and not st:
            con.close()
            return f"Unknown status '{status}'. Use one of: {', '.join(STATUSES)}."
        notes = (r["notes"] + f"\n[{_now()}] {note.strip()}").strip() if note else r["notes"]
        with con:
            con.execute("UPDATE jobs SET status=?, notes=?, quote=?, phone=?, updated=? WHERE id=?",
                        (st, notes, quote or r["quote"], phone or r["phone"], _now(), r["id"]))
        r = con.execute("SELECT * FROM jobs WHERE id=?", (r["id"],)).fetchone()
        con.close()
    index("job", _row_text(r) + " " + (r["notes"] or ""), ref=str(r["id"]), base=base)
    activity.log("shop", f"Job {r['id']} -> {st}" + (f" ({note})" if note else ""))
    return f"Job {r['id']} ({r['customer']}'s {r['vehicle']}) is now '{st}'." + (" Note added." if note else "")


@tool("list_jobs", "List shop jobs. status: open (default: everything not picked up), all, or one status.",
      {"type": "object", "properties": {"status": {"type": "string"}}})
def list_jobs(ctx, status: str = "open"):
    con, _ = _db(ctx)
    status = (status or "open").lower()
    if status == "all":
        rows = con.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 40").fetchall()
    elif status == "open":
        rows = con.execute(f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(OPEN))}) ORDER BY id",
                           OPEN).fetchall()
    else:
        rows = con.execute("SELECT * FROM jobs WHERE status=? ORDER BY id", (_status(status),)).fetchall()
    con.close()
    if not rows:
        return "No jobs on the board." if status in ("open", "all") else f"No jobs with status '{status}'."
    return "\n".join(_row_text(r) for r in rows)


def open_jobs_summary(ctx=None) -> str:
    con, _ = _db(ctx)
    rows = con.execute(f"SELECT status, COUNT(*) n FROM jobs WHERE status IN ({','.join('?' * len(OPEN))}) "
                       "GROUP BY status", OPEN).fetchall()
    con.close()
    return ", ".join(f"{r['n']} {r['status']}" for r in rows)


@tool("text_customer_bike_ready", "Text a customer that their vehicle is ready for pickup (from the shop's Quo "
      "number) and mark the job 'ready'. Always asks the owner first.",
      {"type": "object", "properties": {"job": {"type": "string", "description": "job number or customer name"},
                                        "extra": {"type": "string", "description": "optional extra line, e.g. total"}},
       "required": ["job"]}, risk="send")
def text_customer_bike_ready(ctx, job: str, extra: str = ""):
    from .sms import get_link

    con, _ = _db(ctx)
    r = _find(con, job)
    con.close()
    if r is None:
        return f"I couldn't find a job for '{job}'."
    if not r["phone"]:
        return f"Job {r['id']} has no phone number. Tell me the number and I'll save it (update_job phone)."
    link = get_link()
    if not link.configured():
        return "Texting isn't set up yet. Add the Quo settings in Dashboard > Settings > SMS (Quo)."
    shop = env("SHOP_NAME", "Integrity Powersports")
    first = r["customer"].split()[0] if r["customer"] else "there"
    msg = env("SHOP_READY_TEXT", "Hi {first}, your {vehicle} is ready for pickup at {shop}. {extra}").format(
        first=first, vehicle=r["vehicle"], shop=shop, extra=extra).strip()
    out = link.send(r["phone"], msg)
    update_job(ctx, str(r["id"]), status="ready", note=f"Texted customer: {msg}")
    return out + f" Job {r['id']} marked ready."


@tool("add_parts_reminder", "Remind the owner about a part (to order, or to check if it arrived), optionally for a "
      "job, after a number of days (0 = tomorrow morning's briefing).",
      {"type": "object", "properties": {"part": {"type": "string"}, "job": {"type": "string"},
                                        "days": {"type": "integer"}}, "required": ["part"]})
def add_parts_reminder(ctx, part: str, job: str = "", days: int = 0):
    with _LOCK:
        con, _ = _db(ctx)
        r = _find(con, job) if job else None
        on = (datetime.now() + timedelta(days=max(0, int(days or 0)))).strftime("%Y-%m-%d")
        with con:
            con.execute("INSERT INTO parts(part, job_id, remind_on, created) VALUES (?,?,?,?)",
                        (part.strip(), r["id"] if r else None, on, _now()))
        con.close()
    return f"I'll remind you about {part}" + (f" for job {r['id']}" if r else "") + f" on {on}."


def due_parts_reminders(ctx=None, mark_done: bool = False) -> list[str]:
    con, _ = _db(ctx)
    today = datetime.now().strftime("%Y-%m-%d")
    rows = con.execute("SELECT p.*, j.customer, j.vehicle FROM parts p LEFT JOIN jobs j ON j.id = p.job_id "
                       "WHERE p.done=0 AND p.remind_on <= ?", (today,)).fetchall()
    if mark_done and rows:
        with con:
            con.executemany("UPDATE parts SET done=1 WHERE id=?", [(r["id"],) for r in rows])
    con.close()
    return [f"{r['part']}" + (f" (job {r['job_id']}: {r['customer']}'s {r['vehicle']})" if r["job_id"] else "")
            for r in rows]


@tool("list_parts_reminders", "List parts reminders that are still open.")
def list_parts_reminders(ctx):
    con, _ = _db(ctx)
    rows = con.execute("SELECT * FROM parts WHERE done=0 ORDER BY remind_on").fetchall()
    con.close()
    return "\n".join(f"{r['remind_on']}: {r['part']}" + (f" (job {r['job_id']})" if r["job_id"] else "")
                     for r in rows) or "No open parts reminders."


def _money(x: float) -> str:
    return f"${x:,.2f}"


@tool("draft_quote", "Draft a repair quote (saved as a text file, NOT sent). Labour at the shop rate "
      "(LABOUR_RATE), Ontario HST (TAX_RATE, default 13%).",
      {"type": "object", "properties": {
          "customer": {"type": "string"}, "vehicle": {"type": "string"}, "work": {"type": "string"},
          "labour_hours": {"type": "number"},
          "parts": {"type": "array", "description": "parts with prices",
                    "items": {"type": "object", "properties": {"name": {"type": "string"}, "price": {"type": "number"},
                                                               "qty": {"type": "number"}}}}},
       "required": ["customer", "vehicle", "work"]})
def draft_quote(ctx, customer: str, vehicle: str, work: str, labour_hours: float = 0, parts: list | None = None):
    rate = env_float("LABOUR_RATE", 0)
    tax = env_float("TAX_RATE", 13)
    lines, subtotal = [], 0.0
    for p in parts or []:
        if isinstance(p, str):
            p = {"name": p}
        qty = float(p.get("qty") or 1)
        price = float(p.get("price") or 0)
        subtotal += qty * price
        lines.append(f"  {p.get('name', 'part')} x{qty:g} @ {_money(price)} = {_money(qty * price)}"
                     + ("  (PRICE NEEDED)" if not price else ""))
    labour = float(labour_hours or 0) * rate
    subtotal += labour
    taxes = subtotal * tax / 100
    shop = env("SHOP_NAME", "Integrity Powersports")
    text = "\n".join([f"{shop} - QUOTE (draft)", f"Date: {datetime.now():%B %d, %Y}", f"Customer: {customer}",
                      f"Vehicle: {vehicle}", f"Work: {work}", "", "Parts:", *(lines or ["  (none listed)"]),
                      f"Labour: {float(labour_hours or 0):g} h @ {_money(rate)}/h = {_money(labour)}"
                      + ("  (SET LABOUR_RATE in settings)" if not rate else ""),
                      f"Subtotal: {_money(subtotal)}", f"HST {tax:g}%: {_money(taxes)}",
                      f"TOTAL: {_money(subtotal + taxes)}", "", "Quote valid 30 days. Prices may change if extra "
                      "problems are found; we'll call you before doing any extra work."])
    folder = data_dir() / "quotes"
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w]+", "_", f"{datetime.now():%Y%m%d_%H%M}_{customer}")[:60] + ".txt"
    (folder / name).write_text(text, encoding="utf-8")
    activity.log("shop", f"Drafted quote for {customer}: {_money(subtotal + taxes)}")
    return f"Quote drafted: total {_money(subtotal + taxes)} including HST. Saved as data/quotes/{name}." + \
        (" Note: the labour rate isn't set yet." if not rate and labour_hours else "")
