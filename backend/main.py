"""
main.py
-------
FastAPI entry point for the Solrei CPT Negotiation Helper API.

Run with:
    cd /Users/deanpedersen/projects/solrei/cpt-dashboard
    uvicorn backend.main:app --reload

Then open: http://localhost:8000/docs
"""

import os
from fastapi import FastAPI
from backend.security import public_access_control
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.routers import payers, contracts, fee_schedules, dashboard, letters, intermediaries
from backend.routers import sync_rates
from backend.routers import sheets_sync
from backend.routers import medicare_import
from backend.routers import best_channel
from backend.routers import channel_overrides

# Paths relative to this file
_APP_ROOT      = os.path.dirname(os.path.dirname(__file__))
DASHBOARD_PATH = os.path.join(_APP_ROOT, "dashboard.html")
ASSETS_PATH    = os.path.join(_APP_ROOT, "assets")

app = FastAPI(
    title="Solrei CPT Negotiation Helper",
    description=(
        "API for managing payer contracts, fee schedules, and negotiation targets "
        "for Solrei Behavioral Health, Inc."
    ),
    version="0.1.0",
)

# Allow all local origins (file://, localhost ports for dev tools)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.middleware("http")(public_access_control)

# Register routers
app.include_router(payers.router)
app.include_router(contracts.router)
app.include_router(fee_schedules.router)
app.include_router(dashboard.router)
app.include_router(letters.router)
app.include_router(intermediaries.router)
app.include_router(sync_rates.router)
app.include_router(sheets_sync.router)
app.include_router(medicare_import.router)
app.include_router(best_channel.router)
app.include_router(channel_overrides.router)


# Serve static assets (logo, etc.) — mount AFTER routers so /api routes take priority
os.makedirs(ASSETS_PATH, exist_ok=True)
app.mount("/assets", StaticFiles(directory=ASSETS_PATH), name="assets")


@app.options("/{rest_of_path:path}")
async def preflight_handler(rest_of_path: str):
    return {}

@app.get("/dashboard", tags=["Dashboard"], include_in_schema=False)
def serve_dashboard():
    """Serve the negotiation dashboard HTML file."""
    return FileResponse(
        DASHBOARD_PATH,
        media_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/", tags=["Health"])
def root():
    """Health check — confirms the API is running."""
    return {
        "status": "ok",
        "app": "Solrei CPT Negotiation Helper",
        "version": "0.1.0",
        "dashboard": "/dashboard",
        "docs": "/docs",
    }


@app.get("/health", tags=["Health"])
def health():
    """Detailed health check including database connectivity."""
    from backend.database import get_db
    try:
        with get_db() as cur:
            cur.execute("SELECT COUNT(*) AS payer_count FROM payers")
            result = cur.fetchone()
        return {
            "status": "ok",
            "database": "connected",
            "payer_count": result["payer_count"],
        }
    except Exception as e:
        return {"status": "error", "database": "disconnected", "detail": str(e)}
