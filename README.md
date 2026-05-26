# Omens Draft Public Report

Public Flask wrapper for the Talishar/FAB Insights Omens draft report.

## What this serves

- `/` redirects to the latest report HTML.
- `/status` shows the latest generated timestamp and refresh state.
- `/healthz` is a simple health check.
- `/admin/refresh?token=...` starts a protected refresh.

The generated public artifact removes full player/opponent hashes from the report payload and keeps only the short labels used in the UI.

## Local setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `FAB_INSIGHTS_API_KEY` in `.env`, then generate and serve:

```bash
python scripts/generate_report.py
python app.py
```

Open `http://localhost:8080`.

## Replit setup

1. Import this GitHub repo into Replit.
2. Add Secrets:
   - `FAB_INSIGHTS_API_KEY`
   - `REFRESH_TOKEN`
   - optional: `ENABLE_SCHEDULER=true`, `SCHEDULER_TIMEZONE=Europe/Madrid`, `CRON_HOUR=8`, `CRON_MINUTE=5`
3. Run the repl once. If `RUN_ON_START=true`, it refreshes on boot.
4. Manual refresh:

```bash
curl "https://YOUR-REPLIT-URL/admin/refresh?token=YOUR_REFRESH_TOKEN"
```

For a reliable cron, keep the Replit deployment/container awake or use Replit Deployments/Scheduled Deployments if enabled on the account. The app also includes a GitHub Actions cron alternative.

## GitHub Actions cron alternative

Add repository secrets:

- `FAB_INSIGHTS_API_KEY`

Then enable Actions. The included workflow refreshes the static report daily and commits changed report files back to `main`.
