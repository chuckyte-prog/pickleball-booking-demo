"""
Oakland Pickleball Court Availability Agent
Scrapes available slots from PerfectMind using pure Playwright — no LLM.
Dry-run only: finds Reserve buttons but does NOT click them.

Uses the public BookMe4 widget — no login required.

Setup:
  1. pip install -r requirements.txt
  2. playwright install chromium
  3. python court_agent.py [YYYY-MM-DD]  (defaults to tomorrow)
"""

import asyncio
import json
import re
import sys
from datetime import date, datetime, timedelta

from playwright.async_api import async_playwright, Page, BrowserContext

# Public BookMe4 widget URL — no login required
FACILITY_ID = "6c5367b9-a180-46af-beff-fef0a898a533"
WIDGET_ID = "ef709cfd-55bf-4afc-b375-1be4775ff667"
CALENDAR_ID = "e4facb93-38df-44e3-8e2e-db4778408327"
BASE_URL = "https://cityofoakland.perfectmind.com/23603/Reports/BookMe4LandingPages/Facility"
COURT_NAME = "Bushrod Tennis Court # 1"


def build_public_url(target_date: str) -> str:
    """Build the direct public URL for the Bushrod court calendar."""
    return (
        f"{BASE_URL}"
        f"?facilityId={FACILITY_ID}"
        f"&widgetId={WIDGET_ID}"
        f"&calendarId={CALENDAR_ID}"
        f"&arrivalDate={target_date}T12:00:00.000Z"
    )


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
    """Keeps a single browser + page alive across requests.
    Uses the public BookMe4 widget — no login required.
    Automatically recovers from errors by re-launching.
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page: Page | None = None
        self._on_calendar = False
        self._lock = asyncio.Lock()

    async def _launch(self) -> None:
        """Launch browser."""
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        headless = sys.platform != "win32"
        extra_args = ["--no-sandbox", "--disable-dev-shm-usage"] if sys.platform != "win32" else []
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
        """Navigate directly to the public Bushrod court calendar."""
        page = self._page
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        url = build_public_url(tomorrow)

        await page.goto(url, wait_until="networkidle", timeout=30000)

        # Verify the page loaded correctly — check for the scheduler grid
        scheduler = await page.query_selector(".k-scheduler, .k-scheduler-content")
        if not scheduler:
            # Check if we got redirected to a login page
            if await page.query_selector("#textBoxUsername"):
                raise RuntimeError(
                    "PUBLIC_URL_REQUIRES_LOGIN: The public BookMe4 URL is now "
                    "requiring authentication. The URL or widget IDs may have "
                    "changed. Check the PerfectMind portal manually."
                )
            raise RuntimeError(
                "PUBLIC_URL_BROKEN: The public BookMe4 URL did not load the "
                "scheduler grid. The facility/widget/calendar IDs may have "
                "changed. Check the PerfectMind portal manually."
            )

        self._on_calendar = True
        print("Navigated to public calendar successfully", flush=True)

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


# ── Standalone CLI ───────────────────────────────────────────────────────────

async def run_once(target_date: str) -> dict:
    """Used by the CLI to run a single scrape and exit."""
    async with async_playwright() as p:
        if sys.platform == "win32":
            browser = await p.chromium.launch(headless=False)
        else:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        try:
            url = build_public_url(target_date)
            await page.goto(url, wait_until="networkidle", timeout=30000)

            # Verify page loaded
            scheduler = await page.query_selector(".k-scheduler, .k-scheduler-content")
            if not scheduler:
                if await page.query_selector("#textBoxUsername"):
                    raise RuntimeError("Public URL now requires login -- IDs may have changed")
                raise RuntimeError("Scheduler grid not found -- public URL may be broken")

            slots = await scrape_calendar(page, target_date)
        finally:
            await context.close()
            await browser.close()

    return {
        "city": "Oakland",
        "start_date": target_date,
        "end_date": target_date,
        "scraped_at": datetime.now().isoformat(),
        "court_name": COURT_NAME,
        "days": [{"date": target_date, "available_slots": slots}],
    }


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        target = sys.argv[1]
    else:
        target = (date.today() + timedelta(days=1)).isoformat()

    data = asyncio.run(run_once(target))
    print(json.dumps(data, indent=2))
