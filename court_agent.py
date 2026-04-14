"""
Oakland Pickleball Court Availability Agent
Scrapes available slots from PerfectMind using pure Playwright — no LLM.
Dry-run only: finds Reserve buttons but does NOT click them.

Setup:
  1. Copy .env.example to .env and fill in your values
  2. pip install -r requirements.txt
  3. playwright install chrome
  4. python court_agent.py [YYYY-MM-DD]  (defaults to tomorrow)
"""

import asyncio
import json
import os
import random
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext

load_dotenv()

TARGET_URL = "https://cityofoakland.perfectmind.com/"
COURT_NAME = "Bushrod Tennis Court # 1"


def get_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        print(f"ERROR: {key} not set in .env", file=sys.stderr)
        sys.exit(1)
    return val


def merge_slots(slots: list) -> list:
    """Merge adjacent time blocks into continuous ranges."""
    if not slots:
        return slots
    sorted_slots = sorted(slots, key=lambda s: s["start"])
    merged = [dict(sorted_slots[0])]
    for slot in sorted_slots[1:]:
        last = merged[-1]
        if slot["start"] == last["end"]:
            last["end"] = slot["end"]
        else:
            merged.append(dict(slot))
    return merged


def to_24h(time_str: str) -> str:
    """Convert '2:00 PM' to '14:00'."""
    for fmt in ("%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(time_str.strip(), fmt).strftime("%H:%M")
        except ValueError:
            continue
    return time_str


async def login(page: Page, username: str, password: str) -> None:
    await page.wait_for_selector("#textBoxUsername", timeout=10000)
    await page.fill("#textBoxUsername", username)
    await page.fill("#textBoxPassword", password)
    await page.click("#buttonLogin")
    await page.wait_for_load_state("networkidle", timeout=15000)


async def navigate_to_sports_rentals(page: Page) -> None:
    await asyncio.sleep(random.uniform(0.8, 1.5))
    await page.click("a:has-text('Facility Reservation')")
    await page.wait_for_load_state("networkidle", timeout=10000)

    await asyncio.sleep(random.uniform(0.8, 1.5))
    await page.click("a:has-text('Sports Rentals')")
    await page.wait_for_load_state("networkidle", timeout=10000)


async def search_bushrod(page: Page, start_date: str, end_date: str) -> None:
    await page.wait_for_selector("#facilityFilter\\.KeyWord", timeout=10000)
    await page.fill("#facilityFilter\\.KeyWord", "Bushrod")

    def fmt(d: str) -> str:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return dt.strftime("%#m/%#d/%Y") if sys.platform == "win32" else dt.strftime("%-m/%-d/%Y")

    start_fmt = fmt(start_date)
    end_fmt = fmt(end_date)

    await asyncio.sleep(random.uniform(0.5, 1.0))
    await page.click("#from")
    await page.fill("#from", start_fmt)
    await page.press("#from", "Tab")
    await asyncio.sleep(random.uniform(0.5, 1.0))
    await page.click("#to")
    await page.fill("#to", end_fmt)
    await page.press("#to", "Tab")
    await asyncio.sleep(random.uniform(0.5, 1.0))
    await page.click("button:has-text('Check Availability'), [role='button']:has-text('Check Availability')")
    await page.wait_for_load_state("networkidle", timeout=30000)
    await asyncio.sleep(random.uniform(1.5, 2.5))


async def select_bushrod_court_1(page: Page) -> None:
    await page.wait_for_selector("text=Bushrod Tennis Court # 1", timeout=30000)
    await asyncio.sleep(random.uniform(0.8, 1.5))

    # Find the Choose button in the same row/container as "Bushrod Tennis Court # 1"
    # Use evaluate to find the exact element and its sibling/nearby Choose button
    chosen = await page.evaluate("""
    () => {
        // Find all elements containing the exact court name
        const allEls = Array.from(document.querySelectorAll('*'));
        const courtEl = allEls.find(el =>
            el.children.length === 0 &&
            (el.innerText || el.textContent || '').trim() === 'Bushrod Tennis Court # 1'
        );
        if (!courtEl) return false;

        // Walk up to find a row/container that also has a Choose button
        let ancestor = courtEl.parentElement;
        for (let i = 0; i < 10; i++) {
            if (!ancestor) break;
            const btn = ancestor.querySelector('button, a, [role="button"]');
            if (btn && (btn.innerText || btn.textContent || '').trim() === 'Choose') {
                btn.click();
                return true;
            }
            ancestor = ancestor.parentElement;
        }
        return false;
    }
    """)

    if not chosen:
        raise RuntimeError("Could not find Choose button for Bushrod Tennis Court # 1")

    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_selector(".k-scheduler, .k-scheduler-content", timeout=15000)
    await asyncio.sleep(2)


async def jump_to_date(page: Page, target_date: str) -> None:
    """Use the Jump To Date calendar to navigate to the target date's week."""
    dt = datetime.strptime(target_date, "%Y-%m-%d")

    # Close any open calendar popup first by pressing Escape
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.3)

    # Click "Jump To Date" link
    await page.click("text=Jump To Date")
    await asyncio.sleep(1.0)

    # Wait for the calendar popup to appear
    await page.wait_for_selector(".k-calendar", timeout=10000)
    await asyncio.sleep(0.5)

    # Dump calendar DOM to understand selectors (first call only for debugging)
    cal_html = await page.evaluate("""
    () => {
        const cal = document.querySelector('.k-calendar');
        return cal ? cal.outerHTML.substring(0, 2000) : 'not found';
    }
    """)
    print(f"CALENDAR DOM: {cal_html}", flush=True)

    # Navigate to the correct month using JS to read and click arrows reliably
    for _ in range(24):  # max 24 month navigations (2 years)
        # Read the current month/year from the calendar header via JS
        month_text = await page.evaluate("""
        () => {
            const cal = document.querySelector('.k-calendar');
            if (!cal) return '';
            const el = cal.querySelector('.k-nav-fast, .k-calendar-title, .k-title');
            return el ? el.innerText.trim() : '';
        }
        """)

        if not month_text:
            break

        try:
            displayed = datetime.strptime(month_text, "%B %Y")
        except ValueError:
            break

        if displayed.year == dt.year and displayed.month == dt.month:
            break

        # Click next or prev arrow via JS
        if displayed.year < dt.year or (displayed.year == dt.year and displayed.month < dt.month):
            clicked = await page.evaluate("""
            () => {
                const cal = document.querySelector('.k-calendar');
                const btn = cal && cal.querySelector('a[data-action="next"]');
                if (btn) { btn.click(); return true; }
                return false;
            }
            """)
        else:
            clicked = await page.evaluate("""
            () => {
                const cal = document.querySelector('.k-calendar');
                const btn = cal && cal.querySelector('a[data-action="prev"]');
                if (btn) { btn.click(); return true; }
                return false;
            }
            """)
        if not clicked:
            raise RuntimeError(f"Could not find calendar navigation arrow. Current month: {month_text}")
        await asyncio.sleep(0.5)

    # Wait for the calendar to settle on the right month (Kendo re-renders async)
    for _ in range(20):
        month_text = await page.evaluate("""
        () => {
            const cal = document.querySelector('.k-calendar');
            if (!cal) return '';
            const el = cal.querySelector('.k-nav-fast, .k-calendar-title, .k-title');
            return el ? el.innerText.trim() : '';
        }
        """)
        try:
            displayed = datetime.strptime(month_text, "%B %Y")
            if displayed.year == dt.year and displayed.month == dt.month:
                break
        except ValueError:
            pass
        await asyncio.sleep(0.2)
    else:
        raise RuntimeError(f"Calendar never settled on {dt.strftime('%B %Y')}, last saw: {month_text}")

    await asyncio.sleep(0.4)  # let cells fully render

    # Build the data-value Kendo uses: e.g. 2026/4/7
    data_value = f"{dt.year}/{dt.month}/{dt.day}"
    day_str = str(dt.day)

    clicked_day = await page.evaluate(f"""
    () => {{
        // Strategy 1: Kendo data-value attribute on td
        const byValue = document.querySelector('.k-calendar td[data-value="{data_value}"]');
        if (byValue) {{
            const link = byValue.querySelector('a') || byValue;
            link.click();
            return 'data-value';
        }}

        // Strategy 2: find <a> inside td whose text matches day number, not in other-month
        const tds = Array.from(document.querySelectorAll('.k-calendar td'));
        for (const td of tds) {{
            if (td.classList.contains('k-other-month') || td.classList.contains('k-out-range-day')) continue;
            const link = td.querySelector('a');
            if (link && (link.innerText || link.textContent || '').trim() === '{day_str}') {{
                link.click();
                return 'link-text';
            }}
            // day number directly in td
            if ((td.innerText || td.textContent || '').trim() === '{day_str}') {{
                td.click();
                return 'td-text';
            }}
        }}
        return false;
    }}
    """)

    if not clicked_day:
        # Dump cells to stderr for diagnosis
        cells = await page.evaluate("""
        () => Array.from(document.querySelectorAll('.k-calendar td')).map(td => ({
            cls: td.className,
            dv: td.getAttribute('data-value'),
            txt: (td.innerText || td.textContent || '').trim()
        }))
        """)
        print(f"DAY CLICK FAILED. data-value={data_value} day_str={day_str}", file=sys.stderr)
        print(f"Calendar cells: {cells}", file=sys.stderr)
        raise RuntimeError(f"Could not find day {day_str} in calendar (data-value={data_value})")

    await asyncio.sleep(0.5)

    # Wait for calendar to update
    await page.wait_for_load_state("networkidle", timeout=15000)
    await asyncio.sleep(1.5)


async def scrape_calendar(page: Page, target_date: str) -> list:
    """Read available Reserve slots directly from the DOM."""

    # Strategy 1: data-start / data-end attributes on Reserve elements
    slots_from_attrs = await page.evaluate("""
    (targetDate) => {
        const results = [];
        document.querySelectorAll('[data-start][data-end]').forEach(el => {
            const text = (el.innerText || el.textContent || '').trim();
            if (text === 'Reserve') {
                const start = el.getAttribute('data-start');
                const end = el.getAttribute('data-end');
                if (start && start.startsWith(targetDate)) {
                    results.push({ start, end });
                }
            }
        });
        return results;
    }
    """, target_date)

    if slots_from_attrs:
        slots = []
        for s in slots_from_attrs:
            try:
                start = datetime.fromisoformat(s["start"]).strftime("%H:%M")
                end = datetime.fromisoformat(s["end"]).strftime("%H:%M")
                slots.append({"start": start, "end": end, "status": "available"})
            except ValueError:
                pass
        return merge_slots(slots)

    # Strategy 2: title attributes with time ranges (e.g. "06:00 AM-06:30 AM")
    slots_from_title = await page.evaluate("""
    (targetDate) => {
        const results = [];
        const dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
        const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        const dt = new Date(targetDate + 'T12:00:00');
        const dayName = dayNames[dt.getDay()];
        const monthName = monthNames[dt.getMonth()];
        const dayNum = dt.getDate().toString();

        let colLeft = null, colRight = null;
        document.querySelectorAll('th, .k-scheduler-header td').forEach(el => {
            const text = el.innerText || '';
            if (text.includes(dayName) && text.includes(monthName) && text.includes(dayNum)) {
                const rect = el.getBoundingClientRect();
                colLeft = rect.left - 5;
                colRight = rect.right + 5;
            }
        });

        const seen = new Set();
        document.querySelectorAll('[title]').forEach(el => {
            const text = (el.innerText || el.textContent || '').trim();
            const title = el.getAttribute('title') || '';
            if (text === 'Reserve' && title.match(/\\d+:\\d+\\s*[AP]M/)) {
                const rect = el.getBoundingClientRect();
                if (colLeft !== null && (rect.left < colLeft || rect.left > colRight)) return;
                if (!seen.has(title)) {
                    seen.add(title);
                    results.push(title);
                }
            }
        });
        return results;
    }
    """, target_date)

    if slots_from_title:
        slots = []
        for title in slots_from_title:
            m = re.search(r'(\d+:\d+\s*[AP]M)\s*[-–]?\s*(\d+:\d+\s*[AP]M)', title)
            if m:
                try:
                    slots.append({
                        "start": to_24h(m.group(1)),
                        "end": to_24h(m.group(2)),
                        "status": "available"
                    })
                except ValueError:
                    pass
        if slots:
            return merge_slots(slots)

    # Strategy 3: x/y position mapping
    positional = await page.evaluate("""
    (targetDateStr) => {
        const targetDt = new Date(targetDateStr + 'T12:00:00');
        const dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
        const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        const dayName = dayNames[targetDt.getDay()];
        const monthName = monthNames[targetDt.getMonth()];
        const dayNum = targetDt.getDate().toString();

        let colLeft = null, colRight = null;
        document.querySelectorAll('th, .k-scheduler-header td').forEach(el => {
            const text = el.innerText || '';
            if (text.includes(dayName) && text.includes(monthName) && text.includes(dayNum)) {
                const rect = el.getBoundingClientRect();
                colLeft = rect.left - 5;
                colRight = rect.right + 5;
            }
        });

        const timeRows = [];
        document.querySelectorAll('.k-scheduler-times tr, .k-time-cell').forEach(el => {
            const text = (el.innerText || '').trim();
            if (/\\d+:\\d+/.test(text)) {
                const rect = el.getBoundingClientRect();
                timeRows.push({ text, y: Math.round(rect.top) });
            }
        });

        const seen = new Set();
        const slots = [];
        document.querySelectorAll('a, div, span').forEach(el => {
            if (el.querySelector('a, div, span')) return;
            const text = (el.innerText || el.textContent || '').trim();
            if (text === 'Reserve') {
                const rect = el.getBoundingClientRect();
                const x = rect.left;
                const y = Math.round(rect.top);
                if (colLeft !== null && (x < colLeft || x > colRight)) return;
                if (seen.has(y)) return;
                seen.add(y);
                const closest = timeRows.reduce((a, b) =>
                    Math.abs(a.y - rect.top) < Math.abs(b.y - rect.top) ? a : b, timeRows[0]);
                slots.push({ timeLabel: closest ? closest.text : '', y });
            }
        });
        return slots;
    }
    """, target_date)

    if positional:
        slots = []
        for s in positional:
            m = re.search(r'(\d+:\d+\s*[AP]M)', s["timeLabel"])
            if m:
                try:
                    start = to_24h(m.group(1))
                    dt = datetime.strptime(start, "%H:%M")
                    end_dt = dt.replace(minute=0) if dt.minute == 30 else dt.replace(minute=30)
                    if dt.minute == 30:
                        end_dt = dt.replace(hour=dt.hour + 1, minute=0)
                    end = end_dt.strftime("%H:%M")
                    slots.append({"start": start, "end": end, "status": "available"})
                except ValueError:
                    pass
        return merge_slots(slots)

    return []


# ── Persistent browser session ──────────────────────────────────────────────

class BrowserSession:
    """Keeps a single browser + logged-in page alive across requests.
    Automatically recovers from errors by re-launching and re-logging in.
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page: Page | None = None
        self._on_calendar = False  # True once we've reached the Bushrod calendar
        self._lock = asyncio.Lock()
        self._username = None
        self._password = None

    async def _launch(self) -> None:
        """Launch browser and log in."""
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        headless = sys.platform != "win32"
        extra_args = ["--no-sandbox", "--disable-dev-shm-usage"] if not sys.platform == "win32" else []
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=extra_args,
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
        )
        self._page = await self._context.new_page()

        self._on_calendar = False

    async def _navigate_to_calendar(self) -> None:
        """Full navigation: login → Sports Rentals → Bushrod → calendar."""
        page = self._page
        # Use tomorrow so the search always returns results (today may be past booking window)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()

        await page.goto(TARGET_URL, wait_until="networkidle", timeout=20000)
        if await page.query_selector("#textBoxUsername"):
            await login(page, self._username, self._password)

        await navigate_to_sports_rentals(page)
        await search_bushrod(page, tomorrow, tomorrow)
        await select_bushrod_court_1(page)
        self._on_calendar = True

    async def _ensure_calendar(self) -> None:
        """Make sure we're on the calendar page, launching/navigating if needed."""
        if self._page is None or self._page.is_closed():
            await self._launch()
            await self._navigate_to_calendar()
            return

        if not self._on_calendar:
            await self._navigate_to_calendar()

    async def get_slots(self, target_date: str) -> list:
        """Get available slots for target_date, with auto-recovery on failure."""
        async with self._lock:
            for attempt in range(3):
                try:
                    await self._ensure_calendar()
                    await jump_to_date(self._page, target_date)
                    slots = await scrape_calendar(self._page, target_date)
                    return slots
                except Exception as e:
                    print(f"Attempt {attempt + 1} failed: {e}", file=sys.stderr)
                    self._on_calendar = False
                    if attempt < 2:
                        print("Recovering: re-launching browser...", file=sys.stderr)
                        await self._close_browser()
                        await asyncio.sleep(2)
                        await self._launch()
            raise RuntimeError("Failed to get slots after 3 attempts")

    async def _close_browser(self) -> None:
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._page = None
        self._on_calendar = False

    async def close(self) -> None:
        await self._close_browser()
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None


# Singleton used by api.py
_session: BrowserSession | None = None


def get_session() -> BrowserSession:
    global _session
    if _session is None:
        _session = BrowserSession()
    return _session


def set_credentials(username: str, password: str) -> None:
    sess = get_session()
    sess._username = username
    sess._password = password


# ── Standalone CLI ───────────────────────────────────────────────────────────

async def run_once(target_date: str) -> dict:
    """Used by the CLI to run a single scrape and exit."""
    chrome_profile = os.getenv("CHROME_PROFILE_PATH", "/tmp/chrome-profile")
    username = get_env("OAKLAND_USERNAME")
    password = get_env("OAKLAND_PASSWORD")

    async with async_playwright() as p:
        if sys.platform == "win32":
            context = await p.chromium.launch_persistent_context(
                user_data_dir=chrome_profile,
                channel="chrome",
                headless=False,
                args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
                no_viewport=True,
            )
        else:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(viewport={"width": 1280, "height": 900})

        page = context.pages[0] if context.pages else await context.new_page()

        try:
            today = date.today().isoformat()
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=20000)
            if await page.query_selector("#textBoxUsername"):
                await login(page, username, password)
            await navigate_to_sports_rentals(page)
            await search_bushrod(page, today, today)
            await select_bushrod_court_1(page)
            await jump_to_date(page, target_date)
            slots = await scrape_calendar(page, target_date)
        finally:
            await context.close()

    return {
        "city": "Oakland",
        "start_date": target_date,
        "end_date": target_date,
        "scraped_at": datetime.now().isoformat(),
        "court_name": COURT_NAME,
        "days": [{"date": target_date, "available_slots": slots}],
        "dry_run": True,
    }


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        target = sys.argv[1]
    else:
        target = (date.today() + timedelta(days=1)).isoformat()

    data = asyncio.run(run_once(target))
    print(json.dumps(data, indent=2))
