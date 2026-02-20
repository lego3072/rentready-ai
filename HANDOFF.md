# RentReady AI / Condition Report — Handoff Document
# Last updated: Feb 20, 2026 (Session 5)

## Product
- **URL**: https://condition-report.com
- **What it does**: AI-powered property condition report generator
- **How it works**: Upload room photos → AI analyzes condition with structured ratings → Professional PDF with checklist, ratings, flags
- **Pitch**: "TurboTax for property condition reports"
- **Target users**: Landlords, property managers, tenants
- **Mostly used on MOBILE** — all design decisions must be mobile-first
- **Parent company**: DataWeaveAI INC (same Stripe account, same bank)

## Stack
- **Backend**: Python/FastAPI (`api.py` ~1550 lines)
- **Frontend**: Single HTML file (`landing/app.html` ~1750 lines) — no framework, inline CSS/JS
- **AI**: Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) with Sonnet 4.5 fallback (`claude-sonnet-4-5-20250929`)
- **DB**: PostgreSQL on Railway
- **Payments**: Stripe (single $4.99, monthly $29, annual $249) — SHARES Stripe account with DataWeaveAI
- **Email**: Resend API (sender: `reports@condition-report.com`, domain verified with DKIM/SPF)
- **Hosting**: Railway (auto-deploy from github.com/lego3072/rentready-ai, builder: dockerfile)
- **DNS**: Cloudflare (proxied)
- **Project dir**: `/Users/josephvarga/Desktop/rentready-ai`

## What's Working (All confirmed live on condition-report.com)

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
- **Email Report**: Sends PDF as attachment via Resend API from `reports@condition-report.com`
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
- **Clickable status text** in header — tapping "1 free report" or "X reports left" shows/hides pricing banner
- **Stripe isolation**: Subscription checks filtered by Condition Report price IDs only (DataWeave Pro subs don't bleed over)

### Other
- Google Ads tracking (AW-17926414862) with conversion events on purchase + begin_checkout
- SEO: meta tags, JSON-LD structured data, sitemap.xml, robots.txt
- Account system: signup, login, profile with past reports
- Logo clickable (goes to /)
- Photo upload accepts camera + gallery + PDF on mobile
- "a DataWeaveAI company" sub-text under logo
- Security: bcrypt passwords, slowapi rate limiting, security headers middleware, connection pooling

## Known Issues / TODO

### HIGH PRIORITY (do these next session)
1. **Stripe checkout descriptions**: May still say "5,000 contracts/month" (carried over from DataWeave). Fix in **Stripe Dashboard** product descriptions, not in code. User needs to verify.
2. **Submit sitemap to Google Search Console**: `/sitemap.xml` endpoint exists. Need to submit at search.google.com/search-console.
3. **End-to-end test on mobile**: Full flow — buy report → generate → email → text → share. Not yet tested post-Session 5 deploys.
4. **Ad campaign launch**: Google Ads campaign prep — targeting, ad copy, landing page optimization.

### MEDIUM PRIORITY
5. **File cleanup**: Uploaded photos and generated PDFs never deleted. Unbounded disk growth on Railway.
6. **Email verification**: Signup doesn't verify email ownership.
7. **Password reset**: No password reset flow exists.
8. **Rate limiting tuning**: slowapi installed with defaults, may need adjustment under real traffic.

### LOW PRIORITY
9. **Fingerprint bypass**: Users can clear localStorage to get new fingerprints = unlimited free trials. Mitigated by reports being useless without payment (can't download/email/text).
10. **Security headers**: No X-Frame-Options, CSP, HSTS, etc. (middleware added but may need hardening)
11. **CORS**: Allows all methods/headers (should restrict for production).

## Key Files

### `api.py` (~1550 lines)
- Lines 1-65: Imports, env vars, Stripe/Anthropic client setup, price ID constants
- Lines 65-85: Directories, DB connection pool
- Lines 85-200: PostgreSQL helpers (get_user, update_user, check_access, etc.)
- Lines 200-420: Image processing (resize to 768px, base64 encode)
- **Lines 420-555: `analyze_room_photos_sync()`** — THE core AI function. Builds structured prompt per inspection type, calls Haiku 4.5 with Sonnet fallback, returns JSON.
- **Lines 555-810: `generate_pdf_report()`** — PDF generation with ReportLab. Rating tables, photos, flags, signatures, disclaimer.
- Lines 810-880: Root route, health, OG image, SEO routes
- Lines 880-960: User status, upload photos
- **Lines 960-1060: `/api/analyze`** — Parallel room analysis endpoint. Uses asyncio.gather + thread pool executor.
- Lines 1060-1100: PDF download, report data endpoints
- **Lines 1100-1160: Share link endpoints** — `/api/report/{id}/share` creates token, `/share/{token}` serves PDF
- Lines 1160-1220: Stripe checkout (single, pro)
- **Lines 1220-1280: Email report** — Resend API with branded HTML template, sender: `reports@condition-report.com`
- Lines 1280-1400: Stripe webhook, account system (signup, login, profile, update)
- **Lines 1717-1726, 1805-1815: Stripe subscription check** — Filters by `STRIPE_PRICE_MONTHLY` and `STRIPE_PRICE_ANNUAL` to avoid DataWeave Pro bleed-over

### `landing/app.html` (~1750 lines)
- Lines 1-100: Head, meta tags, Google Ads gtag, JSON-LD structured data
- Lines 100-450: CSS (mobile-first, responsive)
- Lines 450-925: HTML (3-step wizard, pricing banner, modals, profile)
- Lines 925-1000: Core JS (init, UTM capture, payment success handling)
- Lines 1000-1100: Room management (add/remove rooms, photo upload)
- **Lines 1100-1290: `analyzePhotos()`** — Uploads photos, calls analyze API, renders structured results with ratings
- Lines 1290-1400: Paywall functions (hasPaidAccess, hasNewReportCredits, showHardPaywall, showPaywall)
- **Line ~1474: `showUpgradeBanner()`** — Toggle pricing banner from header status click
- Lines 1400-1475: Download PDF, email modal, send email
- **Lines 1475-1530: Text/Share functions** — getShareLink, textReport, shareReport
- Lines 1530-1620: Stripe checkout, account functions
- Lines 1620-1700: Profile modal, account check, boot

## Environment Variables (Railway)
- `ANTHROPIC_API_KEY` — Claude AI
- `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` — Payments
- `STRIPE_PRICE_SINGLE`, `STRIPE_PRICE_MONTHLY`, `STRIPE_PRICE_ANNUAL` — Stripe price IDs (MUST be Condition Report products, not DataWeave)
- `RESEND_API_KEY` — Email
- `BASE_URL=https://condition-report.com`
- `DATABASE_URL` — Auto-provisioned by Railway PostgreSQL

## Common Commands
```bash
# Deploy (preferred — forces immediate deploy)
cd /Users/josephvarga/Desktop/rentready-ai && railway up

# Or git push (auto-deploys via GitHub webhook)
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
- Edit tool fails when pattern matches multiple locations — use more context or `replace_all`
- DataWeaveAI (`/Users/josephvarga/Desktop/dataweave-ai-full-build/`) is a SEPARATE project — DO NOT modify it
- **Shared Stripe account**: Both DataWeaveAI and Condition Report use the same Stripe account. Subscription checks MUST filter by price ID to avoid cross-product contamination.
- Owner's email (`joey.varga.1@gmail.com` / `joseph@dataweaveai.com`) has a DataWeave Pro sub — will show as "free" on Condition Report (correct behavior after Session 5 fix)

## User Preferences
- DO NOT change existing UI/UX unless explicitly asked
- DO NOT add features the user didn't ask for
- Deploy immediately after changes
- User has an investor call — everything must be polished
- Mostly used on MOBILE — test everything mobile-first
- Keep it functional, not flashy
- DataWeaveAI is a SEPARATE entity — don't touch it
- This company is condition-report.com — branded as "ConditionReport" with "a DataWeaveAI company" sub-text

## Session History

### Session 3 (Feb 20, 2026)
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

### Session 4 (Feb 20, 2026)
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
8. **Railway deployment battle** — Metal builder congested, multiple attempts, finally succeeded

### Session 5 (Feb 20, 2026 — Current)
1. **Email sender switched to `reports@condition-report.com`** — Domain verified in Resend with DKIM/SPF/DMARC records in Cloudflare. Commit: `a7b9f67`
2. **Stripe subscription isolation** — Login/signup subscription check now filters by `STRIPE_PRICE_MONTHLY` and `STRIPE_PRICE_ANNUAL` (Condition Report price IDs only). DataWeave Pro subs on same Stripe account no longer grant CR Pro status. Commit: `70d2e6b`
3. **Clickable pricing in header** — `showUpgradeBanner()` function. Tapping "1 free report" or status text toggles the pricing banner. Pro users clicking it is a no-op. Commit: `70d2e6b`
4. All changes deployed via GitHub auto-deploy, confirmed building successfully on Railway.

---

## NEXT SESSION PRIORITIES

### 1. Immediate TODO (left over from Session 5)
- **Stripe checkout descriptions**: Verify/fix in Stripe Dashboard → Products. Remove any "5,000 contracts/month" text. Update to: Single = "One AI property condition report", Monthly = "Unlimited AI property condition reports — monthly", Annual = "Unlimited AI property condition reports — annual"
- **Submit sitemap to Google Search Console**: Go to search.google.com/search-console, add property `condition-report.com`, submit `https://condition-report.com/sitemap.xml`
- **End-to-end test on mobile**: Full flow — open on phone → fill form → upload photos → generate → download PDF → email → text → share link. Verify everything works post-Session 5 deploys.

### 2. Google Ads Campaign Launch
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

### 3. Polish & Hardening
- File cleanup cron (uploaded photos/PDFs never deleted)
- Email verification on signup
- Password reset flow
- Rate limiting tuning under real traffic
- Persist share tokens fully in PostgreSQL (partially done)
