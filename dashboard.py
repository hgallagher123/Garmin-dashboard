#!/usr/bin/env python3
"""
Garmin Training Dashboard
--------------------------
Pulls data from your Garmin account (via cached login token) and writes a
self-contained index.html you can open in any browser.

First run: needs your Garmin email/password once, to create a cached
           session token on disk (~/.garminconnect).
Every run after that: silent, no password prompt, just refreshes the data.

Usage:
    pip install garminconnect
    python dashboard.py

Then open the generated index.html in your browser.
"""

import os
import json
import math
import getpass
from datetime import datetime, timedelta

import garminconnect
from garth.exc import GarthHTTPError

TOKEN_STORE = os.path.expanduser("~/.garminconnect")
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

# How far back to pull data
ACTIVITY_LOOKBACK_DAYS = 150   # extra history so CTL/ATL have time to "warm up"
CHART_WINDOW_DAYS = 84         # ~12 weeks shown on charts
RECENT_ACTIVITY_DAYS = 28      # ~3-4 weeks for the activity table
HRV_SLEEP_LOOKBACK_DAYS = 30   # Garmin HRV trend tool caps at 30 days anyway

MAX_HR_ESTIMATE = 220 - 25     # rough fallback if we can't read HR zones; adjust if you know yours


def get_client():
    """Log in using a cached token if one exists; otherwise ask once and cache it."""
    garmin = garminconnect.Garmin()
    try:
        garmin.login(TOKEN_STORE)
        print("Logged in using cached session.")
        return garmin
    except (FileNotFoundError, GarthHTTPError, Exception):
        print("No valid cached session found — need to log in once.")
        email = input("Garmin email: ").strip()
        password = getpass.getpass("Garmin password: ")
        garmin = garminconnect.Garmin(email=email, password=password)
        garmin.login()
        garmin.garth.dump(TOKEN_STORE)
        print("Logged in and cached session for next time.")
        return garmin


def safe_call(fn, *args, default=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"  (skipped {fn.__name__}: {e})")
        return default


def fetch_activities(garmin, days):
    """Pull activities and keep only those within `days` of today."""
    cutoff = datetime.now() - timedelta(days=days)
    activities = []
    start = 0
    page = 100
    while True:
        batch = safe_call(garmin.get_activities, start, page, default=[])
        if not batch:
            break
        stop = False
        for a in batch:
            try:
                a_date = datetime.strptime(a["startTimeLocal"][:10], "%Y-%m-%d")
            except Exception:
                continue
            if a_date < cutoff:
                stop = True
                break
            activities.append(a)
        if stop or len(batch) < page:
            break
        start += page
    return activities


def training_load_from_activity(a, resting_hr, max_hr):
    """Approximate a Banister-style TRIMP load score for one activity."""
    duration_min = (a.get("duration") or 0) / 60.0
    avg_hr = a.get("averageHR")
    if not duration_min or not avg_hr or not max_hr or not resting_hr or max_hr <= resting_hr:
        # fall back to a simple duration-based load if HR data is missing
        return round(duration_min * 5, 1)
    hr_ratio = max(0.0, min(1.0, (avg_hr - resting_hr) / (max_hr - resting_hr)))
    # Banister TRIMP (male coefficients ~0.64/1.92; close enough as a relative index)
    trimp = duration_min * hr_ratio * 0.64 * math.exp(1.92 * hr_ratio)
    return round(trimp, 1)


def build_ctl_atl_tsb(daily_load, days):
    """Rolling CTL(42d)/ATL(7d)/TSB from a dict of {date_str: load}."""
    end = datetime.now().date()
    start = end - timedelta(days=days + 1)
    ctl = atl = 0.0
    series = []
    d = start
    while d <= end:
        key = d.isoformat()
        load = daily_load.get(key, 0.0)
        ctl = ctl + (load - ctl) / 42.0
        atl = atl + (load - atl) / 7.0
        series.append({
            "date": key,
            "load": round(load, 1),
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(ctl - atl, 1),
        })
        d += timedelta(days=1)
    # only return the display window
    cutoff = (end - timedelta(days=CHART_WINDOW_DAYS)).isoformat()
    return [p for p in series if p["date"] >= cutoff]


def build_weekly_mileage(activities, weeks=12):
    end = datetime.now().date()
    start = end - timedelta(weeks=weeks)
    buckets = {}
    for a in activities:
        if "running" not in (a.get("activityType", {}).get("typeKey") or ""):
            continue
        try:
            a_date = datetime.strptime(a["startTimeLocal"][:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if a_date < start:
            continue
        iso_year, iso_week, _ = a_date.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
        km = (a.get("distance") or 0) / 1000.0
        buckets[key] = buckets.get(key, 0.0) + km
    weeks_sorted = sorted(buckets.keys())
    values = [round(buckets[w], 1) for w in weeks_sorted]
    # simple linear trend line (least squares)
    n = len(values)
    trend = []
    if n >= 2:
        xs = list(range(n))
        mean_x = sum(xs) / n
        mean_y = sum(values) / n
        num = sum((xs[i] - mean_x) * (values[i] - mean_y) for i in range(n))
        den = sum((xs[i] - mean_x) ** 2 for i in range(n)) or 1
        slope = num / den
        intercept = mean_y - slope * mean_x
        trend = [round(slope * x + intercept, 1) for x in xs]
    return weeks_sorted, values, trend


def fetch_hrv_sleep_rhr(garmin, days):
    hrv_series, sleep_series, rhr_series = [], [], []
    for i in range(days):
        d = (datetime.now().date() - timedelta(days=days - 1 - i)).isoformat()
        hrv = safe_call(garmin.get_hrv_data, d)
        if hrv and hrv.get("hrvSummary"):
            val = hrv["hrvSummary"].get("lastNightAvg")
            if val:
                hrv_series.append({"date": d, "value": val})
        sleep = safe_call(garmin.get_sleep_data, d)
        if sleep and sleep.get("dailySleepDTO"):
            secs = sleep["dailySleepDTO"].get("sleepTimeSeconds")
            if secs:
                sleep_series.append({"date": d, "hours": round(secs / 3600.0, 2)})
        stats = safe_call(garmin.get_stats, d)
        if stats and stats.get("restingHeartRate"):
            rhr_series.append({"date": d, "value": stats["restingHeartRate"]})
    return hrv_series, sleep_series, rhr_series


def fetch_vo2max(garmin, days):
    series = []
    seen = set()
    for i in range(0, days, 3):  # sample every few days, VO2max updates slowly anyway
        d = (datetime.now().date() - timedelta(days=days - i)).isoformat()
        m = safe_call(garmin.get_max_metrics, d)
        if m and isinstance(m, list) and m:
            val = m[0].get("generic", {}).get("vo2MaxPreciseValue") or m[0].get("generic", {}).get("vo2MaxValue")
            if val and val not in seen:
                series.append({"date": d, "value": val})
                seen.add(val)
    return series


def build_activity_table(activities, days):
    cutoff = datetime.now() - timedelta(days=days)
    rows = []
    for a in activities:
        try:
            a_date = datetime.strptime(a["startTimeLocal"][:10], "%Y-%m-%d")
        except Exception:
            continue
        if a_date < cutoff:
            continue
        dist_km = (a.get("distance") or 0) / 1000.0
        dur_s = a.get("duration") or 0
        pace = "-"
        if dist_km > 0.1 and "running" in (a.get("activityType", {}).get("typeKey") or ""):
            pace_min_per_km = (dur_s / 60.0) / dist_km
            m, s = divmod(int(pace_min_per_km * 60), 60)
            pace = f"{m}:{s:02d} /km"
        rows.append({
            "date": a_date.strftime("%Y-%m-%d"),
            "name": a.get("activityName", ""),
            "type": (a.get("activityType", {}).get("typeKey") or "").replace("_", " ").title(),
            "distance_km": round(dist_km, 2),
            "duration_min": round(dur_s / 60.0, 1),
            "pace": pace,
            "avg_hr": a.get("averageHR") or "-",
        })
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def main():
    garmin = get_client()

    print("Fetching activities...")
    activities = fetch_activities(garmin, ACTIVITY_LOOKBACK_DAYS)

    print("Fetching resting HR / HR zones for load calc...")
    today_stats = safe_call(garmin.get_stats, datetime.now().strftime("%Y-%m-%d")) or {}
    resting_hr = today_stats.get("restingHeartRate") or 55
    max_hr = MAX_HR_ESTIMATE

    print("Computing training load (CTL/ATL/TSB)...")
    daily_load = {}
    for a in activities:
        try:
            a_date = datetime.strptime(a["startTimeLocal"][:10], "%Y-%m-%d").date().isoformat()
        except Exception:
            continue
        load = training_load_from_activity(a, resting_hr, max_hr)
        daily_load[a_date] = daily_load.get(a_date, 0.0) + load
    load_series = build_ctl_atl_tsb(daily_load, ACTIVITY_LOOKBACK_DAYS)

    print("Building weekly mileage...")
    weeks_labels, weekly_km, weekly_trend = build_weekly_mileage(activities, weeks=12)

    print("Fetching HRV / sleep / resting HR history (this can take a minute)...")
    hrv_series, sleep_series, rhr_series = fetch_hrv_sleep_rhr(garmin, HRV_SLEEP_LOOKBACK_DAYS)

    print("Fetching VO2 max trend...")
    vo2_series = fetch_vo2max(garmin, ACTIVITY_LOOKBACK_DAYS)

    print("Building recent activity table...")
    table_rows = build_activity_table(activities, RECENT_ACTIVITY_DAYS)

    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "load_series": load_series,
        "weeks_labels": weeks_labels,
        "weekly_km": weekly_km,
        "weekly_trend": weekly_trend,
        "hrv_series": hrv_series,
        "sleep_series": sleep_series,
        "rhr_series": rhr_series,
        "vo2_series": vo2_series,
        "table_rows": table_rows,
    }

    html = render_html(data)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nDone. Wrote {OUTPUT_FILE}")
    print("Open it in your browser (double-click it, or run: open index.html)")


def render_html(data):
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Training Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.4/chart.umd.min.js"></script>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0f1115; color:#e6e6e6; margin:0; padding:24px; }
  h1 { font-size:22px; margin-bottom:4px; }
  .meta { color:#888; font-size:13px; margin-bottom:24px; }
  .card { background:#1a1d24; border-radius:10px; padding:18px; margin-bottom:20px; }
  .card h2 { font-size:15px; margin:0 0 12px 0; color:#bbb; text-transform:uppercase; letter-spacing:0.5px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
  @media (max-width:900px){ .grid{ grid-template-columns:1fr; } }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid #2a2d35; }
  th { color:#888; font-weight:500; }
  canvas { max-height:280px; }
</style>
</head>
<body>
<h1>Training Dashboard</h1>
<div class="meta">Last refreshed __GENERATED_AT__ &middot; run <code>python dashboard.py</code> to refresh</div>

<div class="card">
  <h2>Training Load (CTL / ATL / TSB)</h2>
  <canvas id="loadChart"></canvas>
</div>

<div class="grid">
  <div class="card">
    <h2>Weekly Mileage</h2>
    <canvas id="mileageChart"></canvas>
  </div>
  <div class="card">
    <h2>VO2 Max Trend</h2>
    <canvas id="vo2Chart"></canvas>
  </div>
</div>

<div class="card">
  <h2>Recovery: HRV / Resting HR / Sleep</h2>
  <canvas id="recoveryChart"></canvas>
</div>

<div class="card">
  <h2>Recent Activities</h2>
  <table id="activityTable">
    <thead><tr><th>Date</th><th>Name</th><th>Type</th><th>Distance (km)</th><th>Duration (min)</th><th>Pace</th><th>Avg HR</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<script>
const DATA = __DATA_JSON__;

function line(ctx, datasets, labels) {
  return new Chart(ctx, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      interaction: { mode: 'index', intersect: false },
      scales: { x: { ticks: { color: '#888', maxTicksLimit: 10 } }, y: { ticks: { color: '#888' } } },
      plugins: { legend: { labels: { color: '#ccc' } } }
    }
  });
}

// Training load
line(document.getElementById('loadChart'),
  [
    { label: 'CTL (fitness)', data: DATA.load_series.map(p => p.ctl), borderColor: '#4f9dfe', tension: 0.3, pointRadius: 0 },
    { label: 'ATL (fatigue)', data: DATA.load_series.map(p => p.atl), borderColor: '#fe6b6b', tension: 0.3, pointRadius: 0 },
    { label: 'TSB (form)', data: DATA.load_series.map(p => p.tsb), borderColor: '#7ee787', tension: 0.3, pointRadius: 0 },
  ],
  DATA.load_series.map(p => p.date)
);

// Weekly mileage (bar + trend line)
new Chart(document.getElementById('mileageChart'), {
  data: {
    labels: DATA.weeks_labels,
    datasets: [
      { type: 'bar', label: 'km', data: DATA.weekly_km, backgroundColor: '#4f9dfe88' },
      { type: 'line', label: 'trend', data: DATA.weekly_trend, borderColor: '#fbbf24', pointRadius: 0 },
    ]
  },
  options: { scales: { x: { ticks: { color: '#888' } }, y: { ticks: { color: '#888' } } }, plugins: { legend: { labels: { color: '#ccc' } } } }
});

// VO2 max
line(document.getElementById('vo2Chart'),
  [{ label: 'VO2 Max', data: DATA.vo2_series.map(p => p.value), borderColor: '#a78bfa', tension: 0.3, pointRadius: 0 }],
  DATA.vo2_series.map(p => p.date)
);

// Recovery (dual axis-ish, just overlay for simplicity)
line(document.getElementById('recoveryChart'),
  [
    { label: 'HRV (ms)', data: DATA.hrv_series.map(p => p.value), borderColor: '#4f9dfe', tension: 0.3, pointRadius: 0 },
    { label: 'Resting HR (bpm)', data: DATA.rhr_series.map(p => p.value), borderColor: '#fe6b6b', tension: 0.3, pointRadius: 0 },
    { label: 'Sleep (hrs)', data: DATA.sleep_series.map(p => p.hours), borderColor: '#7ee787', tension: 0.3, pointRadius: 0 },
  ],
  (DATA.hrv_series.length ? DATA.hrv_series : DATA.rhr_series).map(p => p.date)
);

// Activity table
const tbody = document.querySelector('#activityTable tbody');
DATA.table_rows.forEach(r => {
  const tr = document.createElement('tr');
  tr.innerHTML = `<td>${r.date}</td><td>${r.name}</td><td>${r.type}</td><td>${r.distance_km}</td><td>${r.duration_min}</td><td>${r.pace}</td><td>${r.avg_hr}</td>`;
  tbody.appendChild(tr);
});
</script>
</body>
</html>
""".replace("__GENERATED_AT__", data["generated_at"]).replace("__DATA_JSON__", json.dumps(data))


if __name__ == "__main__":
    main()
