# RentReady AI / Condition Report — Handoff Document
# Last updated: Feb 20, 2026 (Session 6)

## Product
- **URL**: https://condition-report.com
- **What it does**: AI-powered property condition report generator
- **How it works**: Upload room photos → AI analyzes condition with structured ratings → Professional PDF with checklist, ratings, flags
- **Pitch**: "TurboTax for property condition reports"
- **Target users**: Landlords, property managers, tenants
- **Mostly used on MOBILE** — all design decisions must be mobile-first
- **Parent company**: DataWeaveAI INC (same Stripe account, same bank)

## Stack
- **Backend**: Python/FastAPI (`api.py` ~2335 lines)
- **Frontend**: Single HTML file (`landing/app.html` ~2250 lines) — no framework, inline CSS/JS
- **AI**: Claude Haiku 4.5 (`claude-haiku-4-5-20251001`) with Sonnet 4.5 fallback (`claude-sonnet-4-5-20250929`)
- **DB**: PostgreSQL on Railway (tables: users, reports, accounts, account_sessions, processed_stripe_sessions, report_share_tokens, email_verification_tokens, password_reset_tokens)
- **Payments**: Stripe (single $4.99, monthly $29, annual $249) — SHARES Stripe account with DataWeaveAI
- **Email**: Resend API (sender: `reports@condition-report.com`, domain verified with DKIM/SPF)
- **Hosting**: Railway (auto-deploy from github.com/lego3072/rentready-ai, builder: dockerfile)
- **DNS**: Cloudflare (proxied, Full strict SSL)
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

### Account System
- Signup with email/password (bcrypt hashed)
- Login with fingerprint-based sessions
- **Email verification**: Sends verification email on signup, "Verify email" link in profile, resend button
- **Password reset**: "Forgot password?" in login modal → email with reset link → set new password form
- Profile modal: email (with verified badge), name, plan, reports generated, single reports purchased, member since

### Security & Ops
- Google Ads tracking (AW-17926414862) with conversion events on purchase + begin_checkout
- SEO: meta tags, JSON-LD structured data, sitemap.xml, robots.txt
- **Rate limiting**: slowapi with real IP detection (`CF-Connecting-IP` / `X-Forwarded-For`). Limits: signup/login 5/min, reset 3/min, analysis 10/min, upload 20/min
- **File cleanup**: Auto-delete uploads/reports older than 24h (runs on startup + hourly)
- **Security headers**: HSTS (preload), X-Frame-Options DENY, X-Content-Type-Options nosniff, CSP upgrade-insecure-requests, Referrer-Policy
- HTTPS redirect middleware (server-side + Cloudflare 301)
- bcrypt passwords, connection pooling, idempotent Stripe processing

## Known Issues / TODO

### IMMEDIATE — SEO & Ads Push (Next Session)
1. **Submit sitemap to Google Search Console**: `/sitemap.xml` endpoint exists. Go to search.google.com/search-console, add property `condition-report.com`, submit `https://condition-report.com/sitemap.xml`
2. **Google Ads campaign launch**: Full campaign setup — see "Google Ads Campaign Plan" section below
3. **Stripe checkout descriptions**: May still say "5,000 contracts/month" (carried over from DataWeave). Fix in **Stripe Dashboard** → Products, not in code.
4. **End-to-end test on mobile**: Full flow — open on phone → fill form → upload photos → generate → download PDF → email → text → share link.

### LOW PRIORITY
5. **Fingerprint bypass**: Users can clear localStorage to get new fingerprints = unlimited free trials. Mitigated by reports being useless without payment (can't download/email/text).
6. **CORS**: Allows all methods/headers (should restrict for production).
7. **Share token cleanup**: Old share tokens in PG never expire/cleaned up.

## Google Ads Campaign Plan

### Already Done
- **Conversion tracking installed**: `AW-17926414862` on all pages (app.html)
- **Conversion events firing**: `purchase` event on payment success, `begin_checkout` on checkout click
- **UTM capture**: `captureUTM()` stores utm_source, utm_medium, utm_campaign, utm_term, utm_content, gclid, fbclid in sessionStorage

### Conversion Actions to Create in Google Ads
- **Report Generated** (primary) — fire when AI analysis completes
- **Payment Completed** (primary) — already fires on payment success
- **Sign Up** (secondary) — fire on account creation

### Search Campaign
- **Keywords**: "property condition report", "move-in inspection report", "rental inspection app", "condition report template", "move out inspection", "property damage report", "rental property inspection", "landlord inspection report", "tenant move-in checklist", "AI property report"
- **Negative keywords**: "free", "template download", "pdf free", "excel"
- **Match types**: Phrase match + exact match for high-intent terms
- **Ad copy angles**:
  - Speed: "Professional Condition Report in 60 Seconds"
  - AI: "AI-Powered Property Inspection Reports"
  - Price: "Property Reports from $4.99"
  - Mobile: "Snap Photos → Get Report. Works on Your Phone."

### Performance Max
- Use condition report screenshots + PDF examples as creatives
- Target: property managers, landlords, real estate professionals

### Bid Strategy
- **Mobile bid adjustment**: +30% (mobile-heavy audience)
- **Start with**: Manual CPC or Maximize Conversions with target CPA
- **Budget**: Start $20-50/day, scale based on CPA

### Landing Page Considerations
- Current page IS the product — no separate landing page needed
- Ensure above-the-fold makes value prop clear for ad traffic
- UTM parameters already captured for attribution
- Consider adding a `/lp` route with stripped-down conversion-focused page if CPA is too high

## Key Files

### `api.py` (~2335 lines)
- Lines 1-65: Imports, env vars, Stripe/Anthropic client setup, price ID constants
- Lines 65-95: Directories, DB connection pool
- Lines 95-225: PostgreSQL helpers (init_database with all tables, get_user, update_user, check_access)
- Lines 252-295: Rate limiter (real IP via CF-Connecting-IP), HTTPS redirect middleware, security headers, CORS
- Lines 295-430: Image processing (resize to 768px, base64 encode)
- **Lines 430-555: `analyze_room_photos_sync()`** — THE core AI function. Structured prompt per inspection type, Haiku 4.5 with Sonnet fallback.
- Lines 529-550: `send_transactional_email()` — Resend API helper for verification/reset emails
- **Lines 555-810: `generate_pdf_report()`** — PDF generation with ReportLab.
- Lines 810-880: Root route, health, OG image, SEO routes (sitemap.xml, robots.txt)
- Lines 880-960: User status, upload photos
- **Lines 960-1060: `/api/analyze`** — Parallel room analysis endpoint.
- Lines 1060-1100: PDF download, report data endpoints
- **Lines 1100-1160: Share link endpoints**
- Lines 1160-1220: Stripe checkout (single, pro)
- **Lines 1220-1280: Email report** — Resend API with branded template
- Lines 1280-1400: Stripe webhook
- Lines 1400-1850: Account system (signup with verification email, login, profile with email_verified, update)
- **Lines 1850-1950: Profile endpoint** — Returns email_verified status
- **Lines 2050-2170: Email verification** — verify-email GET (HTML response), resend-verification POST
- **Lines 2170-2290: Password reset** — request-reset POST (anti-enumeration), reset-password POST (token validation, bcrypt rehash)
- **Lines 2290-2320: File cleanup** — startup + hourly periodic cleanup of files >24h old
- Line 2323: Static file mount

### `landing/app.html` (~2250 lines)
- Lines 1-100: Head, meta tags, Google Ads gtag, JSON-LD structured data
- Lines 100-450: CSS (mobile-first, responsive)
- Lines 450-790: HTML (3-step wizard, pricing banner, modals)
- **Lines 775-800: Auth modal** — signup/login with "Forgot password?" link
- **Lines 800-830: Password reset modal** — request view + execute view (triggered by `?reset_token=` URL param)
- Lines 830-930: Profile modal with email verification status badge + resend button
- Lines 930-1000: Core JS (init with reset_token detection, UTM capture, payment success handling)
- Lines 1000-1100: Room management (add/remove rooms, photo upload)
- **Lines 1100-1290: `analyzePhotos()`** — Uploads photos, calls analyze API, renders structured results
- Lines 1290-1400: Paywall functions
- **Line ~1474: `showUpgradeBanner()`** — Toggle pricing banner from header status click
- Lines 1400-1475: Download PDF, email modal, send email
- **Lines 1475-1530: Text/Share functions** — getShareLink, textReport, shareReport
- Lines 1530-1620: Stripe checkout, account functions
- Lines 1620-1700: Profile modal with email_verified badge, account check
- **Lines 2095-2230: Password reset JS** — showForgotPassword, showResetForm, requestPasswordReset, executePasswordReset
- **Lines 2230-2260: Email verification JS** — resendVerification()
- Lines 2260-2270: Boot (init + checkAccountOnLoad)

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
- Rate limiter uses `CF-Connecting-IP` header for real IP behind Cloudflare — verified working (429 after 3 requests/min on reset endpoint)

## User Preferences
- DO NOT change existing UI/UX unless explicitly asked
- DO NOT add features the user didn't ask for
- Deploy immediately after changes
- User has an investor call — everything must be polished
- Mostly used on MOBILE — test everything mobile-first
- Keep it functional, not flashy
- DataWeaveAI is a SEPARATE entity — don't touch it
- This company is condition-report.com — branded as "ConditionReport" with "a DataWeaveAI company" sub-text
- **Next session is full SEO + Google Ads push**

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

### Session 5 (Feb 20, 2026)
1. **Email sender switched to `reports@condition-report.com`** — Domain verified in Resend with DKIM/SPF/DMARC records in Cloudflare. Commit: `a7b9f67`
2. **Stripe subscription isolation** — Login/signup subscription check now filters by `STRIPE_PRICE_MONTHLY` and `STRIPE_PRICE_ANNUAL` (Condition Report price IDs only). DataWeave Pro subs on same Stripe account no longer grant CR Pro status. Commit: `70d2e6b`
3. **Clickable pricing in header** — `showUpgradeBanner()` function. Tapping "1 free report" or status text toggles the pricing banner. Pro users clicking it is a no-op. Commit: `70d2e6b`

### Session 6 (Feb 20, 2026 — Current)
1. **Email verification on signup** — Verification email sent on signup via Resend API. Token stored in `email_verification_tokens` table (24h expiry). GET `/api/account/verify-email?token=` shows branded HTML success/error. POST `/api/account/resend-verification` resends from profile. Profile modal shows "✓ Verified" green badge or "Verify email" link. Commit: `d2e71a1`
2. **Password reset flow** — Full flow: "Forgot password?" link in login modal → POST `/api/account/request-reset` sends branded email with 1h token → user clicks link → `?reset_token=` param detected in `init()` → opens reset form → POST `/api/account/reset-password` validates token, bcrypt rehashes. Anti-email-enumeration (always returns success). One-time-use tokens. Commits: `d2e71a1`, `e277ade`
3. **File cleanup** — `cleanup_old_files()` deletes files in uploads/ and reports/ older than 24h. Runs on server startup + hourly via `asyncio.create_task(periodic_cleanup())`. Commit: `d2e71a1`
4. **Rate limiting fixed** — Custom `get_real_ip()` function uses `CF-Connecting-IP` then `X-Forwarded-For` then fallback. Previously used `get_remote_address` which saw proxy IP, making rate limiting ineffective. Verified working: 4th request returns 429. Commit: `c8913ff`
5. **Security headers hardened** — Added `Content-Security-Policy: upgrade-insecure-requests`, added `preload` to HSTS header. Commit: `70874e6`
6. All changes deployed via GitHub auto-deploy, all endpoints tested and verified live.

---

## NEXT SESSION PRIORITIES — Full SEO & Ad Push

### 1. Google Search Console
- Go to search.google.com/search-console
- Add property `condition-report.com` (DNS verification via Cloudflare TXT record)
- Submit sitemap: `https://condition-report.com/sitemap.xml`
- Request indexing for key pages

### 2. Google Ads Campaign Setup
- **Account**: AW-17926414862 (already installed)
- **Create conversion actions**:
  - Report Generated (primary) — needs `gtag('event', 'conversion', ...)` on analysis complete
  - Payment Completed (primary) — already fires
  - Sign Up (secondary) — add to signup success
- **Search campaign**:
  - Keywords: "property condition report", "move-in inspection report", "rental inspection app", "condition report template", "move out inspection", "property damage report", "rental property inspection", "landlord inspection report"
  - Negative keywords: "free", "template download", "pdf free", "excel"
  - Ad groups by intent: [move-in], [move-out], [general condition report], [landlord tools]
  - Ad copy: focus on speed (60 seconds), AI, price ($4.99), mobile-first
  - Mobile bid adjustment: +30%
  - Location targeting: United States (start), expand to AU/UK/CA later
  - Budget: $20-50/day to start
- **Optional Performance Max**: product screenshots + PDF examples as creatives
- **UTM format**: `?utm_source=google&utm_medium=cpc&utm_campaign={campaign}&utm_term={keyword}`

### 3. On-Page SEO Improvements
- Verify meta tags, OG tags, JSON-LD are optimal
- Check page speed (Lighthouse audit)
- Ensure sitemap.xml has correct URLs
- robots.txt is serving properly
- Consider adding FAQ schema markup for common questions
- Blog/content pages for organic traffic (optional, longer-term)

### 4. Quick Fixes
- **Stripe checkout descriptions**: Fix in Stripe Dashboard → Products (remove "5,000 contracts/month")
- **Add conversion event on signup**: `gtag('event', 'conversion', ...)` in `submitAuth()` on signup success
- **Add conversion event on report generation**: `gtag('event', 'conversion', ...)` in `analyzePhotos()` on success
- **End-to-end mobile test**: Verify full flow works on phone
