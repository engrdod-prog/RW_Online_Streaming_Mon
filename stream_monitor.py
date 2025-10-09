# stream_monitor.py

import streamlit as st
import requests
from datetime import datetime, time as dt_time, timedelta
import pytz
import json
import sqlite3
import os
import io
import csv
import zipfile
from typing import Optional, Dict, Tuple, Any
from requests.exceptions import RequestException, Timeout, SSLError, ConnectionError
from urllib.parse import urlparse, urlunparse

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    st_autorefresh = None

# --- Configuration ---
STREAMS = [
    {"name": "Website", "url": "http://in-icecast.eradioportal.com:8000/rwluzon"},
]

SCHEDULE = {
    "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "start_time": dt_time(4, 30),  # 10:00 AM
    "end_time": dt_time(22, 0),    # 2:00 PM
    "timezone": "Asia/Singapore"
}

REFRESH_INTERVAL = 30  # seconds

# Logging configuration
LOGGING_CONFIG = {
    "enabled": True,
    "log_to_file": True,
    "log_to_database": True,
    "log_file_path": "uptime_logs.json",
    "database_path": "uptime_monitor.db",
    "log_interval": 300  # Log every 5 minutes (300 seconds)
}

# --- Helper Functions ---
def is_within_schedule():
    tz = pytz.timezone(SCHEDULE["timezone"])
    now = datetime.now(tz)
    current_day = now.strftime("%A")
    current_time = now.time()

    if current_day in SCHEDULE["days"]:
        if SCHEDULE["start_time"] <= current_time <= SCHEDULE["end_time"]:
            return True
    return False

@st.cache_data(ttl=REFRESH_INTERVAL)
def check_url_status(url: str) -> bool:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36"
    }
    try:
        # Prefer HEAD to avoid downloading bodies on streaming endpoints.
        resp = requests.head(url, headers=headers, timeout=8, allow_redirects=True)
        # Some servers don’t implement HEAD; fallback to GET if HEAD not allowed.
        if resp.status_code == 405:
            resp = requests.get(url, headers=headers, timeout=8, allow_redirects=True, stream=True)
        return 200 <= resp.status_code < 400
    except (Timeout, SSLError, ConnectionError, RequestException):
        return False

def check_icecast_mount(mount_url: str) -> bool:
    try:
        parsed = urlparse(mount_url)
        status_url = urlunparse((parsed.scheme, parsed.netloc, "/status-json.xsl", "", "", ""))
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36"
        }
        resp = requests.get(status_url, headers=headers, timeout=8)
        if not (200 <= resp.status_code < 400):
            return False
        data = resp.json()
        sources = data.get("icestats", {}).get("source", [])
        if isinstance(sources, dict):
            sources = [sources]
        mount_path = parsed.path  # e.g., "/rwluzon"
        for s in sources:
            listenurl = s.get("listenurl", "") or s.get("url", "")
            mount = s.get("mount", "")
            if (mount and mount == mount_path) or (listenurl and listenurl.endswith(mount_path)):
                st.session_state["last_icecast_meta"] = s  # optional for debugging
                return True
        return False
    except Exception:
        return False

# --- Logging Functions ---
def init_database():
    """Initialize SQLite database for uptime logging"""
    if not LOGGING_CONFIG["log_to_database"]:
        return
    
    conn = sqlite3.connect(LOGGING_CONFIG["database_path"])
    cursor = conn.cursor()
    
    # Create uptime_logs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS uptime_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            stream_name TEXT NOT NULL,
            status TEXT NOT NULL,
            response_time REAL,
            error_message TEXT,
            timezone TEXT NOT NULL
        )
    ''')
    
    # Create index for faster queries
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_timestamp 
        ON uptime_logs(timestamp)
    ''')
    
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_stream_name 
        ON uptime_logs(stream_name)
    ''')
    
    conn.commit()
    conn.close()

def log_to_file(stream_name, status, response_time=None, error_message=None):
    """Log uptime data to JSON file"""
    if not LOGGING_CONFIG["log_to_file"]:
        return
    
    tz = pytz.timezone(SCHEDULE["timezone"])
    timestamp = datetime.now(tz).isoformat()
    
    log_entry = {
        "timestamp": timestamp,
        "stream_name": stream_name,
        "status": status,
        "response_time": response_time,
        "error_message": error_message,
        "timezone": SCHEDULE["timezone"]
    }
    
    # Append to JSON file
    log_file = LOGGING_CONFIG["log_file_path"]
    logs = []
    
    # Read existing logs
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r') as f:
                logs = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            logs = []
    
    # Add new log entry
    logs.append(log_entry)
    
    # Keep only last 1000 entries to prevent file from growing too large
    if len(logs) > 1000:
        logs = logs[-1000:]
    
    # Write back to file
    with open(log_file, 'w') as f:
        json.dump(logs, f, indent=2)

def log_to_database(stream_name, status, response_time=None, error_message=None):
    """Log uptime data to SQLite database"""
    if not LOGGING_CONFIG["log_to_database"]:
        return
    
    tz = pytz.timezone(SCHEDULE["timezone"])
    timestamp = datetime.now(tz).isoformat()
    
    conn = sqlite3.connect(LOGGING_CONFIG["database_path"])
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO uptime_logs 
        (timestamp, stream_name, status, response_time, error_message, timezone)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (timestamp, stream_name, status, response_time, error_message, SCHEDULE["timezone"]))
    
    conn.commit()
    conn.close()

def log_uptime_status(stream_name, status, response_time=None, error_message=None):
    """Main logging function that handles both file and database logging"""
    if not LOGGING_CONFIG["enabled"]:
        return
    
    # Log to file
    log_to_file(stream_name, status, response_time, error_message)
    
    # Log to database
    log_to_database(stream_name, status, response_time, error_message)

def get_uptime_stats():
    """Get uptime statistics from database"""
    if not LOGGING_CONFIG["log_to_database"]:
        return None
    
    conn = sqlite3.connect(LOGGING_CONFIG["database_path"])
    cursor = conn.cursor()
    
    # Get overall uptime stats
    cursor.execute('''
        SELECT 
            stream_name,
            COUNT(*) as total_checks,
            SUM(CASE WHEN status = 'online' THEN 1 ELSE 0 END) as online_count,
            AVG(response_time) as avg_response_time
        FROM uptime_logs 
        WHERE timestamp >= datetime('now', '-24 hours')
        GROUP BY stream_name
    ''')
    
    stats = cursor.fetchall()
    conn.close()
    
    return stats

def get_timeout_stats():
    """Get timeout statistics for TODAY only in configured timezone"""
    if not LOGGING_CONFIG["log_to_database"]:
        return None
    
    # We'll filter in Python using today's bounds in configured timezone
    conn = sqlite3.connect(LOGGING_CONFIG["database_path"])
    cursor = conn.cursor()
    cursor.execute('''
        SELECT timestamp, stream_name, status, response_time, error_message
        FROM uptime_logs
        WHERE status = 'offline' AND response_time > 0
        ORDER BY timestamp DESC
    ''')
    rows = cursor.fetchall()
    conn.close()

    start_today, end_today, tz = _get_today_bounds()

    # Filter rows to today in configured TZ
    today_rows = []
    for ts, stream_name, status, response_time, error_message in rows:
        dt = _parse_iso_to_tz(ts, tz)
        if dt is None:
            continue
        if start_today <= dt <= end_today:
            today_rows.append((ts, stream_name, response_time, error_message))

    # Aggregate stats by stream
    from collections import defaultdict
    by_stream = defaultdict(list)
    for ts, stream_name, response_time, error_message in today_rows:
        by_stream[stream_name].append(response_time)

    timeout_stats = []
    for stream_name, times in by_stream.items():
        if not times:
            continue
        total = len(times)
        avg_t = sum(times) / total
        min_t = min(times)
        max_t = max(times)
        timeout_stats.append((stream_name, total, avg_t, min_t, max_t))

    # Recent timeouts limited to 10 for today
    recent_timeouts = today_rows[:10]

    return timeout_stats, recent_timeouts

def get_downtime_periods():
    """Get downtime periods and their lengths from database"""
    if not LOGGING_CONFIG["log_to_database"]:
        return None
    
    conn = sqlite3.connect(LOGGING_CONFIG["database_path"])
    cursor = conn.cursor()
    
    # Get all status changes for each stream in chronological order
    cursor.execute('''
        SELECT 
            stream_name,
            timestamp,
            status,
            response_time,
            error_message
        FROM uptime_logs 
        WHERE timestamp >= datetime('now', '-24 hours')
        ORDER BY stream_name, timestamp ASC
    ''')
    
    all_events = cursor.fetchall()
    conn.close()
    
    # Group events by stream and calculate downtime periods
    downtime_periods = {}
    
    for stream_name in set(event[0] for event in all_events):
        stream_events = [event for event in all_events if event[0] == stream_name]
        downtime_periods[stream_name] = []
        
        current_downtime_start = None
        
        for i, (_, timestamp, status, response_time, error_message) in enumerate(stream_events):
            if status == 'offline' and current_downtime_start is None:
                # Start of a downtime period
                current_downtime_start = timestamp
            elif status == 'online' and current_downtime_start is not None:
                # End of a downtime period
                try:
                    start_time = datetime.fromisoformat(current_downtime_start.replace('Z', '+00:00'))
                    end_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    duration = (end_time - start_time).total_seconds()
                    
                    downtime_periods[stream_name].append({
                        'start': current_downtime_start,
                        'end': timestamp,
                        'duration_seconds': duration,
                        'duration_formatted': format_duration(duration),
                        'error_message': error_message
                    })
                except:
                    # Handle timestamp parsing errors
                    pass
                current_downtime_start = None
        
        # Handle ongoing downtime (if the last event was offline)
        if current_downtime_start is not None and stream_events:
            last_event = stream_events[-1]
            if last_event[2] == 'offline':
                try:
                    start_time = datetime.fromisoformat(current_downtime_start.replace('Z', '+00:00'))
                    current_time = datetime.now()
                    duration = (current_time - start_time).total_seconds()
                    
                    downtime_periods[stream_name].append({
                        'start': current_downtime_start,
                        'end': 'Ongoing',
                        'duration_seconds': duration,
                        'duration_formatted': format_duration(duration) + ' (ongoing)',
                        'error_message': last_event[4]
                    })
                except:
                    pass
    
    return downtime_periods

def format_duration(seconds):
    """Format duration in seconds to human readable format"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        minutes = seconds / 60
        return f"{minutes:.1f}m"
    else:
        hours = seconds / 3600
        return f"{hours:.1f}h"

def _get_today_bounds() -> Tuple[datetime, datetime, Any]:
    """Return (start_of_today, end_of_today, tz) in the configured timezone."""
    tz = pytz.timezone(SCHEDULE["timezone"])
    now_tz = datetime.now(tz)
    start = now_tz.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start, end, tz

def _parse_iso_to_tz(dt_iso: str, tz: Any) -> Optional[datetime]:
    try:
        # Support stored timestamps with offset (e.g., +08:00) or 'Z'
        dt = datetime.fromisoformat(dt_iso.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return tz.localize(dt)
        return dt.astimezone(tz)
    except Exception:
        return None

def _interval_overlaps_today(start_iso: str, end_iso: str) -> bool:
    start_today, end_today, tz = _get_today_bounds()
    start_dt = _parse_iso_to_tz(start_iso, tz)
    end_dt = datetime.now(tz) if end_iso == 'Ongoing' else _parse_iso_to_tz(end_iso, tz)
    if start_dt is None or end_dt is None:
        return False
    return max(start_dt, start_today) <= min(end_dt, end_today)

def _split_duration_by_day(start_iso: str, end_iso: str, duration_seconds: float) -> Dict[str, float]:
    """Split a downtime interval across calendar days and return seconds per YYYY-MM-DD.
    Assumes ISO timestamps. If parsing fails, falls back to attributing all to the start date.
    """
    per_day: Dict[str, float] = {}
    try:
        start_dt = datetime.fromisoformat(start_iso.replace('Z', '+00:00'))
        if end_iso == 'Ongoing':
            end_dt = datetime.now()
        else:
            end_dt = datetime.fromisoformat(end_iso.replace('Z', '+00:00'))

        current = start_dt
        while current < end_dt:
            # end of current day
            day_end = current.replace(hour=23, minute=59, second=59, microsecond=999999)
            segment_end = min(day_end, end_dt)
            seg_seconds = (segment_end - current).total_seconds()
            date_key = current.date().isoformat()
            per_day[date_key] = per_day.get(date_key, 0.0) + max(seg_seconds, 0.0)
            current = segment_end + timedelta(microseconds=1)
        # Normalize tiny rounding
        for k in list(per_day.keys()):
            if per_day[k] < 0:
                per_day[k] = 0.0
    except Exception:
        # Fallback: attribute entire duration to start date
        try:
            date_key = datetime.fromisoformat(start_iso.replace('Z', '+00:00')).date().isoformat()
        except Exception:
            date_key = datetime.now().date().isoformat()
        per_day[date_key] = per_day.get(date_key, 0.0) + float(duration_seconds or 0.0)
    return per_day

def export_csv_combined() -> str:
    """Build a single CSV with both Downtime_Periods and Daily_Totals sections."""
    output = io.StringIO()
    writer = csv.writer(output)
    data = get_downtime_periods() or {}
    
    # Section 1: Downtime_Periods
    writer.writerow(["=== DOWNTIME PERIODS ==="])
    writer.writerow(["Stream Name", "Start Time", "End Time", "Duration (seconds)", "Duration (formatted)", "Error Message"])
    daily_totals: Dict[Tuple[str, str], float] = {}
    for stream_name, periods in data.items():
        for p in periods:
            writer.writerow([
                stream_name,
                p.get("start", ""),
                p.get("end", ""),
                p.get("duration_seconds", 0),
                p.get("duration_formatted", ""),
                p.get("error_message") or "No error message",
            ])
            # Accumulate daily totals
            per_day = _split_duration_by_day(p.get("start", ""), p.get("end", ""), float(p.get("duration_seconds", 0) or 0))
            for date_key, seconds in per_day.items():
                key = (stream_name, date_key)
                daily_totals[key] = daily_totals.get(key, 0.0) + seconds
    
    # Section 2: Daily_Totals
    writer.writerow([])  # Empty row separator
    writer.writerow(["=== DAILY TOTALS ==="])
    writer.writerow(["Stream Name", "Date", "Total Downtime (seconds)", "Total Downtime (formatted)"])
    for (stream_name, date_key), seconds in sorted(daily_totals.items(), key=lambda x: (x[0][0], x[0][1])):
        writer.writerow([stream_name, date_key, int(seconds), format_duration(seconds)])
    
    return output.getvalue()

def export_zip_combined_csv() -> bytes:
    """Create a ZIP containing a single combined CSV file."""
    combined_csv = export_csv_combined()
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("uptime_report_combined.csv", combined_csv)
    return zip_buffer.getvalue()

## JSON export removed per request

# --- Streamlit App ---

# Initialize database
init_database()

st.title("📡 Online Stream Uptime Monitor")

st.write(f"⏰ Schedule: {', '.join(SCHEDULE['days'])} from {SCHEDULE['start_time']} to {SCHEDULE['end_time']} ({SCHEDULE['timezone']})")
within_schedule = is_within_schedule()

st.markdown(f"### Current Status: {'🟢 Monitoring' if within_schedule else '⚪ Outside Schedule'}")

# Manual refresh button and status
col1, col2 = st.columns([1, 3])
with col1:
    if st.button("🔄 Refresh Now", type="secondary"):
        st.rerun()
with col2:
    if not st_autorefresh:
        st.warning("⚠️ Auto-refresh active (page reload)")
        st.caption("Install 'streamlit-autorefresh' for smoother experience")

# Auto-refresh implementation
if st_autorefresh:
    st_autorefresh(interval=REFRESH_INTERVAL * 1000, key="uptime-autorefresh")
else:
    # Reliable browser-based meta refresh fallback
    st.markdown(f"""
    <meta http-equiv="refresh" content="{REFRESH_INTERVAL}">
    """, unsafe_allow_html=True)
    st.caption(f"⏰ Auto-refresh every {REFRESH_INTERVAL} seconds (browser reload)")

# Optional: show last checked timestamp (in schedule TZ)
tz = pytz.timezone(SCHEDULE["timezone"])
st.caption(f"Last checked: {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S %Z')}")

for stream in STREAMS:
    col1, col2 = st.columns([1, 4])
    with col1:
        st.markdown(f"**{stream['name']}**")
    with col2:
        if within_schedule:
            url = stream["url"]
            start_time = datetime.now()
            
            # Check website stream using icecast mount
            status = check_icecast_mount(url)
            if not status:
                # Fallback to regular URL check
                status = check_url_status(url)
            
            response_time = (datetime.now() - start_time).total_seconds()
            
            # Log the status
            log_uptime_status(
                stream["name"], 
                "online" if status else "offline", 
                response_time, 
                None if status else "Stream offline"
            )
            
            st.success("✅ Online" if status else "❌ Offline")
        else:
            st.info("Not checked (outside schedule)")

if "last_icecast_meta" in st.session_state:
    s = st.session_state["last_icecast_meta"]
    st.caption(f"Website mount: listeners={s.get('listeners')} bitrate={s.get('bitrate') or s.get('ice-bitrate')} type={s.get('server_type')}")

# Uptime (Today) using (1050 mins - total downtime mins) / 1050
st.markdown("---")
st.markdown("### 📊 Uptime (Today)")

def _calculate_uptime_today(stream_name: str, schedule_minutes: int = 1050) -> Tuple[float, float]:
    """Return (uptime_percentage, total_downtime_minutes) for today in schedule TZ.
    Uses today's downtime periods and applies: (schedule_minutes - total_downtime_minutes) / schedule_minutes.
    """
    downtime_data = get_downtime_periods() or {}
    periods = downtime_data.get(stream_name) or []
    today_periods = [p for p in periods if _interval_overlaps_today(p['start'], p['end'])]
    total_downtime_seconds = sum(p.get('duration_seconds', 0.0) for p in today_periods)
    total_downtime_minutes = max(0.0, total_downtime_seconds / 60.0)
    total_downtime_minutes = min(total_downtime_minutes, float(schedule_minutes))
    uptime_percentage = 0.0 if schedule_minutes <= 0 else max(0.0, (float(schedule_minutes) - total_downtime_minutes) / float(schedule_minutes) * 100.0)
    return uptime_percentage, total_downtime_minutes

uptime_pct, downtime_mins = _calculate_uptime_today('Website', 1050)
st.metric(
    label="Website Uptime (today)",
    value=f"{uptime_pct:.1f}%",
    delta=f"Downtime: {downtime_mins:.1f} min"
)

#

# Display downtime periods (Website only, recent first) - Today only
st.markdown("---")
st.markdown("### 🔴 Website Downtime Events (Today, Recent First)")
downtime_data = get_downtime_periods()
if downtime_data and 'Website' in downtime_data:
    periods = downtime_data.get('Website') or []
    if periods:
        # Keep only periods overlapping today
        today_periods = [p for p in periods if _interval_overlaps_today(p['start'], p['end'])]
        periods = today_periods
        # Calculate total/avg/longest
        total_downtime = sum(period['duration_seconds'] for period in periods)
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric(
                label="Total Downtime",
                value=format_duration(total_downtime),
                delta=f"{len(periods)} events"
            )
        with col2:
            avg_downtime = total_downtime / len(periods) if periods else 0
            st.metric(label="Average Downtime", value=format_duration(avg_downtime))
        with col3:
            longest_downtime = max(period['duration_seconds'] for period in periods) if periods else 0
            st.metric(label="Longest Downtime", value=format_duration(longest_downtime))

        # Sort recent first by end time (ongoing last refresh considered most recent)
        sorted_periods = sorted(
            periods,
            key=lambda p: (p['end'] if p['end'] != 'Ongoing' else '9999-12-31T23:59:59'),
            reverse=True,
        )
        for i, period in enumerate(sorted_periods, 1):
            status_icon = "🟡" if period['end'] == 'Ongoing' else "🔴"
            st.caption(
                f"{status_icon} **Event {i}**: {period['start']} → {period['end']} "
                f"({period['duration_formatted']}) - {period['error_message'] or 'No error message'}"
            )
    else:
        st.success("✅ Website: No downtime periods in the last 24 hours!")
else:
    st.info("No downtime data available yet. Downtime information will appear after monitoring begins.")

# Timeout Analysis (Today) - recent first, placed below downtime events
st.markdown("---")
st.markdown("### ⏱️ Timeout Analysis (Today)")
timeout_data = get_timeout_stats()
if timeout_data and timeout_data[1]:
    _, recent_timeouts = timeout_data
    st.markdown("#### 🔍 Recent Timeouts (most recent first)")
    for timeout in recent_timeouts:
        timestamp, stream_name, response_time, error_message = timeout
        st.caption(f"**{timestamp}** - {stream_name}: {response_time:.2f}s timeout - {error_message or 'No error message'}")
else:
    st.info("No timeout data available yet. Timeout information will appear after monitoring begins.")

# Export functionality (simplified)
st.markdown("---")
st.markdown("### 📊 Export Data")

col1, col2 = st.columns(2)

with col1:
    if st.button("📦 Export ZIP (CSV)"):
        zip_bytes = export_zip_combined_csv()
        st.download_button(
            label="📥 Download ZIP",
            data=zip_bytes,
            file_name=f"uptime_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip"
        )

with col2:
    if st.button("📄 Export CSV (Combined)"):
        csv_text = export_csv_combined()
        st.download_button(
            label="📥 Download CSV",
            data=csv_text,
            file_name=f"uptime_report_combined_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv"
        )
