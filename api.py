"""
Pickleball Court Availability API
FastAPI backend for the startuptosold.com demo.

Usage:
  uvicorn api:app --reload                  # local dev
  uvicorn api:app --host 0.0.0.0 --port 8000  # production
"""

import os
from datetime import date, timedelta, datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from court_agent import get_session, set_credentials, COURT_NAME

# Load .env from this file's directory regardless of cwd
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://startuptosold.com", "https://www.startuptosold.com"],
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type"],
)


@app.on_event("startup")
async def startup():
    username = os.getenv("OAKLAND_USERNAME")
    password = os.getenv("OAKLAND_PASSWORD")
    if not username or not password:
        raise RuntimeError("OAKLAND_USERNAME and OAKLAND_PASSWORD must be set")
    set_credentials(username, password)
    # Warm up: navigate to the calendar now so the first request is fast
    session = get_session()
    try:
        await session._ensure_calendar()
        print("Browser session warmed up successfully", flush=True)
    except Exception as e:
        print(f"Warm-up failed (will retry on first request): {e}", flush=True)


@app.on_event("shutdown")
async def shutdown():
    session = get_session()
    await session.close()


class AvailabilityRequest(BaseModel):
    date: str  # YYYY-MM-DD


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/screenshot")
async def screenshot():
    """Return a screenshot of whatever the browser is currently looking at."""
    from fastapi.responses import Response
    session = get_session()
    if session._page is None or session._page.is_closed():
        raise HTTPException(status_code=503, detail="No active browser session")
    img = await session._page.screenshot(full_page=True)
    return Response(content=img, media_type="image/png")


@app.get("/debug/{target_date}")
async def debug(target_date: str):
    """Jump to date and return raw DOM slot data for debugging."""
    from court_agent import jump_to_date, scrape_calendar
    session = get_session()
    if session._page is None or session._page.is_closed():
        raise HTTPException(status_code=503, detail="No active browser session")
    await jump_to_date(session._page, target_date)
    # Return raw evaluate results before merge
    page = session._page
    attrs = await page.evaluate("""
    (targetDate) => {
        const results = [];
        document.querySelectorAll('[data-start][data-end]').forEach(el => {
            const text = (el.innerText || el.textContent || '').trim();
            if (text === 'Reserve') {
                results.push({
                    start: el.getAttribute('data-start'),
                    end: el.getAttribute('data-end'),
                });
            }
        });
        return results;
    }
    """, target_date)
    return {"date": target_date, "raw_slots": attrs}


@app.post("/availability")
async def availability(req: AvailabilityRequest):
    try:
        requested = date.fromisoformat(req.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    if requested < date.today():
        raise HTTPException(status_code=400, detail="Date must be today or in the future.")

    if requested > date.today() + timedelta(days=60):
        raise HTTPException(status_code=400, detail="Date must be within the next 60 days.")

    try:
        session = get_session()
        slots = await session.get_slots(req.date)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scrape failed: {str(e)}")

    data = {
        "city": "Oakland",
        "start_date": req.date,
        "end_date": req.date,
        "scraped_at": datetime.now().isoformat(),
        "court_name": COURT_NAME,
        "days": [{"date": req.date, "available_slots": slots}],
        "dry_run": True,
    }

    return JSONResponse(content=data)


# Serve static files — must come after API routes
_static_dir = str(Path(__file__).parent / "static")
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
