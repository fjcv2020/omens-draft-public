from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, jsonify, redirect, request, send_from_directory


ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
REPORT_DIR = PUBLIC_DIR / "reports" / "omen_draft_3_0"
INDEX_FILE = REPORT_DIR / "omen_draft_3_0_report.html"
LOG_FILE = ROOT / "logs" / "last_refresh.log"

app = Flask(__name__, static_folder=None)
refresh_lock = threading.Lock()
last_status = {
    "state": "idle",
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "message": "",
}


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip("\"'"))


load_env_file(ROOT / ".env")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_refresh() -> dict[str, object]:
    if not refresh_lock.acquire(blocking=False):
        return {"state": "already_running", **last_status}
    try:
        last_status.update(
            {
                "state": "running",
                "started_at": utc_now(),
                "finished_at": None,
                "returncode": None,
                "message": "refresh started",
            }
        )
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(ROOT / "scripts" / "generate_report.py")]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=int(os.environ.get("REFRESH_TIMEOUT_SECONDS", "900")),
        )
        LOG_FILE.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
        state = "ok" if completed.returncode == 0 else "failed"
        last_status.update(
            {
                "state": state,
                "finished_at": utc_now(),
                "returncode": completed.returncode,
                "message": f"refresh {state}",
            }
        )
        return dict(last_status)
    except Exception as exc:  # noqa: BLE001 - status endpoint should expose operational failures
        last_status.update(
            {
                "state": "failed",
                "finished_at": utc_now(),
                "returncode": -1,
                "message": str(exc),
            }
        )
        LOG_FILE.write_text(str(exc), encoding="utf-8")
        return dict(last_status)
    finally:
        refresh_lock.release()


def require_refresh_token() -> None:
    expected = os.environ.get("REFRESH_TOKEN", "").strip()
    if not expected:
        abort(403)
    supplied = request.headers.get("X-Refresh-Token", "") or request.args.get("token", "")
    if supplied != expected:
        abort(403)


def start_background_refresh() -> None:
    thread = threading.Thread(target=run_refresh, daemon=True)
    thread.start()


@app.get("/")
def home():
    if INDEX_FILE.exists():
        return redirect("/reports/omen_draft_3_0/omen_draft_3_0_report.html")
    return (
        "<h1>Omens Draft Report</h1>"
        "<p>No report has been generated yet. Configure FAB_INSIGHTS_API_KEY and run a refresh.</p>",
        200,
    )


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "report_exists": INDEX_FILE.exists(), "status": last_status})


@app.get("/status")
def status():
    generated_at = None
    json_path = REPORT_DIR / "omen_draft_3_0_report.json"
    if json_path.exists():
        import json

        generated_at = json.loads(json_path.read_text(encoding="utf-8")).get("generated_at")
    return jsonify({"report_exists": INDEX_FILE.exists(), "generated_at": generated_at, "refresh": last_status})


@app.route("/admin/refresh", methods=["GET", "POST"])
def refresh():
    require_refresh_token()
    if request.args.get("sync") == "1":
        return jsonify(run_refresh())
    start_background_refresh()
    return jsonify({"state": "accepted", "refresh": last_status}), 202


@app.get("/reports/<path:filename>")
def reports(filename: str):
    return send_from_directory(PUBLIC_DIR / "reports", filename)


def configure_scheduler() -> BackgroundScheduler | None:
    if os.environ.get("ENABLE_SCHEDULER", "").lower() not in {"1", "true", "yes"}:
        return None
    scheduler = BackgroundScheduler(timezone=os.environ.get("SCHEDULER_TIMEZONE", "UTC"))
    scheduler.add_job(
        run_refresh,
        "cron",
        hour=int(os.environ.get("CRON_HOUR", "6")),
        minute=int(os.environ.get("CRON_MINUTE", "0")),
        id="refresh-omens-draft-report",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    return scheduler


scheduler = configure_scheduler()

if os.environ.get("RUN_ON_START", "").lower() in {"1", "true", "yes"}:
    start_background_refresh()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
