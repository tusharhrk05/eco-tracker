"""Create a labeled, high-usage demo month for report and prediction views."""

import random
from datetime import date, timedelta

from app import (
    MONTHLY_CARBON_LIMIT_KG,
    app_categories,
    carbon_for_rows,
    connect_db,
    init_db,
)


APPS = (
    "Chrome", "Edge", "Netflix", "YouTube", "Prime Video", "Twitch", "Zoom",
    "Google Meet", "Microsoft Teams", "WhatsApp", "Discord", "Instagram",
    "Facebook", "Minecraft", "Steam",
)
BASE_MINUTES = {
    "Chrome": 80, "Edge": 70, "Netflix": 300, "YouTube": 130,
    "Prime Video": 120, "Twitch": 120, "Zoom": 100, "Google Meet": 100,
    "Microsoft Teams": 100, "WhatsApp": 45, "Discord": 80, "Instagram": 60,
    "Facebook": 50, "Minecraft": 140, "Steam": 100,
}
SAMPLE_DAYS = 16
TARGET_SCREEN_MINUTES_PER_DAY = 1430


def main() -> None:
    init_db()
    today = date.today()
    first_day = today - timedelta(days=SAMPLE_DAYS - 1)
    month_start = today.replace(day=1)
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    rng = random.Random(27112026)
    rows = []

    for day_index in range(SAMPLE_DAYS):
        sample_day = (first_day + timedelta(days=day_index)).isoformat()
        start = (day_index * 5) % len(APPS)
        selected_apps = [APPS[(start + offset) % len(APPS)] for offset in range(12)]
        varied = {app_name: BASE_MINUTES[app_name] * rng.uniform(0.92, 1.08) for app_name in selected_apps}
        raw_total = sum(varied.values())
        daily_minutes = {
            app_name: round(value * TARGET_SCREEN_MINUTES_PER_DAY / raw_total)
            for app_name, value in varied.items()
        }
        adjustment = TARGET_SCREEN_MINUTES_PER_DAY - sum(daily_minutes.values())
        longest_app = max(daily_minutes, key=daily_minutes.get)
        daily_minutes[longest_app] += adjustment

        for app_name, minutes in daily_minutes.items():
            rows.append((sample_day, app_name, minutes * 60, "sample"))

    with connect_db() as connection:
        # Refresh only this month's generated rows; tracked user records stay intact.
        connection.execute(
            "DELETE FROM usage WHERE source='sample' AND day>=? AND day<?",
            (month_start.isoformat(), next_month.isoformat()),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO usage(day, app, seconds, source) VALUES (?, ?, ?, ?)",
            rows,
        )
        sample_rows = connection.execute(
            """SELECT app, SUM(seconds) AS seconds FROM usage
            WHERE source='sample' AND day>=? AND day<? GROUP BY app""",
            (month_start.isoformat(), next_month.isoformat()),
        ).fetchall()
        sample_count = connection.execute(
            "SELECT COUNT(*) FROM usage WHERE source='sample' AND day>=? AND day<?",
            (month_start.isoformat(), next_month.isoformat()),
        ).fetchone()[0]

    sample_co2_kg = carbon_for_rows(sample_rows)
    status = "UNSAFE: above monthly limit" if sample_co2_kg > MONTHLY_CARBON_LIMIT_KG else "within monthly limit"
    print(f"Added {sample_count} labeled sample records across {len(sample_rows)} apps.")
    print(f"Sample period: {first_day.isoformat()} through {today.isoformat()}.")
    print(f"Sample-only monthly estimate: {sample_co2_kg:.2f} kg CO2e; limit {MONTHLY_CARBON_LIMIT_KG:.1f} kg - {status}.")
    print("App types: " + ", ".join(sorted(set(app_categories.get(name, "Other") for name in APPS))) + ".")


if __name__ == "__main__":
    main()
