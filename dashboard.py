"""Lightweight zero-dependency web dashboard for EC Pro Lesson Watcher."""

from __future__ import annotations

import html
import json
import os
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

APP_DIR = Path(__file__).parent
STATE_FILE = APP_DIR / "seen_dates.json"
HISTORY_FILE = APP_DIR / "history.json"
AUDIO_DIR = APP_DIR / "audio"
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Europe/London"))
BASE_URL = os.getenv("ECPRO_BASE_URL", "https://your-stables.ecpro.co.uk").strip().rstrip("/")
SERVICE_NAME = os.getenv("WATCHER_SERVICE_NAME", "ecpro-watcher")
PORT = int(os.getenv("DASHBOARD_PORT", "8080"))


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "Never"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.astimezone(LOCAL_TZ).strftime("%d %b %Y %H:%M:%S")
    except Exception:
        return ts


def _service_status() -> str:
    try:
        out = subprocess.check_output(
            ["systemctl", "is-active", SERVICE_NAME],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        ).strip()
        return out or "unknown"
    except Exception:
        return "unknown"


def _recent_logs() -> str:
    try:
        return subprocess.check_output(
            ["journalctl", "-u", SERVICE_NAME, "-n", "35", "--no-pager"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=4,
        )
    except Exception as exc:
        return f"Could not read journalctl: {exc}"


def build_status_payload() -> dict[str, Any]:
    state = _read_json(STATE_FILE, {"dates": [], "last_checked": None})
    history = _read_json(
        HISTORY_FILE,
        {"total_checks": 0, "total_notifications": 0, "notifications": [], "checks": [], "current_details": {}},
    )
    return {
        "service_status": _service_status(),
        "state": state,
        "history": history,
        "logs": _recent_logs(),
    }


def render_html() -> str:
    payload = build_status_payload()
    status = payload["service_status"]
    state = payload["state"]
    history = payload["history"]
    logs = payload["logs"]

    badge_cls = "ok" if status == "active" else "err"
    total_checks = history.get("total_checks", 0)
    total_notifs = history.get("total_notifications", 0)
    last_checked = _fmt_ts(history.get("last_check") or state.get("last_checked"))
    dates = state.get("dates", [])
    details = history.get("current_details", {})

    if dates:
        cards = []
        for d in dates:
            lines = details.get(d, [])
            items = "".join(f"<li>{html.escape(l.lstrip(' •'))}</li>" for l in lines)
            if not items:
                items = "<li class='muted'>Details will appear after the next poll</li>"
            link = f"{BASE_URL}/book/lessons?date={d}+00%3A00%3A00"
            cards.append(
                f"<div class='day-card'>"
                f"<div class='day-head'><a href='{html.escape(link)}' target='_blank' rel='noopener'>{html.escape(d)}</a></div>"
                f"<ul>{items}</ul>"
                f"</div>"
            )
        lessons_html = "".join(cards)
    else:
        lessons_html = "<p class='muted'>No available dates recorded yet.</p>"

    notifs = history.get("notifications", [])
    if notifs:
        notif_rows = "".join(
            f"<tr><td>{html.escape(_fmt_ts(n.get('timestamp')))}</td>"
            f"<td>{html.escape(str(n.get('kind', '')))}</td>"
            f"<td>{html.escape(', '.join(n.get('dates', [])))}</td></tr>"
            for n in notifs[:20]
        )
    else:
        notif_rows = "<tr><td colspan='3' class='muted'>No notifications logged yet.</td></tr>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>EC Pro Lesson Watcher</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0f172a; --card:#1e293b; --border:#334155; --text:#f1f5f9; --muted:#94a3b8; --accent:#38bdf8; --ok:#22c55e; --err:#ef4444; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:var(--bg); color:var(--text); padding:20px; }}
    .wrap {{ max-width: 1000px; margin: 0 auto; }}
    header {{ display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:12px; margin-bottom:20px; }}
    h1 {{ margin:0; font-size:1.4rem; }}
    .badge {{ padding:4px 12px; border-radius:999px; font-size:0.85rem; font-weight:600; text-transform:uppercase; }}
    .badge.ok {{ background:rgba(34,197,94,.18); color:var(--ok); border:1px solid rgba(34,197,94,.4); }}
    .badge.err {{ background:rgba(239,68,68,.18); color:var(--err); border:1px solid rgba(239,68,68,.4); }}
    .stats {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap:12px; margin-bottom:20px; }}
    .stat {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px; }}
    .stat .label {{ color:var(--muted); font-size:0.8rem; margin-bottom:4px; }}
    .stat .val {{ font-size:1.35rem; font-weight:700; }}
    .panel {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; margin-bottom:20px; }}
    .panel h2 {{ margin:0 0 12px; font-size:1.05rem; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:12px; }}
    .day-card {{ background:#0f172a; border:1px solid var(--border); border-radius:8px; padding:12px; }}
    .day-head a {{ color:var(--accent); font-weight:700; text-decoration:none; }}
    .day-head a:hover {{ text-decoration:underline; }}
    ul {{ margin:8px 0 0; padding-left:18px; font-size:0.9rem; line-height:1.45; }}
    table {{ width:100%; border-collapse:collapse; font-size:0.9rem; }}
    th, td {{ text-align:left; padding:8px; border-bottom:1px solid var(--border); }}
    th {{ color:var(--muted); font-weight:600; }}
    pre {{ margin:0; background:#020617; border:1px solid var(--border); border-radius:8px; padding:12px; overflow-x:auto; font-size:0.8rem; line-height:1.4; color:#cbd5e1; }}
    .muted {{ color:var(--muted); }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>EC Pro Lesson Watcher</h1>
      <span class="badge {badge_cls}">Service: {html.escape(status)}</span>
    </header>

    <div class="stats">
      <div class="stat"><div class="label">Available Dates</div><div class="val">{len(dates)}</div></div>
      <div class="stat"><div class="label">Total Checks</div><div class="val">{total_checks}</div></div>
      <div class="stat"><div class="label">Notifications Sent</div><div class="val">{total_notifs}</div></div>
      <div class="stat"><div class="label">Last Check</div><div class="val" style="font-size:1rem">{html.escape(last_checked)}</div></div>
    </div>

    <div class="panel">
      <h2>Currently Available Dates &amp; Lessons</h2>
      <div class="grid">{lessons_html}</div>
    </div>

    <div class="panel">
      <h2>Notification History</h2>
      <table>
        <thead><tr><th>Time</th><th>Type</th><th>Dates</th></tr></thead>
        <tbody>{notif_rows}</tbody>
      </table>
    </div>

    <div class="panel">
      <h2>Recent Watcher Logs <span class="muted" style="font-weight:400;font-size:0.8rem">(auto-refreshes every 30s)</span></h2>
      <pre>{html.escape(logs)}</pre>
    </div>
  </div>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/audio/"):
            filename = Path(self.path.split("?", 1)[0]).name
            audio_file = AUDIO_DIR / filename
            if audio_file.is_file() and audio_file.suffix == ".mp3":
                data = audio_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.end_headers()
            return

        if self.path == "/api/status":
            body = json.dumps(build_status_payload(), indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = render_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"Dashboard listening on http://0.0.0.0:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
