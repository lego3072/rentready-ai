"""
Condition Report — AI Property Condition Report Generator
condition-report.com

Upload room photos → AI describes condition → Professional PDF report
"""

import os
import uuid
import json
import base64
import hashlib
import time
import logging
import secrets
import asyncio
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

import anthropic
import bcrypt
import httpx
import stripe
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image as RLImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()

logger = logging.getLogger("condition-report")

# --- Config ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
STRIPE_PRICE_SINGLE = os.getenv("STRIPE_PRICE_SINGLE", "")
STRIPE_PRICE_MONTHLY = os.getenv("STRIPE_PRICE_MONTHLY", "")
STRIPE_PRICE_ANNUAL = os.getenv("STRIPE_PRICE_ANNUAL", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

stripe.api_key = STRIPE_SECRET_KEY
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

UPLOAD_DIR = Path("uploads")
REPORT_DIR = Path("reports")
UPLOAD_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)

# --- PostgreSQL Database ---
try:
    import psycopg2
    import psycopg2.pool
    from psycopg2.extras import Json, RealDictCursor
    DATABASE_URL = os.environ.get("DATABASE_URL")
    POSTGRES_AVAILABLE = bool(DATABASE_URL)

    if POSTGRES_AVAILABLE:
        # Connection pool: min 2, max 10 connections
        _connection_pool = psycopg2.pool.ThreadedConnectionPool(
            2, 10, DATABASE_URL
        )

        def get_db_connection():
            return _connection_pool.getconn()

        def release_db_connection(conn):
            """Return connection to pool instead of closing it."""
            if conn:
                try:
                    _connection_pool.putconn(conn)
                except Exception:
                    pass

        def init_database():
            conn = None
            try:
                conn = get_db_connection()
                cur = conn.cursor()

                # Users table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        fingerprint VARCHAR(255),
                        email VARCHAR(255),
                        plan VARCHAR(32) DEFAULT 'free',
                        reports_used INTEGER DEFAULT 0,
                        single_reports_purchased INTEGER DEFAULT 0,
                        stripe_customer_id VARCHAR(255),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(fingerprint)
                    )
                """)

                # Email index for login lookups
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)
                    WHERE email IS NOT NULL
                """)

                # Reports table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS reports (
                        id VARCHAR(255) PRIMARY KEY,
                        fingerprint VARCHAR(255) NOT NULL,
                        email VARCHAR(255),
                        report_type VARCHAR(64),
                        property_info JSONB DEFAULT '{}',
                        rooms JSONB DEFAULT '[]',
                        pdf_path VARCHAR(512),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_reports_fingerprint ON reports(fingerprint)
                """)

                # Accounts table (email/password auth)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS accounts (
                        id SERIAL PRIMARY KEY,
                        email VARCHAR(255) NOT NULL UNIQUE,
                        password_hash VARCHAR(255) NOT NULL,
                        name VARCHAR(255),
                        company VARCHAR(255),
                        plan VARCHAR(32) DEFAULT 'free',
                        stripe_customer_id VARCHAR(255),
                        fingerprint VARCHAR(255),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Account sessions: maps fingerprints to account emails (multi-device support)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS account_sessions (
                        id SERIAL PRIMARY KEY,
                        email VARCHAR(255) NOT NULL,
                        fingerprint VARCHAR(255) NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(email, fingerprint)
                    )
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_account_sessions_fp ON account_sessions(fingerprint)
                """)

                # Share tokens table (persistent share links)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS share_tokens (
                        token VARCHAR(255) PRIMARY KEY,
                        report_id VARCHAR(255) NOT NULL,
                        fingerprint VARCHAR(255) NOT NULL,
                        expires_at TIMESTAMP NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_share_tokens_expires ON share_tokens(expires_at)
                """)

                # Processed Stripe sessions (prevent double-crediting from webhook + verify)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS processed_stripe_sessions (
                        session_id VARCHAR(255) PRIMARY KEY,
                        fingerprint VARCHAR(255) NOT NULL,
                        purchase_type VARCHAR(32) NOT NULL,
                        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                conn.commit()
                cur.close()
                logger.info("Database tables initialized successfully")
            except Exception as e:
                logger.error(f"Database initialization error: {e}")
            finally:
                if conn:
                    release_db_connection(conn)

        init_database()
except ImportError:
    POSTGRES_AVAILABLE = False
    logger.warning("psycopg2 not available - using in-memory storage")

    def release_db_connection(conn):
        if conn:
            try:
                conn.close()
            except Exception:
                pass

# --- In-memory fallback ---
users_db: dict = {}
reports_db: dict = {}

app = FastAPI(title="Condition Report", version="1.0.0")

# --- Rate Limiter ---
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please try again later."},
    )


@app.middleware("http")
async def https_redirect(request: Request, call_next):
    """Redirect HTTP to HTTPS in production (behind Railway/Cloudflare proxy)."""
    proto = request.headers.get("x-forwarded-proto", "https")
    if proto == "http" and "localhost" not in str(request.url):
        url = str(request.url).replace("http://", "https://", 1)
        from starlette.responses import RedirectResponse
        return RedirectResponse(url, status_code=301)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://condition-report.com", "https://www.condition-report.com", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Helpers ---

def get_fingerprint(request: Request, x_fingerprint: Optional[str] = None) -> str:
    """Get user fingerprint from header or generate from IP."""
    if x_fingerprint:
        return x_fingerprint
    ip = request.client.host if request.client else "unknown"
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def get_user(fingerprint: str) -> dict:
    """Get or create user record from DB (with in-memory fallback)."""
    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)

            cur.execute("SELECT * FROM users WHERE fingerprint = %s", (fingerprint,))
            row = cur.fetchone()

            if not row:
                cur.execute("""
                    INSERT INTO users (fingerprint, plan, reports_used, single_reports_purchased)
                    VALUES (%s, 'free', 0, 0)
                    ON CONFLICT (fingerprint) DO NOTHING
                    RETURNING *
                """, (fingerprint,))
                conn.commit()
                row = cur.fetchone()
                if not row:
                    cur.execute("SELECT * FROM users WHERE fingerprint = %s", (fingerprint,))
                    row = cur.fetchone()

            cur.close()
            return {
                "fingerprint": row["fingerprint"],
                "email": row.get("email"),
                "reports_used": row["reports_used"],
                "is_pro": row["plan"] == "pro",
                "plan": row["plan"],
                "stripe_customer_id": row.get("stripe_customer_id"),
                "single_reports_purchased": row["single_reports_purchased"],
                "created_at": str(row["created_at"]),
            }
        except Exception as e:
            logger.error(f"DB get_user error: {e}")
        finally:
            if conn:
                release_db_connection(conn)

    # Fallback to in-memory
    if fingerprint not in users_db:
        users_db[fingerprint] = {
            "fingerprint": fingerprint,
            "email": None,
            "reports_used": 0,
            "is_pro": False,
            "plan": "free",
            "stripe_customer_id": None,
            "single_reports_purchased": 0,
            "reports": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    return users_db[fingerprint]


def get_user_reports(fingerprint: str) -> list:
    """Get user's reports from DB."""
    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT id, report_type, property_info, pdf_path, created_at
                FROM reports WHERE fingerprint = %s
                ORDER BY created_at DESC LIMIT 50
            """, (fingerprint,))
            rows = cur.fetchall()
            cur.close()
            return [{
                "id": r["id"],
                "date": r["created_at"].strftime("%B %d, %Y") if r["created_at"] else "",
                "address": (r["property_info"] or {}).get("address", ""),
                "report_type": r["report_type"],
            } for r in rows]
        except Exception as e:
            logger.error(f"DB get_user_reports error: {e}")
        finally:
            if conn:
                release_db_connection(conn)
    return []


def save_report_to_db(report_data: dict, fingerprint: str):
    """Save report to PostgreSQL."""
    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO reports (id, fingerprint, report_type, property_info, rooms, pdf_path)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (
                report_data["id"],
                fingerprint,
                report_data.get("report_type"),
                Json(report_data.get("property_info", {})),
                Json(report_data.get("rooms", [])),
                report_data.get("pdf_path"),
            ))
            # Increment reports_used
            cur.execute("""
                UPDATE users SET reports_used = reports_used + 1, updated_at = CURRENT_TIMESTAMP
                WHERE fingerprint = %s
            """, (fingerprint,))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"DB save_report error: {e}")
        finally:
            if conn:
                release_db_connection(conn)


def get_report_from_db(report_id: str) -> dict | None:
    """Get report from DB."""
    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM reports WHERE id = %s", (report_id,))
            row = cur.fetchone()
            cur.close()
            if row:
                return {
                    "id": row["id"],
                    "fingerprint": row["fingerprint"],
                    "date": row["created_at"].strftime("%B %d, %Y") if row["created_at"] else "",
                    "report_type": row["report_type"],
                    "property_info": row["property_info"] or {},
                    "rooms": row["rooms"] or [],
                    "pdf_path": row["pdf_path"],
                }
            return None
        except Exception as e:
            logger.error(f"DB get_report error: {e}")
        finally:
            if conn:
                release_db_connection(conn)
    return None


def update_user_plan(fingerprint: str, plan: str, stripe_customer_id: str = None):
    """Update user plan in DB."""
    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            if stripe_customer_id:
                cur.execute("""
                    UPDATE users SET plan = %s, stripe_customer_id = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE fingerprint = %s
                """, (plan, stripe_customer_id, fingerprint))
            else:
                cur.execute("""
                    UPDATE users SET plan = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE fingerprint = %s
                """, (plan, fingerprint))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"DB update_user_plan error: {e}")
        finally:
            if conn:
                release_db_connection(conn)


def add_single_report_purchase(fingerprint: str):
    """Increment single_reports_purchased in DB."""
    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                UPDATE users SET single_reports_purchased = single_reports_purchased + 1, updated_at = CURRENT_TIMESTAMP
                WHERE fingerprint = %s
            """, (fingerprint,))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"DB add_single_report error: {e}")
        finally:
            if conn:
                release_db_connection(conn)


_processed_sessions_mem = set()  # in-memory fallback

def mark_session_processed(session_id: str, fingerprint: str, purchase_type: str) -> bool:
    """Mark a Stripe session as processed. Returns True if newly marked, False if already processed."""
    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO processed_stripe_sessions (session_id, fingerprint, purchase_type) VALUES (%s, %s, %s) ON CONFLICT (session_id) DO NOTHING",
                (session_id, fingerprint, purchase_type),
            )
            inserted = cur.rowcount > 0
            conn.commit()
            cur.close()
            return inserted
        except Exception as e:
            logger.error(f"mark_session_processed error: {e}")
            # Fall through to in-memory
        finally:
            if conn:
                release_db_connection(conn)

    # In-memory fallback
    if session_id in _processed_sessions_mem:
        return False
    _processed_sessions_mem.add(session_id)
    return True


def check_access(user: dict) -> dict:
    """Check if user can generate a report. Returns {allowed, reason}."""
    # Pro users: unlimited
    if user["is_pro"]:
        return {"allowed": True, "reason": "pro"}

    # Free trial: 1 report (4 rooms max)
    if user["reports_used"] == 0:
        return {"allowed": True, "reason": "free_trial", "max_rooms": 4}

    # Purchased single reports
    reports_remaining = user["single_reports_purchased"] - (user["reports_used"] - 1)  # -1 for free trial
    if reports_remaining > 0:
        return {"allowed": True, "reason": "single_purchase", "remaining": reports_remaining}

    return {"allowed": False, "reason": "limit_reached"}


def encode_image(image_bytes: bytes) -> str:
    """Base64 encode image for Claude Vision API."""
    return base64.standard_b64encode(image_bytes).decode("utf-8")


def resize_image(image_bytes: bytes, max_size: int = 768) -> bytes:
    """Resize image to max dimension while keeping aspect ratio."""
    img = Image.open(BytesIO(image_bytes))
    if img.mode == "RGBA":
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def analyze_room_photos_sync(room_name: str, photos: list[bytes], report_type: str = "Move-In") -> str:
    """Send room photos to Claude Vision and get structured condition assessment."""
    content = []
    for photo_bytes in photos:
        resized = resize_image(photo_bytes)
        b64 = encode_image(resized)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64,
            },
        })

    # Inspection-type-specific instructions
    type_instructions = {
        "Move-In": """INSPECTION TYPE: MOVE-IN (Baseline Documentation)
PURPOSE: Document the property's current condition as a baseline record before tenant occupancy.
TONE: Neutral and balanced — this is a documentation tool, not a critique. Most lived-in properties will have minor cosmetic wear, and that is NORMAL and expected.
FOCUS AREAS:
- Document what you see factually — don't dramatize or exaggerate
- Note positives first (clean, functional, good condition items) then any concerns
- Minor cosmetic wear (small scuffs, light marks, normal aging) is EXPECTED and should still rate "Good"
- Only flag items as "Fair" if they clearly need attention (not just minor imperfections)
- Only flag items as "Poor" if there is obvious damage, safety hazards, or non-functional components
- Personal items, stored belongings, or clutter are NOT defects — ignore them""",
        "Move-Out": """INSPECTION TYPE: MOVE-OUT (Condition Documentation)
PURPOSE: Document the property's current condition for comparison against move-in records.
TONE: Neutral and fair — distinguish between normal wear and actual damage. Most wear is expected over a tenancy.
FOCUS AREAS:
- NORMAL WEAR (rate "Good"): faded paint, minor scuffs, light carpet wear near doors, small nail holes, minor marks
- MINOR ISSUES (rate "Fair"): noticeable stains, moderate scuffs, items needing cleaning, cosmetic repairs
- ACTUAL DAMAGE (rate "Poor"): holes in walls, broken fixtures, burns, water damage, missing items, unauthorized modifications
- Note cleaning needs factually without being harsh
- Compare to what a reasonable lived-in condition looks like""",
        "Periodic": """INSPECTION TYPE: PERIODIC / ROUTINE INSPECTION
PURPOSE: Quick check on maintenance needs and safety items during tenancy.
TONE: Helpful and practical — focus on actionable maintenance items, not cosmetic opinions.
FOCUS AREAS:
- SAFETY: smoke/CO detectors, water leaks, mold/mildew, electrical issues
- MAINTENANCE: plumbing drips, caulking, weatherstripping, HVAC filters, pest signs
- Note overall condition positively where warranted
- Only flag items that genuinely need landlord attention""",
    }

    type_ctx = type_instructions.get(report_type, type_instructions["Move-In"])

    content.append({
        "type": "text",
        "text": f"""You are a property condition documentation assistant helping a landlord record the state of this "{room_name}".

{type_ctx}

CRITICAL RULES:
- ONLY describe what you can ACTUALLY SEE in the photo. Do NOT assume, guess, or invent details.
- Do NOT fabricate descriptions like "window above sink" if there is no sink visible. Every detail must be verifiable from the photo.
- If an item is not visible or you can't assess it from the photo, still include it but rate it "N/A" with notes "Not visible in photo". This keeps the report complete.

TONE GUIDANCE:
- Be BALANCED and FAIR. Most properties are in acceptable condition with normal wear.
- Lead with positives. Note what's in good shape before mentioning concerns.
- Don't be an alarmist — minor imperfections are normal in any lived-in space.
- Personal items, clutter, or stored belongings are NOT property defects.
- When in doubt, rate "Good" — only downgrade when clearly warranted by visible evidence.

Analyze the photo(s) and return a JSON object with this EXACT structure:
{{
  "overall_rating": "Good" | "Fair" | "Poor",
  "items": [
    {{
      "name": "Walls",
      "rating": "Good" | "Fair" | "Poor" | "N/A",
      "notes": "Brief specific description"
    }}
  ],
  "summary": "2 sentence overview: start with the overall condition positively, then note any items worth attention if applicable.",
  "flags": ["Only genuine issues requiring action — empty array if none. Do NOT flag minor cosmetic wear."]
}}

CHECKLIST — include ALL items below. If visible, describe what you see. If not visible, rate "N/A" with "Not visible in photo":
- Walls (paint condition, holes, marks, cracks, water damage)
- Ceiling (stains, cracks, peeling, discoloration)
- Flooring (type, wear, stains, scratches, damage)
- Windows (glass, frames, locks, screens, seals)
- Doors (condition, hardware, locks, hinges)
- Lighting/Electrical (fixtures, outlets, switches, covers)
- Cleanliness (general tidiness — don't penalize for personal items)
- Fixtures & Appliances (faucets, cabinets, countertops, appliances)

RATING GUIDELINES (default to "Good" unless clearly not):
- Good = Functional, acceptable condition, normal wear. This is the DEFAULT for most items.
- Fair = Notable cosmetic issues or maintenance items that should be addressed
- Poor = Obvious damage, broken/non-functional components, or safety hazards

OVERALL RATING: If most items are "Good", the overall rating should be "Good" even if 1-2 items are "Fair".

Be specific about locations ("left wall near window" not just "walls"). Emphasize positives alongside any concerns.

FINAL CHECK: Before returning, verify EVERY description references something ACTUALLY VISIBLE in the photo. If you described something not in the photo, change its rating to "N/A" and notes to "Not visible in photo". Zero filler, zero guessing.

Return ONLY valid JSON. No markdown, no code fences.""",
    })

    # Try fast model first, fall back to proven model
    models_to_try = ["claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929"]
    raw = None
    last_error = None

    for model_id in models_to_try:
        try:
            logger.info(f"Analyzing {room_name} with {model_id}")
            response = client.messages.create(
                model=model_id,
                max_tokens=1000,
                messages=[{"role": "user", "content": content}],
            )
            raw = response.content[0].text.strip()
            logger.info(f"Analysis of {room_name} complete with {model_id}")
            break
        except Exception as e:
            last_error = e
            logger.warning(f"Model {model_id} failed for {room_name}: {e}")
            continue

    if raw is None:
        logger.error(f"All models failed for {room_name}: {last_error}")
        return json.dumps({
            "overall_rating": "N/A",
            "items": [{"name": "Error", "rating": "N/A", "notes": f"Analysis failed: {str(last_error)[:100]}"}],
            "summary": "Analysis could not be completed. Please try again.",
            "flags": ["Analysis error — please retry"]
        })

    try:
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        parsed = json.loads(raw)
        return json.dumps(parsed)
    except json.JSONDecodeError:
        # Fallback: return raw text wrapped in a basic structure
        return json.dumps({
            "overall_rating": "Fair",
            "items": [{"name": "General Condition", "rating": "Fair", "notes": raw[:500]}],
            "summary": "AI analysis completed. See item details above.",
            "flags": []
        })


def generate_pdf_report(report_data: dict) -> str:
    """Generate a professional PDF condition report."""
    report_id = report_data["id"]
    pdf_path = REPORT_DIR / f"{report_id}.pdf"

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Heading1"],
        fontSize=20,
        spaceAfter=6,
        textColor=colors.HexColor("#1a1a2e"),
        fontName="Helvetica-Bold",
    )
    subtitle_style = ParagraphStyle(
        "ReportSubtitle",
        parent=styles["Normal"],
        fontSize=11,
        spaceAfter=20,
        textColor=colors.HexColor("#666666"),
    )
    room_title_style = ParagraphStyle(
        "RoomTitle",
        parent=styles["Heading2"],
        fontSize=14,
        spaceBefore=16,
        spaceAfter=8,
        textColor=colors.HexColor("#1a1a2e"),
        fontName="Helvetica-Bold",
        borderWidth=0,
        borderPadding=0,
    )
    body_style = ParagraphStyle(
        "BodyText",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        spaceAfter=10,
        textColor=colors.HexColor("#333333"),
    )
    meta_style = ParagraphStyle(
        "MetaText",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#888888"),
    )

    elements = []

    # Title
    elements.append(Paragraph("Property Condition Report", title_style))
    elements.append(Paragraph(
        f"Generated by condition-report.com — {report_data.get('report_type', 'Move-In')} Inspection",
        subtitle_style,
    ))

    # Property info table
    prop = report_data.get("property_info", {})
    info_data = []
    if prop.get("address"):
        info_data.append(["Property Address:", prop["address"]])
    if prop.get("unit"):
        info_data.append(["Unit:", prop["unit"]])
    if prop.get("tenant_name"):
        info_data.append(["Tenant Name:", prop["tenant_name"]])
    if prop.get("landlord_name"):
        info_data.append(["Landlord/Manager:", prop["landlord_name"]])
    info_data.append(["Inspection Date:", report_data.get("date", datetime.now().strftime("%B %d, %Y"))])
    info_data.append(["Report Type:", report_data.get("report_type", "Move-In") + " Inspection"])

    if info_data:
        info_table = Table(info_data, colWidths=[1.8 * inch, 4.7 * inch])
        info_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
            ("TEXTCOLOR", (1, 0), (1, -1), colors.HexColor("#1a1a2e")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("LINEBELOW", (0, -1), (-1, -1), 1, colors.HexColor("#e0e0e0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        elements.append(info_table)
        elements.append(Spacer(1, 20))

    # Rating colors for PDF
    rating_colors = {
        "Good": colors.HexColor("#16a34a"),
        "Fair": colors.HexColor("#f59e0b"),
        "Poor": colors.HexColor("#dc2626"),
        "N/A": colors.HexColor("#9ca3af"),
    }

    rating_style = ParagraphStyle(
        "RatingText", parent=styles["Normal"], fontSize=10, fontName="Helvetica-Bold",
    )
    item_name_style = ParagraphStyle(
        "ItemName", parent=styles["Normal"], fontSize=10, fontName="Helvetica-Bold",
        textColor=colors.HexColor("#333333"),
    )
    item_notes_style = ParagraphStyle(
        "ItemNotes", parent=styles["Normal"], fontSize=9, leading=12,
        textColor=colors.HexColor("#555555"),
    )
    flag_style = ParagraphStyle(
        "FlagText", parent=styles["Normal"], fontSize=9, leading=12,
        textColor=colors.HexColor("#dc2626"), fontName="Helvetica-Bold",
    )
    summary_style = ParagraphStyle(
        "SummaryText", parent=styles["Normal"], fontSize=10, leading=14,
        textColor=colors.HexColor("#1a1a2e"), fontName="Helvetica-Bold",
        spaceBefore=4, spaceAfter=8,
    )

    # Room sections
    for room in report_data.get("rooms", []):
        # Parse structured description
        description_raw = room.get("description", "{}")
        try:
            room_data = json.loads(description_raw) if isinstance(description_raw, str) else description_raw
        except (json.JSONDecodeError, TypeError):
            room_data = {"overall_rating": "N/A", "items": [], "summary": description_raw, "flags": []}

        overall = room_data.get("overall_rating", "N/A")
        rc = rating_colors.get(overall, colors.HexColor("#9ca3af"))

        elements.append(Paragraph(f'{room["name"]}  —  Overall: {overall}', room_title_style))

        # Add room photos — size based on count
        photo_paths_valid = [p for p in room.get("photo_paths", [])[:3] if os.path.exists(p)]
        num_photos = len(photo_paths_valid)
        if num_photos == 1:
            pw, ph = 4.0 * inch, 3.0 * inch  # Single photo: large
        elif num_photos == 2:
            pw, ph = 2.8 * inch, 2.1 * inch  # Two photos: medium
        else:
            pw, ph = 2.1 * inch, 1.6 * inch  # Three photos: compact

        photo_row = []
        for photo_path in photo_paths_valid:
            try:
                img = RLImage(photo_path, width=pw, height=ph)
                img.hAlign = "CENTER"
                photo_row.append(img)
            except Exception:
                pass

        if photo_row:
            col_w = (pw + 0.1 * inch)
            photo_table = Table([photo_row], colWidths=[col_w] * len(photo_row))
            photo_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]))
            elements.append(photo_table)

        # Summary
        summary_text = room_data.get("summary", "")
        if summary_text:
            elements.append(Paragraph(summary_text, summary_style))

        # Item checklist table — use Paragraph objects for text wrapping
        items = room_data.get("items", [])
        if items:
            header_style = ParagraphStyle("TH", parent=styles["Normal"], fontSize=9,
                fontName="Helvetica-Bold", textColor=colors.HexColor("#333333"))
            table_data = [[
                Paragraph("Item", header_style),
                Paragraph("Rating", header_style),
                Paragraph("Condition Notes", header_style),
            ]]
            rating_row_map = []  # track ratings for coloring
            for item in items:
                item_rating = item.get("rating", "N/A")
                rc_item = rating_colors.get(item_rating, colors.HexColor("#9ca3af"))
                r_style = ParagraphStyle("R", parent=styles["Normal"], fontSize=9,
                    fontName="Helvetica-Bold", textColor=rc_item)
                n_style = ParagraphStyle("N", parent=styles["Normal"], fontSize=9,
                    leading=12, textColor=colors.HexColor("#555555"))
                table_data.append([
                    Paragraph(item.get("name", ""), item_name_style),
                    Paragraph(item_rating, r_style),
                    Paragraph(item.get("notes", ""), n_style),
                ])
                rating_row_map.append(item_rating)

            item_table = Table(table_data, colWidths=[1.2 * inch, 0.7 * inch, 4.6 * inch])
            table_styles = [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
            item_table.setStyle(TableStyle(table_styles))
            elements.append(item_table)

        # Flags / Action Items
        flags = room_data.get("flags", [])
        if flags:
            elements.append(Spacer(1, 6))
            elements.append(Paragraph("<b>Action Items / Issues:</b>", ParagraphStyle(
                "FlagHeader", parent=styles["Normal"], fontSize=9, fontName="Helvetica-Bold",
                textColor=colors.HexColor("#991b1b"), spaceAfter=4,
            )))
            for flag in flags:
                if flag:
                    elements.append(Paragraph(f"- {flag}", flag_style))

        elements.append(Spacer(1, 10))

    # Signature section
    elements.append(Spacer(1, 30))
    elements.append(Paragraph("Signatures", room_title_style))

    sig_path = report_data.get("signature_path", "")
    if not sig_path:
        # Check if a signature file exists on disk for this report
        potential_sig = REPORT_DIR / f"{report_id}_sig.png"
        if potential_sig.exists():
            sig_path = str(potential_sig)

    if sig_path and os.path.exists(sig_path):
        # Digital signature present — embed the image
        elements.append(Paragraph("Inspector/Manager Signature:", body_style))
        try:
            sig_img = RLImage(sig_path, width=2.5 * inch, height=0.8 * inch)
            sig_img.hAlign = "LEFT"
            elements.append(sig_img)
        except Exception:
            elements.append(Paragraph("[Signature image could not be loaded]", meta_style))
        elements.append(Paragraph(
            f"Signed digitally on {datetime.now().strftime('%B %d, %Y at %I:%M %p')}",
            meta_style,
        ))
        elements.append(Spacer(1, 12))
        sig_data = [
            ["Tenant:", "____________________________", "Date: _______________"],
        ]
    else:
        # No digital signature — show blank lines for both
        sig_data = [
            ["Landlord/Manager:", "____________________________", "Date: _______________"],
            ["", "", ""],
            ["Tenant:", "____________________________", "Date: _______________"],
        ]

    sig_table = Table(sig_data, colWidths=[1.5 * inch, 3.2 * inch, 2 * inch])
    sig_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
    ]))
    elements.append(sig_table)

    # Disclaimer
    elements.append(Spacer(1, 20))
    disclaimer_style = ParagraphStyle(
        "Disclaimer", parent=styles["Normal"], fontSize=8, leading=11,
        textColor=colors.HexColor("#999999"), spaceBefore=10,
    )
    elements.append(Paragraph(
        f"<b>DISCLAIMER:</b> This condition report was generated on "
        f"{datetime.now().strftime('%B %d, %Y at %I:%M %p')} using AI-assisted analysis via "
        f"condition-report.com. Photo analysis is based on visible conditions only and does not "
        f"constitute a professional building inspection. Hidden defects, structural issues, and "
        f"mechanical systems not visible in photographs are not assessed. Both parties should review "
        f"this report together, note any discrepancies, and sign to acknowledge the documented "
        f"condition. This report may be used as evidence in security deposit disputes.",
        disclaimer_style,
    ))
    elements.append(Spacer(1, 8))
    elements.append(Paragraph(
        f"Report ID: {report_id} | condition-report.com",
        meta_style,
    ))

    doc.build(elements)
    return str(pdf_path)


# --- Routes ---

@app.get("/health")
async def health():
    return {"status": "ok", "service": "condition-report"}


@app.get("/og-image.png")
async def og_image():
    """Generate OG image for social/iMessage link previews."""
    from PIL import ImageDraw, ImageFont
    from fastapi.responses import StreamingResponse

    img = Image.new("RGB", (1200, 630), color=(248, 249, 250))
    draw = ImageDraw.Draw(img)

    # Top accent bar
    draw.rectangle([(0, 0), (1200, 8)], fill=(37, 99, 235))

    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 64)
        sub_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 32)
    except (OSError, IOError):
        title_font = ImageFont.load_default()
        sub_font = ImageFont.load_default()

    draw.text((80, 200), "Condition Report", fill=(26, 26, 46), font=title_font)
    draw.text((80, 300), "AI Property Condition Reports in 60 Seconds", fill=(100, 100, 100), font=sub_font)
    draw.text((80, 380), "Upload photos  >  AI analysis  >  PDF report", fill=(37, 99, 235), font=sub_font)
    draw.text((80, 480), "condition-report.com", fill=(150, 150, 150), font=sub_font)

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/robots.txt")
async def robots_txt():
    content = """User-agent: *
Allow: /
Disallow: /api/
Disallow: /static/

Sitemap: https://condition-report.com/sitemap.xml
"""
    return HTMLResponse(content=content, media_type="text/plain")


@app.get("/sitemap.xml")
async def sitemap_xml():
    content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url>
        <loc>https://condition-report.com</loc>
        <changefreq>weekly</changefreq>
        <priority>1.0</priority>
    </url>
</urlset>
"""
    return HTMLResponse(content=content, media_type="application/xml")


@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path("landing/app.html")
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>Condition Report</h1><p>App loading...</p>")


@app.get("/api/user/status")
async def user_status(request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Get user's current status — reports used, access level."""
    fp = get_fingerprint(request, x_fingerprint)
    user = get_user(fp)
    access = check_access(user)
    reports = get_user_reports(fp)
    return {
        "fingerprint": fp,
        "email": user.get("email"),
        "reports_used": user["reports_used"],
        "is_pro": user["is_pro"],
        "plan": user.get("plan", "free"),
        "single_reports_purchased": user["single_reports_purchased"],
        "access": access,
        "reports": reports,
    }


@app.post("/api/upload-photos")
@limiter.limit("20/minute")
async def upload_photos(
    request: Request,
    photos: list[UploadFile] = File(...),
    room_names: str = Form(...),
    x_fingerprint: Optional[str] = Header(None),
):
    """Upload photos and assign room names. Returns photo IDs for analysis."""
    fp = get_fingerprint(request, x_fingerprint)
    user = get_user(fp)
    access = check_access(user)
    if not access["allowed"]:
        raise HTTPException(402, "Report limit reached. Purchase access to upload more photos.")

    rooms = json.loads(room_names)  # ["Kitchen", "Living Room", "Bedroom 1", ...]

    if len(photos) == 0:
        raise HTTPException(400, "No photos uploaded")

    # Save photos grouped by room
    session_id = str(uuid.uuid4())[:8]
    uploaded = []

    photo_idx = 0
    for room in rooms:
        room_photos = []
        room_count = room.get("photo_count", 1) if isinstance(room, dict) else 1
        room_name = room.get("name", room) if isinstance(room, dict) else room

        for _ in range(room_count):
            if photo_idx >= len(photos):
                break
            photo = photos[photo_idx]
            photo_bytes = await photo.read()

            # Save to disk
            ext = Path(photo.filename or "photo.jpg").suffix or ".jpg"
            filename = f"{session_id}_{room_name.replace(' ', '_')}_{photo_idx}{ext}"
            filepath = UPLOAD_DIR / filename
            filepath.write_bytes(photo_bytes)

            room_photos.append({
                "filename": filename,
                "path": str(filepath),
                "size": len(photo_bytes),
            })
            photo_idx += 1

        uploaded.append({
            "room_name": room_name,
            "photos": room_photos,
        })

    return {
        "session_id": session_id,
        "rooms": uploaded,
        "total_photos": photo_idx,
    }


@app.post("/api/analyze")
@limiter.limit("10/minute")
async def analyze_report(
    request: Request,
    x_fingerprint: Optional[str] = Header(None),
):
    """Analyze uploaded photos and generate condition descriptions."""
    fp = get_fingerprint(request, x_fingerprint)
    user = get_user(fp)
    access = check_access(user)

    if not access["allowed"]:
        return JSONResponse(
            status_code=402,
            content={
                "error": "Report limit reached",
                "message": "Purchase a single report or upgrade to Pro for unlimited reports.",
                "checkout_url": f"{BASE_URL}/api/checkout/single",
            },
        )

    body = await request.json()
    rooms_data = body.get("rooms", [])
    property_info = body.get("property_info", {})
    report_type = body.get("report_type", "Move-In")

    if not rooms_data:
        raise HTTPException(400, "No rooms to analyze")

    # Free trial: max 4 rooms
    if access.get("reason") == "free_trial" and len(rooms_data) > 4:
        rooms_data = rooms_data[:4]

    # Prepare room data for parallel analysis
    rooms_to_analyze = []
    for room in rooms_data:
        room_name = room["room_name"]
        photo_paths = [p["path"] for p in room.get("photos", [])]
        photo_bytes_list = []
        for path in photo_paths:
            if os.path.exists(path):
                photo_bytes_list.append(Path(path).read_bytes())
        if photo_bytes_list:
            rooms_to_analyze.append({
                "name": room_name,
                "photos": photo_bytes_list,
                "photo_paths": photo_paths,
            })

    # Analyze ALL rooms in parallel for speed (6s per room → all rooms in ~6-8s total)
    loop = asyncio.get_event_loop()
    async def analyze_one(r):
        # Run sync Claude API call in thread pool for true parallelism
        desc = await loop.run_in_executor(
            None,
            lambda name=r["name"], photos=r["photos"]: analyze_room_photos_sync(name, photos, report_type)
        )
        return {"name": r["name"], "description": desc, "photo_paths": r["photo_paths"], "photo_count": len(r["photos"])}

    analyzed_rooms = await asyncio.gather(*[analyze_one(r) for r in rooms_to_analyze])
    analyzed_rooms = list(analyzed_rooms)  # convert from tuple

    # Build report
    report_id = str(uuid.uuid4())
    report_data = {
        "id": report_id,
        "fingerprint": fp,
        "date": datetime.now().strftime("%B %d, %Y"),
        "report_type": report_type,
        "property_info": property_info,
        "rooms": analyzed_rooms,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Generate PDF
    pdf_path = generate_pdf_report(report_data)
    report_data["pdf_path"] = pdf_path

    # Store report
    reports_db[report_id] = report_data
    save_report_to_db(report_data, fp)
    if not POSTGRES_AVAILABLE:
        user["reports"].append(report_data)
        user["reports_used"] += 1

    return {
        "report_id": report_id,
        "rooms_analyzed": len(analyzed_rooms),
        "report_type": report_type,
        "pdf_url": f"/api/report/{report_id}/pdf",
        "rooms": [{"name": r["name"], "description": r["description"]} for r in analyzed_rooms],
        "is_free_trial": access.get("reason") == "free_trial",
    }


@app.get("/api/report/{report_id}/pdf")
async def download_report_pdf(
    report_id: str,
    request: Request,
    x_fingerprint: Optional[str] = Header(None),
    fp: Optional[str] = None,
):
    """Download the generated PDF report."""
    # Accept fingerprint from query param (browser GET) or header
    fingerprint = fp or get_fingerprint(request, x_fingerprint)

    # Try DB first, then in-memory
    report = get_report_from_db(report_id) or reports_db.get(report_id)

    if not report:
        raise HTTPException(404, "Report not found")

    # Check ownership
    if report["fingerprint"] != fingerprint:
        raise HTTPException(403, "Not your report")

    pdf_path = report.get("pdf_path")
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(404, "PDF not found")

    address = report.get("property_info", {}).get("address", "property")
    safe_address = "".join(c for c in address if c.isalnum() or c in " -_").strip().replace(" ", "_") or "report"
    filename = f"Condition_Report_{safe_address}_{report['date'].replace(' ', '_')}.pdf"

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=filename,
    )


@app.post("/api/report/{report_id}/signature")
async def add_signature(report_id: str, request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Save a signature image and regenerate the PDF with it embedded."""
    fp = get_fingerprint(request, x_fingerprint)
    report = get_report_from_db(report_id) or reports_db.get(report_id)

    if not report:
        raise HTTPException(404, "Report not found")
    if report["fingerprint"] != fp:
        raise HTTPException(403, "Not your report")

    body = await request.json()
    sig_data = body.get("signature", "")

    if not sig_data or not sig_data.startswith("data:image/png;base64,"):
        raise HTTPException(400, "Invalid signature data")

    # Save signature image to disk
    sig_b64 = sig_data.split(",", 1)[1]
    sig_bytes = base64.b64decode(sig_b64)
    sig_path = REPORT_DIR / f"{report_id}_sig.png"
    sig_path.write_bytes(sig_bytes)

    # Regenerate PDF with signature embedded
    report["signature_path"] = str(sig_path)
    generate_pdf_report(report)

    return {"ok": True}


@app.get("/api/report/{report_id}")
async def get_report(report_id: str, request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Get report data (without PDF)."""
    fp = get_fingerprint(request, x_fingerprint)
    report = get_report_from_db(report_id) or reports_db.get(report_id)

    if not report:
        raise HTTPException(404, "Report not found")
    if report["fingerprint"] != fp:
        raise HTTPException(403, "Not your report")

    rooms = report.get("rooms", [])
    return {
        "id": report["id"],
        "date": report.get("date", ""),
        "report_type": report.get("report_type", ""),
        "property_info": report.get("property_info", {}),
        "rooms": [{"name": r.get("name", ""), "description": r.get("description", ""), "photo_count": r.get("photo_count", 0)} for r in rooms],
        "pdf_url": f"/api/report/{report_id}/pdf",
    }


# --- Stripe Checkout ---

@app.post("/api/checkout/single")
@limiter.limit("10/minute")
async def checkout_single(request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Create Stripe checkout for a single report."""
    fp = get_fingerprint(request, x_fingerprint)

    if not STRIPE_PRICE_SINGLE:
        raise HTTPException(500, "Stripe not configured — single report price missing")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": STRIPE_PRICE_SINGLE, "quantity": 1}],
        mode="payment",
        success_url=f"{BASE_URL}?payment=success&type=single&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}?payment=cancelled",
        metadata={"fingerprint": fp, "type": "single"},
    )
    return {"checkout_url": session.url}


@app.post("/api/verify-payment")
@limiter.limit("10/minute")
async def verify_payment(request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Verify a Stripe checkout session and credit user immediately (no webhook wait)."""
    fp = get_fingerprint(request, x_fingerprint)
    body = await request.json()
    session_id = body.get("session_id", "")

    if not session_id:
        raise HTTPException(400, "Missing session_id")

    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        logger.error(f"Stripe session retrieve error: {e}")
        raise HTTPException(400, "Invalid session")

    if session.payment_status != "paid":
        return {"verified": False, "reason": "Payment not completed"}

    # Verify fingerprint matches
    meta_fp = session.metadata.get("fingerprint", "")
    if meta_fp != fp:
        raise HTTPException(403, "Session does not belong to this user")

    purchase_type = session.metadata.get("type", "")

    # Idempotency: check if this session was already processed (by webhook or previous verify call)
    if not mark_session_processed(session_id, fp, purchase_type):
        # Already processed — just return success without double-crediting
        return {"verified": True, "type": purchase_type, "already_processed": True}

    if purchase_type == "single":
        add_single_report_purchase(fp)
        if fp in users_db:
            users_db[fp]["single_reports_purchased"] += 1

    elif purchase_type == "pro":
        customer_id = session.get("customer", "") or ""
        update_user_plan(fp, "pro", customer_id)
        if fp in users_db:
            users_db[fp]["is_pro"] = True
            users_db[fp]["plan"] = "pro"
            users_db[fp]["stripe_customer_id"] = customer_id

    return {"verified": True, "type": purchase_type}


# --- Share link for texting/sharing ---
share_tokens_mem = {}  # in-memory fallback only


def save_share_token(token: str, report_id: str, fingerprint: str, expires_at: datetime):
    """Save share token to PostgreSQL (with in-memory fallback)."""
    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO share_tokens (token, report_id, fingerprint, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (token) DO UPDATE SET expires_at = EXCLUDED.expires_at
            """, (token, report_id, fingerprint, expires_at))
            conn.commit()
            cur.close()
            return
        except Exception as e:
            logger.error(f"DB save share token error: {e}")
        finally:
            if conn:
                release_db_connection(conn)
    # Fallback to in-memory
    share_tokens_mem[token] = {"report_id": report_id, "fingerprint": fingerprint, "expires": expires_at.timestamp()}


def get_share_token(token: str) -> Optional[dict]:
    """Get share token from PostgreSQL (with in-memory fallback)."""
    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM share_tokens WHERE token = %s", (token,))
            row = cur.fetchone()
            cur.close()
            if row:
                return {"report_id": row["report_id"], "fingerprint": row["fingerprint"], "expires": row["expires_at"].timestamp()}
            return None
        except Exception as e:
            logger.error(f"DB get share token error: {e}")
        finally:
            if conn:
                release_db_connection(conn)
    return share_tokens_mem.get(token)


def delete_share_token(token: str):
    """Delete expired share token."""
    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("DELETE FROM share_tokens WHERE token = %s", (token,))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"DB delete share token error: {e}")
        finally:
            if conn:
                release_db_connection(conn)
    share_tokens_mem.pop(token, None)


@app.post("/api/report/{report_id}/share")
async def create_share_link(report_id: str, request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Generate a shareable link for the report PDF (valid 7 days)."""
    fp = get_fingerprint(request, x_fingerprint)
    report = get_report_from_db(report_id) or reports_db.get(report_id)

    if not report:
        raise HTTPException(404, "Report not found")
    if report["fingerprint"] != fp:
        raise HTTPException(403, "Not your report")

    # Generate token and persist to DB
    token = secrets.token_urlsafe(16)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    save_share_token(token, report_id, fp, expires_at)

    share_url = f"{BASE_URL}/share/{token}"
    return {"share_url": share_url, "expires_in": "7 days"}


@app.get("/share/{token}")
async def download_shared_report(token: str):
    """Download a shared report PDF via token (no auth needed)."""
    share = get_share_token(token)
    if not share:
        return HTMLResponse("<h2>Link expired or invalid.</h2><p><a href='/'>Go to Condition Report</a></p>", status_code=404)
    if time.time() > share["expires"]:
        delete_share_token(token)
        return HTMLResponse("<h2>This link has expired.</h2><p><a href='/'>Generate a new report</a></p>", status_code=410)

    report_id = share["report_id"]
    report = get_report_from_db(report_id) or reports_db.get(report_id)
    if not report:
        return HTMLResponse("<h2>Report not found.</h2>", status_code=404)

    pdf_path = report.get("pdf_path")
    if not pdf_path or not os.path.exists(pdf_path):
        return HTMLResponse("<h2>PDF not found.</h2>", status_code=404)

    address = report.get("property_info", {}).get("address", "property")
    safe_address = "".join(c for c in address if c.isalnum() or c in " -_").strip().replace(" ", "_") or "report"
    filename = f"Condition_Report_{safe_address}.pdf"

    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


@app.post("/api/checkout/pro")
@limiter.limit("10/minute")
async def checkout_pro(request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Create Stripe checkout for Pro subscription."""
    fp = get_fingerprint(request, x_fingerprint)
    body = await request.json()
    billing = body.get("billing", "monthly")

    price_id = STRIPE_PRICE_MONTHLY if billing == "monthly" else STRIPE_PRICE_ANNUAL
    if not price_id:
        raise HTTPException(500, "Stripe not configured — pro price missing")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=f"{BASE_URL}?payment=success&type={'annual' if billing == 'annual' else 'pro'}&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{BASE_URL}?payment=cancelled",
        metadata={"fingerprint": fp, "type": "pro"},
    )
    return {"checkout_url": session.url}


@app.post("/api/email-report")
@limiter.limit("10/minute")
async def email_report(request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Email a PDF report to the specified address."""
    fp = get_fingerprint(request, x_fingerprint)
    body = await request.json()
    report_id = body.get("report_id", "")
    email = body.get("email", "").strip()

    if not email or "@" not in email:
        raise HTTPException(400, "Valid email address required")

    report = get_report_from_db(report_id) or reports_db.get(report_id)
    if not report:
        raise HTTPException(404, "Report not found")
    if report["fingerprint"] != fp:
        raise HTTPException(403, "Not your report")

    pdf_path = report.get("pdf_path")
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(404, "PDF not found")

    if not RESEND_API_KEY:
        raise HTTPException(500, "Email not configured")

    # Read PDF and base64 encode for Resend attachment
    pdf_bytes = Path(pdf_path).read_bytes()
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    address = report.get("property_info", {}).get("address", "Property")
    report_type = report.get("report_type", "Inspection")
    report_date = report.get("date", "")

    async with httpx.AsyncClient() as http:
        res = await http.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": "Condition Report <reports@condition-report.com>",
                "to": [email],
                "subject": f"Property Condition Report — {address}",
                "html": f"""
                    <div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto;padding:20px;background:#fff;">
                        <div style="text-align:center;padding:16px 0;border-bottom:3px solid #2563eb;">
                            <span style="font-size:24px;font-weight:800;color:#1a1a2e;">Condition</span><span style="font-size:24px;font-weight:800;color:#2563eb;">Report</span>
                            <br><span style="font-size:11px;color:#999;font-weight:500;">a DataWeaveAI company</span>
                        </div>
                        <div style="padding:24px 0;">
                            <h2 style="color:#1a1a2e;margin:0 0 8px;font-size:20px;">{report_type} Inspection Report</h2>
                            <p style="color:#555;margin:0 0 20px;font-size:15px;">Your condition report for <strong>{address}</strong> is attached as a PDF.</p>
                            <table style="width:100%;border-collapse:collapse;margin:16px 0;background:#f8f9fa;border-radius:8px;">
                                <tr><td style="padding:10px 14px;border-bottom:1px solid #e5e7eb;color:#888;font-size:13px;width:120px;">Address</td><td style="padding:10px 14px;border-bottom:1px solid #e5e7eb;font-size:14px;font-weight:600;">{address}</td></tr>
                                <tr><td style="padding:10px 14px;border-bottom:1px solid #e5e7eb;color:#888;font-size:13px;">Report Type</td><td style="padding:10px 14px;border-bottom:1px solid #e5e7eb;font-size:14px;">{report_type}</td></tr>
                                <tr><td style="padding:10px 14px;border-bottom:1px solid #e5e7eb;color:#888;font-size:13px;">Date</td><td style="padding:10px 14px;border-bottom:1px solid #e5e7eb;font-size:14px;">{report_date}</td></tr>
                                <tr><td style="padding:10px 14px;color:#888;font-size:13px;">Rooms</td><td style="padding:10px 14px;font-size:14px;">{len(report.get('rooms', []))}</td></tr>
                            </table>
                            <p style="color:#555;font-size:13px;margin:20px 0 0;">Open the attached PDF to view the full report with photos, condition ratings, and action items.</p>
                        </div>
                        <div style="border-top:1px solid #e5e7eb;padding:16px 0 0;text-align:center;">
                            <p style="color:#999;font-size:11px;margin:0;">Generated by <a href="https://condition-report.com" style="color:#2563eb;text-decoration:none;">condition-report.com</a></p>
                        </div>
                    </div>
                """,
                "attachments": [{
                    "filename": f"Condition_Report_{report_date.replace(' ', '_')}.pdf",
                    "content": pdf_b64,
                }],
            },
        )

    if res.status_code >= 400:
        raise HTTPException(500, f"Failed to send email: {res.text}")

    return {"sent": True, "email": email}


@app.post("/api/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(400, "Invalid webhook")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        stripe_session_id = session.get("id", "")
        fp = session.get("metadata", {}).get("fingerprint", "")
        purchase_type = session.get("metadata", {}).get("type", "")

        if fp:
            # Idempotency: skip if already processed by /api/verify-payment
            if not mark_session_processed(stripe_session_id, fp, purchase_type):
                logger.info(f"Webhook skipping already-processed session {stripe_session_id}")
            else:
                if purchase_type == "single":
                    add_single_report_purchase(fp)
                    if fp in users_db:
                        users_db[fp]["single_reports_purchased"] += 1
                elif purchase_type == "pro":
                    customer_id = session.get("customer", "")
                    update_user_plan(fp, "pro", customer_id)
                    if fp in users_db:
                        users_db[fp]["is_pro"] = True
                        users_db[fp]["plan"] = "pro"
                        users_db[fp]["stripe_customer_id"] = customer_id

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        customer_id = sub.get("customer", "")
        # DB: find user by stripe_customer_id and downgrade
        if POSTGRES_AVAILABLE:
            conn = None
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("""
                    UPDATE users SET plan = 'free', updated_at = CURRENT_TIMESTAMP
                    WHERE stripe_customer_id = %s
                """, (customer_id,))
                cur.execute("""
                    UPDATE accounts SET plan = 'free', updated_at = CURRENT_TIMESTAMP
                    WHERE stripe_customer_id = %s
                """, (customer_id,))
                conn.commit()
                cur.close()
            except Exception as e:
                logger.error(f"DB subscription cancel error: {e}")
            finally:
                if conn:
                    release_db_connection(conn)
        # In-memory fallback
        for fp_key, user in users_db.items():
            if user.get("stripe_customer_id") == customer_id:
                user["is_pro"] = False
                user["plan"] = "free"
                break

    return {"received": True}


# --- Account Signup / Login ---

@app.post("/api/account/signup")
@limiter.limit("5/minute")
async def account_signup(request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Create account with email + password. Links fingerprint to email."""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password", "")
    name = body.get("name", "").strip()

    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    fp = get_fingerprint(request, x_fingerprint)

    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)

            # Check if email already exists
            cur.execute("SELECT id FROM accounts WHERE email = %s", (email,))
            if cur.fetchone():
                raise HTTPException(409, "An account with this email already exists. Try logging in.")

            # Hash password with bcrypt (per-user salt, slow hash)
            pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

            # Check if this fingerprint has an existing user row (to preserve purchase history)
            cur.execute("SELECT plan, reports_used, single_reports_purchased, stripe_customer_id FROM users WHERE fingerprint = %s", (fp,))
            existing_user = cur.fetchone()

            plan = "free"
            stripe_cid = None
            if existing_user:
                plan = existing_user["plan"] or "free"
                stripe_cid = existing_user.get("stripe_customer_id")

            # Check Stripe for active subscription
            try:
                customers = stripe.Customer.list(email=email, limit=1)
                if customers.data:
                    subscriptions = stripe.Subscription.list(customer=customers.data[0].id, status="active", limit=1)
                    if subscriptions.data:
                        plan = "pro"
                        stripe_cid = customers.data[0].id
            except Exception:
                pass

            # Create account
            cur.execute("""
                INSERT INTO accounts (email, password_hash, name, plan, stripe_customer_id, fingerprint)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (email, pw_hash, name, plan, stripe_cid, fp))

            # Link email to user record
            cur.execute("""
                UPDATE users SET email = %s, plan = %s, stripe_customer_id = COALESCE(%s, stripe_customer_id), updated_at = CURRENT_TIMESTAMP
                WHERE fingerprint = %s
            """, (email, plan, stripe_cid, fp))

            # Multi-device session tracking
            cur.execute("""
                INSERT INTO account_sessions (email, fingerprint) VALUES (%s, %s)
                ON CONFLICT (email, fingerprint) DO NOTHING
            """, (email, fp))

            conn.commit()
            cur.close()

            return {"success": True, "email": email, "plan": plan, "name": name}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Signup error: {e}")
            raise HTTPException(500, "Signup failed")
        finally:
            if conn:
                release_db_connection(conn)
    else:
        raise HTTPException(503, "Database not available")


@app.post("/api/account/login")
@limiter.limit("5/minute")
async def account_login(request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Login with email + password. Returns account info and links fingerprint."""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password", "")

    if not email or not password:
        raise HTTPException(400, "Email and password required")

    fp = get_fingerprint(request, x_fingerprint)

    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)

            cur.execute("SELECT * FROM accounts WHERE email = %s", (email,))
            account = cur.fetchone()

            if not account:
                raise HTTPException(401, "Invalid email or password")

            # Verify password with bcrypt (supports legacy SHA-256 migration)
            stored_hash = account["password_hash"]
            if stored_hash.startswith("$2"):
                # bcrypt hash
                if not bcrypt.checkpw(password.encode(), stored_hash.encode()):
                    raise HTTPException(401, "Invalid email or password")
            else:
                # Legacy SHA-256 hash — verify then upgrade to bcrypt
                legacy_hash = hashlib.sha256((password + "cr_salt_2026").encode()).hexdigest()
                if stored_hash != legacy_hash:
                    raise HTTPException(401, "Invalid email or password")
                # Upgrade to bcrypt on successful login
                new_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
                cur.execute("UPDATE accounts SET password_hash = %s WHERE email = %s", (new_hash, email))

            plan = account["plan"] or "free"

            # Auto-heal: check Stripe if plan shows free
            if plan == "free":
                try:
                    customers = stripe.Customer.list(email=email, limit=1)
                    if customers.data:
                        subscriptions = stripe.Subscription.list(customer=customers.data[0].id, status="active", limit=1)
                        if subscriptions.data:
                            plan = "pro"
                            cur.execute("UPDATE accounts SET plan = 'pro', stripe_customer_id = %s WHERE email = %s",
                                        (customers.data[0].id, email))
                except Exception:
                    pass

            # Link fingerprint to this account's email in users table
            cur.execute("""
                INSERT INTO users (fingerprint, email, plan)
                VALUES (%s, %s, %s)
                ON CONFLICT (fingerprint) DO UPDATE SET
                    email = EXCLUDED.email,
                    plan = GREATEST(users.plan, EXCLUDED.plan),
                    updated_at = CURRENT_TIMESTAMP
            """, (fp, email, plan))

            # Also link fingerprint to account (keeps latest for quick lookup)
            cur.execute("UPDATE accounts SET fingerprint = %s, updated_at = CURRENT_TIMESTAMP WHERE email = %s", (fp, email))

            # Multi-device session tracking
            cur.execute("""
                INSERT INTO account_sessions (email, fingerprint) VALUES (%s, %s)
                ON CONFLICT (email, fingerprint) DO NOTHING
            """, (email, fp))

            conn.commit()
            cur.close()

            return {
                "success": True,
                "email": email,
                "name": account.get("name", ""),
                "plan": plan,
                "company": account.get("company", ""),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Login error: {e}")
            raise HTTPException(500, "Login failed")
        finally:
            if conn:
                release_db_connection(conn)
    else:
        raise HTTPException(503, "Database not available")


@app.get("/api/account/profile")
async def account_profile(request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Get account profile for logged-in user (by fingerprint -> email lookup)."""
    fp = get_fingerprint(request, x_fingerprint)

    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor(cursor_factory=RealDictCursor)

            # Find account linked to this fingerprint (direct match first, then session table)
            cur.execute("""
                SELECT a.email, a.name, a.company, a.plan, a.created_at, a.stripe_customer_id
                FROM accounts a WHERE a.fingerprint = %s
            """, (fp,))
            account = cur.fetchone()

            if not account:
                # Multi-device fallback: check session table
                cur.execute("""
                    SELECT a.email, a.name, a.company, a.plan, a.created_at, a.stripe_customer_id
                    FROM account_sessions s
                    JOIN accounts a ON a.email = s.email
                    WHERE s.fingerprint = %s
                    ORDER BY s.created_at DESC LIMIT 1
                """, (fp,))
                account = cur.fetchone()

            if not account:
                return {"logged_in": False}

            # Get report count across all devices for this account
            cur.execute("""
                SELECT COUNT(*) as count FROM reports
                WHERE fingerprint IN (SELECT fingerprint FROM account_sessions WHERE email = %s)
                   OR fingerprint = %s
            """, (account["email"], fp))
            report_count = cur.fetchone()["count"]

            # Get user purchase data
            cur.execute("SELECT reports_used, single_reports_purchased FROM users WHERE fingerprint = %s", (fp,))
            user_data = cur.fetchone()

            cur.close()

            return {
                "logged_in": True,
                "email": account["email"],
                "name": account.get("name", ""),
                "company": account.get("company", ""),
                "plan": account["plan"] or "free",
                "reports_generated": report_count,
                "reports_used": user_data["reports_used"] if user_data else 0,
                "single_reports_purchased": user_data["single_reports_purchased"] if user_data else 0,
                "member_since": account["created_at"].strftime("%B %Y") if account["created_at"] else "",
                "has_subscription": account["plan"] in ("pro",),
            }
        except Exception as e:
            logger.error(f"Profile error: {e}")
        finally:
            if conn:
                release_db_connection(conn)

    return {"logged_in": False}


@app.post("/api/account/update")
async def account_update(request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Update account name/company."""
    fp = get_fingerprint(request, x_fingerprint)
    body = await request.json()

    if POSTGRES_AVAILABLE:
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                UPDATE accounts SET name = %s, company = %s, updated_at = CURRENT_TIMESTAMP
                WHERE fingerprint = %s
            """, (body.get("name", ""), body.get("company", ""), fp))
            conn.commit()
            cur.close()
            return {"success": True}
        except Exception as e:
            logger.error(f"Account update error: {e}")
            raise HTTPException(500, "Update failed")
        finally:
            if conn:
                release_db_connection(conn)

    raise HTTPException(503, "Database not available")


# Mount static files
app.mount("/static", StaticFiles(directory="landing"), name="static")
