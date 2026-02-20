# RentReady AI — Property Condition Report Generator

## Project Overview
Upload room photos → AI analyzes condition → Professional PDF report. Built for landlords and property managers.

## Tech Stack
- **Backend**: Python 3.11, FastAPI
- **Frontend**: Single HTML/CSS/JS file in `landing/app.html`
- **AI**: Anthropic Claude Vision API (Sonnet)
- **Payments**: Stripe (same account as DataWeaveAI)
- **PDF**: ReportLab

## Key Files
- `api.py` — FastAPI backend (all endpoints)
- `landing/app.html` — Frontend SPA
- `uploads/` — Temporary photo storage
- `reports/` — Generated PDF reports

## Commands
```bash
uvicorn api:app --host 0.0.0.0 --port 8000 --reload
```

## Design Philosophy
- "TurboTax for property condition reports"
- Functional, not flashy — tool that works
- No landing page — just the tool
- Mobile-first, no signup wall
- Fingerprint auth (like DataWeave)

## Deployment
- Docker + Railway
- Same pattern as DataWeaveAI
