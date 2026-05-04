# Thanyan

FastAPI storefront/admin application with static frontend assets, WebSocket updates, and optional Redis/Mongo integrations.

## Features
- FastAPI backend (`app.main:app`)
- Jinja templates + static asset serving
- Admin authentication/session middleware
- Optional Redis presence tracking
- Optional MongoDB persistence for visitors/submissions/settings

## Local Development

### 1) Create environment
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2) Run
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000`.

## Deploy on Render

This repo includes `render.yaml` for one-click Blueprint deploy.

### Steps
1. Push this repository to GitHub.
2. In Render, create a new Blueprint and select the repo.
3. Set secret env vars:
   - `ADMIN_USERNAME`
   - `ADMIN_PASSWORD`
   - `ADMIN_SESSION_SECRET`
   - `REDIS_URL` (optional)
   - `MONGO_URI` (optional)
4. Deploy.

Render starts with:
```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Required Environment Variables
- `ENV` (`production` in hosting)
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `ADMIN_SESSION_SECRET`

## Recommended
- `ALLOWED_HOSTS` (comma-separated hostnames instead of `*`)
- `CORS_ORIGINS` (comma-separated origins when cross-origin requests are needed)

## Repository Hygiene
- Secrets are excluded via `.gitignore`.
- Use `.env.example` as the public configuration template.
