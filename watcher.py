"""EC Pro Lesson Availability Watcher.

Logs into an EC Pro booking portal using Playwright, queries /book/calendar/data
and /book/calendar/details for upcoming availability, compares against previously
seen dates, and sends push notifications (ntfy) plus optional Google Cast TTS
announcements when new dates appear.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page, sync_playwright

APP_DIR = Path(__file__).parent
STATE_FILE = APP_DIR / "seen_dates.json"
HISTORY_FILE = APP_DIR / "history.json"
AUDIO_DIR = APP_DIR / "audio"
DEBUG_DIR = APP_DIR / "debug"
DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def load_config() -> dict[str, Any]:
    """Load and validate configuration from environment variables."""
    load_dotenv()
    base_url = os.getenv("ECPRO_BASE_URL", "").strip().rstrip("/")
    email = os.getenv("ECPRO_EMAIL", "").strip()
    password = os.getenv("ECPRO_PASSWORD", "").strip()
    ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
    ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")
    timezone_name = os.getenv("TZ", "Europe/London").strip()
    months_ahead = int(os.getenv("MONTHS_AHEAD", "3"))
    cast_devices = [
        d.strip()
        for d in os.getenv("CAST_DEVICES", "").split(",")
        if d.strip()
    ]
    cast_quiet_start = int(os.getenv("CAST_QUIET_START", "22"))
    cast_quiet_end = int(os.getenv("CAST_QUIET_END", "7"))
    dashboard_url = os.getenv("DASHBOARD_URL", "http://localhost:8080").strip().rstrip("/")

    missing = [
        name
        for name, val in [
            ("ECPRO_BASE_URL", base_url),
            ("ECPRO_EMAIL", email),
            ("ECPRO_PASSWORD", password),
            ("NTFY_TOPIC", ntfy_topic),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    return {
        "base_url": base_url,
        "email": email,
        "password": password,
        "ntfy_topic": ntfy_topic,
        "ntfy_server": ntfy_server,
        "tz": ZoneInfo(timezone_name),
        "months_ahead": max(1, months_ahead),
        "cast_devices": cast_devices,
        "cast_quiet_start": cast_quiet_start,
        "cast_quiet_end": cast_quiet_end,
        "dashboard_url": dashboard_url,
    }


def send_ntfy(
    server: str,
    topic: str,
    title: str,
    message: str,
    click_url: str | None = None,
    priority: str = "high",
    tags: str = "horse_racing,calendar",
) -> None:
    """Send a push notification via ntfy."""
    url = f"{server}/{topic}"
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
    }
    if click_url:
        headers["Click"] = click_url

    resp = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=15)
    resp.raise_for_status()


def add_months(start: date, offset: int) -> date:
    """Return the first day of the month `offset` months after `start`."""
    month_index = (start.month - 1) + offset
    year = start.year + month_index // 12
    month = (month_index % 12) + 1
    return date(year, month, 1)


def login(page: Page, base_url: str, email: str, password: str) -> None:
    """Log into the EC Pro portal."""
    page.goto(f"{base_url}/login", wait_until="domcontentloaded", timeout=30000)

    if "/login" not in page.url:
        return

    page.fill("#login-email", email)
    page.fill("#login-password", password)

    with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
        page.click('form[action$="/login"] button[type="submit"]')

    if "/login" in page.url:
        error_text = ""
        alert = page.locator(".alert-danger, .invalid-feedback").first
        if alert.count() > 0:
            error_text = alert.inner_text().strip()
        raise RuntimeError(f"Login failed (still on /login). {error_text}".strip())


def parse_utc_to_local_date(start_val: str, tz: ZoneInfo) -> str | None:
    """Convert EC Pro's UTC start timestamp into a local YYYY-MM-DD date."""
    if not start_val:
        return None
    cleaned = start_val.strip().replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(cleaned[:19], fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(tz).date().isoformat()
        except ValueError:
            continue
    m = DATE_RE.search(start_val)
    return m.group(1) if m else None


def _format_time(ts: Any, tz: ZoneInfo) -> str:
    """Format an ISO/SQL timestamp string to HH:MM in the configured timezone."""
    if not ts:
        return ""
    cleaned = str(ts).strip().replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(cleaned[:19], fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(tz).strftime("%H:%M")
        except ValueError:
            continue
    return cleaned[11:16] if len(cleaned) >= 16 else ""


def extract_available_dates_from_calendar(
    events: list[dict[str, Any]], tz: ZoneInfo
) -> set[str]:
    """Return YYYY-MM-DD dates that have at least one event with available space."""
    today_str = datetime.now(tz).date().isoformat()
    available: set[str] = set()

    for event in events:
        if not isinstance(event, dict) or not event.get("hasSpace"):
            continue
        day_str = parse_utc_to_local_date(str(event.get("start", "")), tz)
        if day_str and day_str >= today_str:
            available.add(day_str)

    return available


def fetch_event_details(
    context: BrowserContext, base_url: str, event_id: Any, category: Any
) -> dict[str, Any]:
    """Fetch instructor and start/end time for a specific calendar event."""
    try:
        resp = context.request.get(
            f"{base_url}/book/calendar/details",
            params={"id": event_id, "category": category},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            timeout=15000,
        )
        if resp.ok:
            data = resp.json()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def build_details_by_date(
    context: BrowserContext,
    base_url: str,
    events: list[dict[str, Any]],
    tz: ZoneInfo,
) -> dict[str, list[str]]:
    """Build per-date lesson and instructor summaries for all events with spaces."""
    today_str = datetime.now(tz).date().isoformat()
    details_by_date: dict[str, list[str]] = {}

    for event in events:
        if not isinstance(event, dict) or not event.get("hasSpace"):
            continue
        day_str = parse_utc_to_local_date(str(event.get("start", "")), tz)
        if not day_str or day_str < today_str:
            continue

        title = " ".join(str(event.get("title") or "Lesson").split())
        ext = event.get("extendedProps") or {}
        event_id = ext.get("id")
        category = ext.get("category")

        time_range = ""
        instructor = ""
        if event_id and category:
            info = fetch_event_details(context, base_url, event_id, category)
            starts_at = str(info.get("startsAt") or "").strip()
            ends_at = str(info.get("endsAt") or "").strip()
            start_time = starts_at.split(" ", 1)[1] if " " in starts_at else starts_at
            if start_time and ends_at:
                time_range = f"{start_time}-{ends_at}"
            elif start_time:
                time_range = start_time
            instructor = str(info.get("instructor") or "").strip()

        if not time_range and not event.get("allDay"):
            time_range = _format_time(event.get("start"), tz)

        if title.startswith("Potential Private Availability with:"):
            title = "Private (" + title.split(":", 1)[1].strip() + ")"

        line = f"  • {time_range} {title}".replace("  •  ", "  • ")
        if instructor:
            line += f" ({instructor})"

        details_by_date.setdefault(day_str, [])
        if line not in details_by_date[day_str]:
            details_by_date[day_str].append(line)

    return details_by_date


def _poll_once_in_context(
    context: BrowserContext,
    page: Page,
    cfg: dict[str, Any],
    debug: bool = False,
) -> tuple[set[str], dict[str, list[str]]]:
    """Query /book/calendar/data using an existing Playwright context, re-logging in if needed."""
    tz = cfg["tz"]
    base_url = cfg["base_url"]
    today = datetime.now(tz).date()
    end_date = add_months(today, cfg["months_ahead"])
    start_param = f"{today.isoformat()} 00:00:00"
    end_param = f"{end_date.isoformat()} 23:59:59"

    def _query() -> Any:
        return context.request.get(
            f"{base_url}/book/calendar/data",
            params={"start": start_param, "end": end_param},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            timeout=30000,
        )

    resp = _query()
    if resp.status in (401, 403, 419) or "/login" in resp.url:
        print("Session expired; re-logging in...")
        login(page, base_url, cfg["email"], cfg["password"])
        page.goto(f"{base_url}/book/lessons", wait_until="domcontentloaded", timeout=30000)
        resp = _query()

    if not resp.ok:
        raise RuntimeError(f"Calendar API error {resp.status}: {resp.status_text}")

    events = resp.json()
    if not isinstance(events, list):
        raise RuntimeError(f"Unexpected /book/calendar/data response type: {type(events)}")

    available_dates = extract_available_dates_from_calendar(events, tz)
    details_by_date = build_details_by_date(context, base_url, events, tz)

    if debug:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_DIR / "calendar_data.json").write_text(
            json.dumps(events, indent=2), encoding="utf-8"
        )

    print(f"Fetched {len(events)} calendar event(s) from {start_param} to {end_param}.")
    return available_dates, details_by_date


def fetch_available_dates(
    cfg: dict[str, Any], debug: bool = False
) -> tuple[set[str], dict[str, list[str]]]:
    """Log in and query /book/calendar/data across upcoming months."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        login(page, cfg["base_url"], cfg["email"], cfg["password"])
        page.goto(f"{cfg['base_url']}/book/lessons", wait_until="domcontentloaded", timeout=30000)
        result = _poll_once_in_context(context, page, cfg, debug=debug)
        browser.close()
    return result


def load_state() -> dict[str, Any]:
    """Load previously seen dates from seen_dates.json."""
    if not STATE_FILE.exists():
        return {"initialized": False, "dates": [], "last_checked": None}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(dates: set[str]) -> None:
    """Persist seen dates to seen_dates.json."""
    payload = {
        "initialized": True,
        "dates": sorted(dates),
        "last_checked": datetime.now(timezone.utc).isoformat(),
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def record_history(
    current_dates: set[str],
    new_dates: list[str],
    details_by_date: dict[str, list[str]],
    notified: bool,
    notification_kind: str | None = None,
) -> None:
    """Persist check history and current lesson details for the web dashboard."""
    try:
        if HISTORY_FILE.exists():
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        else:
            data = {"total_checks": 0, "total_notifications": 0, "notifications": [], "checks": []}
    except Exception:
        data = {"total_checks": 0, "total_notifications": 0, "notifications": [], "checks": []}

    now_iso = datetime.now(timezone.utc).isoformat()
    data["total_checks"] = int(data.get("total_checks", 0)) + 1
    data["last_check"] = now_iso
    data["current_details"] = {d: details_by_date.get(d, []) for d in sorted(current_dates)}

    checks = data.get("checks", [])
    checks.insert(
        0,
        {
            "timestamp": now_iso,
            "available_count": len(current_dates),
            "new_dates": new_dates,
            "notified": notified,
        },
    )
    data["checks"] = checks[:100]

    if notified:
        data["total_notifications"] = int(data.get("total_notifications", 0)) + 1
        notifs = data.get("notifications", [])
        notifs.insert(
            0,
            {
                "timestamp": now_iso,
                "kind": notification_kind or "new_dates",
                "dates": new_dates if new_dates else sorted(current_dates),
            },
        )
        data["notifications"] = notifs[:50]

    HISTORY_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _in_quiet_hours(hour: int, start_hour: int, end_hour: int) -> bool:
    """Return True if `hour` falls within quiet hours (supports overnight ranges)."""
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def announce_on_google_speakers(
    new_dates: list[str], cfg: dict[str, Any], force: bool = False
) -> None:
    """Generate a TTS MP3 and cast it to configured Google speakers."""
    devices = cfg.get("cast_devices") or []
    if not devices:
        return

    now_local = datetime.now(cfg["tz"])
    if not force and _in_quiet_hours(
        now_local.hour, cfg["cast_quiet_start"], cfg["cast_quiet_end"]
    ):
        print(
            f"Skipping Google speaker announcement during quiet hours "
            f"({cfg['cast_quiet_start']:02d}:00-{cfg['cast_quiet_end']:02d}:00)."
        )
        return

    try:
        from gtts import gTTS
        import pychromecast
    except ImportError:
        print("gTTS/pychromecast not installed; skipping speaker announcement.")
        return

    try:
        readable_dates = [
            datetime.strptime(d, "%Y-%m-%d").strftime("%A %-d %B")
            for d in new_dates[:3]
        ]
    except Exception:
        readable_dates = new_dates[:3]

    if len(new_dates) > 3:
        dates_phrase = f"{', '.join(readable_dates)}, and {len(new_dates) - 3} more"
    else:
        dates_phrase = ", ".join(readable_dates)

    text = f"Attention! New riding lesson dates are available for {dates_phrase}."
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    mp3_path = AUDIO_DIR / "alert.mp3"
    gTTS(text=text, lang="en", tld="co.uk").save(str(mp3_path))
    media_url = f"{cfg['dashboard_url']}/audio/alert.mp3?t={int(time.time())}"

    try:
        chromecasts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=devices, discovery_timeout=8
        )
        for cc in chromecasts:
            try:
                cc.wait(timeout=5)
                mc = cc.media_controller
                mc.play_media(media_url, "audio/mpeg")
                mc.block_until_active(timeout=5)
                print(f"Cast announcement to: {cc.name}")
            except Exception as exc:
                print(f"Failed casting to {cc.name}: {exc}")
        browser.stop_discovery()
    except Exception as exc:
        print(f"Google speaker discovery error: {exc}")


def _date_sections(
    dates: list[str], details_by_date: dict[str, list[str]]
) -> list[str]:
    """Build per-date text blocks."""
    sections: list[str] = []
    for d in dates:
        lines = details_by_date.get(d)
        if lines:
            sections.append(f"{d}:\n" + "\n".join(lines))
        else:
            sections.append(f"- {d}")
    return sections


def format_dates_with_details(
    dates: list[str], details_by_date: dict[str, list[str]]
) -> str:
    """Format dates with optional per-lesson/instructor details."""
    return "\n".join(_date_sections(dates, details_by_date))


def send_ntfy_sections(
    server: str,
    topic: str,
    title: str,
    header: str,
    sections: list[str],
    click_url: str | None = None,
    priority: str = "high",
    max_bytes: int = 3500,
) -> None:
    """Send ntfy notification(s), splitting by date so ntfy never converts to a .txt file."""
    chunks: list[str] = []
    current = header
    for sec in sections:
        candidate = f"{current}\n{sec}" if current else sec
        if len(candidate.encode("utf-8")) > max_bytes and current != header:
            chunks.append(current)
            current = sec
        else:
            current = candidate
    if current:
        chunks.append(current)

    total = len(chunks)
    for idx, body in enumerate(chunks, start=1):
        chunk_title = f"{title} ({idx}/{total})" if total > 1 else title
        send_ntfy(
            server=server,
            topic=topic,
            title=chunk_title,
            message=body,
            click_url=click_url,
            priority=priority,
        )


def _process_check(
    cfg: dict[str, Any],
    current_dates: set[str],
    details_by_date: dict[str, list[str]],
    dry_run: bool,
) -> None:
    """Compare current dates to saved state, notify if needed, and update state."""
    state = load_state()
    previous_dates = set(state.get("dates", []))
    new_dates = sorted(current_dates - previous_dates)
    base_url = cfg["base_url"]

    print(f"Found {len(current_dates)} available date(s): {sorted(current_dates)}")
    print(f"New date(s): {new_dates}")

    if dry_run:
        if details_by_date:
            print(format_dates_with_details(sorted(current_dates), details_by_date))
        return

    if not state.get("initialized"):
        if current_dates:
            send_ntfy_sections(
                server=cfg["ntfy_server"],
                topic=cfg["ntfy_topic"],
                title="Lesson Watcher Active",
                header="Monitoring started. Currently available:",
                sections=_date_sections(sorted(current_dates), details_by_date),
                click_url=f"{base_url}/book/lessons",
                priority="default",
            )
        else:
            send_ntfy(
                server=cfg["ntfy_server"],
                topic=cfg["ntfy_topic"],
                title="Lesson Watcher Active",
                message="Monitoring started. Currently available: none found right now",
                click_url=f"{base_url}/book/lessons",
                priority="default",
            )
        save_state(current_dates)
        record_history(current_dates, new_dates, details_by_date, notified=True, notification_kind="initialized")
        return

    notified = False
    if new_dates:
        send_ntfy_sections(
            server=cfg["ntfy_server"],
            topic=cfg["ntfy_topic"],
            title=f"New Riding Lesson Dates ({len(new_dates)})",
            header="New dates available:",
            sections=_date_sections(new_dates, details_by_date),
            click_url=f"{base_url}/book/lessons?date={new_dates[0]}+00%3A00%3A00",
            priority="high",
        )
        announce_on_google_speakers(new_dates, cfg)
        notified = True

    save_state(current_dates)
    record_history(current_dates, new_dates, details_by_date, notified=notified, notification_kind="new_dates" if notified else None)


def run_loop(cfg: dict[str, Any], interval_seconds: int, dry_run: bool, debug: bool) -> None:
    """Continuously poll EC Pro using a persistent browser session."""
    print(f"Starting loop mode (polling every {interval_seconds}s)...")
    while True:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/130.0.0.0 Safari/537.36"
                    )
                )
                page = context.new_page()
                login(page, cfg["base_url"], cfg["email"], cfg["password"])
                page.goto(f"{cfg['base_url']}/book/lessons", wait_until="domcontentloaded", timeout=30000)

                while True:
                    try:
                        current_dates, details_by_date = _poll_once_in_context(
                            context, page, cfg, debug=debug
                        )
                        _process_check(cfg, current_dates, details_by_date, dry_run)
                    except Exception as exc:
                        print(f"[{datetime.now(timezone.utc).isoformat()}] Poll error: {exc}")
                    time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("Stopping loop.")
            return
        except Exception as exc:
            retry_delay = min(interval_seconds, 30)
            print(f"Browser session error: {exc}. Restarting in {retry_delay}s...")
            time.sleep(retry_delay)


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch EC Pro for new lesson dates.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check dates and print results without updating state or notifying.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save raw /book/calendar/data JSON response to ./debug/.",
    )
    parser.add_argument(
        "--loop",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Run continuously, polling every N seconds with one persistent login session.",
    )
    parser.add_argument(
        "--test-cast",
        action="store_true",
        help="Play a test announcement on configured Google speakers and exit.",
    )
    args = parser.parse_args()

    cfg = load_config()

    if args.test_cast:
        today_str = datetime.now(cfg["tz"]).date().isoformat()
        announce_on_google_speakers([today_str], cfg, force=True)
        return 0

    if args.loop > 0:
        run_loop(cfg, interval_seconds=args.loop, dry_run=args.dry_run, debug=args.debug)
        return 0

    current_dates, details_by_date = fetch_available_dates(cfg, debug=args.debug)
    _process_check(cfg, current_dates, details_by_date, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
