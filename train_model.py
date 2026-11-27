"""Save a simple linear usage forecast from the local usage database."""

import pickle
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
with sqlite3.connect(BASE_DIR / "usage.db") as connection:
    rows = connection.execute(
        "SELECT day, SUM(seconds) FROM usage GROUP BY day ORDER BY day"
    ).fetchall()

if len(rows) < 2:
    raise SystemExit("Record usage on at least two different days before training a forecast.")

values = [seconds / 60 for _, seconds in rows]
count = len(values)
mean_x = (count - 1) / 2
mean_y = sum(values) / count
slope = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values))
slope /= sum((index - mean_x) ** 2 for index in range(count))
intercept = mean_y - slope * mean_x

model = {
    "slope_minutes_per_day": slope,
    "intercept_minutes": intercept,
    "predicted_next_day_minutes": max(0, round(slope * count + intercept)),
    "training_days": [day for day, _ in rows],
}
with (BASE_DIR / "model.pkl").open("wb") as model_file:
    pickle.dump(model, model_file)

print(f"Forecast saved using {count} days of usage data.")
