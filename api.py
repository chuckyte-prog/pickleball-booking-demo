"""
Pickleball Court Availability API
FastAPI backend for the startuptosold.com demo.

Usage:
  uvicorn api:app --reload                  # local dev
  uvicorn api:app --host 0.0.0.0 --port 8000  # production
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load .env from this file's directory regardless of cwd
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)

_agent_path = (Path(__file__).parent / "court_agent.py").resolve()

app = FastAPI()

# One request at a time — Chrome is heavy
_lock = asyncio.Lock()


class AvailabilityRequest(BaseModel):
    date: str  # YYYY-MM-DD


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/availability")
async def availability(req: AvailabilityRequest):
    # Basic date validation
    try:
        requested = date.fromisoformat(req.date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    if requested < date.today():
        raise HTTPException(status_code=400, detail="Date must be today or in the future.")

    if requested > date.today() + timedelta(days=60):
        raise HTTPException(status_code=400, detail="Date must be within the next 60 days.")

    # Run scraper in a subprocess to avoid event loop conflicts (Windows + Playwright)
    async with _lock:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(_agent_path), req.date, req.date,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(_agent_path.parent),
                env={**os.environ, "DOTENV_PATH": str(_env_path)},
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
            except asyncio.TimeoutError:
                proc.kill()
                raise HTTPException(status_code=504, detail="Scrape timed out. Please try again.")

            if proc.returncode != 0:
                err = stderr.decode()[:500] or f"exit code {proc.returncode}, stdout: {stdout.decode()[:200]}"
                raise HTTPException(status_code=500, detail=f"Scrape failed: {err}")

            data = json.loads(stdout.decode())
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Scrape failed: {str(e)}")

    return JSONResponse(content=data)


# Serve static files — must come after API routes
_static_dir = str(Path(__file__).parent / "static")
app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
