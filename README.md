# Eco-Tracker

Eco-Tracker is a local Flask app for estimating the carbon impact of digital activity. On Windows, it can record foreground application use while the app is open. It also includes a manual calculator, history, analytics, trend predictions, a local account flow, a contact-message inbox in SQLite, and a shared poll.

## Run it in VS Code

1. Install Python 3.11 or newer and the **Python** extension for VS Code.
2. Open this project folder in VS Code.
3. Open **Terminal → Run Task… → Prepare Eco-Tracker environment**. This creates `.venv` and installs the project packages.
4. Select `.venv` as the Python interpreter if VS Code asks.
5. Press **F5** and choose **Eco-Tracker (Flask)** if prompted. The app opens at <http://127.0.0.1:5001>.

You can also run it from the VS Code terminal after setup:

```powershell
.\.venv\Scripts\python.exe app.py
```

Stop the server with **Ctrl+C** in the terminal.

## Tracking

Windows foreground-app tracking needs `pywin32` and `psutil`. The setup task installs them. If the tracker cannot access Windows foreground-window information, the website still starts and the manual calculator remains available. To run without desktop tracking, set `ECO_TRACKER_DISABLE=1` in the VS Code launch environment.

The database is `usage.db` in this project folder. It stores usage totals, local accounts, poll counts, contact messages, and short-lived password-reset tokens. Account passwords are stored as hashes. Since this project has no email service configured, password recovery displays a one-time link in the local app; the link expires after 30 minutes.

To add the labeled high-usage demo month for charts, limit warnings, and forecasts, run `.\.venv\Scripts\python.exe seed_sample_data.py` from the project folder. It creates 192 entries across 15 apps over the latest 16 days and intentionally exceeds the 8.5 kg monthly example limit. Demo rows are marked `source='sample'` and identified in the analysis pages.

The included `train_model.py` can save a forecast snapshot to `model.pkl`. The prediction page calculates its forecast from the latest database contents each time it loads.

## VS Code setup task

The task locates an installed Python interpreter, creates `.venv` when needed, and installs `requirements.txt`. If Python is not installed, install Python first, then rerun the task.

## Deploy to Render

The `render.yaml` Blueprint configures a free Singapore web service, installs the app dependencies, seeds the labeled demo month, and serves Flask with Gunicorn. Connect this repository in Render and create a Blueprint from `render.yaml` to publish it.

Render runs on Linux, so it cannot capture foreground apps from your Windows device. The web app and its sample analysis still work; the local desktop tracker remains available when running from VS Code on Windows. The free service uses temporary storage, so its SQLite records can reset when Render restarts or spins down. Do not use the deployed demo for lasting personal records.
