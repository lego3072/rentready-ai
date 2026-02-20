# RentReady AI / Condition Report — Handoff Document
# Last updated: Feb 20, 2026 (Session 4)

## Product
- **URL**: https://condition-report.com
- **What it does**: AI-powered property condition report generator
- **How it works**: Upload room photos → AI analyzes condition with structured ratings → Professional PDF with checklist, ratings, flags
- **Pitch**: "TurboTax for property condition reports"
- **Target users**: Landlords, property managers, tenants
- **Mostly used on MOBILE** — all design decisions must be mobile-first

## Stack
- **Backend**: Python/FastAPI (`api.py` ~1500 lines)
- **Frontend**: Single HTML file (`landing/app.html` ~1700 lines) — no framework, inline CSS/JS
- **AI**: Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) with Sonnet 4.5 fallback (`claude-sonnet-4-5-20250929`)
- **DB**: PostgreSQL on Railway
- **Payments**: Stripe (single $4.99, monthly $29, annual $249)
- **Email**: Resend API (sender: `joseph@dataweaveai.com` — see note below)
- **Hosting**: Railway (auto-deploy from github.com/lego3072/rentready-ai)
- **DNS**: Cloudflare (proxied)
- **Project dir**: `/Users/josephvarga/Desktop/rentready-ai`

## What's Working (All confirmed via end-to-end API audit)

### Core Flow
- **Step 1**: Property info form (address, unit, tenant/landlord name, report type)
- **Step 2**: Room photos upload (up to 3 photos per room, up to 4 rooms free trial)
- **Step 3**: AI analysis → structured report with per-item ratings

### Three Inspection Types (each has specific AI rules)
- **Move-In**: Baseline condition assessment. Flags anything not move-in ready. Documents existing defects for security deposit purposes.
- **Move-Out**: Damage assessment. Distinguishes normal wear vs tenant damage. Flags cleaning issues, unauthorized modifications.
- **Periodic**: Maintenance/safety inspection. Flags safety hazards, lease violations, preventive maintenance needs.

### AI Analysis Output (Structured JSON)
Each room returns:
- `overall_rating`: Good / Fair / Poor
- `items[]`: Array of {name, rating, notes} for each visible element (Walls, Ceiling, Flooring, Windows, Doors, Lighting, Cleanliness, Fixtures)
- `summary`: 2-sentence key takeaway
- `flags[]`: Actionable issues requiring attention

### Speed
- **Haiku 4.5**: ~6-7 seconds per room
- **Parallel analysis**: All rooms analyzed simultaneously via thread pool executor
- 4 rooms completes in ~8-10 seconds total (not 24-28 seconds sequential)
- Fallback to Sonnet 4.5 if Haiku fails

### Report Delivery
- **PDF Download**: Professional PDF with rating tables, photos, action items, signatures, legal disclaimer
- **Email Report**: Sends PDF as attachment via Resend API
- **Text Report**: Opens native SMS app with share link pre-filled
- **Share Link**: Generates 7-day shareable URL (no auth needed to download PDF). Uses Web Share API on mobile, clipboard fallback on desktop.

### Payments & Access
- 1 free analysis (proves AI quality), then paywall
- Single report: $4.99 via Stripe
- Pro monthly: $29/month
- Pro annual: $249/year
- Hard paywall after free trial used — replaces page content
- All download/email/text/share buttons locked behind paywall
- `viewingResults` flag prevents paywall from hiding the free report while user is viewing it

### Other
- Google Ads tracking (AW-17926414862) with conversion events on purchase + begin_checkout
- SEO: meta tags, JSON-LD structured data, sitemap.xml, robots.txt
- Account system: signup, login, profile with past reports
- Logo clickable (goes to /)
- Photo upload accepts camera + gallery + PDF on mobile

## Known Issues / TODO

### HIGH PRIORITY
1. **Email sender domain**: Currently `joseph@dataweaveai.com` (verified in Resend). User wants emails from `condition-report.com`. To fix:
   - Go to Resend dashboard → Domains → Add `condition-report.com`
   - Add the DNS records (DKIM, SPF, DMARC) to Cloudflare
   - Update `api.py` line ~1253: change `from` to `reports@condition-report.com`
   - This is a MANUAL step the user must do in the Resend dashboard + Cloudflare DNS

2. **Stripe checkout descriptions**: Says "5,000 contracts/month" (carried over from DataWeave). Needs fix in Stripe Dashboard product descriptions, not in code.

3. **Share tokens stored in memory**: `share_tokens` dict in `api.py` resets on every deploy. Should move to PostgreSQL for persistence. Currently share links break after redeploy.

4. **Ad campaign prep**: User requested Google Ads campaign prep — targeting, ad copy, landing page optimization. Not yet started.

### MEDIUM PRIORITY
5. **Rate limiting**: No rate limiting on any endpoint. `/api/analyze` calls Claude API (costs money). Should add per-IP rate limiting.
6. **File cleanup**: Uploaded photos and generated PDFs never deleted. Unbounded disk growth.
7. **Password hashing**: Uses SHA256 with static salt. Should upgrade to bcrypt.
8. **Email verification**: Signup doesn't verify email ownership.
9. **Password reset**: No password reset flow exists.

### LOW PRIORITY
10. **Fingerprint bypass**: Users can clear localStorage to get new fingerprints = unlimited free trials. Mitigated by reports being useless without payment (can't download/email/text).
11. **Security headers**: No X-Frame-Options, CSP, HSTS, etc.
12. **CORS**: Allows all methods/headers (should restrict).

## Key Files

### `api.py` (~1500 lines)
- Lines 1-60: Imports, env vars, Stripe/Anthropic client setup
- Lines 62-80: Directories, DB connection
- Lines 80-190: PostgreSQL helpers (get_user, update_user, check_access, etc.)
- Lines 190-420: Image processing (resize to 768px, base64 encode)
- **Lines 420-555: `analyze_room_photos_sync()`** — THE core AI function. Builds structured prompt per inspection type, calls Haiku 4.5 with Sonnet fallback, returns JSON.
- **Lines 555-810: `generate_pdf_report()`** — PDF generation with ReportLab. Rating tables, photos, flags, signatures, disclaimer.
- Lines 810-880: Root route, health, OG image, SEO routes
- Lines 880-960: User status, upload photos
- **Lines 960-1060: `/api/analyze`** — Parallel room analysis endpoint. Uses asyncio.gather + thread pool executor.
- Lines 1060-1100: PDF download, report data endpoints
- **Lines 1100-1160: Share link endpoints** — `/api/report/{id}/share` creates token, `/share/{token}` serves PDF
- Lines 1160-1220: Stripe checkout (single, pro)
- **Lines 1220-1280: Email report** — Resend API with branded HTML template
- Lines 1280-1400: Stripe webhook, account system (signup, login, profile, update)

### `landing/app.html` (~1700 lines)
- Lines 1-100: Head, meta tags, Google Ads gtag, JSON-LD structured data
- Lines 100-450: CSS (mobile-first, responsive)
- Lines 450-900: HTML (3-step wizard, pricing, modals, profile)
- Lines 900-1000: Core JS (init, UTM capture, payment success handling)
- Lines 1000-1100: Room management (add/remove rooms, photo upload)
- **Lines 1100-1290: `analyzePhotos()`** — Uploads photos, calls analyze API, renders structured results with ratings
- Lines 1290-1400: Paywall functions (hasPaidAccess, showHardPaywall, showPaywall)
- Lines 1400-1450: Download PDF, email modal, send email
- **Lines 1450-1510: Text/Share functions** — getShareLink, textReport, shareReport
- Lines 1510-1600: Stripe checkout, account functions
- Lines 1600-1680: Profile modal, account check, boot

## Environment Variables (Railway)
- `ANTHROPIC_API_KEY` — Claude AI
- `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` — Payments
- `STRIPE_PRICE_SINGLE`, `STRIPE_PRICE_MONTHLY`, `STRIPE_PRICE_ANNUAL` — Stripe price IDs
- `RESEND_API_KEY` — Email
- `BASE_URL=https://condition-report.com`
- `DATABASE_URL` — Auto-provisioned by Railway PostgreSQL

## Common Commands
```bash
# Deploy (preferred — forces immediate deploy)
cd /Users/josephvarga/Desktop/rentready-ai && railway up

# Or git push (auto-deploys but sometimes delayed)
git push origin main

# Run locally
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

## Gotchas
- `python3` not `python` on this system
- Railway deploy takes 1-2 min. Hard refresh (Cmd+Shift+R) often needed after deploy.
- Cloudflare error 1016 appears during deploys — just wait 1-2 min
- `railway up` is more reliable than `git push` for forcing deploys
- `analyze_room_photos_sync()` is a SYNC function despite being called from async context — it runs in thread pool via `loop.run_in_executor()`
- The `share_tokens` dict is in-memory only — share links break on redeploy
- Edit tool fails when pattern matches multiple locations — use more context or `replace_all`
- DataWeaveAI (`/Users/josephvarga/Desktop/dataweave-ai-full-build/`) is a SEPARATE project — DO NOT modify it

## User Preferences
- DO NOT change existing UI/UX unless explicitly asked
- DO NOT add features the user didn't ask for
- Deploy immediately after changes
- User has an investor call — everything must be polished
- Mostly used on MOBILE — test everything mobile-first
- Keep it functional, not flashy
- DataWeaveAI is a SEPARATE entity — don't touch it
- This company is condition-report.com — no DataWeave branding anywhere

## Session 3 Commits (Feb 20, 2026)
1. `d965ae4` — Professional condition report overhaul: structured ratings, inspection-type rules, Haiku 4.5
2. `7490b00` — Add model fallback: try Haiku 4.5 first, fall back to Sonnet 4.5
3. `c67b22d` — Fix PDF text wrapping, add parallel room analysis, add legal disclaimer
4. `263e871` — Mobile-optimized report display + parallel analysis + PDF fixes
5. `895409b` — Add Text Report + Share Link features for landlords
6. `b65b4e0` — Fix email sender: use verified dataweave-ai.com domain
7. `2a01b14` — Fix email: use resend.dev default sender
8. `c552361` — Fix email sender: use verified joseph@dataweaveai.com domain
9. `6eaecb4` — Rebrand email: remove DataWeave references, professional ConditionReport template
10. `053cf1a` — Polish PDF format: remove Report ID from header, better photo sizing

## Session 4 Work (Feb 20, 2026 — Current)

### Critical Fixes Deployed (pending Railway build)
1. **Post-payment lockout fix** — 3 stacked bugs:
   - Added `/api/verify-payment` endpoint (checks Stripe directly, no webhook dependency)
   - Checkout success URLs now include `{CHECKOUT_SESSION_ID}`
   - Split `hasPaidAccess()` (permanent after any purchase) vs `hasNewReportCredits()` (consumable)
   - Past reports stay visible even on paywall screen
2. **Idempotent Stripe processing** — `processed_stripe_sessions` PG table, `INSERT ON CONFLICT DO NOTHING`
3. **Profile "Reports Generated" clickable** — scrolls to past reports section
4. **"a DataWeaveAI company" branding** — sub-text under logo in header + email template
5. **Security improvements** — bcrypt, connection pooling, rate limiting (slowapi), security headers middleware, share tokens in PG
6. **AI prompt refinements** — anti-hallucination, N/A for non-visible items, softer tone
7. **GitHub repo connected to Railway** — auto-deploy on push to main

### Deployment Status
- All code committed and pushed to `lego3072/rentready-ai` main branch
- GitHub auto-deploy connected in Railway settings
- **Railway Metal builder was congested** — builds were queuing/stalling
- Old deployment (pre-fixes) still ACTIVE and serving traffic
- Once build succeeds, all fixes go live automatically
- **VERIFY AFTER DEPLOY**: health check, payment flow, branding, past reports

### Email Domain
- Sender changed from `joseph@dataweaveai.com` to `noreply@dataweaveai.com`
- Domain `condition-report.com` added in Resend but DNS records still pending in Cloudflare
- Once DNS verified, switch sender to `reports@condition-report.com`

---

## NEXT SESSION PRIORITY: Thorough SEO + Ad Campaign Launch

### 1. SEO (High Priority)
- **Meta tags**: description, Open Graph, Twitter Cards on app.html
- **OG image**: Create 1200x630px social sharing image
- **Structured data**: JSON-LD for SaaS/SoftwareApplication + Organization + FAQ
- **Sitemap**: `/sitemap.xml` endpoint
- **Robots.txt**: Allow all crawlers
- **Core Web Vitals**: Lighthouse audit, lazy loading, image optimization
- **H1/heading hierarchy**: Audit and fix
- **Alt text**: All images
- **Google Search Console**: Submit sitemap, verify indexing

### 2. Google Ads Campaign (High Priority)
- **Conversion tracking**: AW-17926414862 already installed
- **Conversion actions to create**:
  - Report generated (primary)
  - Payment completed (primary)
  - Sign up (secondary)
- **Search campaign keywords**: "property condition report", "move-in inspection report", "rental inspection app", "condition report template", "move out inspection", "property damage report"
- **Performance Max**: Use condition report screenshots as creatives
- **Mobile bid adjustment**: +30% (mobile-heavy audience)
- **Landing page**: Consider dedicated `/lp` stripped for conversion
- **UTM tracking**: Ensure all ad URLs use utm_source, utm_medium, utm_campaign

### 3. Post-Deploy Verification
- `curl https://condition-report.com/health` → `{"status":"ok"}`
- "a DataWeaveAI company" shows in header
- Buy single report → verify payment → generate → email
- Profile shows credits + clickable "Reports Generated"
- Past reports visible after credits used
- Test on mobile (iPhone Safari, Android Chrome)
- Test share link + text report
- Test signature pad flow

### 4. Lower Priority
- Persist share tokens in PostgreSQL (partially done)
- Rate limiting tuning
- Stripe checkout descriptions (fix in Stripe Dashboard)
- Password reset flow
- Email verification on signup
