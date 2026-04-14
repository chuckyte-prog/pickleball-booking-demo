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

    court_row = page.locator("text=Bushrod Tennis Court 1").locator("xpath=ancestor::tr | ancestor::div[contains(@class,'row')] | ancestor::li").first
    choose_btn = court_row.locator("button:has-text('Choose'), a:has-text('Choose'), [role='button']:has-text('Choose')")

    if await choose_btn.count() == 0:
        all_choose = page.locator("button:has-text('Choose'), a:has-text('Choose')")
        count = await all_choose.count()
        if count > 0:
            await all_choose.first.click()
    else:
        await choose_btn.first.click()

    await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_selector(".k-scheduler, .k-scheduler-content", timeout=15000)
    await asyncio.sleep(2)


async def jump_to_date(page: Page, target_date: str) -> None:
    """Use the Jump To Date calendar to navigate to the target date's week."""
    dt = datetime.strptime(target_date, "%Y-%m-%d")

    # Click "Jump To Date" link
    await page.click("text=Jump To Date")
    await asyncio.sleep(0.5)

    # Wait for the calendar popup to appear
    await page.wait_for_selector(".k-calendar, .k-popup", timeout=10000)

    # Navigate to the correct month if needed
    for _ in range(12):  # max 12 month navigations
        # Check current month/year displayed
        month_text = await page.locator(".k-nav-fast, .k-calendar-title, .k-title").first.inner_text()
        displayed = datetime.strptime(month_text.strip(), "%B %Y") if month_text else None
        if displayed and displayed.year == dt.year and displayed.month == dt.month:
            break
        # Click next or prev arrow
        if displayed and (displayed.year < dt.year or (displayed.year == dt.year and displayed.month < dt.month)):
            await page.click(".k-nav-next, [aria-label='Next']")
        else:
            await page.click(".k-nav-prev, [aria-label='Previous']")
        await asyncio.sleep(0.4)

    # Click the target day number in the calendar
    day_str = str(dt.day)
    # Find the td containing this day number that isn't greyed out
    await page.locator(f".k-calendar td:not(.k-other-month) a:text-is('{day_str}'), .k-calendar td:not(.k-other-month) span:text-is('{day_str}')").first.click()
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

        if sys.platform == "win32":
            chrome_profile = os.getenv("CHROME_PROFILE_PATH", "/tmp/chrome-profile")
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=chrome_profile,
                channel="chrome",
                headless=False,
                args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
                no_viewport=True,
            )
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
            )
            self._page = await self._context.new_page()

        self._on_calendar = False

    async def _navigate_to_calendar(self) -> None:
        """Full navigation: login → Sports Rentals → Bushrod → calendar."""
        page = self._page
        today = date.today().isoformat()

        await page.goto(TARGET_URL, wait_until="networkidle", timeout=20000)
        if await page.query_selector("#textBoxUsername"):
            await login(page, self._username, self._password)

        await navigate_to_sports_rentals(page)
        await search_bushrod(page, today, today)
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
