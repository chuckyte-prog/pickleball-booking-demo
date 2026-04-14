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

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

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

    # #from = start date picker, #to = end date picker (both visible k-input fields)
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
    await page.wait_for_load_state("networkidle", timeout=15000)


async def select_bushrod_court_1(page: Page) -> None:
    # Find the row containing "Bushrod Tennis Court # 1" and click its Choose button
    await page.wait_for_selector("text=Bushrod Tennis Court # 1", timeout=15000)
    await asyncio.sleep(random.uniform(0.8, 1.5))

    # The Choose button is in the same row — find the closest button
    court_row = page.locator("text=Bushrod Tennis Court 1").locator("xpath=ancestor::tr | ancestor::div[contains(@class,'row')] | ancestor::li").first
    choose_btn = court_row.locator("button:has-text('Choose'), a:has-text('Choose'), [role='button']:has-text('Choose')")

    if await choose_btn.count() == 0:
        # Fallback: find Choose button near the text
        all_choose = page.locator("button:has-text('Choose'), a:has-text('Choose')")
        count = await all_choose.count()
        # Click the first one (Bushrod Tennis Court 1 is first result)
        if count > 0:
            await all_choose.first.click()
    else:
        await choose_btn.first.click()

    await page.wait_for_load_state("networkidle", timeout=15000)
    # Wait for calendar grid
    await page.wait_for_selector(".k-scheduler, .k-scheduler-content", timeout=15000)
    await asyncio.sleep(2)  # let JS finish rendering slots


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
        // Only collect from elements whose title contains a time range,
        // and filter to the target date column by x-position if possible.
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

    # Strategy 3: x/y position mapping (same approach the LLM was doing)
    positional = await page.evaluate("""
    (targetDateStr) => {
        // Find column headers to identify the target date column
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

        // Find time row labels
        const timeRows = [];
        document.querySelectorAll('.k-scheduler-times tr, .k-time-cell').forEach(el => {
            const text = (el.innerText || '').trim();
            if (/\\d+:\\d+/.test(text)) {
                const rect = el.getBoundingClientRect();
                timeRows.push({ text, y: Math.round(rect.top) });
            }
        });

        // Find Reserve elements in the target column — deduplicate by y position
        const seen = new Set();
        const slots = [];
        document.querySelectorAll('a, div, span').forEach(el => {
            // Only leaf-level elements (no Reserve children)
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


async def run(start_date: str, end_date: str, headless: bool = False) -> dict:
    chrome_profile = get_env("CHROME_PROFILE_PATH")
    username = get_env("OAKLAND_USERNAME")
    password = get_env("OAKLAND_PASSWORD")

    # Build list of dates in range
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    dates = []
    cur = start_dt
    while cur <= end_dt:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=chrome_profile,
            channel="chrome",
            headless=headless,
            args=["--start-maximized", "--no-first-run", "--no-default-browser-check"],
            no_viewport=True,
        )

        page = context.pages[0] if context.pages else await context.new_page()

        try:
            await page.goto(TARGET_URL, wait_until="networkidle", timeout=20000)

            if await page.query_selector("#textBoxUsername"):
                await login(page, username, password)

            await navigate_to_sports_rentals(page)
            # Search with the full date range so the calendar shows all days at once
            await search_bushrod(page, start_date, end_date)
            await select_bushrod_court_1(page)

            # Scrape each requested date from the same calendar view
            court_days = []
            for d in dates:
                slots = await scrape_calendar(page, d)
                court_days.append({"date": d, "available_slots": slots})

        finally:
            await context.close()

    data = {
        "city": "Oakland",
        "start_date": start_date,
        "end_date": end_date,
        "scraped_at": datetime.now().isoformat(),
        "court_name": COURT_NAME,
        "days": court_days,
        "dry_run": True,
    }

    return data


if __name__ == "__main__":
    if len(sys.argv) == 3:
        start, end = sys.argv[1], sys.argv[2]
    elif len(sys.argv) == 2:
        start = end = sys.argv[1]
    else:
        tomorrow = date.today() + timedelta(days=1)
        start = end = tomorrow.isoformat()

    data = asyncio.run(run(start, end))
    print(json.dumps(data, indent=2))
