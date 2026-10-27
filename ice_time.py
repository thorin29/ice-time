#!/usr/bin/env python3
"""
ice-time — a small service that reads public ice-time schedules (stick & puck,
drop-in, open hockey…) from Finnly registration pages and serves them as one
simple JSON feed, with a settings page for choosing which pages to read.

Everything rink-specific lives in this service's data folder (/data): the
links, the rink name, the cached sessions. Nothing about a particular rink is
in the code. See README.md for setup and the feed format, DECISIONS.md for why
it is built this way.

Standard library only.
"""
from __future__ import annotations

import base64
import datetime as dt
import hmac
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.2"

DATA_DIR = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8080"))
POLL_MINUTES = max(10, int(os.environ.get("POLL_MINUTES", "30")))
FEED_TOKEN = os.environ.get("FEED_TOKEN", "").strip()
SETTINGS_PASSWORD = os.environ.get("SETTINGS_PASSWORD", "").strip()
TIMEZONE = os.environ.get("TIMEZONE", "").strip() or None
USER_AGENT = f"ice-time/{VERSION} (personal schedule viewer; polls every {POLL_MINUTES} min)"

SOURCES_FILE = os.path.join(DATA_DIR, "sources.json")
CACHE_FILE = os.path.join(DATA_DIR, "cache.json")

# Sections, in the order the feed lists them. Anything else a link is filed
# under ("Learn to skate", "Freestyle") follows in alphabetical order.
SECTION_ORDER = ["Stick & puck", "Drop-in", "Parent & child", "Youth open hockey"]
ROLES = {"skater": "Skaters", "goalie": "Goalies", "all": "Players"}

# Only Finnly registration pages are fetched: the settings page takes a URL
# from whoever can open it, so the server must never be pointed anywhere else.
LINK_RE = re.compile(
    r"^https://([a-z0-9-]+)\.finnlyconnect\.com/registration/activityitem(v2)?/(\d+)/?(\?.*)?$",
    re.I,
)

# --------------------------------------------------------------------------
# Reading a Finnly registration page
# --------------------------------------------------------------------------


class ParseError(Exception):
    pass


def _js_value_after(text: str, key: str) -> str | None:
    """The JSON object or array that follows `key:` in a script, matched by
    brackets with strings skipped (a description may contain "]" or "}")."""
    i = text.find(key + ":")
    if i < 0:
        return None
    j = i + len(key) + 1
    while j < len(text) and text[j] in " \t\r\n":
        j += 1
    if j >= len(text) or text[j] not in "[{":
        return None
    opener, closer = text[j], "]" if text[j] == "[" else "}"
    depth, k, in_str, esc = 0, j, False, False
    while k < len(text):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return text[j : k + 1]
        k += 1
    return None


def parse_activity_page(page: str) -> dict:
    """{name, sessions:[{start,end,enrolled,max,remaining,sessionId,facility,canPurchase}]}"""
    raw = _js_value_after(page, "registrationScheduleList")
    if raw is None:
        raise ParseError("The page has no schedule list (registrationScheduleList).")
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ParseError(f"The schedule list isn't readable JSON ({e.msg}).") from e
    name = None
    act = _js_value_after(page, "activity")
    if act:
        try:
            name = (json.loads(act).get("Name") or "").strip() or None
        except json.JSONDecodeError:
            name = None
    if not name:
        m = re.search(r'class="[^"]*activityItemActivityName[^"]*"[^>]*>([^<]+)<', page)
        name = html.unescape(m.group(1)).strip() if m else None
    sessions = []
    for r in rows:
        try:
            start, end = r["Start"], r["End"]
            dt.datetime.fromisoformat(start)
            dt.datetime.fromisoformat(end)
        except (KeyError, TypeError, ValueError):
            continue
        sessions.append(
            {
                "start": start[:19],
                "end": end[:19],
                "enrolled": _int(r.get("EnrollCount")),
                "max": _int(r.get("MaxEnroll")),
                "remaining": _int(r.get("RemainingSpots")),
                "sessionId": r.get("SessionId"),
                "facility": (r.get("DisplayFacility") or "").strip() or None,
                "canPurchase": bool(r.get("CanPurchase", True)),
            }
        )
    sessions.sort(key=lambda s: s["start"])
    return {"name": name, "sessions": sessions}


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def describe_activity(name: str | None, sessions: list[dict]) -> dict:
    """Best guesses from the activity's own name, e.g.
    "Sample Arena - Adult Drop In (GOALIE) 26/27" → section Drop-in, role goalie,
    rink Sample Arena, season 26/27. Every guess is editable before saving."""
    n = (name or "").strip()
    low = n.lower()
    if "stick" in low and "puck" in low:
        section = "Stick & puck"
    elif "drop" in low and "in" in low:
        section = "Drop-in"
    elif "parent" in low:
        section = "Parent & child"
    elif "open hockey" in low or "youth" in low:
        section = "Youth open hockey"
    else:
        section = ""
    role = "goalie" if "goalie" in low or "goaltender" in low else ("skater" if "skater" in low else "all")
    rink = n.split(" - ", 1)[0].strip() if " - " in n else ""
    if not rink:
        rink = next((s["facility"] for s in sessions if s.get("facility")), "") or ""
    m = re.search(r"(\d{2})\s*/\s*(\d{2})\s*$", n)
    season = f"{m.group(1)}/{m.group(2)}" if m else None
    return {"section": section, "role": role, "rink": rink, "season": season}


def current_season(today: dt.date | None = None) -> str:
    """Rink seasons run July 1 – June 30: on 2026-10-01 this is "26/27"."""
    d = today or dt.date.today()
    y = d.year if d.month >= 7 else d.year - 1
    return f"{y % 100:02d}/{(y + 1) % 100:02d}"


def normalize_link(url: str) -> str | None:
    m = LINK_RE.match((url or "").strip())
    if not m:
        return None
    host, _, ident = m.group(1).lower(), m.group(2), m.group(3)
    return f"https://{host}.finnlyconnect.com/registration/activityitemv2/{ident}"


def fetch_page(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ParseError("The page is unexpectedly large.")
        charset = resp.headers.get_content_charset() or "utf-8"
        return body.decode(charset, errors="replace")


def read_link(url: str) -> dict:
    """Fetch and parse one registration link. Older-style links
    (/activityitem/<id>) are read through the newer page for the same id."""
    link = normalize_link(url)
    if not link:
        raise ParseError("That isn't a Finnly registration link (…finnlyconnect.com/registration/activityitemv2/<number>).")
    return parse_activity_page(fetch_page(link))


# --------------------------------------------------------------------------
# State: sources (settings) and their latest sessions (cache)
# --------------------------------------------------------------------------

_lock = threading.RLock()


def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: str, value) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)
    os.replace(tmp, path)


def load_sources() -> list[dict]:
    return _load(SOURCES_FILE, {"sources": []}).get("sources", [])


def save_sources(sources: list[dict]) -> None:
    _save(SOURCES_FILE, {"version": 1, "sources": sources})


CACHE: dict = _load(CACHE_FILE, {})  # source id -> {sessions, name, okAt, error, errorAt}


def refresh_source(src: dict) -> None:
    now = _now_iso()
    try:
        page = read_link(src["url"])
        with _lock:
            CACHE[src["id"]] = {
                "sessions": page["sessions"],
                "name": page["name"],
                "okAt": now,
                "error": None,
                "errorAt": None,
            }
    except Exception as e:  # noqa: BLE001 — any failure is recorded per source
        with _lock:
            prev = CACHE.get(src["id"], {})
            CACHE[src["id"]] = {**prev, "error": _short_error(e), "errorAt": now}
    with _lock:
        _save(CACHE_FILE, CACHE)


def refresh_all() -> None:
    for src in load_sources():
        if src.get("enabled", True):
            refresh_source(src)
            time.sleep(2)  # one page at a time, gently


def _short_error(e: Exception) -> str:
    if isinstance(e, ParseError):
        return str(e)
    if isinstance(e, urllib.error.HTTPError):
        return f"The site answered {e.code}."
    if isinstance(e, urllib.error.URLError):
        return f"Couldn't reach the site ({e.reason})."
    return f"{type(e).__name__}: {e}"[:200]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def poll_forever() -> None:
    while True:
        try:
            refresh_all()
        except Exception as e:  # noqa: BLE001
            print(f"[ice-time] refresh failed: {e}", file=sys.stderr, flush=True)
        time.sleep(POLL_MINUTES * 60)


# --------------------------------------------------------------------------
# The feed
# --------------------------------------------------------------------------


def source_health(src: dict, now_local: dt.datetime) -> dict:
    c = CACHE.get(src["id"], {})
    sessions = c.get("sessions") or []
    upcoming = [s for s in sessions if s["end"] >= now_local.isoformat(timespec="seconds")]
    season = src.get("season")
    warnings = []
    if c.get("error"):
        warnings.append(c["error"])
    if c.get("okAt") and not upcoming:
        warnings.append("No upcoming sessions on this page.")
    if season and season != current_season(now_local.date()) and not upcoming:
        warnings.append(f"This link is for the {season} season — paste the new season's link.")
    return {
        "id": src["id"],
        "label": src.get("label") or c.get("name"),
        "section": src.get("section"),
        "role": src.get("role"),
        "rink": src.get("rink"),
        "season": season,
        "url": src.get("url"),
        "enabled": src.get("enabled", True),
        "lastSuccess": c.get("okAt"),
        "lastError": c.get("error"),
        "lastErrorAt": c.get("errorAt"),
        "upcomingSessions": len(upcoming),
        "ok": bool(c.get("okAt")) and not c.get("error"),
        "warnings": warnings,
    }


def build_feed(days: int = 7, now_local: dt.datetime | None = None) -> dict:
    """Sessions from now through the end of the `days`-th day, skater/goalie
    pages for the same section and rink combined by start time."""
    now_local = now_local or _local_now()
    end_local = dt.datetime.combine(now_local.date() + dt.timedelta(days=days), dt.time(0, 0))
    lo, hi = now_local.isoformat(timespec="seconds"), end_local.isoformat(timespec="seconds")
    with _lock:
        sources = [s for s in load_sources() if s.get("enabled", True)]
        merged: dict[tuple, dict] = {}
        for src in sources:
            role = src.get("role") or "all"
            key_role = ROLES.get(role, "Players").lower()
            for s in (CACHE.get(src["id"], {}).get("sessions") or []):
                if s["end"] < lo or s["start"] >= hi:
                    continue
                k = (src.get("section") or "Other", src.get("rink") or "", s["start"])
                row = merged.setdefault(
                    k,
                    {
                        "section": k[0],
                        "rink": k[1] or None,
                        "start": s["start"],
                        "end": s["end"],
                        "minutes": int((dt.datetime.fromisoformat(s["end"]) - dt.datetime.fromisoformat(s["start"])).total_seconds() // 60),
                        "skaters": None,
                        "goalies": None,
                        "players": None,
                        "links": {},
                    },
                )
                row[key_role] = {"enrolled": s["enrolled"], "max": s["max"], "remaining": s["remaining"]}
                row["links"][key_role] = src["url"]
        health = [source_health(s, now_local) for s in load_sources()]
    order = {name: i for i, name in enumerate(SECTION_ORDER)}
    sections: dict[str, list] = {}
    for row in sorted(merged.values(), key=lambda r: (order.get(r["section"], 99), r["section"], r["start"], r["rink"] or "")):
        sections.setdefault(row["section"], []).append(row)
    return {
        "service": "ice-time",
        "version": VERSION,
        "generatedAt": _now_iso(),
        "timezone": TIMEZONE,
        "from": lo,
        "to": hi,
        "sections": [{"name": name, "sessions": rows} for name, rows in sections.items()],
        "sources": health,
        "warnings": [f"{h['label'] or h['url']}: {w}" for h in health if h["enabled"] for w in h["warnings"]],
    }


def _local_now() -> dt.datetime:
    """Wall-clock "now" in the rink's zone (TIMEZONE, else the container's)."""
    if TIMEZONE:
        try:
            from zoneinfo import ZoneInfo

            return dt.datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None, microsecond=0)
        except Exception:  # noqa: BLE001
            pass
    return dt.datetime.now().replace(microsecond=0)


# --------------------------------------------------------------------------
# Settings page (server-rendered; no JavaScript needed)
# --------------------------------------------------------------------------

CSS = """
:root{--ink:#10202f;--muted:#5b6b7d;--line:#dce3ea;--ground:#edf1f5;--accent:#0f5c63;--on-accent:#fff;--ok:#16623a;--warn:#8a4b00;--bad:#a12a2a}
@media (prefers-color-scheme: dark){:root{--ink:#e7ecf3;--muted:#94a3b8;--line:#2a3547;--ground:#0b1220;--accent:#2dd4bf;--on-accent:#06201d;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}
.card,input,select{background:#161e2e!important;color:var(--ink)!important}}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 48px}h1{font-size:28px;margin:0 0 4px}h2{font-size:19px;margin:0 0 12px}
p{margin:0 0 12px}.muted{color:var(--muted)}.card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:18px 20px;margin:16px 0}
label{display:block;font-weight:600;font-size:14px;margin:10px 0 4px}input,select{width:100%;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:#fff}
.row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}@media (max-width:640px){.row{grid-template-columns:1fr}}
button,.btn{font:inherit;font-weight:600;border:none;border-radius:999px;padding:10px 18px;cursor:pointer;background:var(--accent);color:var(--on-accent);text-decoration:none;display:inline-block;min-height:44px}
.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}.danger{background:transparent;color:var(--bad);border:1px solid var(--line)}
.src{border-top:1px solid var(--line);padding:14px 0;display:flex;gap:12px;justify-content:space-between;align-items:flex-start;flex-wrap:wrap}.src:first-of-type{border-top:none}
.tag{font-size:12px;font-weight:600;padding:2px 9px;border-radius:999px;border:1px solid var(--line);margin-right:6px}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:6px 8px;border-top:1px solid var(--line)}code{font-size:13px}a{color:var(--accent)}.check{display:flex;gap:8px;align-items:center;font-weight:600;font-size:14px;margin-top:12px}.check input{width:auto}
.actions{display:flex;gap:8px;flex-wrap:wrap}form.inline{display:inline}
"""


def page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>"
        f"{body}<p class='muted' style='margin-top:32px;font-size:13px'>ice-time v{VERSION}</p></main></body></html>"
    ).encode("utf-8")


def _e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _fmt_session(s: dict) -> str:
    a, b = dt.datetime.fromisoformat(s["start"]), dt.datetime.fromisoformat(s["end"])
    t = lambda x: x.strftime("%I:%M %p").lstrip("0")  # noqa: E731
    cnt = f"{s['enrolled']}/{s['max']}" if s.get("max") is not None else "—"
    return f"<tr><td>{a.strftime('%a %b')} {a.day}</td><td>{t(a)} – {t(b)}</td><td>{_e(cnt)}</td></tr>"


def section_options(selected: str) -> str:
    opts = SECTION_ORDER + ([selected] if selected and selected not in SECTION_ORDER else [])
    out = ["<option value=''>Choose…</option>"]
    for o in opts:
        out.append(f"<option{' selected' if o == selected else ''}>{_e(o)}</option>")
    return "".join(out)


def role_options(selected: str) -> str:
    labels = {"skater": "Skaters", "goalie": "Goalies", "all": "Everyone (no skater/goalie split)"}
    return "".join(f"<option value='{k}'{' selected' if k == selected else ''}>{v}</option>" for k, v in labels.items())


def home_page(msg: str = "") -> bytes:
    now_local = _local_now()
    sources = load_sources()
    rows = []
    for src in sources:
        h = source_health(src, now_local)
        status = (
            f"<span class='bad'>{_e(h['lastError'])}</span>"
            if h["lastError"]
            else (f"<span class='ok'>OK</span> · {h['upcomingSessions']} upcoming" if h["lastSuccess"] else "<span class='muted'>Not read yet</span>")
        )
        extra = "".join(f"<div class='warn'>{_e(w)}</div>" for w in h["warnings"] if w != h["lastError"])
        rows.append(
            "<div class='src'><div style='min-width:0;flex:1 1 360px'>"
            f"<div><strong>{_e(h['label'] or h['url'])}</strong></div>"
            f"<div style='margin:4px 0'><span class='tag'>{_e(h['section'] or 'No section')}</span>"
            f"<span class='tag'>{_e(ROLES.get(h['role'] or 'all'))}</span>"
            f"<span class='tag'>{_e(h['rink'] or 'No rink')}</span>"
            + (f"<span class='tag'>{_e(h['season'])}</span>" if h["season"] else "")
            + ("" if h["enabled"] else "<span class='tag warn'>Paused</span>")
            + f"</div><div style='font-size:14px'>{status}</div>{extra}"
            f"<div class='muted' style='font-size:13px;word-break:break-all'>{_e(h['url'])}</div></div>"
            "<div class='actions'>"
            f"<a class='btn ghost' href='/sources/{_e(src['id'])}'>Edit</a>"
            f"<form class='inline' method='post' action='/sources/{_e(src['id'])}/delete'><button class='danger' type='submit'>Remove</button></form>"
            "</div></div>"
        )
    listing = "".join(rows) or "<p class='muted'>No links yet. Paste one above to start.</p>"
    note = f"<div class='card' role='status'>{_e(msg)}</div>" if msg else ""
    body = f"""
<h1>ice-time</h1>
<p class="muted">Reads the ice-time schedules you choose and serves them at <code>/api/sessions</code>. Checks every {POLL_MINUTES} minutes.</p>
{note}
<section class="card"><h2>Add a schedule</h2>
<form method="post" action="/sources/preview">
<label for="url">Registration link</label>
<input id="url" name="url" type="url" required placeholder="https://yourclub.finnlyconnect.com/registration/activityitemv2/12345">
<p class="muted" style="font-size:14px;margin-top:6px">Open the session's registration page on the rink's site and copy its address. Skater and goalie pages are separate links: add both.</p>
<button type="submit">Read this link</button></form></section>
<section class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap"><h2 style="margin:0">Schedules ({len(sources)})</h2>
<form method="post" action="/refresh"><button class="ghost" type="submit">Check all now</button></form></div>{listing}</section>
"""
    return page("ice-time", body)


def source_form(action: str, values: dict, page_info: dict | None, heading: str, submit: str) -> bytes:
    preview = ""
    if page_info:
        upcoming = [s for s in page_info["sessions"] if s["end"] >= _local_now().isoformat(timespec="seconds")][:6]
        trs = "".join(_fmt_session(s) for s in upcoming) or "<tr><td colspan='3' class='muted'>No upcoming sessions on this page.</td></tr>"
        preview = (
            f"<p>Found <strong>{_e(page_info['name'] or 'an activity')}</strong> with {len(page_info['sessions'])} sessions.</p>"
            f"<table><thead><tr><th>Day</th><th>Time</th><th>Signed up</th></tr></thead><tbody>{trs}</tbody></table>"
        )
    body = f"""
<p><a href="/">← Back</a></p><h1>{_e(heading)}</h1>
<section class="card">{preview}
<form method="post" action="{_e(action)}">
<input type="hidden" name="url" value="{_e(values.get('url'))}"><input type="hidden" name="label" value="{_e(values.get('label'))}"><input type="hidden" name="season" value="{_e(values.get('season'))}">
<div class="row">
<div><label for="section">Section</label><select id="section" name="section">{section_options(values.get('section') or '')}</select></div>
<div><label for="role">Who it counts</label><select id="role" name="role">{role_options(values.get('role') or 'all')}</select></div>
<div><label for="rink">Rink name</label><input id="rink" name="rink" value="{_e(values.get('rink'))}" required></div>
</div>
{('<label class="check"><input type="checkbox" name="enabled" value="1"' + (' checked' if values.get('enabled', True) else '') + '> Include in the feed</label>') if action != '/sources/add' else ''}
<p class="muted" style="font-size:14px;margin-top:10px">Filled in from the page's activity name — change anything that's wrong. The rink name is what the calendar event will be called after ("Drop-in: Rink name").</p>
<div class="actions"><button type="submit">{_e(submit)}</button><a class="btn ghost" href="/">Cancel</a></div>
</form></section>"""
    return page(heading, body)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"ice-time/{VERSION}"

    def log_message(self, fmt, *args):  # method + path + status; never the feed token
        sys.stderr.write("[ice-time] %s\n" % re.sub(r"token=[^&\s\"]*", "token=***", fmt % args))

    # --- auth ---
    def _feed_ok(self) -> bool:
        if not FEED_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        if not token:
            token = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("token", [""])[0]
        return hmac.compare_digest(token, FEED_TOKEN)

    def _settings_ok(self) -> bool:
        if not SETTINGS_PASSWORD:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("basic "):
            try:
                _, pw = base64.b64decode(auth[6:]).decode().split(":", 1)
                return hmac.compare_digest(pw, SETTINGS_PASSWORD)
            except Exception:  # noqa: BLE001
                return False
        return False

    def _need_login(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ice-time settings"')
        self.end_headers()

    # --- responses ---
    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, value):
        self._send(code, json.dumps(value, indent=2).encode("utf-8"), "application/json")

    def _redirect(self, to: str):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def _form(self) -> dict:
        n = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        data = self.rfile.read(n).decode("utf-8", errors="replace")
        return {k: v[0] for k, v in urllib.parse.parse_qs(data).items()}

    def _same_origin(self) -> bool:
        """Settings forms only accept posts from this page (no cross-site forms)."""
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urllib.parse.urlparse(origin).netloc == host

    # --- routes ---
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/healthz":
            return self._json(200, {"ok": True, "version": VERSION})
        if path == "/api/sessions":
            if not self._feed_ok():
                return self._json(401, {"error": "token required"})
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                days = max(1, min(int(q.get("days", ["7"])[0]), 31))
            except ValueError:
                days = 7
            return self._json(200, build_feed(days))
        if not self._settings_ok():
            return self._need_login()
        if path == "/":
            msg = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("msg", [""])[0]
            return self._send(200, home_page(msg))
        m = re.fullmatch(r"/sources/([A-Za-z0-9_-]+)", path)
        if m:
            src = next((s for s in load_sources() if s["id"] == m.group(1)), None)
            if not src:
                return self._redirect("/?msg=" + urllib.parse.quote("That schedule is gone."))
            info = None
            c = CACHE.get(src["id"])
            if c and c.get("sessions") is not None:
                info = {"name": c.get("name"), "sessions": c.get("sessions") or []}
            return self._send(200, source_form(f"/sources/{src['id']}", src, info, "Edit schedule", "Save"))
        return self._send(404, page("Not found", "<h1>Not found</h1><p><a href='/'>Back</a></p>"))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if not self._settings_ok():
            return self._need_login()
        if not self._same_origin():
            return self._send(403, page("Refused", "<h1>Refused</h1><p>That form came from another site.</p>"))
        f = self._form()
        if path == "/refresh":
            threading.Thread(target=refresh_all, daemon=True).start()
            return self._redirect("/?msg=" + urllib.parse.quote("Checking every schedule now — reload in a minute."))
        if path == "/sources/preview":
            link = normalize_link(f.get("url", ""))
            if not link:
                return self._redirect("/?msg=" + urllib.parse.quote("That isn't a Finnly registration link. It should look like …finnlyconnect.com/registration/activityitemv2/12345."))
            if any(s["url"] == link for s in load_sources()):
                return self._redirect("/?msg=" + urllib.parse.quote("That link is already in the list."))
            try:
                info = parse_activity_page(fetch_page(link))
            except Exception as e:  # noqa: BLE001
                return self._redirect("/?msg=" + urllib.parse.quote(f"Couldn't read that page: {_short_error(e)}"))
            guess = describe_activity(info["name"], info["sessions"])
            values = {"url": link, "label": info["name"], **guess}
            return self._send(200, source_form("/sources/add", values, info, "Add schedule", "Add schedule"))
        if path == "/sources/add":
            link = normalize_link(f.get("url", ""))
            if not link:
                return self._redirect("/?msg=" + urllib.parse.quote("That link isn't valid."))
            with _lock:
                sources = load_sources()
                if any(s["url"] == link for s in sources):
                    return self._redirect("/?msg=" + urllib.parse.quote("That link is already in the list."))
                src = {
                    "id": secrets.token_hex(6),
                    "url": link,
                    "label": (f.get("label") or "").strip()[:200] or None,
                    "section": (f.get("section") or "").strip()[:60] or None,
                    "role": f.get("role") if f.get("role") in ROLES else "all",
                    "rink": (f.get("rink") or "").strip()[:80] or None,
                    "season": (f.get("season") or "").strip()[:10] or None,
                    "enabled": True,
                    "addedAt": _now_iso(),
                }
                sources.append(src)
                save_sources(sources)
            threading.Thread(target=refresh_source, args=(src,), daemon=True).start()
            return self._redirect("/?msg=" + urllib.parse.quote("Added. Its sessions will show in a moment."))
        m = re.fullmatch(r"/sources/([A-Za-z0-9_-]+)(/delete)?", path)
        if m:
            with _lock:
                sources = load_sources()
                src = next((s for s in sources if s["id"] == m.group(1)), None)
                if not src:
                    return self._redirect("/")
                if m.group(2):
                    sources = [s for s in sources if s["id"] != src["id"]]
                    CACHE.pop(src["id"], None)
                    _save(CACHE_FILE, CACHE)
                    msg = "Removed."
                else:
                    src["section"] = (f.get("section") or "").strip()[:60] or None
                    src["role"] = f.get("role") if f.get("role") in ROLES else "all"
                    src["rink"] = (f.get("rink") or "").strip()[:80] or None
                    src["enabled"] = f.get("enabled") == "1"
                    msg = "Saved."
                save_sources(sources)
            return self._redirect("/?msg=" + urllib.parse.quote(msg))
        return self._send(404, page("Not found", "<h1>Not found</h1>"))


def drop_privileges() -> None:
    """Started as root (the image's default), take ownership of the data folder
    and switch to PUID/PGID — 99:100 by default, the nobody:users that owns
    Unraid's appdata — so the container can write its links with no extra
    settings. Started as any other user (e.g. `--user`), this does nothing.
    PUID=0 keeps running as root."""
    if not hasattr(os, "getuid") or os.getuid() != 0:
        return
    uid = int(os.environ.get("PUID", "99"))
    gid = int(os.environ.get("PGID", "100"))
    if uid == 0:
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    for root, _dirs, files in os.walk(DATA_DIR):
        for path in [root] + [os.path.join(root, f) for f in files]:
            try:
                os.chown(path, uid, gid)
            except OSError as e:
                print(f"[ice-time] couldn't take ownership of {path}: {e}", file=sys.stderr, flush=True)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main() -> None:
    drop_privileges()
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=poll_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[ice-time] v{VERSION} on :{PORT}, data in {DATA_DIR}, checking every {POLL_MINUTES} min", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
