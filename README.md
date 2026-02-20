# Condition Report — AI Property Condition Reports

**URL**: https://condition-report.com
**Brand**: ConditionReport (a DataWeaveAI company)
**Pitch**: "TurboTax for property condition reports"
**Built in**: ~6 sessions over 3 days (Feb 2026)

Upload room photos → AI analyzes condition with structured ratings → Professional PDF report in 60 seconds.

---

## Quick Start (Local Dev)

```bash
# Install dependencies
pip install -r requirements.txt

# Set env vars (see Environment Variables below)
cp .env.example .env  # fill in values

# Run
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

Open http://localhost:8000

---

## Architecture

**Single-file backend + single-file frontend.** No framework, no build step, no npm.

| Layer | Tech | File |
|-------|------|------|
| Backend | Python 3.11, FastAPI, Pydantic | `api.py` (~2370 lines) |
| Frontend | Vanilla HTML/CSS/JS (SPA) | `landing/app.html` (~2350 lines) |
| AI | Claude Haiku 4.5 (primary), Sonnet 4.5 (fallback) | Via Anthropic SDK |
| Database | PostgreSQL (Railway) | psycopg2 connection pool |
| Payments | Stripe (checkout + webhooks) | stripe SDK |
| Email | Resend API | httpx POST calls |
| PDF | ReportLab | reportlab |
| Images | Pillow | Resize before AI analysis |
| Hosting | Railway (Dockerfile) | Auto-deploy from GitHub |
| DNS/CDN | Cloudflare (proxied, Full strict SSL) | condition-report.com |

### Why Single-File?

Speed of iteration. One backend file, one frontend file. No routing config, no component tree, no webpack. Search `Cmd+F`, edit, deploy. Built the entire product in 3 days this way.

---

## File Structure

```
rentready-ai/
├── api.py                 # Entire backend — FastAPI app, all endpoints, AI, PDF, Stripe, auth
├── landing/
│   └── app.html           # Entire frontend — SPA with all views, CSS, JS inline
├── requirements.txt       # Python dependencies (13 packages)
├── Dockerfile             # python:3.11-slim, fonts-dejavu-core for PDF
├── railway.toml           # Railway config: Dockerfile builder, health check
├── uploads/               # Temporary photo storage (auto-cleaned every 24h)
├── reports/               # Generated PDFs + signatures (auto-cleaned every 24h)
└── README.md              # This file
```

---

## Environment Variables

All set in Railway dashboard (or `.env` for local dev):

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Claude AI API key |
| `STRIPE_SECRET_KEY` | Stripe secret key |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |
| `STRIPE_PRICE_SINGLE` | Stripe Price ID for $4.99 single report |
| `STRIPE_PRICE_MONTHLY` | Stripe Price ID for $29/mo Pro |
| `STRIPE_PRICE_ANNUAL` | Stripe Price ID for $249/yr Pro |
| `RESEND_API_KEY` | Resend API key (scoped to condition-report.com domain!) |
| `BASE_URL` | `https://condition-report.com` (used for email links, share URLs) |
| `DATABASE_URL` | PostgreSQL connection string (auto-set by Railway) |

**Critical**: The Resend API key MUST be scoped to the `condition-report.com` domain. A key scoped to a different domain (e.g., dataweaveai.com) will get 403 errors on email send.

**Critical**: Stripe Price IDs are specific to Condition Report products. The app shares a Stripe account with DataWeaveAI, so subscription checks MUST filter by these price IDs to prevent cross-product bleed.

---

## Database Schema (PostgreSQL)

7 tables, all created automatically on startup via `init_database()`:

### `users`
Fingerprint-based user tracking (no signup required for free trial).
```sql
id SERIAL PRIMARY KEY,
fingerprint VARCHAR(255) UNIQUE,
email VARCHAR(255),
plan VARCHAR(32) DEFAULT 'free',     -- 'free' | 'pro'
reports_used INTEGER DEFAULT 0,
single_reports_purchased INTEGER DEFAULT 0,
stripe_customer_id VARCHAR(255),
created_at TIMESTAMP, updated_at TIMESTAMP
```

### `accounts`
Email/password accounts (optional — users can use the app without signing up).
```sql
id SERIAL PRIMARY KEY,
email VARCHAR(255) UNIQUE NOT NULL,
password_hash VARCHAR(255) NOT NULL,  -- bcrypt with per-user salt
name VARCHAR(255), company VARCHAR(255),
plan VARCHAR(32) DEFAULT 'free',
stripe_customer_id VARCHAR(255),
fingerprint VARCHAR(255),
email_verified BOOLEAN DEFAULT FALSE,
created_at TIMESTAMP, updated_at TIMESTAMP
```

### `account_sessions`
Maps fingerprints to account emails for multi-device support.
```sql
email VARCHAR(255) NOT NULL,
fingerprint VARCHAR(255) NOT NULL,
UNIQUE(email, fingerprint)
```

### `reports`
Persisted report data (rooms JSON, property info, PDF path).
```sql
id VARCHAR(255) PRIMARY KEY,
fingerprint VARCHAR(255) NOT NULL,
email VARCHAR(255),
report_type VARCHAR(64),             -- 'Move-In' | 'Move-Out' | 'Periodic'
property_info JSONB DEFAULT '{}',
rooms JSONB DEFAULT '[]',
pdf_path VARCHAR(512),
created_at TIMESTAMP
```

### `share_tokens`
Shareable report links (7-day expiry, token-based, no auth needed).
```sql
token VARCHAR(255) PRIMARY KEY,
report_id VARCHAR(255) NOT NULL,
fingerprint VARCHAR(255) NOT NULL,
expires_at TIMESTAMP NOT NULL
```

### `processed_stripe_sessions`
Idempotency guard — prevents double-crediting from webhook + verify-payment race.
```sql
session_id VARCHAR(255) PRIMARY KEY,
fingerprint VARCHAR(255) NOT NULL,
purchase_type VARCHAR(32) NOT NULL
```

### `email_verification_tokens` / `password_reset_tokens`
Standard token tables for email verification (24h expiry) and password reset (1h expiry).

---

## API Endpoints

### Core Product
| Method | Path | Description | Auth |
|--------|------|-------------|------|
| `GET` | `/` | Serve frontend SPA | None |
| `GET` | `/health` | Health check | None |
| `GET` | `/api/user/status` | User status, reports, access level | Fingerprint |
| `POST` | `/api/upload-photos` | Upload room photos (20/min) | Fingerprint |
| `POST` | `/api/analyze` | AI analysis + PDF generation (10/min) | Fingerprint |
| `GET` | `/api/report/{id}/pdf` | Download report PDF | Fingerprint (owner) |
| `GET` | `/api/report/{id}` | Get report data (JSON) | Fingerprint (owner) |
| `POST` | `/api/report/{id}/signature` | Add signature, regenerate PDF | Fingerprint (owner) |
| `POST` | `/api/report/{id}/share` | Create 7-day share link | Fingerprint (owner) |
| `GET` | `/share/{token}` | Download shared PDF (no auth) | None |

### Payments
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/checkout/single` | Stripe checkout for $4.99 single report |
| `POST` | `/api/checkout/pro` | Stripe checkout for Pro subscription |
| `POST` | `/api/verify-payment` | Verify Stripe session + credit user |
| `POST` | `/api/webhook` | Stripe webhook handler |

### Email
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/email-report` | Email PDF as attachment via Resend |

### Account System
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/account/signup` | Create account (5/min) |
| `POST` | `/api/account/login` | Login + link fingerprint (5/min) |
| `GET` | `/api/account/profile` | Get profile by fingerprint |
| `POST` | `/api/account/update` | Update name/company |
| `GET` | `/api/account/verify-email` | Email verification via token link |
| `POST` | `/api/account/resend-verification` | Resend verification email (3/min) |
| `POST` | `/api/account/request-reset` | Request password reset (3/min) |
| `POST` | `/api/account/reset-password` | Reset password with token (5/min) |

### SEO
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/og-image.png` | Dynamically generated OG image (Pillow) |
| `GET` | `/robots.txt` | Allows /, disallows /api/ and /static/ |
| `GET` | `/sitemap.xml` | Single-URL sitemap |

---

## Authentication Model

**Fingerprint-first, accounts optional.**

- Every request sends `X-Fingerprint` header (browser-generated hash)
- The `users` table tracks fingerprints — no signup needed for free trial
- `accounts` table is for optional email/password signup (preserves data across devices)
- `account_sessions` table links multiple fingerprints to one email (multi-device)
- Login auto-heals: checks Stripe for active subscriptions and upgrades plan if found
- Profile lookup: fingerprint → accounts table → session table fallback

**No API keys, no JWTs, no cookies.** Authentication is entirely fingerprint-based with optional email/password overlay.

---

## AI Analysis Pipeline

### How Room Analysis Works

1. User uploads 1-3 photos per room
2. Photos resized to max 768px (Pillow LANCZOS) and JPEG compressed at 85%
3. Photos base64-encoded and sent to Claude Vision API
4. **All rooms analyzed in parallel** via `asyncio.gather()` + `run_in_executor()` (sync Claude SDK call in thread pool)
5. AI returns structured JSON: overall rating, per-item ratings, summary, flags
6. Results assembled into report → PDF generated → stored in DB

### Performance
- ~6-8 seconds for 4 rooms (parallel, not sequential)
- Uses Haiku 4.5 (fast, cheap) with Sonnet 4.5 fallback if Haiku fails

### AI Prompt Design (Key Decisions)
- **Balanced tone**: "Lead with positives. Note what's in good shape before mentioning concerns."
- **Anti-hallucination**: "ONLY describe what you can ACTUALLY SEE. Do NOT assume, guess, or invent details."
- **Calibrated ratings**: Default is "Good" — only downgrade with visible evidence. Minor cosmetic wear = still Good.
- **Three inspection types** with different prompts:
  - **Move-In**: Baseline documentation, neutral, don't dramatize
  - **Move-Out**: Normal wear vs damage, fair comparison
  - **Periodic**: Maintenance/safety focus, actionable items
- **Structured output**: 8 mandatory checklist items (Walls, Ceiling, Flooring, Windows, Doors, Lighting, Cleanliness, Fixtures). Items not visible = "N/A".
- **max_tokens=1000**: Keeps responses focused and fast

### AI Response Format
```json
{
  "overall_rating": "Good",
  "items": [
    {"name": "Walls", "rating": "Good", "notes": "Light beige paint in good condition..."},
    {"name": "Ceiling", "rating": "N/A", "notes": "Not visible in photo"}
  ],
  "summary": "Room is in good overall condition with clean surfaces...",
  "flags": ["Small water stain near window — recommend monitoring"]
}
```

---

## PDF Generation (ReportLab)

Professional PDF with:
- Property info header (address, type, date, inspection type)
- Room-by-room tables: item name, color-coded rating, condition notes
- Rating colors: Good=#16a34a, Fair=#d97706, Poor=#dc2626, N/A=#9ca3af
- Action items/flags section per room
- Optional digital signature (canvas-drawn, saved as PNG, embedded in PDF)
- Signature lines for landlord + tenant
- Legal disclaimer
- Report ID footer

Fonts: DejaVu Sans (installed via `fonts-dejavu-core` in Dockerfile for Railway Linux compatibility).

---

## Stripe Integration

### Pricing
| Plan | Price | Mode | Stripe Price Env Var |
|------|-------|------|---------------------|
| Single Report | $4.99 | one-time payment | `STRIPE_PRICE_SINGLE` |
| Pro Monthly | $29/mo | subscription | `STRIPE_PRICE_MONTHLY` |
| Pro Annual | $249/yr | subscription | `STRIPE_PRICE_ANNUAL` |

### Flow
1. Frontend calls `/api/checkout/single` or `/api/checkout/pro`
2. Backend creates Stripe Checkout Session with fingerprint in metadata
3. User completes payment on Stripe-hosted page
4. **Two credit paths** (idempotent — only one processes):
   - **Verify path**: Frontend calls `/api/verify-payment` with session_id on redirect
   - **Webhook path**: Stripe calls `/api/webhook` with `checkout.session.completed`
5. `mark_session_processed()` ensures no double-crediting (INSERT ... ON CONFLICT DO NOTHING)

### Subscription Cancellation
- Webhook handles `customer.subscription.deleted`
- Downgrades user plan to "free" in both `users` and `accounts` tables

### Shared Stripe Account
This app shares a Stripe account with DataWeaveAI. Subscription checks filter by Condition Report price IDs only:
```python
cr_price_ids = {STRIPE_PRICE_MONTHLY, STRIPE_PRICE_ANNUAL} - {""}
```

---

## Email System (Resend)

- **From address**: `reports@condition-report.com`
- **Domain**: condition-report.com (DKIM + SPF verified in Resend)
- **API key**: Must be scoped to condition-report.com domain specifically
- **Emails sent**:
  - Report delivery (PDF attachment)
  - Email verification (24h token link)
  - Password reset (1h token link)
- **Error handling**: Logs Resend API errors with status code + response body

---

## Access Control / Paywall

### Who Can Generate Reports
```
Free trial: 1 report (max 4 rooms), no signup required
Single purchase: +1 report per $4.99 purchase
Pro: Unlimited reports
```

### Who Can Access Past Reports
**Everyone who generated or purchased a report can always access it** — download PDF, email, text, share. The paywall only blocks generating NEW reports (Step 1 → Step 2 navigation).

### Access Check Function
```python
def check_access(user):
    if user["is_pro"]: return {"allowed": True, "reason": "pro"}
    if user["reports_used"] == 0: return {"allowed": True, "reason": "free_trial", "max_rooms": 4}
    if user["single_reports_purchased"] > user["reports_used"] - 1:
        return {"allowed": True, "reason": "purchased"}
    return {"allowed": False, "reason": "limit_reached"}
```

---

## Security

### Middleware (every response)
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Content-Security-Policy: upgrade-insecure-requests`

### Rate Limiting (slowapi)
- Uses real client IP via `CF-Connecting-IP` header (Cloudflare) or `X-Forwarded-For` fallback
- Per-endpoint limits: uploads 20/min, analysis 10/min, auth 5/min, email verification 3/min

### Password Security
- bcrypt with per-user salt (`bcrypt.hashpw` + `bcrypt.gensalt()`)
- Legacy SHA-256 hashes auto-upgraded to bcrypt on successful login
- Password reset tokens: 1-hour expiry, single-use, all other tokens invalidated on use

### Other
- HTTPS redirect middleware (HTTP → HTTPS in production)
- Fingerprint ownership checks on all report access endpoints
- No email enumeration on password reset (always returns success)
- File cleanup: uploads and reports older than 24h deleted on startup + hourly

---

## Frontend (landing/app.html)

Single HTML file with everything inline:

### Views (SPA routing via JS)
- **Step 1**: Property info form (address, type, inspection type)
- **Step 2**: Room photo upload (camera + gallery, 3 photos per room, drag to reorder)
- **Step 3**: AI analysis progress (per-room spinners)
- **Results**: Room-by-room ratings, expandable details, action items
- **Past Reports**: List of previously generated reports
- **Account**: Login/signup/profile modal
- **Pricing**: Paywall modal with 3 tiers

### Key Frontend Patterns
- **No framework**: Vanilla JS, `document.getElementById`, template literals
- **Mobile-first**: `viewport-fit=cover`, `user-scalable=no`, touch-optimized buttons
- **Fingerprint auth**: Generated on first visit, stored in localStorage, sent as `X-Fingerprint` header
- **Photo handling**: FileReader → preview → upload via FormData
- **State**: All in JS variables (`currentStep`, `roomsData`, `reportData`, `viewingResults`)
- **`viewingResults` flag**: Prevents paywall from triggering while viewing free trial results

### SEO (inline in app.html)
- OG/Twitter meta tags with dynamic OG image (`/og-image.png`)
- JSON-LD: SoftwareApplication, WebApplication, FAQPage schemas
- Canonical URL, robots meta, keywords
- FAQ schema with 5 property-related questions

---

## Deployment

### Railway
- **Builder**: Dockerfile (see `railway.toml`)
- **Health check**: `GET /health` (300s timeout)
- **Restart policy**: ON_FAILURE, max 10 retries
- **Auto-deploy**: Push to `github.com/lego3072/rentready-ai` → Railway builds and deploys
- **Manual deploy**: `railway up` (more reliable for forcing deploys)
- **Build time**: 1-2 minutes

### Dockerfile
```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p uploads reports
ENV PORT=8000
CMD uvicorn api:app --host 0.0.0.0 --port ${PORT}
```

`fonts-dejavu-core` is required for PDF generation (ReportLab) and OG image generation (Pillow) on Linux.

### Cloudflare
- DNS proxied through Cloudflare
- SSL: Full (strict)
- Cache: May cache OG image aggressively — the `/og-image.png` endpoint returns `Cache-Control: no-cache, no-store` + `CDN-Cache-Control: no-store` headers
- Expect Cloudflare 1016 errors during Railway deploys — just wait

---

## Google Ads / Analytics

- **Google Ads tag**: AW-17926414862
- **Conversion events**: `generate_report`, `sign_up`, `purchase` (via `gtag('event', ...)`)
- **Page tracking**: Scroll depth, CTA clicks, page load attribution

---

## Common Gotchas

1. **Resend API key domain scope**: Must be scoped to condition-report.com. A key scoped to another domain → 403 error.
2. **Shared Stripe account**: Always filter subscriptions by CR-specific price IDs. Without filter, DataWeave Pro subs could bleed through.
3. **Cloudflare caching**: OG image and other dynamic endpoints can get cached. Headers are set to prevent it, but may need manual purge via Cloudflare dashboard.
4. **Fingerprint stickiness**: Same device = same fingerprint = same user. Logging in with a different email on the same device links that fingerprint to the new account. This is by design (security feature).
5. **`python3` not `python`**: On macOS, use `python3` to run scripts.
6. **Railway deploy lag**: 1-2 min build time. Hard refresh (Cmd+Shift+R) needed to see changes.
7. **`analyze_room_photos_sync()` is sync**: Runs in thread pool via `run_in_executor()` for true parallelism with `asyncio.gather()`.
8. **In-memory fallback**: If PostgreSQL is unavailable, the app falls back to in-memory dicts (`users_db`, `reports_db`). Data is lost on restart.

---

## Dependencies (requirements.txt)

```
fastapi==0.109.2          # Web framework
uvicorn[standard]==0.27.1 # ASGI server
python-multipart==0.0.9   # File upload support
anthropic>=0.49.0         # Claude AI SDK
stripe==8.4.0             # Stripe payments
python-dotenv==1.0.1      # .env file loading
Pillow==10.2.0            # Image processing (resize, OG image generation)
reportlab==4.1.0          # PDF generation
aiofiles==23.2.1          # Async file operations
httpx==0.27.0             # HTTP client (Resend API calls)
psycopg2-binary==2.9.9    # PostgreSQL driver
bcrypt==4.1.2             # Password hashing
slowapi==0.1.9            # Rate limiting
```

---

## Build Timeline

| Session | What Was Built |
|---------|---------------|
| 1-2 | Core product: upload → AI analysis → PDF download. Basic paywall. |
| 3 | Structured ratings (Good/Fair/Poor per item), 3 inspection types, Haiku 4.5, parallel analysis, text/share report, email report |
| 4 | Post-payment lockout fix, idempotent Stripe (webhook + verify race), security (bcrypt, rate limiting, security headers) |
| 5 | Email sender (reports@condition-report.com via Resend), Stripe isolation (filter by CR price IDs), clickable pricing toggle |
| 6 | Email verification, password reset, file cleanup (24h, startup + hourly), rate limit IP fix (CF-Connecting-IP), HSTS preload |
| 7 | Resend API key fix (domain scope), OG image cache-busting, past reports paywall removal, spam folder note, comprehensive README |

Total: ~6 working sessions. Functional SaaS product with AI, payments, accounts, email, PDF generation, SEO, and analytics.
