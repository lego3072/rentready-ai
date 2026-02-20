"""
RentReady AI — Property Condition Report Generator
A DataWeaveAI Company

Upload room photos → AI describes condition → Professional PDF report
"""

import os
import uuid
import json
import base64
import hashlib
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

import anthropic
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

load_dotenv()

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

# --- In-memory storage (swap for DB later) ---
# fingerprint -> {reports_used: int, is_pro: bool, stripe_customer_id: str, reports: [...]}
users_db: dict = {}
# report_id -> report data
reports_db: dict = {}

app = FastAPI(title="RentReady AI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    """Get or create user record."""
    if fingerprint not in users_db:
        users_db[fingerprint] = {
            "fingerprint": fingerprint,
            "reports_used": 0,
            "is_pro": False,
            "stripe_customer_id": None,
            "single_reports_purchased": 0,
            "reports": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    return users_db[fingerprint]


def check_access(user: dict) -> dict:
    """Check if user can generate a report. Returns {allowed, reason}."""
    # Pro users: unlimited
    if user["is_pro"]:
        return {"allowed": True, "reason": "pro"}

    # Free trial: 1 report (3 rooms max)
    if user["reports_used"] == 0:
        return {"allowed": True, "reason": "free_trial", "max_rooms": 3}

    # Purchased single reports
    reports_remaining = user["single_reports_purchased"] - (user["reports_used"] - 1)  # -1 for free trial
    if reports_remaining > 0:
        return {"allowed": True, "reason": "single_purchase", "remaining": reports_remaining}

    return {"allowed": False, "reason": "limit_reached"}


def encode_image(image_bytes: bytes) -> str:
    """Base64 encode image for Claude Vision API."""
    return base64.standard_b64encode(image_bytes).decode("utf-8")


def resize_image(image_bytes: bytes, max_size: int = 1024) -> bytes:
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


async def analyze_room_photos(room_name: str, photos: list[bytes]) -> str:
    """Send room photos to Claude Vision and get condition description."""
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

    content.append({
        "type": "text",
        "text": f"""You are a professional property inspector creating a move-in/move-out condition report.

Analyze the photo(s) of the "{room_name}" and describe the condition in detail.

For EACH of these categories that are visible, provide a condition assessment:
- Walls & Paint (color, condition, marks, holes, cracks)
- Flooring (type, condition, stains, damage)
- Ceiling (condition, stains, cracks)
- Windows (condition, screens, blinds/curtains)
- Doors (condition, locks, handles)
- Lighting/Electrical (fixtures, outlets, switches)
- Fixtures & Appliances (if visible — counters, cabinets, sink, toilet, tub, etc.)
- General Cleanliness
- Notable Damage or Issues

Write in professional, objective language suitable for a legal document. Be specific about locations (e.g., "north wall", "near the window", "left of the door"). Use terms like "good condition", "normal wear", "minor scuff", "significant damage", etc.

If something is NOT visible in the photos, skip it — don't guess.

Format as a clean paragraph per category, no bullet points. Keep each category to 1-2 sentences.""",
    })

    response = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1500,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text


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
        f"Generated by RentReady AI — {report_data.get('report_type', 'Move-In')} Inspection",
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
    info_data.append(["Report Type:", report_data.get("report_type", "Move-In")])
    info_data.append(["Report ID:", report_id[:12]])

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

    # Room sections
    for room in report_data.get("rooms", []):
        elements.append(Paragraph(room["name"], room_title_style))

        # Add room photos
        photo_row = []
        for photo_path in room.get("photo_paths", [])[:3]:
            if os.path.exists(photo_path):
                try:
                    img = RLImage(photo_path, width=2.1 * inch, height=1.6 * inch)
                    img.hAlign = "LEFT"
                    photo_row.append(img)
                except Exception:
                    pass

        if photo_row:
            # Create a table for photos side by side
            photo_table = Table([photo_row], colWidths=[2.2 * inch] * len(photo_row))
            photo_table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]))
            elements.append(photo_table)

        # Condition description
        description = room.get("description", "No description available.")
        for para in description.split("\n\n"):
            para = para.strip()
            if para:
                elements.append(Paragraph(para, body_style))

        elements.append(Spacer(1, 10))

    # Signature section
    elements.append(Spacer(1, 30))
    elements.append(Paragraph("Signatures", room_title_style))

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

    # Footer
    elements.append(Spacer(1, 30))
    elements.append(Paragraph(
        f"This report was generated on {datetime.now().strftime('%B %d, %Y at %I:%M %p')} "
        f"using RentReady AI (rentready.ai). Photos and descriptions are provided as documentation "
        f"of property condition at the time of inspection.",
        meta_style,
    ))

    doc.build(elements)
    return str(pdf_path)


# --- Routes ---

@app.get("/health")
async def health():
    return {"status": "ok", "service": "rentready-ai"}


@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path("landing/app.html")
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>RentReady AI</h1><p>App loading...</p>")


@app.get("/api/user/status")
async def user_status(request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Get user's current status — reports used, access level."""
    fp = get_fingerprint(request, x_fingerprint)
    user = get_user(fp)
    access = check_access(user)
    return {
        "fingerprint": fp,
        "reports_used": user["reports_used"],
        "is_pro": user["is_pro"],
        "single_reports_purchased": user["single_reports_purchased"],
        "access": access,
        "reports": [{"id": r["id"], "date": r["date"], "address": r.get("property_info", {}).get("address", "")} for r in user["reports"]],
    }


@app.post("/api/upload-photos")
async def upload_photos(
    request: Request,
    photos: list[UploadFile] = File(...),
    room_names: str = Form(...),
    x_fingerprint: Optional[str] = Header(None),
):
    """Upload photos and assign room names. Returns photo IDs for analysis."""
    fp = get_fingerprint(request, x_fingerprint)
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

    # Free trial: max 3 rooms
    if access.get("reason") == "free_trial" and len(rooms_data) > 3:
        rooms_data = rooms_data[:3]

    # Analyze each room
    analyzed_rooms = []
    for room in rooms_data:
        room_name = room["room_name"]
        photo_paths = [p["path"] for p in room.get("photos", [])]

        # Read photo bytes
        photo_bytes_list = []
        for path in photo_paths:
            if os.path.exists(path):
                photo_bytes_list.append(Path(path).read_bytes())

        if not photo_bytes_list:
            continue

        # Call Claude Vision
        description = await analyze_room_photos(room_name, photo_bytes_list)

        analyzed_rooms.append({
            "name": room_name,
            "description": description,
            "photo_paths": photo_paths,
            "photo_count": len(photo_bytes_list),
        })

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
    report = reports_db.get(report_id)

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
    filename = f"RentReady_{safe_address}_{report['date'].replace(' ', '_')}.pdf"

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=filename,
    )


@app.get("/api/report/{report_id}")
async def get_report(report_id: str, request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Get report data (without PDF)."""
    fp = get_fingerprint(request, x_fingerprint)
    report = reports_db.get(report_id)

    if not report:
        raise HTTPException(404, "Report not found")
    if report["fingerprint"] != fp:
        raise HTTPException(403, "Not your report")

    return {
        "id": report["id"],
        "date": report["date"],
        "report_type": report["report_type"],
        "property_info": report["property_info"],
        "rooms": [{"name": r["name"], "description": r["description"], "photo_count": r.get("photo_count", 0)} for r in report.get("rooms", [])],
        "pdf_url": f"/api/report/{report_id}/pdf",
    }


# --- Stripe Checkout ---

@app.post("/api/checkout/single")
async def checkout_single(request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Create Stripe checkout for a single report."""
    fp = get_fingerprint(request, x_fingerprint)

    if not STRIPE_PRICE_SINGLE:
        raise HTTPException(500, "Stripe not configured — single report price missing")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": STRIPE_PRICE_SINGLE, "quantity": 1}],
        mode="payment",
        success_url=f"{BASE_URL}?payment=success&type=single",
        cancel_url=f"{BASE_URL}?payment=cancelled",
        metadata={"fingerprint": fp, "type": "single"},
    )
    return {"checkout_url": session.url}


@app.post("/api/checkout/pro")
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
        success_url=f"{BASE_URL}?payment=success&type=pro",
        cancel_url=f"{BASE_URL}?payment=cancelled",
        metadata={"fingerprint": fp, "type": "pro"},
    )
    return {"checkout_url": session.url}


@app.post("/api/email-report")
async def email_report(request: Request, x_fingerprint: Optional[str] = Header(None)):
    """Email a PDF report to the specified address."""
    fp = get_fingerprint(request, x_fingerprint)
    body = await request.json()
    report_id = body.get("report_id", "")
    email = body.get("email", "").strip()

    if not email or "@" not in email:
        raise HTTPException(400, "Valid email address required")

    report = reports_db.get(report_id)
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
                "from": "RentReady AI <reports@dataweaveai.com>",
                "to": [email],
                "subject": f"Property Condition Report — {address}",
                "html": f"""
                    <div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
                        <h2 style="color:#1a1a2e;">Property Condition Report</h2>
                        <p style="color:#555;">Your <strong>{report_type}</strong> report for <strong>{address}</strong> is attached.</p>
                        <table style="width:100%;border-collapse:collapse;margin:16px 0;">
                            <tr><td style="padding:8px;border-bottom:1px solid #eee;color:#888;">Address</td><td style="padding:8px;border-bottom:1px solid #eee;">{address}</td></tr>
                            <tr><td style="padding:8px;border-bottom:1px solid #eee;color:#888;">Report Type</td><td style="padding:8px;border-bottom:1px solid #eee;">{report_type}</td></tr>
                            <tr><td style="padding:8px;border-bottom:1px solid #eee;color:#888;">Date</td><td style="padding:8px;border-bottom:1px solid #eee;">{report_date}</td></tr>
                            <tr><td style="padding:8px;color:#888;">Rooms</td><td style="padding:8px;">{len(report.get('rooms', []))}</td></tr>
                        </table>
                        <p style="color:#999;font-size:12px;margin-top:24px;">Generated by RentReady AI — A DataWeaveAI Company</p>
                    </div>
                """,
                "attachments": [{
                    "filename": f"RentReady_Report_{report_date.replace(' ', '_')}.pdf",
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
        fp = session.get("metadata", {}).get("fingerprint", "")
        purchase_type = session.get("metadata", {}).get("type", "")

        if fp and fp in users_db:
            user = users_db[fp]
            if purchase_type == "single":
                user["single_reports_purchased"] += 1
            elif purchase_type == "pro":
                user["is_pro"] = True
                user["stripe_customer_id"] = session.get("customer", "")

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        customer_id = sub.get("customer", "")
        for fp, user in users_db.items():
            if user.get("stripe_customer_id") == customer_id:
                user["is_pro"] = False
                break

    return {"received": True}


# Mount static files
app.mount("/static", StaticFiles(directory="landing"), name="static")
