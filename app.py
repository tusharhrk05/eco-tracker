"""Eco-Tracker Flask application.

Run from VS Code with F5, or with ``python app.py`` from the project folder.
The activity tracker is available on Windows when pywin32 is installed; the
web app itself also runs on other platforms and when desktop tracking is off.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psutil
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import win32gui
    import win32process
except ImportError:  # pywin32 is only required for Windows foreground-app tracking.
    win32gui = None
    win32process = None


BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("ECO_TRACKER_DB", str(BASE_DIR / "usage.db"))).resolve()
TRACKER_AVAILABLE = win32gui is not None and win32process is not None
TRACKER_ENABLED = TRACKER_AVAILABLE and os.environ.get("ECO_TRACKER_DISABLE", "").lower() not in {"1", "true", "yes"}
tracking_active = threading.Event()
tracking_thread: threading.Thread | None = None
tracking_lock = threading.Lock()

app = Flask(__name__)
app.secret_key = os.environ.get("ECO_TRACKER_SECRET_KEY") or secrets.token_hex(32)

app_power = {
    "Netflix": 70, "YouTube": 60, "Prime Video": 65, "Zoom": 65,
    "Google Meet": 65, "Chrome": 40, "Edge": 40, "Spotify": 30,
    "WhatsApp": 25, "Twitch": 65, "Microsoft Teams": 65, "Discord": 30,
    "Instagram": 25, "Facebook": 25, "Minecraft": 90, "Steam": 55,
    "Default": 50,
}
emission_factor = {
    "Netflix": 0.5, "YouTube": 0.45, "Prime Video": 0.48, "Zoom": 0.4,
    "Google Meet": 0.4, "Chrome": 0.35, "Edge": 0.35, "Spotify": 0.3,
    "WhatsApp": 0.25, "Twitch": 0.45, "Microsoft Teams": 0.4,
    "Discord": 0.3, "Instagram": 0.25, "Facebook": 0.25,
    "Minecraft": 0.35, "Steam": 0.3, "Default": 0.4,
}
MONTHLY_CARBON_LIMIT_KG = 8.5
app_categories = {
    "Chrome": "Browser", "Edge": "Browser", "Netflix": "Video streaming",
    "YouTube": "Video streaming", "Prime Video": "Video streaming", "Twitch": "Live video",
    "Zoom": "Video calls", "Google Meet": "Video calls", "Microsoft Teams": "Calls and chat",
    "WhatsApp": "Messaging", "Discord": "Chat and gaming", "Instagram": "Social media",
    "Facebook": "Social media", "Minecraft": "Gaming", "Steam": "Gaming",
    "Spotify": "Music", "Default": "Other",
}


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    """Create app tables and consolidate any duplicate legacy usage rows."""
    with connect_db() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS usage (
                day TEXT NOT NULL,
                app TEXT NOT NULL,
                seconds INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'tracked'
            )"""
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(usage)")}
        if "source" not in columns:
            connection.execute("ALTER TABLE usage ADD COLUMN source TEXT NOT NULL DEFAULT 'tracked'")
        # The original database did not enforce uniqueness. Merge its rows
        # before adding the constraint used by the tracker's atomic upsert.
        # Keep generated examples separate from real device captures. Older
        # versions combined sources into a single day/app row; `mixed` rows
        # stay isolated because their real and sample portions cannot be split.
        usage_rows = connection.execute(
            """SELECT day, app, SUM(seconds) AS seconds,
            COALESCE(NULLIF(source, ''), 'tracked') AS source
            FROM usage
            GROUP BY day, app, COALESCE(NULLIF(source, ''), 'tracked')"""
        ).fetchall()
        connection.execute("DROP INDEX IF EXISTS usage_day_app")
        connection.execute("DELETE FROM usage")
        connection.executemany(
            "INSERT INTO usage (day, app, seconds, source) VALUES (?, ?, ?, ?)",
            [(row["day"], row["app"], row["seconds"] or 0, row["source"] or "tracked") for row in usage_rows],
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS usage_day_app_source ON usage(day, app, source)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY COLLATE NOCASE,
                name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                mobile TEXT,
                age INTEGER,
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS contact_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                subject TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS poll_votes (
                choice TEXT PRIMARY KEY,
                votes INTEGER NOT NULL DEFAULT 0
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS password_reset_tokens (
                token_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )"""
        )
        connection.executemany(
            "INSERT OR IGNORE INTO poll_votes (choice, votes) VALUES (?, 0)",
            [(choice,) for choice in POLL_CHOICES],
        )


POLL_CHOICES = ("Video Calls", "Cloud Storage", "Streaming", "Social Media")
init_db()


def clean_app_name(process_name: str, hwnd: int) -> str:
    title = win32gui.GetWindowText(hwnd).lower()
    keywords = {
        "netflix": "Netflix", "prime": "Prime Video", "youtube": "YouTube",
        "spotify": "Spotify", "whatsapp": "WhatsApp", "instagram": "Instagram",
        "facebook": "Facebook", "zoom": "Zoom", "meet": "Google Meet",
        "teams": "Microsoft Teams", "twitch": "Twitch", "discord": "Discord",
        "minecraft": "Minecraft", "steam": "Steam",
    }
    for keyword, display_name in keywords.items():
        if keyword in title:
            return display_name
    if process_name.lower() == "applicationframehost.exe":
        return "Store App"
    return Path(process_name).stem.replace("_", " ").title() or "Unknown"


def get_active_app() -> str:
    if not TRACKER_AVAILABLE:
        return "Unknown"
    try:
        hwnd = win32gui.GetForegroundWindow()
        _, process_id = win32process.GetWindowThreadProcessId(hwnd)
        return clean_app_name(psutil.Process(process_id).name(), hwnd)
    except Exception:
        return "Unknown"


def track_usage() -> None:
    previous_time = time.time()
    previous_app = get_active_app()
    while True:
        if not tracking_active.wait(timeout=0.25):
            previous_time = time.time()
            previous_app = get_active_app()
            continue
        time.sleep(1)
        if not tracking_active.is_set():
            previous_time = time.time()
            previous_app = get_active_app()
            continue
        current_time = time.time()
        current_app = get_active_app()
        elapsed = max(0, int(current_time - previous_time))
        if previous_app != "Unknown" and elapsed:
            with connect_db() as connection:
                connection.execute(
                    """INSERT INTO usage(day, app, seconds, source) VALUES (?, ?, ?, 'tracked')
                    ON CONFLICT(day, app, source) DO UPDATE SET
                    seconds = seconds + excluded.seconds""",
                    (date.today().isoformat(), previous_app, elapsed),
                )
        previous_time, previous_app = current_time, current_app


def start_tracking_service() -> bool:
    global tracking_thread
    if not TRACKER_ENABLED:
        return False
    with tracking_lock:
        if tracking_thread is None or not tracking_thread.is_alive():
            tracking_thread = threading.Thread(target=track_usage, name="eco-usage-tracker", daemon=True)
            tracking_thread.start()
        tracking_active.set()
    return True


def usage_for_day(day: str, source: str = "tracked") -> list[sqlite3.Row]:
    with connect_db() as connection:
        return connection.execute(
            "SELECT app, seconds FROM usage WHERE day=? AND source=? ORDER BY seconds DESC",
            (day, source),
        ).fetchall()


def sample_data_counts() -> tuple[int, int, float]:
    current_month = date.today().strftime("%Y-%m")
    with connect_db() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS records, COUNT(DISTINCT app) AS apps FROM usage WHERE source IN ('sample', 'mixed')"
        ).fetchone()
        month_rows = connection.execute(
            """SELECT app, SUM(seconds) AS seconds FROM usage
            WHERE source IN ('sample', 'mixed') AND substr(day, 1, 7)=?
            GROUP BY app""",
            (current_month,),
        ).fetchall()
    return row["records"], row["apps"], carbon_for_rows(month_rows)


def carbon_rate_kg_per_hour(app_name: str) -> float:
    """Estimated kg CO2e per screen-hour for one app's device profile."""
    watts = app_power.get(app_name, app_power["Default"])
    kg_per_kwh = emission_factor.get(app_name, emission_factor["Default"])
    return watts * kg_per_kwh / 1000


def carbon_for_rows(rows) -> float:
    total_kg = 0.0
    for row in rows:
        if isinstance(row, sqlite3.Row):
            app_name, seconds = row["app"], row["seconds"]
        else:
            app_name, seconds = row[0], row[1]
        total_kg += carbon_rate_kg_per_hour(app_name) * (seconds / 3600)
    return round(total_kg, 2)


def app_type_for_name(app_name: str) -> str:
    """Group detailed app labels into the broad types used in analytics."""
    category = app_categories.get(app_name, "Other")
    if category in {"Video streaming", "Live video", "Video calls"}:
        return "Video"
    if category == "Gaming":
        return "Games"
    if category in {"Calls and chat", "Messaging", "Chat and gaming"}:
        return "Chat"
    if category == "Social media":
        return "Social media"
    return "Other"


def build_category_totals(rows) -> list[dict]:
    totals: dict[str, dict] = {}
    for row in rows:
        app_name, seconds = row["app"], int(row["seconds"] or 0)
        app_type = app_type_for_name(app_name)
        item = totals.setdefault(app_type, {"category": app_type, "co2_kg": 0.0, "apps": set()})
        item["co2_kg"] += carbon_rate_kg_per_hour(app_name) * seconds / 3600
        item["apps"].add(app_name)

    total_co2 = sum(item["co2_kg"] for item in totals.values())
    result = []
    for item in totals.values():
        result.append({
            "category": item["category"],
            "co2_kg": round(item["co2_kg"], 2),
            "share_pct": item["co2_kg"] / total_co2 * 100 if total_co2 else 0,
            "app_count": len(item["apps"]),
        })
    return sorted(result, key=lambda item: item["share_pct"], reverse=True)


def fit_line(values: list[float]) -> tuple[float, float]:
    """Return slope and intercept for equally spaced daily observations."""
    count = len(values)
    mean_x = (count - 1) / 2
    mean_y = sum(values) / count
    denominator = sum((x - mean_x) ** 2 for x in range(count))
    slope = sum((x - mean_x) * (y - mean_y) for x, y in enumerate(values)) / denominator
    return slope, mean_y - slope * mean_x


def build_app_totals(rows) -> list[dict]:
    totals: dict[str, dict] = {}
    for row in rows:
        app_name = row["app"]
        seconds = int(row["seconds"] or 0)
        item = totals.setdefault(app_name, {
            "app": app_name,
            "category": app_categories.get(app_name, "Other"),
            "seconds": 0,
            "sample": False,
        })
        item["seconds"] += seconds
        item["sample"] = item["sample"] or row["source"] in {"sample", "mixed"}

    for item in totals.values():
        item["minutes"] = round(item["seconds"] / 60)
        item["hours"] = item["seconds"] / 3600
        item["watts"] = app_power.get(item["app"], app_power["Default"])
        item["factor"] = emission_factor.get(item["app"], emission_factor["Default"])
        item["energy_kwh"] = item["watts"] * item["hours"] / 1000
        item["co2_kg"] = item["energy_kwh"] * item["factor"]
    return sorted(totals.values(), key=lambda item: item["co2_kg"], reverse=True)


def make_reduction_plan(app_totals: list[dict], month_co2_kg: float, days_elapsed: int) -> list[dict]:
    if not app_totals:
        return []
    days_elapsed = max(1, days_elapsed)
    if month_co2_kg <= MONTHLY_CARBON_LIMIT_KG:
        item = app_totals[0]
        hours_to_reduce = item["hours"] * 0.1
        return [{
            "app": item["app"],
            "hours_to_reduce": hours_to_reduce,
            "hours_per_day": hours_to_reduce / days_elapsed,
            "kg_saved": hours_to_reduce * carbon_rate_kg_per_hour(item["app"]),
            "optional": True,
        }]

    remaining_kg = month_co2_kg - MONTHLY_CARBON_LIMIT_KG + 0.1
    plan = []
    for item in app_totals:
        rate = carbon_rate_kg_per_hour(item["app"])
        if rate <= 0 or item["hours"] <= 0:
            continue
        kg_saved = min(remaining_kg, item["co2_kg"])
        hours_to_reduce = min(item["hours"], kg_saved / rate)
        plan.append({
            "app": item["app"],
            "hours_to_reduce": hours_to_reduce,
            "hours_per_day": hours_to_reduce / days_elapsed,
            "kg_saved": hours_to_reduce * rate,
            "optional": False,
        })
        remaining_kg -= hours_to_reduce * rate
        if remaining_kg <= 0.01:
            break
    return plan


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/cal")
def calculator():
    return render_template("cal.html")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        subject = request.form.get("subject", "General Inquiry").strip()
        message = request.form.get("message", "").strip()
        if not all((name, email, subject, message)):
            flash("Please fill in every field before sending your message.", "error")
            return redirect(url_for("contact"))
        with connect_db() as connection:
            connection.execute(
                "INSERT INTO contact_messages(name,email,subject,message,created_at) VALUES(?,?,?,?,?)",
                (name, email, subject, message, datetime.now(timezone.utc).isoformat()),
            )
        return redirect(url_for("send"))
    return render_template("contact.html")


@app.route("/forget", methods=["GET", "POST"])
def forget():
    reset_url = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        with connect_db() as connection:
            user = connection.execute("SELECT email FROM users WHERE email=?", (email,)).fetchone()
            if user:
                token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                expiry = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
                connection.execute("DELETE FROM password_reset_tokens WHERE email=?", (email,))
                connection.execute(
                    "INSERT INTO password_reset_tokens(token_hash,email,expires_at) VALUES(?,?,?)",
                    (token_hash, email, expiry),
                )
                # This is a local app with no mail service configured. Show the
                # one-time link here so the flow works during local development.
                reset_url = url_for("reset_password", token=token, _external=True)
        return render_template("forget.html", reset_url=reset_url, submitted=True)
    return render_template("forget.html")


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with connect_db() as connection:
        reset = connection.execute(
            "SELECT email, expires_at FROM password_reset_tokens WHERE token_hash=?", (token_hash,)
        ).fetchone()
    if not reset or datetime.fromisoformat(reset["expires_at"]) <= datetime.now(timezone.utc):
        flash("That reset link has expired or was already used. Request a new one.", "error")
        return redirect(url_for("forget"))
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 8:
            return render_template("reset.html", token=token, error="Use at least 8 characters.")
        with connect_db() as connection:
            connection.execute(
                "UPDATE users SET password_hash=? WHERE email=?",
                (generate_password_hash(password), reset["email"]),
            )
            connection.execute("DELETE FROM password_reset_tokens WHERE token_hash=?", (token_hash,))
        flash("Your password has been updated. You can now sign in.", "success")
        return redirect(url_for("login"))
    return render_template("reset.html", token=token)


@app.route("/log", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with connect_db() as connection:
            user = connection.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Email or password is incorrect.", "error")
            return redirect(url_for("login"))
        session["user_email"] = user["email"]
        session["user_name"] = user["name"]
        return redirect(url_for("dashboard"))
    return render_template("log.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/pollrs")
def poll_results():
    return render_template("pollrs.html")


@app.route("/start", methods=["POST"])
def start_tracking_route():
    if not start_tracking_service():
        return jsonify(error="Desktop app tracking is unavailable on this setup."), 503
    return jsonify(status="started", message="Desktop app tracking is running.")


@app.route("/stop", methods=["POST"])
def stop_tracking_route():
    tracking_active.clear()
    return jsonify(status="stopped", message="Desktop app tracking is paused.")


@app.route("/data")
def tracked_data():
    rows = usage_for_day(date.today().isoformat())
    return jsonify({row["app"]: round(row["seconds"] / 60, 2) for row in rows})


@app.route("/api/poll", methods=["GET", "POST"])
def poll_api():
    if request.method == "POST":
        choice = request.get_json(silent=True, force=True) or {}
        selected = choice.get("choice")
        if selected not in POLL_CHOICES:
            return jsonify(error="Choose one of the listed poll options."), 400
        with connect_db() as connection:
            connection.execute("UPDATE poll_votes SET votes=votes+1 WHERE choice=?", (selected,))
    with connect_db() as connection:
        results = connection.execute("SELECT choice, votes FROM poll_votes ORDER BY rowid").fetchall()
    return jsonify({row["choice"]: row["votes"] for row in results})


@app.route("/send")
def send():
    return render_template("send.html")


@app.route("/sign", methods=["GET", "POST"])
def sign_up():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        mobile = request.form.get("mobile", "").strip()
        age_text = request.form.get("age", "").strip()
        password = request.form.get("password", "")
        if not name or not email or len(password) < 8:
            flash("Enter your name and email, and choose a password with at least 8 characters.", "error")
            return redirect(url_for("sign_up"))
        try:
            age = int(age_text) if age_text else None
            if age is not None and not 13 <= age <= 120:
                raise ValueError
        except ValueError:
            flash("Enter an age between 13 and 120, or leave age blank.", "error")
            return redirect(url_for("sign_up"))
        try:
            with connect_db() as connection:
                connection.execute(
                    "INSERT INTO users(email,name,password_hash,mobile,age,created_at) VALUES(?,?,?,?,?,?)",
                    (email, name, generate_password_hash(password), mobile, age, datetime.now(timezone.utc).isoformat()),
                )
        except sqlite3.IntegrityError:
            flash("An account with that email already exists. Please sign in.", "error")
            return redirect(url_for("login"))
        session["user_email"] = email
        session["user_name"] = name
        return redirect(url_for("dashboard"))
    return render_template("sign.html")


@app.route("/work")
def work():
    return render_template("work.html")


@app.route("/m")
def dashboard():
    today = date.today().isoformat()
    rows = usage_for_day(today)
    sample_record_count, sample_app_count, sample_month_co2_kg = sample_data_counts()
    total_seconds = sum(row["seconds"] for row in rows)
    details = []
    for row in rows:
        app_name, seconds = row["app"], row["seconds"]
        power = app_power.get(app_name, app_power["Default"])
        factor = emission_factor.get(app_name, emission_factor["Default"])
        hours = seconds / 3600
        energy = power * hours / 1000
        details.append({
            "app": app_name,
            "minutes": int(seconds / 60),
            "hours": hours,
            "power": power,
            "energy": energy,
            "co2": energy * factor,
        })
    return render_template(
        "m.html",
        data=[(row["app"], int(row["seconds"] / 60)) for row in rows],
        today=today,
        total_minutes=int(total_seconds / 60),
        total_co2=carbon_for_rows(rows),
        details=details,
        tracker_enabled=TRACKER_ENABLED,
        tracking_running=tracking_active.is_set(),
        user_name=session.get("user_name"),
        sample_record_count=sample_record_count,
        sample_app_count=sample_app_count,
        sample_month_co2_kg=sample_month_co2_kg,
        sample_month_unsafe=sample_month_co2_kg > MONTHLY_CARBON_LIMIT_KG,
        monthly_limit_kg=MONTHLY_CARBON_LIMIT_KG,
    )


@app.route("/history")
def history():
    with connect_db() as connection:
        days = connection.execute("SELECT day FROM usage GROUP BY day ORDER BY day DESC").fetchall()
        history_data = []
        for item in days:
            rows = connection.execute(
                "SELECT app, seconds FROM usage WHERE day=? ORDER BY seconds DESC", (item["day"],)
            ).fetchall()
            history_data.append({
                "date": item["day"],
                "apps": [{"name": row["app"], "minutes": int(row["seconds"] / 60)} for row in rows],
                "total_minutes": int(sum(row["seconds"] for row in rows) / 60),
                "co2": carbon_for_rows(rows),
            })
    sample_record_count, sample_app_count, sample_month_co2_kg = sample_data_counts()
    return render_template(
        "history.html",
        history=history_data,
        sample_record_count=sample_record_count,
        sample_app_count=sample_app_count,
        sample_month_co2_kg=sample_month_co2_kg,
        sample_month_unsafe=sample_month_co2_kg > MONTHLY_CARBON_LIMIT_KG,
        monthly_limit_kg=MONTHLY_CARBON_LIMIT_KG,
    )


@app.route("/analytics")
def analytics():
    selected_date = request.args.get("date", date.today().isoformat())
    try:
        selected_date = date.fromisoformat(selected_date).isoformat()
    except ValueError:
        selected_date = date.today().isoformat()
    with connect_db() as connection:
        daily_rows = connection.execute(
            "SELECT app, SUM(seconds) AS seconds FROM usage WHERE day=? GROUP BY app ORDER BY seconds DESC",
            (selected_date,),
        ).fetchall()
        daily_totals = connection.execute(
            "SELECT day, app, SUM(seconds) AS seconds FROM usage GROUP BY day, app ORDER BY day"
        ).fetchall()
        totals_by_day: dict[str, list[tuple[str, int]]] = {}
        for row in daily_totals:
            totals_by_day.setdefault(row["day"], []).append((row["app"], row["seconds"]))
        top_rows = connection.execute(
            "SELECT app, SUM(seconds) AS seconds FROM usage GROUP BY app ORDER BY seconds DESC LIMIT 5"
        ).fetchall()
        week_rows = connection.execute(
            "SELECT day, SUM(seconds) AS seconds FROM usage GROUP BY day ORDER BY day DESC LIMIT 7"
        ).fetchall()
    app_labels = [row["app"] for row in daily_rows]
    app_data = [int(row["seconds"] / 60) for row in daily_rows]
    sample_record_count, sample_app_count, sample_month_co2_kg = sample_data_counts()
    week_rows = list(reversed(week_rows))
    carbon_days = sorted(totals_by_day)
    carbon_values = [carbon_for_rows(totals_by_day[day]) for day in carbon_days]
    return render_template(
        "analytics.html",
        selected_date=selected_date,
        app_labels=app_labels,
        app_data=app_data,
        category_totals=build_category_totals(daily_rows),
        carbon_days=carbon_days,
        carbon_values=carbon_values,
        top_apps=[row["app"] for row in top_rows],
        top_minutes=[int(row["seconds"] / 60) for row in top_rows],
        week_days=[row["day"] for row in week_rows],
        week_minutes=[int(row["seconds"] / 60) for row in week_rows],
        total_minutes=sum(app_data),
        total_co2=carbon_for_rows(daily_rows),
        top_app=app_labels[app_data.index(max(app_data))] if app_data else "None",
        app_count=len(app_labels),
        sample_record_count=sample_record_count,
        sample_app_count=sample_app_count,
        sample_month_co2_kg=sample_month_co2_kg,
        sample_month_unsafe=sample_month_co2_kg > MONTHLY_CARBON_LIMIT_KG,
        monthly_limit_kg=MONTHLY_CARBON_LIMIT_KG,
    )


@app.route("/prediction")
def prediction():
    today = date.today()
    recent_start = today - timedelta(days=13)
    month_start = today.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    with connect_db() as connection:
        rows = connection.execute(
            """SELECT day, SUM(seconds) AS seconds FROM usage
            WHERE day>=? AND day<=? GROUP BY day ORDER BY day""",
            (recent_start.isoformat(), today.isoformat()),
        ).fetchall()
        app_rows = connection.execute(
            """SELECT day, app, SUM(seconds) AS seconds FROM usage
            WHERE day>=? AND day<=? GROUP BY day, app ORDER BY day""",
            (recent_start.isoformat(), today.isoformat()),
        ).fetchall()
        month_rows = connection.execute(
            """SELECT day, app, seconds, source FROM usage
            WHERE day>=? AND day<? ORDER BY day, app""",
            (month_start.isoformat(), next_month.isoformat()),
        ).fetchall()

    recent_days = [(recent_start + timedelta(days=index)).isoformat() for index in range(14)]
    seconds_by_day = {row["day"]: row["seconds"] for row in rows}
    actual = [round(seconds_by_day.get(day, 0) / 60, 1) for day in recent_days]
    if len(actual) >= 2 and sum(actual) > 0:
        slope, intercept = fit_line(actual)
        predicted_line = [round(slope * index + intercept, 1) for index in range(len(actual))]
        predicted_minutes = max(0, round(slope * len(actual) + intercept))
        top_app_prediction = "Based on the last 14 days"
    else:
        predicted_line = []
        predicted_minutes = 0
        top_app_prediction = "Not enough data"

    app_seconds_by_day: dict[str, dict[str, int]] = {}
    for row in app_rows:
        app_seconds_by_day.setdefault(row["app"], {})[row["day"]] = row["seconds"]
    app_forecast = []
    for app_name, app_days in app_seconds_by_day.items():
        daily_minutes = [app_days.get(day, 0) / 60 for day in recent_days]
        if not any(daily_minutes):
            continue
        slope, intercept = fit_line(daily_minutes)
        tomorrow_minutes = max(0, round(slope * len(daily_minutes) + intercept))
        tomorrow_kg = tomorrow_minutes / 60 * carbon_rate_kg_per_hour(app_name)
        app_forecast.append({"app": app_name, "minutes": tomorrow_minutes, "co2_kg": tomorrow_kg})
    app_forecast.sort(key=lambda item: item["co2_kg"], reverse=True)
    predicted_co2 = round(sum(item["co2_kg"] for item in app_forecast), 2)

    month_apps = build_app_totals(month_rows)
    month_co2_kg = round(sum(item["co2_kg"] for item in month_apps), 2)
    recommendation_days = today.day
    reduction_plan = make_reduction_plan(month_apps, month_co2_kg, recommendation_days)
    top_contributor = month_apps[0] if month_apps else None
    sample_record_count, sample_app_count, sample_month_co2_kg = sample_data_counts()
    return render_template(
        "prediction.html",
        predicted_minutes=predicted_minutes,
        predicted_co2=predicted_co2,
        top_app_prediction=top_app_prediction,
        app_forecast=app_forecast[:5],
        days=recent_days,
        actual=actual,
        predicted=predicted_line,
        monthly_co2_kg=month_co2_kg,
        reduction_plan=reduction_plan,
        top_contributor=top_contributor,
        over_monthly_limit=month_co2_kg > MONTHLY_CARBON_LIMIT_KG,
        sample_record_count=sample_record_count,
        sample_app_count=sample_app_count,
        sample_month_co2_kg=sample_month_co2_kg,
        sample_month_unsafe=sample_month_co2_kg > MONTHLY_CARBON_LIMIT_KG,
        monthly_limit_kg=MONTHLY_CARBON_LIMIT_KG,
    )


@app.route("/monthly")
def monthly_report():
    selected_month = request.args.get("month", date.today().strftime("%Y-%m"))
    try:
        month_start = date.fromisoformat(f"{selected_month}-01")
        if month_start.strftime("%Y-%m") != selected_month:
            raise ValueError
    except ValueError:
        month_start = date.today().replace(day=1)
        selected_month = month_start.strftime("%Y-%m")
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    today = date.today()
    current_month_start = today.replace(day=1)
    month_days = (next_month - month_start).days
    is_current_month = month_start == current_month_start
    is_completed_month = month_start < current_month_start
    query_end = min(next_month, today + timedelta(days=1)) if is_current_month else next_month

    with connect_db() as connection:
        rows = connection.execute(
            """SELECT day, app, seconds, source FROM usage
            WHERE day>=? AND day<? ORDER BY day, app""",
            (month_start.isoformat(), query_end.isoformat()),
        ).fetchall()

    app_totals = build_app_totals(rows)
    month_co2_kg = sum(item["co2_kg"] for item in app_totals)
    sample_rows = [row for row in rows if row["source"] in {"sample", "mixed"}]
    sample_co2_kg = carbon_for_rows(sample_rows)
    sample_record_count = len(sample_rows)
    sample_app_count = len({row["app"] for row in sample_rows})
    total_seconds = sum(row["seconds"] for row in rows)

    daily_totals: dict[str, dict] = {}
    for row in rows:
        item = daily_totals.setdefault(row["day"], {
            "date": row["day"], "seconds": 0, "co2_kg": 0.0, "sample": False,
        })
        item["seconds"] += row["seconds"]
        item["co2_kg"] += carbon_rate_kg_per_hour(row["app"]) * (row["seconds"] / 3600)
        item["sample"] = item["sample"] or row["source"] in {"sample", "mixed"}

    cumulative_kg = 0.0
    daily_report = []
    for item in daily_totals.values():
        cumulative_kg += item["co2_kg"]
        item["hours"] = item["seconds"] / 3600
        item["co2_kg"] = round(item["co2_kg"], 2)
        item["cumulative_kg"] = round(cumulative_kg, 2)
        item["unsafe"] = cumulative_kg > MONTHLY_CARBON_LIMIT_KG
        item["status"] = "Unsafe: monthly limit passed" if item["unsafe"] else "Within monthly limit"
        daily_report.append(item)

    over_monthly_limit = month_co2_kg > MONTHLY_CARBON_LIMIT_KG
    for item in app_totals:
        item["share_pct"] = (item["co2_kg"] / month_co2_kg * 100) if month_co2_kg else 0
        item["status"] = (
            "Unsafe sample" if over_monthly_limit and item["sample"]
            else "High contributor" if over_monthly_limit and item["share_pct"] >= 10
            else "Within limit"
        )

    days_elapsed = today.day if is_current_month else month_days if is_completed_month else 0
    plan_days = max(1, days_elapsed)
    reduction_plan = make_reduction_plan(app_totals, month_co2_kg, plan_days)
    if is_current_month and rows:
        projected_month_kg = month_co2_kg * month_days / max(days_elapsed, 1)
        projection_available = True
        projection_status = (
            "Already over the monthly limit"
            if over_monthly_limit
            else "On track to exceed the limit"
            if projected_month_kg > MONTHLY_CARBON_LIMIT_KG
            else "On track to stay within the limit"
        )
        projection_note = (
            f"Run-rate estimate from {days_elapsed} of {month_days} days. "
            "It assumes the average daily emissions continue through month-end."
        )
    elif is_completed_month:
        projected_month_kg = month_co2_kg
        projection_available = True
        projection_status = "Month ended over the limit" if over_monthly_limit else "Month ended within the limit"
        projection_note = "This month is complete; this is its final recorded total."
    else:
        projected_month_kg = None
        projection_available = False
        projection_status = "No data yet"
        projection_note = "A projection will appear after usage is recorded for this month."
    projection_over_limit = bool(projection_available and projected_month_kg > MONTHLY_CARBON_LIMIT_KG)
    sample_month_unsafe = sample_co2_kg > MONTHLY_CARBON_LIMIT_KG
    return render_template(
        "monthly.html",
        selected_month=selected_month,
        month_co2_kg=round(month_co2_kg, 2),
        month_hours=total_seconds / 3600,
        monthly_limit_kg=MONTHLY_CARBON_LIMIT_KG,
        over_monthly_limit=over_monthly_limit,
        remaining_kg=max(0, MONTHLY_CARBON_LIMIT_KG - month_co2_kg),
        excess_kg=max(0, month_co2_kg - MONTHLY_CARBON_LIMIT_KG),
        progress_pct=min(100, month_co2_kg / MONTHLY_CARBON_LIMIT_KG * 100) if MONTHLY_CARBON_LIMIT_KG else 0,
        projected_month_kg=round(projected_month_kg, 2) if projected_month_kg is not None else None,
        projection_available=projection_available,
        projection_over_limit=projection_over_limit,
        projection_status=projection_status,
        projection_note=projection_note,
        app_totals=app_totals,
        daily_report=daily_report,
        top_contributor=app_totals[0] if app_totals else None,
        reduction_plan=reduction_plan,
        sample_record_count=sample_record_count,
        sample_app_count=sample_app_count,
        sample_month_co2_kg=round(sample_co2_kg, 2),
        sample_month_unsafe=sample_month_unsafe,
    )


if __name__ == "__main__":
    if TRACKER_ENABLED:
        start_tracking_service()
    else:
        print("Desktop usage tracking is off; the web app and manual calculator are available.")
        if not TRACKER_AVAILABLE:
            print("Install pywin32 on Windows to enable foreground-app tracking.")
    app.run(
        host=os.environ.get("ECO_TRACKER_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5001")),
        debug=os.environ.get("ECO_TRACKER_DEBUG", "1").lower() in {"1", "true", "yes"},
        use_reloader=False,
    )
