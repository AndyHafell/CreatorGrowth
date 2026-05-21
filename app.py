"""
Idea Thumbnail Dashboard — YouTube homepage-style video grid.
Paste YouTube URLs, see them in a clean grid. Local only.
Uses yt-dlp for metadata (no API key needed).
Tabs: Options → Best → In Progress
"""

import re
import json
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request

import uuid
from werkzeug.utils import secure_filename

import os
import hashlib
import secrets
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=30)
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "contentmate2026")
DB_PATH = Path(__file__).resolve().parent / "videos.db"
UPLOAD_DIR = Path(__file__).resolve().parent / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
BLOTATO_API_KEY = os.environ.get("BLOTATO_API_KEY", "")
BLOTATO_X_ACCOUNT_ID = int(os.environ.get("BLOTATO_X_ACCOUNT_ID", "13469"))
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
# THUMB_QUEUE_USERS format: email1:secret1,email2:secret2 — maps a Bearer secret to one user identity
THUMB_QUEUE_USERS = {
    pair.split(":", 1)[0].strip().lower(): pair.split(":", 1)[1].strip()
    for pair in os.environ.get("THUMB_QUEUE_USERS", "").split(",")
    if ":" in pair
}
CONTENT_DIR = Path(os.environ.get("CONTENT_DIR", str(Path(__file__).resolve().parent.parent.parent / "content" / "content_docs")))
CONTENT_DIR.mkdir(parents=True, exist_ok=True)


def get_db():
    # 30s busy timeout — wait instead of erroring when another connection holds the lock
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    # WAL mode lets readers + writers proceed concurrently. PRAGMA is sticky on the db file
    # but we re-issue it cheaply per connection — it's a no-op once already enabled.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.OperationalError:
        pass
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT UNIQUE NOT NULL,
            title TEXT,
            channel_title TEXT,
            channel_thumb TEXT DEFAULT '',
            thumbnail_url TEXT,
            view_count INTEGER,
            duration TEXT,
            published_at TEXT,
            status TEXT DEFAULT 'options',
            outlier_score REAL DEFAULT 0,
            channel_avg_views INTEGER DEFAULT 0,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migration: add columns if upgrading from older schema
    for col, coltype, default in [
        ("channel_thumb", "TEXT", "''"),
        ("status", "TEXT", "'options'"),
        ("outlier_score", "REAL", "0"),
        ("channel_avg_views", "INTEGER", "0"),
        ("transformed", "INTEGER", "0"),
        ("tucked", "INTEGER", "0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {col} {coltype} DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    # Migrate old 'selected' column data if it exists
    try:
        conn.execute("UPDATE videos SET status = 'best' WHERE selected = 1 AND status = 'options'")
    except sqlite3.OperationalError:
        pass
    # Video details table for modal data (thumbnails, titles, custom fields)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER UNIQUE NOT NULL,
            inspo_thumbs TEXT DEFAULT '["","",""]',
            inspo_titles TEXT DEFAULT '["","",""]',
            original_thumbs TEXT DEFAULT '["","","","","","","","",""]',
            original_titles TEXT DEFAULT '["","","","","","","","",""]',
            custom_fields TEXT DEFAULT '[]',
            abc_choices TEXT DEFAULT '[]',
            FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
        )
    """)
    try:
        conn.execute("ALTER TABLE video_details ADD COLUMN abc_choices TEXT DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE video_details ADD COLUMN meta TEXT DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass
    # Pipeline rename (2026-05): in_progress → packaging, done → published.
    # Idempotent: no-op once values are already migrated.
    conn.execute("UPDATE videos SET status = 'packaging' WHERE status = 'in_progress'")
    conn.execute("UPDATE videos SET status = 'published' WHERE status = 'done'")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            name TEXT DEFAULT '',
            thumbnail TEXT DEFAULT '',
            subscriber_count INTEGER DEFAULT 0,
            avg_views INTEGER DEFAULT 0,
            video_count INTEGER DEFAULT 0,
            last_scraped TEXT DEFAULT '',
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migration: add new channel columns if upgrading
    for col, coltype, default in [
        ("thumbnail", "TEXT", "''"),
        ("subscriber_count", "INTEGER", "0"),
        ("avg_views", "INTEGER", "0"),
        ("video_count", "INTEGER", "0"),
        ("last_scraped", "TEXT", "''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE channels ADD COLUMN {col} {coltype} DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    # Keywords table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT UNIQUE NOT NULL,
            search_volume INTEGER DEFAULT 0,
            competition REAL DEFAULT 0,
            overall REAL DEFAULT 0,
            searches_30d INTEGER DEFAULT 0,
            word_count INTEGER DEFAULT 0,
            is_favorite INTEGER DEFAULT 0,
            is_youtube INTEGER DEFAULT 0,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tweets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            posted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            blotato_id TEXT DEFAULT '',
            media_url TEXT DEFAULT '',
            thread_json TEXT DEFAULT '[]'
        )
    """)
    for col, coltype, default in [
        ('media_url', 'TEXT', "''"),
        ('thread_json', 'TEXT', "'[]'"),
    ]:
        try:
            conn.execute(f'ALTER TABLE tweets ADD COLUMN {col} {coltype} DEFAULT {default}')
        except:
            pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS thumb_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            picked_up_at TEXT DEFAULT '',
            clicked_by_email TEXT DEFAULT '',
            FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
        )
    """)
    try:
        conn.execute("ALTER TABLE thumb_queue ADD COLUMN clicked_by_email TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE thumb_queue ADD COLUMN kind TEXT DEFAULT 'pixel_face'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE thumb_queue ADD COLUMN nudge TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS diagrams (
            id TEXT PRIMARY KEY,
            video_id INTEGER NOT NULL,
            name TEXT,
            image_path TEXT,
            audio_path TEXT,
            audio_name TEXT,
            audio_duration TEXT,
            boxes_json TEXT NOT NULL DEFAULT '[]',
            script TEXT DEFAULT '',
            result_url TEXT,
            result_meta_json TEXT,
            position INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_diagrams_video ON diagrams(video_id, position)")
    try:
        conn.execute("ALTER TABLE diagrams ADD COLUMN mode TEXT DEFAULT 'reveal'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE diagrams ADD COLUMN vision_keywords TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        # Auto-detected background color (#RRGGBB) for the current source image.
        # Recomputed every time the image is replaced; used as the canvas fill for
        # slideshow slides and padded letterbox bars.
        conn.execute("ALTER TABLE diagrams ADD COLUMN bg_color_auto TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        # User override (#RRGGBB) set via the eyedropper / hex input. Wins over auto.
        conn.execute("ALTER TABLE diagrams ADD COLUMN bg_color_override TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE videos ADD COLUMN editor_state TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE videos ADD COLUMN vocal_doc TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE videos ADD COLUMN voiceover_state TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE videos ADD COLUMN visuals_doc TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE videos ADD COLUMN visual_tags TEXT")
    except sqlite3.OperationalError:
        pass

    # Background TTS jobs — async synthesis with progress tracking
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tts_jobs (
            id TEXT PRIMARY KEY,
            video_id INTEGER,
            status TEXT,
            chunks_done INTEGER DEFAULT 0,
            chunks_total INTEGER DEFAULT 0,
            generation_json TEXT,
            error TEXT,
            detail TEXT,
            started_at REAL,
            updated_at REAL
        )
    """)

    # Chapter Studio: one list of chapter items per video, rendered to N MP4 clips.
    # One-shot migration: the cloned-from-diagrams schema had `boxes_json`. If that
    # column exists, drop the table and recreate with the new list-driven shape.
    existing_cols = [r[1] for r in conn.execute("PRAGMA table_info(chapters)").fetchall()]
    if "boxes_json" in existing_cols:
        conn.execute("DROP TABLE chapters")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chapters (
            video_id INTEGER PRIMARY KEY,
            items_json TEXT NOT NULL DEFAULT '[]',
            style TEXT NOT NULL DEFAULT 'blue_glass',
            numbering TEXT NOT NULL DEFAULT 'none',
            result_zip_path TEXT,
            last_rendered_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
        )
    """)
    # Migration: add `numbering` column to pre-existing tables
    try:
        conn.execute("ALTER TABLE chapters ADD COLUMN numbering TEXT NOT NULL DEFAULT 'none'")
    except sqlite3.OperationalError:
        pass
    # Migration: add manual layout overrides (auto-fit when 'auto')
    for col, default in [("box_size", "auto"), ("text_size", "auto"), ("wrap_mode", "off")]:
        try:
            conn.execute(f"ALTER TABLE chapters ADD COLUMN {col} TEXT NOT NULL DEFAULT '{default}'")
        except sqlite3.OperationalError:
            pass
    # Migration: card padding (T/R/B/L in px) + reveal style
    for col, default in [("pad_top", 60), ("pad_right", 80), ("pad_bottom", 60), ("pad_left", 80)]:
        try:
            conn.execute(f"ALTER TABLE chapters ADD COLUMN {col} INTEGER NOT NULL DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE chapters ADD COLUMN reveal_style TEXT NOT NULL DEFAULT 'blur_fade'")
    except sqlite3.OperationalError:
        pass

    # ── Andy's own channel — daily-refreshed mirror of YouTube data ──
    # Used by the brief generator to feed past-performance context into Gemini.
    # Refreshed by sync_my_channel.py (cron @ 6 AM daily).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS my_channel_videos (
            video_id TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            published_at TEXT,
            view_count INTEGER DEFAULT 0,
            like_count INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0,
            duration_seconds INTEGER DEFAULT 0,
            thumbnail_url TEXT,
            refreshed_at TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ── Auth ──────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect("/")
        return f(*args, **kwargs)
    return decorated


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    if not email or "@" not in email:
        return jsonify({"error": "Valid email required"}), 400
    if password != DASHBOARD_PASSWORD:
        return jsonify({"error": "Wrong password"}), 401
    # Store login
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        last_login TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    now = datetime.now(timezone.utc).isoformat()
    if existing:
        conn.execute("UPDATE users SET last_login = ? WHERE email = ?", (now, email))
    else:
        conn.execute("INSERT INTO users (email, last_login) VALUES (?, ?)", (email, now))
    conn.commit()
    conn.close()
    session["authenticated"] = True
    session["email"] = email
    session.permanent = True
    return jsonify({"ok": True})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth-status")
def auth_status():
    return jsonify({"authenticated": bool(session.get("authenticated")), "email": session.get("email", "")})


@app.before_request
def require_auth():
    open_paths = {"/", "/api/login", "/api/auth-status", "/static/", "/image-viewer", "/image-gallery", "/api/current-image", "/api/set-image", "/api/set-gallery"}
    path = request.path
    if path == "/" or path.startswith("/static/") or path in open_paths:
        return None
    # Bearer-authed endpoints handle their own auth in the route
    if path.startswith("/api/thumb-queue"):
        return None
    if not session.get("authenticated"):
        return jsonify({"error": "Unauthorized"}), 401


def extract_video_id(url: str) -> str | None:
    patterns = [
        r"(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/shorts/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    if re.match(r"^[a-zA-Z0-9_-]{11}$", url.strip()):
        return url.strip()
    return None


def format_duration(seconds: int) -> str:
    if not seconds:
        return ""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def time_ago(published_at: str) -> str:
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        diff = datetime.now(timezone.utc) - dt
        days = diff.days
        if days < 1:
            hours = diff.seconds // 3600
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        if days < 7:
            return f"{days} day{'s' if days != 1 else ''} ago"
        if days < 30:
            weeks = days // 7
            return f"{weeks} week{'s' if weeks != 1 else ''} ago"
        if days < 365:
            months = days // 30
            return f"{months} month{'s' if months != 1 else ''} ago"
        years = days // 365
        return f"{years} year{'s' if years != 1 else ''} ago"
    except Exception:
        return ""


def format_views(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M views"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k views" if count >= 10_000 else f"{count / 1_000:.1f}k views"
    return f"{count} views"


def row_to_dict(r):
    outlier = r["outlier_score"] if r["outlier_score"] else 0
    return {
        "id": r["id"],
        "video_id": r["video_id"],
        "title": r["title"],
        "channel_title": r["channel_title"],
        "channel_thumb": r["channel_thumb"] or "",
        "thumbnail_url": r["thumbnail_url"],
        "view_count": r["view_count"],
        "view_count_formatted": format_views(r["view_count"]),
        "duration": r["duration"],
        "published_at": r["published_at"],
        "time_ago": time_ago(r["published_at"]),
        "status": r["status"] or "options",
        "outlier_score": outlier,
        "transformed": bool(r["transformed"]) if "transformed" in r.keys() else False,
        "tucked": bool(r["tucked"]) if "tucked" in r.keys() else False,
    }


def fetch_channel_avg_views(channel_url: str) -> int:
    """Fetch the last 30 videos from a channel and return their average view count."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--dump-json", "--no-download",
             "--no-warnings", "--playlist-end", "30", f"{channel_url}/videos"],
            capture_output=True, text=True, timeout=45,
        )
        if result.returncode != 0:
            return 0
        views = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                entry = json.loads(line)
                vc = entry.get("view_count")
                if vc and vc > 0:
                    views.append(vc)
            except json.JSONDecodeError:
                continue
        return int(sum(views) / len(views)) if views else 0
    except subprocess.TimeoutExpired:
        return 0


def _iso8601_duration_to_seconds(s: str) -> int:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + sec


def fetch_video_metadata_api(video_id: str) -> dict | None:
    """Fetch video metadata via YouTube Data API v3. Returns None on any failure."""
    if not YOUTUBE_API_KEY:
        return None
    try:
        api_url = (
            "https://www.googleapis.com/youtube/v3/videos"
            f"?part=snippet,statistics,contentDetails&id={video_id}&key={YOUTUBE_API_KEY}"
        )
        with urlopen(api_url, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    items = payload.get("items") or []
    if not items:
        return None
    item = items[0]
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    details = item.get("contentDetails", {})

    thumbs = snippet.get("thumbnails", {})
    thumbnail_url = (
        thumbs.get("maxres", {}).get("url")
        or thumbs.get("standard", {}).get("url")
        or thumbs.get("high", {}).get("url")
        or thumbs.get("medium", {}).get("url")
        or thumbs.get("default", {}).get("url", "")
    )

    channel_id = snippet.get("channelId", "")
    channel_thumb = ""
    if channel_id:
        try:
            ch_url = (
                "https://www.googleapis.com/youtube/v3/channels"
                f"?part=snippet&id={channel_id}&key={YOUTUBE_API_KEY}"
            )
            with urlopen(ch_url, timeout=10) as resp:
                ch_payload = json.loads(resp.read().decode("utf-8"))
            ch_items = ch_payload.get("items") or []
            if ch_items:
                ch_thumbs = ch_items[0].get("snippet", {}).get("thumbnails", {})
                channel_thumb = (
                    ch_thumbs.get("high", {}).get("url")
                    or ch_thumbs.get("medium", {}).get("url")
                    or ch_thumbs.get("default", {}).get("url", "")
                )
        except Exception:
            pass

    view_count = int(stats.get("viewCount", 0) or 0)

    channel_url = f"https://www.youtube.com/channel/{channel_id}" if channel_id else ""
    channel_avg_views = 0
    outlier_score = 0.0
    if channel_url:
        channel_avg_views = fetch_channel_avg_views(channel_url)
        if channel_avg_views > 0:
            outlier_score = round(view_count / channel_avg_views, 1)

    return {
        "video_id": video_id,
        "title": snippet.get("title", ""),
        "channel_title": snippet.get("channelTitle", ""),
        "channel_thumb": channel_thumb,
        "thumbnail_url": thumbnail_url or "",
        "view_count": view_count,
        "duration": format_duration(_iso8601_duration_to_seconds(details.get("duration", ""))),
        "published_at": snippet.get("publishedAt", ""),
        "outlier_score": outlier_score,
        "channel_avg_views": channel_avg_views,
    }


def fetch_video_metadata(video_id: str) -> dict | None:
    """Fetch video metadata. Prefer YouTube Data API (no bot challenge); fall back to yt-dlp."""
    meta = fetch_video_metadata_api(video_id)
    if meta:
        return meta

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-download", "--no-warnings", url],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None

    upload_date = data.get("upload_date", "")
    published_at = ""
    if upload_date and len(upload_date) == 8:
        published_at = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00Z"

    thumbnail_url = data.get("thumbnail", "")
    for t in data.get("thumbnails", []):
        if "maxresdefault" in t.get("url", ""):
            thumbnail_url = t["url"]
            break

    # Fetch channel avatar from channel page og:image
    channel_thumb = ""
    avatar_url = data.get("uploader_url") or data.get("channel_url", "")
    if avatar_url:
        try:
            result = subprocess.run(
                ["curl", "-s", "-L", "-H",
                 "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                 avatar_url],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                og_match = re.search(r'<meta property="og:image" content="([^"]+)"', result.stdout)
                if og_match:
                    channel_thumb = og_match.group(1)
        except subprocess.TimeoutExpired:
            pass

    view_count = int(data.get("view_count", 0))

    # Calculate outlier score from channel average
    channel_url = data.get("channel_url", "")
    channel_avg_views = 0
    outlier_score = 0.0
    if channel_url:
        channel_avg_views = fetch_channel_avg_views(channel_url)
        if channel_avg_views > 0:
            outlier_score = round(view_count / channel_avg_views, 1)

    return {
        "video_id": video_id,
        "title": data.get("title", ""),
        "channel_title": data.get("channel", data.get("uploader", "")),
        "channel_thumb": channel_thumb,
        "thumbnail_url": thumbnail_url,
        "view_count": view_count,
        "duration": format_duration(int(data.get("duration", 0))),
        "published_at": published_at,
        "outlier_score": outlier_score,
        "channel_avg_views": channel_avg_views,
    }


@app.route("/")
def index():
    return render_template("index.html", authenticated=session.get("authenticated", False))


@app.route("/api/videos", methods=["GET"])
def list_videos():
    conn = get_db()
    rows = conn.execute("SELECT * FROM videos ORDER BY added_at DESC").fetchall()
    conn.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/videos", methods=["POST"])
def add_video():
    data = request.get_json(force=True)
    url_input = data.get("url", "").strip()
    if not url_input:
        return jsonify({"error": "No URL provided"}), 400

    video_id = extract_video_id(url_input)
    if not video_id:
        return jsonify({"error": "Could not extract video ID from URL"}), 400

    conn = get_db()
    if conn.execute("SELECT id FROM videos WHERE video_id = ?", (video_id,)).fetchone():
        conn.close()
        return jsonify({"error": "Video already added"}), 409

    meta = fetch_video_metadata(video_id)
    if not meta:
        conn.close()
        return jsonify({"error": "Video not found on YouTube"}), 404

    conn.execute(
        """INSERT INTO videos (video_id, title, channel_title, channel_thumb, thumbnail_url, view_count, duration, published_at, status, outlier_score, channel_avg_views)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'options', ?, ?)""",
        (meta["video_id"], meta["title"], meta["channel_title"], meta["channel_thumb"],
         meta["thumbnail_url"], meta["view_count"], meta["duration"], meta["published_at"],
         meta["outlier_score"], meta["channel_avg_views"]),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    conn.close()
    return jsonify(row_to_dict(row)), 201


@app.route("/api/videos/<int:vid>/status", methods=["POST"])
def update_status(vid):
    data = request.get_json(force=True)
    new_status = data.get("status", "options")
    if new_status not in ("options", "best", "brief", "packaging", "script", "edited", "archived", "published"):
        return jsonify({"error": "Invalid status"}), 400
    conn = get_db()
    conn.execute("UPDATE videos SET status = ? WHERE id = ?", (new_status, vid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "status": new_status})


@app.route("/api/videos/<int:vid>/tuck", methods=["POST"])
def toggle_tuck(vid):
    """Toggle the tucked flag — hides card behind a '+ N tucked' row in its tab."""
    conn = get_db()
    row = conn.execute("SELECT tucked FROM videos WHERE id = ?", (vid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    new_val = 0 if row["tucked"] else 1
    conn.execute("UPDATE videos SET tucked = ? WHERE id = ?", (new_val, vid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "tucked": bool(new_val)})


@app.route("/api/videos/<int:vid>/thumbnail", methods=["POST"])
def update_thumbnail(vid):
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    title = (data.get("title") or "").strip()
    if not url:
        return jsonify({"error": "Missing url"}), 400
    conn = get_db()
    if title:
        conn.execute("UPDATE videos SET thumbnail_url = ?, title = ? WHERE id = ?", (url, title, vid))
    else:
        conn.execute("UPDATE videos SET thumbnail_url = ? WHERE id = ?", (url, vid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "thumbnail_url": url, "title": title or None})


@app.route("/api/videos/<int:vid>/transform", methods=["POST"])
@login_required
def transform_video(vid):
    """Toggle transform: swap creator photo/name with AI Andy."""
    conn = get_db()
    row = conn.execute("SELECT transformed FROM videos WHERE id = ?", (vid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    new_val = 0 if row["transformed"] else 1
    conn.execute("UPDATE videos SET transformed = ? WHERE id = ?", (new_val, vid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "transformed": bool(new_val)})


@app.route("/api/videos/<int:vid>/queue-thumb", methods=["POST"])
@login_required
def queue_thumb(vid):
    """Queue a thumbnail-generation task. Stamps the caller's email so only their poller picks it up."""
    email = (session.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "No session email"}), 400
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or "pixel_face").strip().lower()
    if kind not in ("pixel_face", "faceless"):
        kind = "pixel_face"
    nudge = (body.get("nudge") or "").strip()
    conn = get_db()
    row = conn.execute("SELECT title FROM videos WHERE id = ?", (vid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    title = row["title"] or ""
    if not title:
        conn.close()
        return jsonify({"error": "Video has no title"}), 400
    existing = conn.execute(
        "SELECT id FROM thumb_queue WHERE video_id = ? AND clicked_by_email = ? AND kind = ? AND status = 'queued'",
        (vid, email, kind),
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"ok": True, "queued": True, "queue_id": existing["id"], "kind": kind, "already": True})
    cur = conn.execute(
        "INSERT INTO thumb_queue (video_id, title, clicked_by_email, kind, nudge) VALUES (?, ?, ?, ?, ?)",
        (vid, title, email, kind, nudge),
    )
    conn.commit()
    qid = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "queued": True, "queue_id": qid, "kind": kind})


def _bearer_user_email():
    """Return the email mapped to the Bearer secret, or None if unknown."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    secret = header[7:]
    for email, expected in THUMB_QUEUE_USERS.items():
        if secret == expected:
            return email
    return None


@app.route("/api/thumb-queue", methods=["GET"])
def thumb_queue_list():
    """Mac poller: list this user's pending queued tasks."""
    user_email = _bearer_user_email()
    if not user_email:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    rows = conn.execute(
        "SELECT id, video_id, title, kind, nudge, created_at FROM thumb_queue "
        "WHERE status = 'queued' AND clicked_by_email = ? ORDER BY id ASC",
        (user_email,),
    ).fetchall()
    conn.close()
    return jsonify([
        {
            "id": r["id"],
            "video_id": r["video_id"],
            "title": r["title"],
            "kind": r["kind"] or "pixel_face",
            "nudge": r["nudge"] or "",
            "created_at": r["created_at"],
        }
        for r in rows
    ])


@app.route("/api/thumb-queue/<int:qid>/done", methods=["POST"])
def thumb_queue_done(qid):
    """Mac poller: mark this user's task as done. Cannot mark another user's row."""
    user_email = _bearer_user_email()
    if not user_email:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    conn.execute(
        "UPDATE thumb_queue SET status = 'done', picked_up_at = CURRENT_TIMESTAMP "
        "WHERE id = ? AND clicked_by_email = ?",
        (qid, user_email),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/videos/<int:vid>", methods=["DELETE"])
def delete_video(vid):
    conn = get_db()
    conn.execute("DELETE FROM videos WHERE id = ?", (vid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/videos/clear", methods=["POST"])
def clear_videos():
    conn = get_db()
    conn.execute("DELETE FROM videos")
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Channel routes ────────────────────────────────────────────

def fetch_rss_dates(channel_id: str) -> dict[str, str]:
    """Fetch publish dates from YouTube RSS feed. Returns {video_id: published_at_iso}."""
    if not channel_id:
        return {}
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return {}
        dates = {}
        # Parse XML with regex (lightweight, no extra deps)
        for match in re.finditer(
            r"<yt:videoId>([^<]+)</yt:videoId>.*?<published>([^<]+)</published>",
            result.stdout, re.DOTALL,
        ):
            vid, pub = match.group(1), match.group(2)
            # Convert RSS date to our format: 2026-03-04T11:32:05+00:00 → ISO
            dates[vid] = pub.replace("+00:00", "Z").replace("+0000", "Z")
        return dates
    except subprocess.TimeoutExpired:
        return {}


def scrape_channel_videos(channel_url: str, limit: int = 20) -> list[dict]:
    """Scrape top N videos from a channel using yt-dlp flat-playlist (fast)."""
    url = channel_url.rstrip("/") + "/videos"
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--dump-json", "--no-download",
             "--no-warnings", "--playlist-end", str(limit), url],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return []
    except subprocess.TimeoutExpired:
        return []

    entries = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Get channel_id from first entry for RSS date lookup
    channel_id = ""
    for e in entries:
        channel_id = e.get("playlist_channel_id") or e.get("channel_id") or ""
        if channel_id:
            break
    rss_dates = fetch_rss_dates(channel_id)

    # Calculate channel avg from scraped videos for outlier score
    view_counts = [int(e.get("view_count") or 0) for e in entries if e.get("view_count")]
    channel_avg = int(sum(view_counts) / len(view_counts)) if view_counts else 0

    videos = []
    for entry in entries:
        video_id = entry.get("id")
        if not video_id:
            continue
        # Try yt-dlp upload_date first, then RSS feed date
        upload_date = entry.get("upload_date", "")
        published_at = ""
        if upload_date and len(upload_date) == 8:
            published_at = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00Z"
        if not published_at and video_id in rss_dates:
            published_at = rss_dates[video_id]
        thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        for t in entry.get("thumbnails", []):
            if "maxresdefault" in t.get("url", ""):
                thumbnail_url = t["url"]
                break
        view_count = int(entry.get("view_count") or 0)
        outlier = round(view_count / channel_avg, 1) if channel_avg > 0 else 0.0
        videos.append({
            "video_id": video_id,
            "title": entry.get("title", ""),
            "channel_title": entry.get("channel", entry.get("uploader", entry.get("playlist_channel", ""))),
            "channel_thumb": "",
            "thumbnail_url": thumbnail_url,
            "view_count": view_count,
            "duration": format_duration(int(entry.get("duration") or 0)),
            "published_at": published_at,
            "outlier_score": outlier,
            "channel_avg_views": channel_avg,
        })
    return videos


@app.route("/api/channels", methods=["GET"])
def list_channels():
    conn = get_db()
    rows = conn.execute("SELECT * FROM channels ORDER BY added_at DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/channels", methods=["POST"])
def add_channel():
    data = request.get_json(force=True)
    url = data.get("url", "").strip().rstrip("/")
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    # Support bare @handle input
    if url.startswith("@"):
        url = f"https://www.youtube.com/{url}"
    elif not url.startswith("http"):
        url = f"https://www.youtube.com/@{url}"
    # Normalize: strip trailing /videos etc
    url = re.sub(r"/(videos|shorts|streams|playlists)$", "", url)
    conn = get_db()
    try:
        conn.execute("INSERT INTO channels (url, name) VALUES (?, ?)", (url, ""))
        conn.commit()
        row = conn.execute("SELECT * FROM channels WHERE url = ?", (url,)).fetchone()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Channel already added"}), 409
    result = dict(row)
    conn.close()
    return jsonify(result), 201


@app.route("/api/channels/<int:cid>", methods=["DELETE"])
def delete_channel(cid):
    conn = get_db()
    conn.execute("DELETE FROM channels WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


def _backfill_dates_for_channel(conn, channel_url: str):
    """Backfill missing published_at dates for videos from this channel using RSS."""
    # Get channel_id by looking up a video from this channel
    try:
        result = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--dump-json", "--no-download",
             "--no-warnings", "--playlist-end", "1", channel_url.rstrip("/") + "/videos"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return 0
        entry = json.loads(result.stdout.strip().split("\n")[0])
        channel_id = entry.get("playlist_channel_id") or entry.get("channel_id") or ""
    except Exception:
        return 0
    if not channel_id:
        return 0
    rss_dates = fetch_rss_dates(channel_id)
    if not rss_dates:
        return 0
    updated = 0
    for vid, pub_date in rss_dates.items():
        cur = conn.execute(
            "UPDATE videos SET published_at = ? WHERE video_id = ? AND (published_at = '' OR published_at IS NULL)",
            (pub_date, vid),
        )
        updated += cur.rowcount
    if updated:
        conn.commit()
    return updated


def _scrape_channel_into_db(conn, channel_row) -> dict:
    """Scrape a channel and insert videos into DB. Returns result dict."""
    channel_url = channel_row["url"]
    cid = channel_row["id"]
    videos = scrape_channel_videos(channel_url, limit=20)
    inserted = skipped = 0
    channel_name = ""
    channel_avg = 0
    for v in videos:
        if not channel_name and v.get("channel_title"):
            channel_name = v["channel_title"]
        if not channel_avg and v.get("channel_avg_views"):
            channel_avg = v["channel_avg_views"]
        try:
            conn.execute(
                """INSERT INTO videos (video_id, title, channel_title, channel_thumb, thumbnail_url,
                       view_count, duration, published_at, status, outlier_score, channel_avg_views)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'options', ?, ?)""",
                (v["video_id"], v["title"], v["channel_title"], v["channel_thumb"],
                 v["thumbnail_url"], v["view_count"], v["duration"], v["published_at"],
                 v["outlier_score"], v["channel_avg_views"]),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            # Update view count, outlier score, and missing fields for existing videos
            if v["view_count"] and v["view_count"] > 0:
                conn.execute(
                    """UPDATE videos SET view_count = ?, outlier_score = ?, channel_avg_views = ?
                       WHERE video_id = ? AND (view_count IS NULL OR view_count = 0 OR view_count < ?)""",
                    (v["view_count"], v["outlier_score"], v["channel_avg_views"],
                     v["video_id"], v["view_count"]),
                )
            skipped += 1
    now = datetime.now(timezone.utc).isoformat()
    updates = {"last_scraped": now, "video_count": len(videos), "avg_views": channel_avg}
    if channel_name:
        updates["name"] = channel_name
    # Fetch channel thumbnail and name from channel page if missing
    if (not channel_row["thumbnail"] or not channel_name) and channel_url:
        try:
            result = subprocess.run(
                ["curl", "-s", "-L", "-H",
                 "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                 channel_url],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                html = result.stdout
                if not channel_row["thumbnail"]:
                    og_match = re.search(r'<meta property="og:image" content="([^"]+)"', html)
                    if og_match:
                        updates["thumbnail"] = og_match.group(1)
                if not channel_name:
                    # Try og:title first, then <title>
                    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html)
                    if title_match:
                        updates["name"] = title_match.group(1)
                    else:
                        title_match = re.search(r'<title>([^<]+)</title>', html)
                        if title_match:
                            name = title_match.group(1).replace(" - YouTube", "").strip()
                            if name:
                                updates["name"] = name
        except subprocess.TimeoutExpired:
            pass
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE channels SET {set_clause} WHERE id = ?", (*updates.values(), cid))
    # Propagate channel thumb/name to videos that are missing them
    ch_thumb = updates.get("thumbnail") or channel_row["thumbnail"] or ""
    ch_name = updates.get("name") or channel_name or channel_row["name"] or ""
    if ch_thumb:
        video_ids = [v["video_id"] for v in videos]
        if video_ids:
            placeholders = ",".join("?" * len(video_ids))
            conn.execute(
                f"UPDATE videos SET channel_thumb = ? WHERE video_id IN ({placeholders}) AND (channel_thumb = '' OR channel_thumb IS NULL)",
                (ch_thumb, *video_ids),
            )
            if ch_name:
                conn.execute(
                    f"UPDATE videos SET channel_title = ? WHERE video_id IN ({placeholders}) AND (channel_title = '' OR channel_title IS NULL)",
                    (ch_name, *video_ids),
                )
    conn.commit()
    return {"inserted": inserted, "skipped": skipped, "name": channel_name, "channel_id": cid}


@app.route("/api/channels/<int:cid>/scrape", methods=["POST"])
def scrape_channel(cid):
    conn = get_db()
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Channel not found"}), 404
    result = _scrape_channel_into_db(conn, row)
    # Backfill dates for videos still missing them
    _backfill_dates_for_channel(conn, row["url"])
    _deep_backfill_dates(conn, limit=20)
    # Re-fetch updated channel
    updated = conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    conn.close()
    return jsonify({**result, "channel": dict(updated) if updated else None})


@app.route("/api/channels/scrape-all", methods=["POST"])
def scrape_all_channels():
    conn = get_db()
    rows = conn.execute("SELECT * FROM channels ORDER BY added_at ASC").fetchall()
    if not rows:
        conn.close()
        return jsonify({"error": "No channels to scrape"}), 400
    total_inserted = total_skipped = 0
    for row in rows:
        r = _scrape_channel_into_db(conn, row)
        total_inserted += r["inserted"]
        total_skipped += r["skipped"]
    # Backfill dates: RSS first, then deep (individual yt-dlp lookups)
    total_backfilled = 0
    for row in rows:
        total_backfilled += _backfill_dates_for_channel(conn, row["url"])
    deep_updated, _ = _deep_backfill_dates(conn, limit=50)
    total_backfilled += deep_updated
    # Return updated channel list
    updated_channels = conn.execute("SELECT * FROM channels ORDER BY added_at DESC").fetchall()
    conn.close()
    return jsonify({
        "inserted": total_inserted,
        "skipped": total_skipped,
        "backfilled_dates": total_backfilled,
        "channels": [dict(c) for c in updated_channels],
    })


@app.route("/api/videos/backfill-dates", methods=["POST"])
def backfill_dates():
    """Backfill missing dates for all channels using YouTube RSS feeds."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM channels ORDER BY added_at ASC").fetchall()
    total = 0
    for row in rows:
        total += _backfill_dates_for_channel(conn, row["url"])
    conn.close()
    return jsonify({"backfilled": total})


def _deep_backfill_dates(conn, limit: int = 20) -> tuple[int, int]:
    """Deep backfill missing dates using yt-dlp individual lookups. Returns (updated, remaining)."""
    missing = conn.execute(
        "SELECT id, video_id FROM videos WHERE published_at = '' OR published_at IS NULL LIMIT ?",
        (limit,),
    ).fetchall()
    if not missing:
        return 0, 0
    updated = 0
    for row in missing:
        vid = row["video_id"]
        try:
            result = subprocess.run(
                ["yt-dlp", "--dump-json", "--no-download", "--no-warnings",
                 f"https://www.youtube.com/watch?v={vid}"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                continue
            data = json.loads(result.stdout)
            upload_date = data.get("upload_date", "")
            if upload_date and len(upload_date) == 8:
                published_at = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00Z"
                conn.execute("UPDATE videos SET published_at = ? WHERE id = ?", (published_at, row["id"]))
                updated += 1
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            continue
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM videos WHERE published_at = '' OR published_at IS NULL"
    ).fetchone()[0]
    return updated, remaining


@app.route("/api/videos/backfill-dates-deep", methods=["POST"])
def backfill_dates_deep():
    """Backfill missing dates using yt-dlp individual video lookups (slow but thorough).
    Processes videos in batches. Pass ?limit=N to control batch size (default 20)."""
    limit = request.args.get("limit", 20, type=int)
    conn = get_db()
    updated, remaining = _deep_backfill_dates(conn, limit)
    conn.close()
    return jsonify({"backfilled": updated, "remaining": remaining})


@app.route("/api/videos/backfill-views", methods=["POST"])
def backfill_views():
    """Backfill missing/zero view counts via YouTube Data API v3 (batched, 50 IDs/call).
    Falls back to per-video yt-dlp if no API key is configured."""
    limit = request.args.get("limit", 50, type=int)
    conn = get_db()
    missing = conn.execute(
        "SELECT id, video_id FROM videos WHERE view_count IS NULL OR view_count = 0 LIMIT ?",
        (limit,),
    ).fetchall()
    if not missing:
        conn.close()
        return jsonify({"backfilled": 0, "remaining": 0})

    updated = 0
    channel_avg_cache: dict[str, int] = {}

    if YOUTUBE_API_KEY:
        ids = [r["video_id"] for r in missing]
        id_to_row = {r["video_id"]: r["id"] for r in missing}
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            try:
                api_url = (
                    "https://www.googleapis.com/youtube/v3/videos"
                    f"?part=snippet,statistics&id={','.join(chunk)}&key={YOUTUBE_API_KEY}"
                )
                with urlopen(api_url, timeout=15) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except Exception:
                continue
            for item in payload.get("items", []):
                vid = item.get("id")
                stats = item.get("statistics", {})
                view_count = int(stats.get("viewCount", 0) or 0)
                if not vid or view_count <= 0 or vid not in id_to_row:
                    continue
                channel_id = item.get("snippet", {}).get("channelId", "")
                if channel_id and channel_id not in channel_avg_cache:
                    channel_url = f"https://www.youtube.com/channel/{channel_id}"
                    channel_avg_cache[channel_id] = fetch_channel_avg_views(channel_url)
                channel_avg = channel_avg_cache.get(channel_id, 0)
                outlier = round(view_count / channel_avg, 1) if channel_avg > 0 else 0.0
                conn.execute(
                    "UPDATE videos SET view_count = ?, outlier_score = ?, channel_avg_views = ? WHERE id = ?",
                    (view_count, outlier, channel_avg, id_to_row[vid]),
                )
                updated += 1
    else:
        for row in missing:
            vid = row["video_id"]
            try:
                result = subprocess.run(
                    ["yt-dlp", "--dump-json", "--no-download", "--no-warnings",
                     f"https://www.youtube.com/watch?v={vid}"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode != 0:
                    continue
                data = json.loads(result.stdout)
                view_count = int(data.get("view_count", 0))
                if view_count > 0:
                    channel_url = data.get("channel_url", "")
                    channel_avg = fetch_channel_avg_views(channel_url) if channel_url else 0
                    outlier = round(view_count / channel_avg, 1) if channel_avg > 0 else 0.0
                    conn.execute(
                        "UPDATE videos SET view_count = ?, outlier_score = ?, channel_avg_views = ? WHERE id = ?",
                        (view_count, outlier, channel_avg, row["id"]),
                    )
                    updated += 1
            except (subprocess.TimeoutExpired, json.JSONDecodeError):
                continue

    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM videos WHERE view_count IS NULL OR view_count = 0"
    ).fetchone()[0]
    conn.close()
    return jsonify({"backfilled": updated, "remaining": remaining})


# ── Video Details (modal data) ────────────────────────────

@app.route("/api/videos/<int:vid>/details", methods=["GET"])
def get_video_details(vid):
    conn = get_db()
    row = conn.execute("SELECT * FROM video_details WHERE video_id = ?", (vid,)).fetchone()
    if not row:
        # Auto-create with defaults, pre-fill first inspo thumb from video's scraped thumbnail
        video = conn.execute("SELECT thumbnail_url, title FROM videos WHERE id = ?", (vid,)).fetchone()
        scraped_thumb = video["thumbnail_url"] if video else ""
        scraped_title = video["title"] if video else ""
        default_inspo = json.dumps([scraped_thumb, "", ""])
        default_inspo_titles = json.dumps([scraped_title, "", ""])
        default_empty3 = json.dumps(["", "", ""])
        default_empty9 = json.dumps(["", "", "", "", "", "", "", "", ""])
        default_fields = json.dumps([{"key": "Content Doc", "value": ""}])
        conn.execute(
            """INSERT INTO video_details (video_id, inspo_thumbs, inspo_titles, original_thumbs, original_titles, custom_fields)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (vid, default_inspo, default_inspo_titles, default_empty9, default_empty9, default_fields),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM video_details WHERE video_id = ?", (vid,)).fetchone()
    conn.close()
    def _pad(arr, n):
        return (arr + [""] * n)[:n]
    def _pad_multiple(arr, step):
        # Pad to nearest multiple of `step`, minimum `step`.
        target = max(step, ((len(arr) + step - 1) // step) * step)
        return arr + [""] * (target - len(arr))
    try:
        abc_choices = json.loads(row["abc_choices"]) if row["abc_choices"] else []
    except (TypeError, ValueError, IndexError):
        abc_choices = []
    try:
        meta = json.loads(row["meta"]) if "meta" in row.keys() and row["meta"] else {}
    except (TypeError, ValueError, IndexError):
        meta = {}
    return jsonify({
        "video_id": row["video_id"],
        "inspo_thumbs": _pad(json.loads(row["inspo_thumbs"]), 3),
        "inspo_titles": _pad(json.loads(row["inspo_titles"]), 3),
        "original_thumbs": _pad_multiple(json.loads(row["original_thumbs"]), 9),
        "original_titles": _pad_multiple(json.loads(row["original_titles"]), 9),
        "custom_fields": json.loads(row["custom_fields"]),
        "abc_choices": abc_choices,
        "meta": meta,
    })


@app.route("/api/videos/<int:vid>/abc-choices", methods=["POST"])
def update_abc_choices(vid):
    data = request.get_json(force=True)
    raw = data.get("choices") or []
    if not isinstance(raw, list):
        return jsonify({"error": "choices must be a list"}), 400
    cleaned = []
    for url in raw:
        if isinstance(url, str) and url.strip() and url not in cleaned:
            cleaned.append(url)
    payload = json.dumps(cleaned)
    conn = get_db()
    existing = conn.execute("SELECT id FROM video_details WHERE video_id = ?", (vid,)).fetchone()
    if existing:
        conn.execute("UPDATE video_details SET abc_choices = ? WHERE video_id = ?", (payload, vid))
    else:
        conn.execute(
            "INSERT INTO video_details (video_id, abc_choices) VALUES (?, ?)",
            (vid, payload),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "abc_choices": cleaned})


@app.route("/api/videos/<int:vid>/details", methods=["POST"])
def save_video_details(vid):
    data = request.get_json(force=True)
    conn = get_db()
    # Upsert
    existing = conn.execute("SELECT id FROM video_details WHERE video_id = ?", (vid,)).fetchone()
    values = {
        "inspo_thumbs": json.dumps(data.get("inspo_thumbs", ["", "", ""])),
        "inspo_titles": json.dumps(data.get("inspo_titles", ["", "", ""])),
        "original_thumbs": json.dumps(data.get("original_thumbs", ["", "", "", "", "", "", "", "", ""])),
        "original_titles": json.dumps(data.get("original_titles", ["", "", "", "", "", "", "", "", ""])),
        "custom_fields": json.dumps(data.get("custom_fields", [])),
    }
    if existing:
        set_clause = ", ".join(f"{k} = ?" for k in values)
        conn.execute(f"UPDATE video_details SET {set_clause} WHERE video_id = ?", (*values.values(), vid))
    else:
        conn.execute(
            """INSERT INTO video_details (video_id, inspo_thumbs, inspo_titles, original_thumbs, original_titles, custom_fields)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (vid, *values.values()),
        )
    # Allow editing the card title from the modal header
    new_title = (data.get("title") or "").strip()
    if new_title:
        conn.execute("UPDATE videos SET title = ? WHERE id = ?", (new_title, vid))

    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Content Doc Creation ──────────────────────────────────

def _fetch_transcript_hook(video_id, max_seconds=150):
    """Fetch verbatim transcript text from the first ~max_seconds of a YouTube video.
    Tries youtube-transcript-api first (works locally), falls back to yt-dlp."""
    # Method 1: youtube-transcript-api (works well locally, may fail on VPS)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        ytt = YouTubeTranscriptApi()
        transcript = ytt.fetch(video_id)
        lines = []
        for entry in transcript.snippets:
            if entry.start > max_seconds:
                break
            text = entry.text.strip()
            if not text or text.startswith("["):
                continue
            lines.append(text)
        result = " ".join(lines)
        if result:
            return result
    except Exception:
        pass

    # Method 2: yt-dlp (fallback)
    import tempfile
    tmp_dir = tempfile.mkdtemp()
    out_path = os.path.join(tmp_dir, "subs")
    try:
        subprocess.run(
            ["yt-dlp", "--write-auto-sub", "--sub-lang", "en", "--sub-format", "json3",
             "--skip-download", "--js-runtimes", "node,deno", "-o", out_path,
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, timeout=30,
        )
        sub_file = out_path + ".en.json3"
        if not os.path.exists(sub_file):
            return ""
        with open(sub_file) as f:
            data = json.load(f)
        lines = []
        max_ms = max_seconds * 1000
        for event in data.get("events", []):
            if event.get("tStartMs", 0) > max_ms:
                break
            for seg in event.get("segs", []):
                text = seg.get("utf8", "").strip()
                if text and text != "\n" and not text.startswith("["):
                    lines.append(text)
        return " ".join(lines)
    except Exception:
        return ""
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _format_views(count):
    """Format view count like '1.2M' or '450K'."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    elif count >= 1_000:
        return f"{count / 1_000:.0f}K"
    return str(count)


def _readable_name(stem):
    """Strip date prefix and convert dashes/underscores to readable title."""
    name = re.sub(r"^\d{4}-\d{2}-\d{2}[-_]?", "", stem)
    name = name.replace("-", " ").replace("_", " ")
    return name.strip().title() or stem


@app.route("/api/videos/create-blank", methods=["POST"])
def create_blank_video():
    """Create a blank video card for original content (not scraped from YouTube)."""
    import uuid
    data = request.get_json(force=True)
    title = data.get("title", "Untitled Video")
    video_id = "custom_" + uuid.uuid4().hex[:8]
    channel_title = "AI Andy"
    channel_thumb = "https://yt3.googleusercontent.com/IbufHQrLvYW5xUpl4Pv6kcYApZGrydwFA5udKmDAmOPsSKqWfS-SVdUMHsaKaxKAzAj0u34zjw=s900-c-k-c0x00ffffff-no-rj"
    conn = get_db()
    conn.execute(
        """INSERT INTO videos (video_id, title, channel_title, channel_thumb, thumbnail_url,
               view_count, duration, published_at, status, outlier_score, channel_avg_views)
           VALUES (?, ?, ?, ?, '', 0, '', '', 'packaging', 0, 0)""",
        (video_id, title, channel_title, channel_thumb),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    conn.close()
    return jsonify(row_to_dict(row)), 201


@app.route("/api/content", methods=["GET"])
def list_content():
    """List folders and files in a content directory."""
    rel_path = request.args.get("path", "")
    dir_path = CONTENT_DIR / rel_path if rel_path else CONTENT_DIR
    if not dir_path.exists() or not dir_path.is_dir():
        return jsonify({"error": "Directory not found"}), 404
    try:
        dir_path.resolve().relative_to(CONTENT_DIR.resolve())
    except ValueError:
        return jsonify({"error": "Invalid path"}), 403
    folders = []
    files = []
    for item in sorted(dir_path.iterdir()):
        if item.name.startswith("."):
            continue
        rel = str(item.relative_to(CONTENT_DIR))
        if item.is_dir():
            md_count = len(list(item.rglob("*.md")))
            if md_count > 0:
                folders.append({"name": _readable_name(item.name), "path": rel, "file_count": md_count})
        elif item.suffix == ".md":
            stat = item.stat()
            files.append({"name": _readable_name(item.stem), "path": rel, "modified": stat.st_mtime})
    crumbs = [{"name": "Content", "path": ""}]
    if rel_path:
        parts = Path(rel_path).parts
        for i, part in enumerate(parts):
            crumbs.append({"name": _readable_name(part), "path": str(Path(*parts[:i + 1]))})
    return jsonify({"folders": folders, "files": files, "breadcrumbs": crumbs})


@app.route("/api/content/resolve", methods=["GET"])
def resolve_content_path():
    """Resolve a filename to its full relative path within content directory."""
    filename = request.args.get("name", "").strip()
    if not filename:
        return jsonify({"error": "No filename provided"}), 400
    # Search all subdirectories for this filename
    for f in CONTENT_DIR.rglob("*.md"):
        if f.name == filename:
            try:
                f.resolve().relative_to(CONTENT_DIR.resolve())
            except ValueError:
                continue
            return jsonify({"path": str(f.relative_to(CONTENT_DIR))})
    return jsonify({"error": "File not found"}), 404


@app.route("/api/content/file", methods=["GET"])
def get_content_file():
    """Read a markdown file and return its content."""
    rel_path = request.args.get("path", "")
    if not rel_path:
        return jsonify({"error": "No path provided"}), 400
    file_path = CONTENT_DIR / rel_path
    if not file_path.exists() or not file_path.is_file():
        return jsonify({"error": "File not found"}), 404
    try:
        file_path.resolve().relative_to(CONTENT_DIR.resolve())
    except ValueError:
        return jsonify({"error": "Invalid path"}), 403
    content = file_path.read_text(encoding="utf-8")
    return jsonify({"name": _readable_name(file_path.stem), "path": rel_path, "content": content, "modified": file_path.stat().st_mtime})


@app.route("/api/content/file", methods=["PUT"])
def save_content_file():
    """Save markdown content to a file."""
    data = request.get_json(force=True)
    rel_path = data.get("path", "")
    content_text = data.get("content", "")
    if not rel_path:
        return jsonify({"error": "No path provided"}), 400
    file_path = CONTENT_DIR / rel_path
    try:
        file_path.resolve().relative_to(CONTENT_DIR.resolve())
    except ValueError:
        return jsonify({"error": "Invalid path"}), 403
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content_text, encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/api/content/file", methods=["DELETE"])
def delete_content_file():
    """Delete a markdown file."""
    data = request.get_json(force=True)
    rel_path = data.get("path", "")
    if not rel_path:
        return jsonify({"error": "No path provided"}), 400
    file_path = CONTENT_DIR / rel_path
    try:
        file_path.resolve().relative_to(CONTENT_DIR.resolve())
    except ValueError:
        return jsonify({"error": "Invalid path"}), 403
    if not file_path.exists() or not file_path.is_file():
        return jsonify({"error": "File not found"}), 404
    file_path.unlink()
    return jsonify({"ok": True})


@app.route("/api/content/folder", methods=["DELETE"])
def delete_content_folder():
    """Delete a folder and all its contents."""
    data = request.get_json(force=True)
    rel_path = data.get("path", "")
    if not rel_path:
        return jsonify({"error": "No path provided"}), 400
    folder_path = CONTENT_DIR / rel_path
    try:
        folder_path.resolve().relative_to(CONTENT_DIR.resolve())
    except ValueError:
        return jsonify({"error": "Invalid path"}), 403
    if not folder_path.exists() or not folder_path.is_dir():
        return jsonify({"error": "Folder not found"}), 404
    import shutil
    shutil.rmtree(folder_path)
    return jsonify({"ok": True})


@app.route("/api/videos/<int:vid>/create-content-doc", methods=["POST"])
def create_content_doc(vid):
    """Create a starter content doc with the inspiration video's hook transcript."""
    conn = get_db()
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    if not video:
        conn.close()
        return jsonify({"error": "Video not found"}), 404

    video_id = video["video_id"]
    title = video["title"]
    channel = video["channel_title"]
    views = video["view_count"] or 0
    outlier = video["outlier_score"] or 0
    published = video["published_at"] or ""

    hook_text = ""

    # Build the slug for the filename
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:80]
    filename = f"{slug}.md"

    # Build the content doc
    views_fmt = _format_views(views)
    outlier_fmt = f"{outlier:.1f}x" if outlier else "N/A"

    doc = f"""# {title.upper()}

Title:


Benefits:
*
*
*

Steps:
1 —
2 —
3 —
4 —
5 —

So by the end of this video:


---

## Inspiration

**{title}**
- Channel: {channel}
- Views: {views_fmt}
- Outlier Score: {outlier_fmt}
- Published: {published}
- Link: https://youtube.com/watch?v={video_id}
- Thumbnail: https://img.youtube.com/vi/{video_id}/maxresdefault.jpg

### Hook (first ~150 seconds):
{hook_text if hook_text else "(transcript unavailable)"}

---

🎯 ONE-LINER


🏷️ TITLES (pick 1 before filming)
*
*
*
*
*

🖼️ THUMBNAIL IDEAS
*
*

🎤 SAY THIS (word-for-word intro — read this aloud):
""

📊 5P FRAMEWORK (inspiration for the intro — not spoken on camera)
Proof image/video:
Problem:
Path:
Promise:
Present:

💎 BENEFITS
Today I'll show you:
*
*
*
*

So by the end:


📋 STEPS
*
*
*
*
*

🔒 WHY STAY TO THE END


🎁 END-OF-VIDEO GIFT (Skool CTA)
What:
Where: Free inside the Skool community (link in description)

📎 SOURCES / LINKS
* https://youtube.com/watch?v={video_id} — Inspiration video
"""

    # Save to content_docs folder (uses CONTENT_DIR which adapts to local vs Docker)
    content_docs_subdir = CONTENT_DIR / "content_docs"
    content_docs_subdir.mkdir(parents=True, exist_ok=True)
    filepath = content_docs_subdir / filename

    # If it already exists, return path so View button still works
    if filepath.exists():
        rel = str(filepath.relative_to(CONTENT_DIR))
        conn.close()
        return jsonify({"ok": True, "path": rel, "filename": filename, "exists": True, "has_transcript": False})

    filepath.write_text(doc, encoding="utf-8")
    rel = str(filepath.relative_to(CONTENT_DIR))

    # Update the Content Doc custom field in video_details
    details = conn.execute("SELECT custom_fields FROM video_details WHERE video_id = ?", (vid,)).fetchone()
    if details:
        fields = json.loads(details["custom_fields"] or "[]")
        for fld in fields:
            if fld.get("key") == "Content Doc":
                fld["value"] = rel
                break
        else:
            fields.append({"key": "Content Doc", "value": rel})
        conn.execute("UPDATE video_details SET custom_fields = ? WHERE video_id = ?", (json.dumps(fields), vid))
        conn.commit()

    conn.close()
    return jsonify({
        "ok": True,
        "path": rel,
        "filename": filename,
        "exists": False,
        "has_transcript": bool(hook_text),
    })


def _fetch_youtube_description(video_id):
    """Fetch a YouTube video's description via YouTube Data API. Returns '' on failure."""
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key or not video_id:
        return ""
    try:
        url = f"https://www.googleapis.com/youtube/v3/videos?part=snippet&id={video_id}&key={api_key}"
        with urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("items") or []
        if not items:
            return ""
        return items[0].get("snippet", {}).get("description", "") or ""
    except (HTTPError, URLError, json.JSONDecodeError, KeyError):
        return ""


def _format_my_channel_context():
    """Pull Andy's last 5 published + top 5 all-time from my_channel_videos.
    Returns a context string to inject into Gemini's system prompt so it can
    flag audience overlap, redundancy, and bias toward winning formats.
    Returns empty string if the table is empty (sync hasn't run yet).
    """
    try:
        conn = get_db()
        recent = conn.execute("""
            SELECT title, view_count, published_at, duration_seconds
            FROM my_channel_videos
            WHERE published_at != ''
            ORDER BY published_at DESC
            LIMIT 5
        """).fetchall()
        top_performers = conn.execute("""
            SELECT title, view_count, published_at
            FROM my_channel_videos
            WHERE published_at != ''
            ORDER BY view_count DESC
            LIMIT 5
        """).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return ""

    if not recent:
        return ""

    def _fmt(row, include_age=False):
        v = row["view_count"] or 0
        v_str = f"{v/1000:.1f}K" if v >= 1000 else str(v)
        line = f'  - "{row["title"]}" — {v_str} views'
        if include_age and row["published_at"]:
            try:
                pub = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
                days_ago = (datetime.now(timezone.utc) - pub).days
                line += f" ({days_ago}d ago)"
            except (ValueError, AttributeError):
                pass
        return line

    recent_block = "\n".join(_fmt(r, include_age=True) for r in recent)
    top_block = "\n".join(_fmt(r) for r in top_performers)

    return f"""

ANDY'S CHANNEL DATA (synced from YouTube Data API — refreshed daily):

Last 5 published videos:
{recent_block}

Top 5 all-time performers (this channel's ceiling):
{top_block}

USE THIS DATA when evaluating the brief:
- AUDIENCE OVERLAP CHECK: if the new idea is too similar to a video published in the last 3 weeks, flag it as redundancy risk in "final_thoughts."
- FORMAT BIAS: if the top performers cluster around a specific format/anchor (Karpathy-style authority hacking, tool launches, named-creator breakdowns), bias toward continuing that pattern. Name the pattern in "final_thoughts" if relevant.
- VELOCITY CHECK: a recent video with much higher views than older ones signals a winning vein worth doubling down on.
- IF the new brief is clearly a duplicate-angle of a recent upload, the verdict should be "Pause and rework: …" not "Ship it."
"""


def _claude_json_call(system_text, user_text, model="claude-sonnet-4-6", max_tokens=4000, timeout=60):
    """Call Claude Messages API with a JSON-mode contract.
    Returns the parsed JSON dict on success, or None on failure (caller falls back).
    Uses ANTHROPIC_API_KEY from env.
    """
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_text + "\n\nReturn ONLY a valid JSON object — no preamble, no markdown fences, no commentary outside the JSON.",
        "messages": [{"role": "user", "content": user_text}],
    }
    req = Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["content"][0]["text"]
        # Strip optional ```json fences if model added them despite instruction
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        return json.loads(text)
    except (HTTPError, URLError, KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
        print(f"[claude] call failed: {type(e).__name__}: {e}", flush=True)
        return None


def _gemini_fill_brief(title, channel, views_fmt, outlier_fmt, video_id, description):
    """Call Gemini to fill a structured brief. Returns a dict with all 11 brief fields, or None on failure."""
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None

    # Andy's project context — what the LLM needs to know to make good calls
    project_context = """
You are filling a video brief for AI Andy (Andy Hafell) — a YouTube channel (@theaiandy, 214K subs) + Skool community ($97/mo "AI Mate") teaching creators to build AI automations without code. Andy ships tools: AgentFlow (native macOS productivity app, public launch May 2026), CreatorGrowth (creator dashboard), Content Mate (AI content factory), ScreenPost. He has a production `skills/` folder with 37 SOPs that Claude Code reads before any task — thumbnails, content docs, packaging, agent dispatch.

His Q2 framework is ACD: Attraction (subs) / Conversion (free→paid) / Delivery (retention). North-star: $40K MRR by June. Current bottleneck: retention (64%).

**The winning content pattern (proven by YTD data):** authority hacking — piggyback on a NAMED external god-mode source (e.g. Karpathy, Anthropic, a specific repo/framework) with the source ON STAGE, then apply it to a working system with measurable proof. The "source's source" rule: don't piggyback on the inspiration creator — go upstream to what THEY were citing. That's the OG authority.

His on-camera workspace: AgentFlow is the preferred workspace when it fits, but the editor and most of the audience use other workspaces. Acceptable workspaces (pick whichever fits the topic + audience best): AgentFlow's Docs tab, Visual Studio Code (Cursor counts), Claude Code CLI in iTerm/Terminal, Claude Desktop app. Don't force AgentFlow into every brief — if the video's topic is genuinely a tool that lives outside AgentFlow (Spline, AntiGravity, web tools), the workspace shown should be whatever the audience would actually use to follow along (usually VS Code or Claude Desktop).

Skool gift rule: every video closes with a fork-able artifact (template, skills pack, repo, workflow) — not a demo, a real product the audience can grab.

Tool-launch videos produce ~5x bigger paying cohorts than tips videos. Default preference: tool-launch frame with AgentFlow tie-in.
""" + _format_my_channel_context()

    field_specs = """
You will return a JSON object with these exact keys. Each value is plain text (no markdown, no quotes inside unless needed for natural prose). Be specific, opinionated, and concise — these are decisions Andy will review, not menus of options.

**VOICE — applies to every field:** Write in **second-person imperative**, addressing Andy directly. "First, open Anthropic's Skills docs. Then switch to AgentFlow…" NEVER write "Andy will open…" / "Andy opens…" / "Anders integrates…" — that third-person reporter voice reads cold. The brief is Andy talking to Andy. Where it adds finesse, use **but/so storytelling** to give a narrative beat (e.g. "You could just transcribe the source, but that's a re-upload — so instead you apply it to your skills/ folder where 37 SOPs already exist"). Tight, direct, conversational. Cut anything that sounds like a press release.

1. "inspiration_plot" — 3-5 sentence summary of what the inspiration video ACTUALLY covers (the plot/narrative). You should absorb the source video's gist in 15 seconds without watching it. Include the central thesis, the main framework/rules/steps the creator introduces (NAME them explicitly — "Rule #1: X, Rule #2: Y…"), the tools or examples they show, and the closing payoff. Use the YouTube description's timestamps/chapter list as a structural guide if present. Be concrete, not vague. NEVER say "discusses prompt engineering best practices" — say "extracts 4 rules: (1) Prompt Skills not Claude, (2) Skills are more than prompts, (3) Build composable skills, (4) Skills get smarter every session." (Voice exception: this field describes the source video, so it can be in third-person referring to the inspiration creator — but don't refer to Andy as "Andy.")

2. "screen_share_todo" — **A JSON array of 3-7 step strings** (sweet spot is 5). Each item is a **high-level milestone** that fits in 1-3 minutes of screen-share time, written in second-person imperative, that chains its concrete micro-actions inline. Total filming time across all steps: 10-15 minutes max.

### HARD RULES — VIOLATING ANY OF THESE FAILS THE BRIEF

**Rule 1 — Use existing tools' shipped features, NOT custom code Andy has to write on camera.** If the video is about Tool X, the steps use Tool X's actual UI, prompts, buttons, and outputs. Andy is NOT writing a new Python/Claude-Code skill on camera unless the entire video is explicitly about writing that skill (build-a-skill tutorials are the only exception).

   - GOOD: "In AntiGravity, prompt the website build using your Spline asset; let it generate."
   - BAD: "Write a Claude Code skill that takes a Spline export path and integrates it into a placeholder in an AntiGravity-generated website structure." (This is a 4-hour engineering task, not a screen-share step.)

**Rule 2 — No step that requires Andy to write working code mid-take.** Writing code IS allowed only if (a) the code is ≤5 lines AND visibly trivial, OR (b) the code is pre-written and Andy is showing a pre-existing file. Never "write a simple skill that does X" as a single step — that's not a step, that's a multi-hour task.

**Rule 3 — Each step must be doable in ~1-3 minutes of screen time.** If a step's success depends on a creative/engineering output ("write a skill that does Y", "design a 3D scene that looks great", "remix a simple object"), it FAILS this rule. Those need to either (a) be pre-baked before filming and the step is "show the pre-built thing," or (b) be split into "open the tool → click button → tool generates it."

**Rule 4 — Don't force Claude Code skills into every video.** If the video's topic is Tool X (Spline, AntiGravity, etc.), the proof is using Tool X's actual features, NOT building a Claude-Code wrapper around it. The skills/ folder showcase is fine when the brief's topic IS skills; otherwise, AgentFlow appears as the workspace/file-viewer, not as the framework being demoed.

**Rule 5 — Skool gift = packaging EXISTING artifacts, not creating new ones.** The gift is your existing skills/, an existing template, an existing repo — zipped and dropped into Skool. NOT "write a brand new skill pack on camera."

### Step shape
   - **Clear milestone, not a creative leap.** GOOD: "Open Chrome, go to spline.design, sign in, create a new project, and pick the 3D website template." BAD: "Remix a simple 3D object" — that leaves Andy lost on what to remix.
   - **Doable from the prior step's state.** If step 4 needs a file, step 2 created it (or it pre-exists on Andy's machine — assume the skills/ folder, ~/Documents/Claude Folder/, AgentFlow, Chrome, Spline account already exist).
   - **Anchored to real apps, URLs, file paths, button names.**
   - **Sequential.** Step N+1 builds on step N's state. Zero to end-result.

3-7 milestones total. Sweet spot is 5. Don't pad with sub-actions — those belong in the Screen Share To-Do doc.

### GOOD example (AntiGravity + Spline video):
```
[
  "Open Chrome, go to spline.design, sign in, and create a new project from the 3D website template.",
  "In a second tab, open antigravity.ai, sign in, and connect the Spline asset from step 1.",
  "In AntiGravity, type the website prompt and click Generate; let it build the site with your Spline asset embedded.",
  "Switch to AgentFlow's Docs tab, open ~/Documents/Claude Folder/skills/, zip 3 site-building SOPs as the Skool gift, and drop the zip into a new Skool classroom post draft.",
  "Open the deployed AntiGravity URL in Chrome and show the 3D site running live with the Spline asset rotating."
]
```

### BAD example — this is what to AVOID:
```
[
  "Showcase a few stunning 3D website examples from Spline's gallery.",  // vague — what does showcase mean?
  "Create a new Claude Code skill file named spline_asset_generator.py.", // unjustified — why are we writing code for a Spline video?
  "Write a simple Claude Code skill that takes a text prompt and outputs a JSON object describing a 3D asset.", // multi-hour task pretending to be a step
  "Run the skill in AgentFlow's terminal, showing the successful execution."  // depends on the unrealistic prior step
]
```

Pick the workspace that fits the audience for this video (AgentFlow / VS Code / Claude Code CLI / Claude Desktop). Don't force AgentFlow if VS Code or Claude Desktop is what the audience would actually use. MUST include the Skool gift reveal moment.

**CRITICAL — proof-segment rule (locked 2026-05-19):** The FINAL step MUST be a **demonstrable end-result** voiceover can point to and say "look, it works." Acceptable: built thing running on real input / item installed + tested working in real environment / test outcome on real input / side-by-side comparison rendered / finished tutorial workflow on a real artifact. If you cannot name a demonstrable end-result step, include "PROOF SEGMENT MISSING" as the final step — the brief fails the gate. React-only setups DO NOT QUALIFY — they need a build/test tail.

This array is the glanceable brief outline only, NOT the deep scene plan (that lives in the separate Screen Share To-Do doc, where each milestone here becomes its own multi-action scene).

2b. "end_result" — ONE sentence (or 2 max) describing the **literal proof shot** the final step produces. This is the "look at this" moment Andy points to in the intro to hook the viewer + the credibility shot he references mid-video + the evidence drop in the Skool post. Sometimes it's simple (a live website scrolling), sometimes complex (a test passing on real input). Be concrete and visual — describe what's actually on screen, framed as the payoff. Example: "The deployed AntiGravity site running live in Chrome, with the Spline 3D asset rotating in the hero section as you scroll." NOT a re-summary of the last step — the SHOT itself, written as a single moment of proof.

3. "sources_source" — The ORIGINAL tool / blog post / video / tweet the inspiration creator was citing. If the inspiration video's description has a direct link, use it. If not, NAME the most likely upstream source explicitly (e.g. "Anthropic's official Skills documentation + Oct 2025 launch blog"). Never say "unknown."

3. "why_god_mode" — One sentence on why the source's source carries authority. Reference views/stars/credibility/scarcity.

4. "frame" — One of: "react" / "breakdown" / "apply" / "contradict". Default to "breakdown" with source-on-stage YES unless the topic clearly demands a different frame. Include "source-on-stage: yes" or "source-on-stage: no" at the end.

5. "differentiator" — ONE LINE in **second-person**: how YOUR angle is meaningfully different from the inspiration video. Must reference a concrete asset you have that the inspiration creator doesn't — your production skills/ folder (37 SOPs), AgentFlow workspace, the AI Mate community, your content pipeline. NEVER say "I'll do it better" or "Andy will…" — name the structural gap directly. Example shape: "You have 37 production SOPs running your actual business; they have a slide deck."

6. "my_angle" — One sentence in **second-person**: what YOU add to the source's source (your application, your workspace, your lens). E.g. "You run Anthropic's Skills rules against your actual production folder, not toy examples."

7. "skool_gift" — A concrete fork-able artifact tied to the topic. Phrase in **second-person**: "Drop your Skills Starter Pack — 5 production SOPs members can fork" / "Hand them the Eval Criteria Template (12 binary checks)" / "Ship your Thumbnail Generator skill pack." Be specific; this is the Skool CTA.

8. "acd_lever" — "Attraction" / "Conversion" / "Delivery". Authority-anchored videos lean Attraction.

9. "tool_tie_in" — Which of YOUR tools naturally showcases here. Default "AgentFlow" if any Claude Code workflow is shown (your workspace invariant). Otherwise CreatorGrowth / Content Mate / etc., or "none — pure tips video" (flag as a risk).

10. "demand_check" — Cite the inspiration video itself as the demand proof (title + view count + days since publish if known). If you know of a sibling video that also crossed 10K, name it.

11. "one_liner" — Single plain-English sentence in **second-person** that passes the cab test: "what is this video about?" Names the source's source + your angle. E.g. "You take Anthropic's official Skills rules and run them against your real production folder so the audience can fork it."

12. "filming_notes" — 2-4 short bullets for the content-doc stage, in **second-person imperative**: "Open with the Anthropic Skills doc, not the inspiration video." / "Don't name the inspiration creator on camera." / "Map each rule to a real skill in your skills/ folder." / "Drop the Skool CTA at the 50% mark and again at the end."

13. "final_thoughts" — 3-5 sentence honest editorial verdict in **second-person**, like a sharp friend reading the brief over your shoulder. NOT a re-summary of the fields above. Weigh: (a) authority hacking strength — is the source's source a real god-mode anchor? (b) audience overlap — does this duplicate a recent upload? (c) **PROOF-SEGMENT QUALITY** — does the screen_share_todo end with a real demonstrable end-result, or is it react-only / vague? Per the May 19 footage rule, react-only without a build/test tail is an automatic "Pause and rework." (d) channel format fit — does this match the winning vein in your top performers? Use but/so beats where useful ("The Anthropic anchor is strong, but your last upload was already a Claude Code skill demo — so the angle has to lean hard on production-scale vs Austin's slides"). End with a one-line confidence call: "Ship it" / "Ship with caveat: …" / "Pause and rework: …" — direct, not diplomatic.
"""

    user_prompt = f"""Inspiration video:
- Title: {title}
- Channel: {channel}
- Views: {views_fmt}
- Outlier: {outlier_fmt}
- YouTube link: https://youtube.com/watch?v={video_id}

Video description (verbatim from YouTube):
---
{description[:4000] if description else "(no description available)"}
---

{field_specs}

Return ONLY the JSON object. No preamble, no markdown fences, no commentary."""

    # Primary: Claude Sonnet 4.6 (better at following negative rules like "no engineering tasks disguised as steps")
    claude_result = _claude_json_call(project_context, user_prompt, model="claude-sonnet-4-6", max_tokens=4000)
    if claude_result is not None:
        return claude_result

    # Fallback: Gemini 2.5 Flash
    body = {
        "contents": [{
            "parts": [
                {"text": project_context},
                {"text": user_prompt},
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.4,
        }
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    req = Request(url, data=json.dumps(body).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (HTTPError, URLError, KeyError, IndexError, json.JSONDecodeError, TypeError):
        return None


@app.route("/api/videos/<int:vid>/link-youtube", methods=["POST"])
def link_card_to_youtube(vid):
    """Link a Published card to its real YouTube video.
    Accepts either a YouTube video_id (11 chars) or a full URL.
    Stores the ID in custom_fields['YouTube Video ID'] and immediately copies
    current view_count + published_at from my_channel_videos if available.
    Called by youtube_publisher.py right after upload — closed loop.
    """
    body = request.get_json(silent=True) or {}
    raw = (body.get("youtube_video_id") or body.get("url") or body.get("youtube_url") or "").strip()
    if not raw:
        return jsonify({"error": "missing youtube_video_id or url"}), 400

    # Extract 11-char YouTube ID from various URL forms
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", raw)
    yt_id = m.group(1) if m else (raw if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw) else None)
    if not yt_id:
        return jsonify({"error": f"could not parse YouTube video id from: {raw}"}), 400

    conn = get_db()
    card = conn.execute("SELECT id, title FROM videos WHERE id = ?", (vid,)).fetchone()
    if not card:
        conn.close()
        return jsonify({"error": f"card {vid} not found"}), 404

    yt = conn.execute(
        "SELECT title, view_count, published_at FROM my_channel_videos WHERE video_id = ?",
        (yt_id,),
    ).fetchone()

    # Store the link in custom_fields
    details = conn.execute(
        "SELECT custom_fields FROM video_details WHERE video_id = ?", (vid,)
    ).fetchone()
    fields = json.loads(details["custom_fields"]) if details and details["custom_fields"] else []
    fields = [f for f in fields if f.get("key") != "YouTube Video ID"]
    fields.append({"key": "YouTube Video ID", "value": yt_id})
    if details:
        conn.execute("UPDATE video_details SET custom_fields = ? WHERE video_id = ?",
                     (json.dumps(fields), vid))
    else:
        conn.execute("INSERT INTO video_details (video_id, custom_fields) VALUES (?, ?)",
                     (vid, json.dumps(fields)))

    # If the channel sync has already pulled this video, refresh card stats now
    refreshed = False
    if yt:
        conn.execute("UPDATE videos SET view_count = ?, published_at = ? WHERE id = ?",
                     (yt["view_count"], yt["published_at"], vid))
        refreshed = True
    conn.commit()
    conn.close()
    return jsonify({
        "ok": True,
        "card_id": vid,
        "youtube_video_id": yt_id,
        "stats_refreshed": refreshed,
        "note": "" if refreshed else "video not yet in my_channel_videos; will refresh on next daily sync (@ 6 AM UTC)",
    })


def _read_brief_for_card(conn, card_id):
    """Read the Brief Doc markdown attached to a card, if any. Returns '' on miss."""
    row = conn.execute(
        "SELECT custom_fields FROM video_details WHERE video_id = ?", (card_id,)
    ).fetchone()
    if not row or not row["custom_fields"]:
        return ""
    try:
        fields = json.loads(row["custom_fields"])
    except (json.JSONDecodeError, TypeError):
        return ""
    brief_path = None
    for f in fields:
        if f.get("key") == "Brief Doc" and f.get("value"):
            brief_path = f["value"]
            break
    if not brief_path:
        return ""
    filepath = CONTENT_DIR / brief_path
    try:
        return filepath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _gemini_fill_screen_share_todo(title, brief_md):
    """Call Gemini to produce a structured screen-share to-do from the brief.
    Returns a dict with pre_production_checklist, scenes, open_questions — or None on failure.
    """
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None

    project_context = """
You are filling a SCREEN-SHARE TO-DO for AI Andy — a tactical pre-production document that maps every on-screen moment of an upcoming YouTube video to a concrete app/URL/cursor action. The to-do is read by Andy right before filming. The content doc (separate artifact) wraps narration around what this to-do says will be on screen.

Andy's on-camera workspace: AgentFlow is preferred when it fits, but the audience + editor mostly use other workspaces. Acceptable workspaces — pick whichever fits the topic best: AgentFlow's Docs tab, Visual Studio Code (Cursor counts), Claude Code CLI in iTerm/Terminal, Claude Desktop app. Don't force AgentFlow if the video's topic is a tool that lives outside it (Spline, AntiGravity, web tools) — use the workspace the audience would actually use to follow along (usually VS Code or Claude Desktop). His skills/ folder lives at ~/Documents/Claude Folder/skills/ with 37 production SOPs (CONTENT_DOC_PROCESS, PACKAGING_EXPERT, THUMBNAIL_SYSTEM, BRIEF_DOC_PROCESS, etc.).

Rules you MUST encode:
- The brief's "Source's source" gets at least one dedicated scene with that source visible on screen.
- Each scene = ONE screen state. App switches = new scenes.
- "Pre-work" is hard-required — if a scene needs a file/tab/account, list it in the pre-production checklist.
- One-line voice-over notes max per scene (the content doc handles the real script).
- Be LITERAL — "scroll to line 42 of skills/PACKAGING_EXPERT_SOP.md" not "show the skills file."

**VOICE — second-person imperative throughout.** Address Andy directly. "Open Chrome. Switch to AgentFlow. Click on the Skills tab." NEVER "Andy opens…" or "Anders switches…" — the to-do is Andy talking to Andy from the future.
"""

    field_specs = """
Return JSON with these exact keys.

**TIGHTNESS RULE — most important rule in this prompt:**
Every string field must be a SHORT, BLUNT imperative — ideally 3-8 words, max ~12 words. NO explanatory clauses, NO "confirm that…", NO "verify that…", NO commentary, NO "this allows you to…". Just the action.

GOOD examples:
- "Go to spline.design"
- "Click 'New Project'"
- "Sign in with Google"
- "Copy the public share URL"
- "Switch to AntiGravity tab"
- "Paste the Spline URL into the prompt box"
- "Click Generate"

BAD examples (NEVER do this):
- "Create a Spline account at spline.design and log in — confirm you can access the Community/Templates section and the 3D Website template category."  (way too long, tail commentary)
- "Verify the Spline public share URL actually renders the 3D object in a plain Chrome tab (no login wall) so the AntiGravity integration shot works cleanly."  (verification clauses, no)
- "Open two Chrome windows pre-logged-in: Window 1 = spline.design/community, Window 2 = antigravity.ai dashboard — arrange on desktop so switching is instant."  (compound setup, split it)

1. "pre_production_checklist" — list of 5-10 short strings. Each item is ONE concrete pre-flight task, 3-8 words. "Sign in to spline.design." / "Sign in to antigravity.ai." / "Pick a Spline asset to use." / "Silence notifications." / "Close unrelated tabs." NEVER include "verify that…" / "confirm that…" — just the prep action.

2. "scenes" — list of 4-8 scene objects (one per major on-screen moment). Each scene's strings follow the tightness rule:
   - "name": 2-5 words, the scene's purpose. e.g. "Open Spline."
   - "app": just the app name. "Chrome." / "AgentFlow." / "Finder."
   - "url_or_path": just the URL or path. "spline.design" / "~/Documents/Claude Folder/skills/"
   - "on_screen": one short sentence describing what's visible. ≤12 words.
   - "cursor_action": one short imperative. ≤10 words. "Click the 3D Website template."
   - "voice_over_note": one short hook line. ≤12 words. (Detail belongs in the content doc.)
   - "why": 2-6 words. "Authority anchor." / "Skool gift reveal." / "Proof segment."
   - "pre_work": one short sentence. ≤12 words. "Spline account logged in."

3. "open_questions" — list of 0-4 short strings, ≤12 words each. Risk or decision per item. "Spline asset may not load in AntiGravity preview." / "Need final pricing for the Skool gift post."
"""

    user_prompt = f"""Video title: {title}

BRIEF (already validated, score ≥7/10):
---
{brief_md[:5000] if brief_md else "(no brief found — generate a sensible default to-do based on the title)"}
---

{field_specs}

Return ONLY the JSON object. No preamble, no markdown fences."""

    # Primary: Claude Sonnet 4.6
    claude_result = _claude_json_call(project_context, user_prompt, model="claude-sonnet-4-6", max_tokens=3000)
    if claude_result is not None:
        return claude_result

    # Fallback: Gemini 2.5 Flash
    body = {
        "contents": [{
            "parts": [
                {"text": project_context},
                {"text": user_prompt},
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.4,
        }
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    req = Request(url, data=json.dumps(body).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (HTTPError, URLError, KeyError, IndexError, json.JSONDecodeError, TypeError):
        return None


@app.route("/api/videos/<int:vid>/create-screen-share-todo", methods=["POST"])
def create_screen_share_todo(vid):
    """Create a screen-share to-do for a card. Auto-fills via Gemini using the
    card's Brief Doc as primary input. Falls back to empty template if Gemini
    or the brief is unavailable.

    Pass ?force=1 (or JSON {"force": true}) to overwrite an existing to-do.
    """
    force = (request.args.get("force") == "1") or bool((request.get_json(silent=True) or {}).get("force"))
    conn = get_db()
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    if not video:
        conn.close()
        return jsonify({"error": "Video not found"}), 404

    title = video["title"]
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:80]
    filename = f"{slug}.md"

    brief_md = _read_brief_for_card(conn, vid)
    filled = _gemini_fill_screen_share_todo(title, brief_md) if brief_md else None
    auto_filled = bool(filled)

    # Build pre-production checklist
    if filled and isinstance(filled.get("pre_production_checklist"), list):
        checklist = "\n".join(f"- [ ] {item}" for item in filled["pre_production_checklist"])
    else:
        checklist = "\n".join([
            "- [ ] Software installed: [fill in]",
            "- [ ] Accounts logged in: [fill in]",
            "- [ ] Files prepared: [fill in]",
            "- [ ] Browser tabs queued: [fill in]",
            "- [ ] Source materials downloaded: [fill in]",
            "- [ ] AgentFlow workspace clean: [yes/no]",
            "- [ ] skills/ folder organized for showcase: [yes/no]",
            "- [ ] Notifications silenced: [iMessage, Slack, etc.]",
        ])

    # Build scenes
    scenes_md = ""
    if filled and isinstance(filled.get("scenes"), list):
        for i, s in enumerate(filled["scenes"], start=1):
            if not isinstance(s, dict):
                continue
            scenes_md += f"""
### Scene {i}: {s.get('name', '[unnamed scene]')}
- **App / tab:** {s.get('app', '[fill in]')}
- **URL or file path:** {s.get('url_or_path', '[fill in]')}
- **On screen:** {s.get('on_screen', '[fill in]')}
- **Cursor action:** {s.get('cursor_action', '[fill in]')}
- **Voice-over note:** {s.get('voice_over_note', '[fill in]')}
- **Why this scene:** {s.get('why', '[tie to a brief field]')}
- **Pre-work:** {s.get('pre_work', '[fill in]')}
"""
    else:
        scenes_md = """
### Scene 1: [Scene name — tied to a brief field]
- **App / tab:** [fill in]
- **URL or file path:** [fill in]
- **On screen:** [fill in]
- **Cursor action:** [fill in]
- **Voice-over note:** [fill in — one line max]
- **Why this scene:** [tie to a brief field]
- **Pre-work:** [fill in]
"""

    # Open questions
    open_q_md = ""
    if filled and isinstance(filled.get("open_questions"), list) and filled["open_questions"]:
        open_q_md = "\n".join(f"- {q}" for q in filled["open_questions"])
    else:
        open_q_md = "- [decisions Andy will make at film time, risks that could break the shoot]"

    doc = f"""# SCREEN SHARE TO-DO — {title.upper()}

> Tactical filming prep. {"Auto-filled from the current brief on disk (re-read every regenerate, so updating the brief and regenerating this picks up the new content)." if auto_filled else "Fill every field before sitting down to film."}
> Pre-production checklist must be 100% done before scene 1.

---

## PRE-PRODUCTION CHECKLIST
{checklist}

## SCENE-BY-SCENE PLAN
Each scene = one screen state / one camera setup. App switches = new scenes.
{scenes_md}

## OPEN QUESTIONS / RISKS
{open_q_md}
"""

    todos_subdir = CONTENT_DIR / "todos"
    todos_subdir.mkdir(parents=True, exist_ok=True)
    filepath = todos_subdir / filename

    if filepath.exists() and not force:
        rel = str(filepath.relative_to(CONTENT_DIR))
        conn.close()
        return jsonify({"ok": True, "path": rel, "filename": filename, "exists": True, "auto_filled": False})

    filepath.write_text(doc, encoding="utf-8")
    rel = str(filepath.relative_to(CONTENT_DIR))

    # Set the Screen Share To-Do custom field
    details = conn.execute("SELECT custom_fields FROM video_details WHERE video_id = ?", (vid,)).fetchone()
    fields = json.loads(details["custom_fields"]) if details and details["custom_fields"] else []
    fields = [f for f in fields if f.get("key") != "Screen Share To-Do"]
    fields.append({"key": "Screen Share To-Do", "value": rel})
    if details:
        conn.execute("UPDATE video_details SET custom_fields = ? WHERE video_id = ?",
                     (json.dumps(fields), vid))
    else:
        conn.execute("INSERT INTO video_details (video_id, custom_fields) VALUES (?, ?)",
                     (vid, json.dumps(fields)))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "path": rel, "filename": filename, "exists": False, "auto_filled": auto_filled, "had_brief": bool(brief_md)})


@app.route("/api/videos/<int:vid>/create-brief", methods=["POST"])
def create_brief(vid):
    """Create a starter brief for idea-validation BEFORE a content doc.
    Auto-fills via Gemini using the inspiration video's YouTube description + project context.
    Falls back to an empty template if Gemini is unavailable or fails.

    Pass ?force=1 (or JSON {"force": true}) to overwrite an existing brief.
    """
    force = (request.args.get("force") == "1") or bool((request.get_json(silent=True) or {}).get("force"))
    conn = get_db()
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    if not video:
        conn.close()
        return jsonify({"error": "Video not found"}), 404

    video_id = video["video_id"]
    title = video["title"]
    channel = video["channel_title"]
    views = video["view_count"] or 0
    outlier = video["outlier_score"] or 0
    published = video["published_at"] or ""

    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:80]
    filename = f"{slug}.md"

    views_fmt = _format_views(views)
    outlier_fmt = f"{outlier:.1f}x" if outlier else "N/A"

    # Try to auto-fill via Gemini using the inspiration video's description
    description = _fetch_youtube_description(video_id)
    filled = _gemini_fill_brief(title, channel, views_fmt, outlier_fmt, video_id, description) if description else None
    auto_filled = bool(filled)

    def _get(k, default=None):
        v = (filled or {}).get(k) if filled else None
        return v if v else (default if default is not None else f"[fill in — {k.replace('_', ' ')}]")

    inspiration_plot   = _get("inspiration_plot")

    # screen_share_todo is expected as a 3-7 milestone list (brief outline).
    # Render as a numbered list. Fall back gracefully if Gemini returns a string.
    sst_raw = (filled or {}).get("screen_share_todo") if filled else None
    if isinstance(sst_raw, list) and sst_raw:
        screen_share_todo = "\n" + "\n".join(f"{i}. {step}" for i, step in enumerate((s for s in sst_raw if str(s).strip()), start=1))
    elif isinstance(sst_raw, str) and sst_raw.strip():
        # Legacy paragraph fallback — preserve as-is
        screen_share_todo = sst_raw
    else:
        screen_share_todo = "[fill in — screen share to-do]"

    end_result = _get("end_result")

    sources_source     = _get("sources_source")
    why_god_mode       = _get("why_god_mode")
    frame              = _get("frame", "breakdown — source-on-stage: yes")
    differentiator     = _get("differentiator")
    my_angle           = _get("my_angle")
    skool_gift         = _get("skool_gift")
    acd_lever          = _get("acd_lever", "Attraction")
    tool_tie_in        = _get("tool_tie_in", "AgentFlow")
    demand_check       = _get("demand_check", f"{title} — {views_fmt} views ({channel})")
    one_liner          = _get("one_liner")
    filming_notes      = (filled or {}).get("filming_notes") if filled else None
    final_thoughts     = (filled or {}).get("final_thoughts") if filled else None

    checkmark = "x" if auto_filled else " "
    score_line = "**11/11** → review the fills; correct anything off (especially Proof segment) before promoting to content-doc-process." if auto_filled else "X/11"

    notes_section = ""
    if filming_notes:
        if isinstance(filming_notes, list):
            notes_section = "\n\n## Notes for content doc stage\n" + "\n".join(f"- {n}" for n in filming_notes)
        else:
            notes_section = f"\n\n## Notes for content doc stage\n{filming_notes}"

    final_thoughts_section = ""
    if final_thoughts:
        final_thoughts_section = f"\n\n## Final thoughts\n{final_thoughts}"

    doc = f"""# BRIEF — {title.upper()}

> Idea-validation gate. {"Auto-filled by Claude Sonnet 4.6 from the inspiration video's description + your channel data — review and correct anything off." if auto_filled else "Fill every field. Score the checklist honestly."}
> If <7/10, kill or rewrite the idea — don't promote to a content doc.

---

**Inspiration card:** [creatorgrowth video {vid}] — {title} ({channel}, {views_fmt} views, outlier {outlier_fmt})
Link: https://youtube.com/watch?v={video_id}

**Inspiration plot:** {inspiration_plot}

**Screen share to-do:** {screen_share_todo}

**End result:** {end_result}

**Source's source:** {sources_source}

**Why god-mode:** {why_god_mode}

**Frame:** {frame}

**Differentiator from inspiration:** {differentiator}

**My angle on top:** {my_angle}

**Skool gift:** {skool_gift}

**ACD lever:** {acd_lever}

**Tool tie-in:** {tool_tie_in}

**Demand check:** {demand_check}

**One-liner:** {one_liner}

---

## BRIEF CHECKLIST

- [{checkmark}] **Authority hacking — yes/no.** Named external god-mode source identified.
- [{checkmark}] **Proof segment — yes/no.** Screen share to-do ends with a demonstrable end-result (built thing running / test outcome / side-by-side / finished workflow). React-only fails the gate. [Locked 2026-05-19 footage rule.]
- [{checkmark}] **Source's source pulled.** Inspiration creator's description checked for the ORIGINAL tool/video.
- [{checkmark}] **Differentiator named — one line.** Our angle is meaningfully different from the inspiration video.
- [{checkmark}] **Frame chosen.** React / breakdown / apply / contradict — source-on-stage decided.
- [{checkmark}] **Demand check passed.** At least one similar YouTube video at ≥10K views.
- [{checkmark}] **Tool-launch over tips.** Tool-launch OR has a tool naturally tied in.
- [{checkmark}] **Skool gift defined.** Fork-able artifact tied to the topic.
- [{checkmark}] **ACD lever named.** Which lever does this pull.
- [{checkmark}] **Subscriber-pullable.** Subs click it in their feed — not pure YT Search bait.
- [{checkmark}] **One-liner passes the cab test.**

BRIEF CHECKLIST SCORE: {score_line}{notes_section}{final_thoughts_section}
"""

    briefs_subdir = CONTENT_DIR / "briefs"
    briefs_subdir.mkdir(parents=True, exist_ok=True)
    filepath = briefs_subdir / filename

    if filepath.exists() and not force:
        rel = str(filepath.relative_to(CONTENT_DIR))
        # Even if brief file exists, make sure the card has been promoted to the Brief tab.
        # Earlier briefs may have been created before auto-promote was wired.
        conn.execute(
            "UPDATE videos SET status = 'brief' WHERE id = ? AND status IN ('options', 'best')",
            (vid,),
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "path": rel, "filename": filename, "exists": True, "auto_filled": False})

    filepath.write_text(doc, encoding="utf-8")
    rel = str(filepath.relative_to(CONTENT_DIR))

    details = conn.execute("SELECT custom_fields FROM video_details WHERE video_id = ?", (vid,)).fetchone()
    if details:
        fields = json.loads(details["custom_fields"] or "[]")
        for fld in fields:
            if fld.get("key") == "Brief Doc":
                fld["value"] = rel
                break
        else:
            fields.append({"key": "Brief Doc", "value": rel})
        conn.execute("UPDATE video_details SET custom_fields = ? WHERE video_id = ?", (json.dumps(fields), vid))
        conn.commit()
    else:
        fields = [{"key": "Brief Doc", "value": rel}]
        conn.execute(
            "INSERT INTO video_details (video_id, custom_fields) VALUES (?, ?)",
            (vid, json.dumps(fields)),
        )
        conn.commit()

    # Auto-promote the card into the Brief tab if it's still upstream of brief stage
    conn.execute(
        "UPDATE videos SET status = 'brief' WHERE id = ? AND status IN ('options', 'best')",
        (vid,),
    )
    conn.commit()

    conn.close()
    return jsonify({"ok": True, "path": rel, "filename": filename, "exists": False, "auto_filled": auto_filled})


@app.route("/api/videos/<int:vid>/bundle", methods=["GET"])
def get_card_bundle(vid):
    """Return a single JSON bundle for a card: metadata + Brief Doc markdown +
    Screen Share To-Do markdown + Andy's last 5 / top 5 channel videos.
    Designed for local Claude Code to fetch one URL and have everything needed
    to generate a content doc via the CONTENT_DOC_PROCESS_SOP.
    """
    conn = get_db()
    v = conn.execute("SELECT * FROM videos WHERE id = ?", (vid,)).fetchone()
    if not v:
        conn.close()
        return jsonify({"error": "Video not found"}), 404

    # Custom fields → paths to brief + todo
    det = conn.execute(
        "SELECT custom_fields FROM video_details WHERE video_id = ?", (vid,)
    ).fetchone()
    fields = json.loads(det["custom_fields"]) if det and det["custom_fields"] else []

    def _field_path(key):
        for f in fields:
            if f.get("key") == key and f.get("value"):
                return f["value"]
        return None

    def _read(rel):
        if not rel:
            return None
        p = CONTENT_DIR / rel
        try:
            return p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    brief_path = _field_path("Brief Doc")
    todo_path = _field_path("Screen Share To-Do")
    brief_md = _read(brief_path)
    todo_md = _read(todo_path)

    # Channel context — last 5 + top 5 (for "what's been working" awareness)
    try:
        recent = [dict(r) for r in conn.execute(
            "SELECT title, view_count, published_at FROM my_channel_videos "
            "WHERE published_at != '' ORDER BY published_at DESC LIMIT 5"
        ).fetchall()]
        top = [dict(r) for r in conn.execute(
            "SELECT title, view_count, published_at FROM my_channel_videos "
            "WHERE published_at != '' ORDER BY view_count DESC LIMIT 5"
        ).fetchall()]
    except sqlite3.OperationalError:
        recent, top = [], []
    conn.close()

    return jsonify({
        "card": {
            "id": v["id"],
            "video_id": v["video_id"],
            "title": v["title"],
            "channel_title": v["channel_title"],
            "view_count": v["view_count"],
            "outlier_score": v["outlier_score"],
            "published_at": v["published_at"],
            "status": v["status"],
            "youtube_url": f"https://youtube.com/watch?v={v['video_id']}" if v["video_id"] and not str(v["video_id"]).startswith("custom_") else None,
        },
        "brief": {
            "path": brief_path,
            "markdown": brief_md,
            "exists": bool(brief_md),
        },
        "screen_share_todo": {
            "path": todo_path,
            "markdown": todo_md,
            "exists": bool(todo_md),
        },
        "channel": {
            "recent": recent,
            "top": top,
        },
    })


@app.route("/api/cards/batch-create-brief", methods=["POST"])
def batch_create_brief():
    """Sequentially create briefs for a list of card IDs. Server-side serialization
    avoids the race condition in the modal UI when multiple in-flight requests
    land on stale state. Pass {"video_ids": [...], "force": bool}.
    Returns {"results": [{"vid": int, "ok": bool, "path": str, "error": str|null}, ...]}.
    """
    data = request.get_json(silent=True) or {}
    vids = data.get("video_ids") or []
    force = bool(data.get("force"))
    if not isinstance(vids, list) or not vids:
        return jsonify({"error": "missing or empty video_ids list"}), 400
    if len(vids) > 200:
        return jsonify({"error": "too many video_ids; max 200 per batch"}), 400

    # Forward the caller's session cookie so internal calls pass auth.
    cookie_header = request.headers.get("Cookie", "")
    results = []
    with app.test_client() as client:
        # Copy cookies onto the test client so login_required passes
        for k, v in request.cookies.items():
            client.set_cookie(k, v, domain="localhost")
        for vid in vids:
            try:
                vid_int = int(vid)
                url = f"/api/videos/{vid_int}/create-brief" + ("?force=1" if force else "")
                resp = client.post(url, headers={"Cookie": cookie_header} if cookie_header else None)
                payload = resp.get_json(silent=True) or {}
                results.append({
                    "vid": vid_int,
                    "ok": resp.status_code == 200,
                    "status": resp.status_code,
                    "path": payload.get("path"),
                    "exists": payload.get("exists"),
                    "auto_filled": payload.get("auto_filled"),
                    "error": payload.get("error"),
                })
            except Exception as e:
                results.append({"vid": vid, "ok": False, "error": f"{type(e).__name__}: {e}"})
    return jsonify({"results": results, "total": len(results), "succeeded": sum(1 for r in results if r["ok"])})


current_image_url = {"url": None}

@app.route("/api/set-image", methods=["POST"])
def set_image():
    current_image_url["url"] = request.json.get("url")
    return jsonify({"ok": True})

@app.route("/api/current-image", methods=["GET"])
def get_current_image():
    return jsonify({"url": current_image_url["url"]})

@app.route("/image-viewer")
def image_viewer():
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Image Viewer</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#111; display:flex; align-items:center; justify-content:center; min-height:100vh; }
  img { max-width:100%; max-height:100vh; object-fit:contain; transition: opacity 0.2s; }
  #placeholder { color:#444; font-family:sans-serif; font-size:18px; }
</style>
</head>
<body>
  <span id="placeholder">Waiting for image...</span>
  <img id="img" src="" style="display:none;">
  <script>
    var lastUrl = null;
    function poll() {
      fetch('/api/current-image')
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data.url && data.url !== lastUrl) {
            lastUrl = data.url;
            var img = document.getElementById('img');
            img.src = data.url;
            img.style.display = 'block';
            document.getElementById('placeholder').style.display = 'none';
          }
        })
        .catch(function(){});
    }
    setInterval(poll, 800);
    poll();
  </script>
</body>
</html>"""


import uuid as _uuid
_gallery_store = {}

@app.route("/api/set-gallery", methods=["POST"])
def set_gallery():
    srcs = request.json.get("srcs", [])
    gid = _uuid.uuid4().hex[:8]
    _gallery_store[gid] = srcs
    return jsonify({"id": gid})

@app.route("/image-gallery")
def image_gallery():
    gid = request.args.get("id", "")
    srcs = _gallery_store.get(gid, [])
    imgs_html = "".join(f'<div class="img-wrap"><img src="{s}"></div>' for s in srcs)
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"><title>Images</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#111; padding:20px; display:flex; flex-wrap:wrap; gap:16px; justify-content:center; align-items:flex-start; }}
  .img-wrap {{ flex:1 1 45%; max-width:48%; }}
  .img-wrap img {{ width:100%; height:auto; border-radius:8px; display:block; cursor:zoom-in; transition:opacity 0.15s; }}
  .img-wrap img:hover {{ opacity:0.85; outline:2px solid #5b9bd5; border-radius:8px; }}
  #lightbox {{ display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.95); z-index:9999; align-items:center; justify-content:center; cursor:zoom-out; }}
  #lightbox.active {{ display:flex; }}
  #lightbox img {{ max-width:95%; max-height:95vh; object-fit:contain; border-radius:8px; }}
  #lightbox-close {{ position:fixed; top:20px; right:28px; color:#aaa; font-size:32px; cursor:pointer; line-height:1; }}
  #lightbox-close:hover {{ color:#fff; }}
  #lb-prev, #lb-next {{ position:fixed; top:50%; transform:translateY(-50%); color:#fff; font-size:48px; cursor:pointer; padding:0 24px; user-select:none; opacity:0.6; display:flex; align-items:center; }}
  #lb-prev {{ left:0; }} #lb-next {{ right:0; }}
  #lb-prev:hover, #lb-next:hover {{ opacity:1; }}
  #lb-counter {{ position:fixed; bottom:20px; left:50%; transform:translateX(-50%); color:#aaa; font-size:14px; }}
</style>
</head>
<body>
{imgs_html}
<div id="lightbox" onclick="closeLightbox()">
  <span id="lightbox-close" onclick="closeLightbox()">&#x2715;</span>
  <span id="lb-prev" onclick="event.stopPropagation();navigate(-1)">&#8592;</span>
  <img id="lightbox-img" src="" onclick="event.stopPropagation()">
  <span id="lb-next" onclick="event.stopPropagation();navigate(1)">&#8594;</span>
  <span id="lb-counter"></span>
</div>
<script>
  var imgs = Array.from(document.querySelectorAll('.img-wrap img'));
  var current = 0;
  function openLightbox(idx) {{
    current = idx;
    document.getElementById('lightbox-img').src = imgs[current].src;
    document.getElementById('lb-counter').textContent = (current+1) + ' / ' + imgs.length;
    document.getElementById('lb-prev').style.display = imgs.length > 1 ? 'flex' : 'none';
    document.getElementById('lb-next').style.display = imgs.length > 1 ? 'flex' : 'none';
    document.getElementById('lightbox').classList.add('active');
  }}
  function closeLightbox() {{
    document.getElementById('lightbox').classList.remove('active');
    document.getElementById('lightbox-img').src = '';
  }}
  function navigate(dir) {{
    current = (current + dir + imgs.length) % imgs.length;
    document.getElementById('lightbox-img').src = imgs[current].src;
    document.getElementById('lb-counter').textContent = (current+1) + ' / ' + imgs.length;
  }}
  imgs.forEach(function(img, i) {{ img.onclick = function() {{ openLightbox(i); }}; }});
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') closeLightbox();
    if (e.key === 'ArrowRight') navigate(1);
    if (e.key === 'ArrowLeft') navigate(-1);
  }});
</script>
</body>
</html>"""

@app.route("/api/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return jsonify({"error": "Invalid file type"}), 400
    filename = f"{uuid.uuid4().hex}{ext}"
    f.save(str(UPLOAD_DIR / filename))
    return jsonify({"url": f"/static/uploads/{filename}"})


@app.route("/api/upload-content-doc", methods=["POST"])
def upload_content_doc():
    """Upload a markdown content doc into CONTENT_DIR/content_docs/.
    Used by the Custom Fields → Content Doc field upload button so
    collaborators (e.g. Hamflix) can drop a .md file via the web UI
    instead of needing scp/VPS access."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in (".md", ".markdown", ".txt"):
        return jsonify({"error": "Only .md / .markdown / .txt files allowed"}), 400
    stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", Path(f.filename).stem)[:120].strip("_")
    if not stem:
        stem = uuid.uuid4().hex[:12]
    content_docs_subdir = CONTENT_DIR / "content_docs"
    content_docs_subdir.mkdir(parents=True, exist_ok=True)
    filename = f"{stem}.md"
    filepath = content_docs_subdir / filename
    if filepath.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{stem}_{ts}.md"
        filepath = content_docs_subdir / filename
    f.save(str(filepath))
    return jsonify({"path": f"content_docs/{filename}", "filename": filename})


# ── Video Upload (NAS) + Blotato Publish ─────────────────

NAS_UPLOAD_URL = os.environ.get("NAS_UPLOAD_URL", "https://media.agentflow.net/upload")
NAS_UPLOAD_SECRET = os.environ.get("NAS_UPLOAD_SECRET", "")
NAS_PUBLIC_BASE = os.environ.get("NAS_PUBLIC_BASE", "https://media.agentflow.net")
BLOTATO_API = "https://backend.blotato.com/v2"


def _details_meta(conn, vid):
    row = conn.execute("SELECT meta FROM video_details WHERE video_id = ?", (vid,)).fetchone()
    if not row:
        return {}, False
    try:
        return (json.loads(row["meta"]) if row["meta"] else {}), True
    except (TypeError, ValueError):
        return {}, True


def _save_details_meta(conn, vid, meta):
    payload = json.dumps(meta)
    existing = conn.execute("SELECT id FROM video_details WHERE video_id = ?", (vid,)).fetchone()
    if existing:
        conn.execute("UPDATE video_details SET meta = ? WHERE video_id = ?", (payload, vid))
    else:
        conn.execute(
            "INSERT INTO video_details (video_id, meta) VALUES (?, ?)",
            (vid, payload),
        )


@app.route("/api/videos/<int:vid>/upload-init", methods=["POST"])
def video_upload_init(vid):
    """Return the NAS upload endpoint + secret + folder so the browser can POST direct."""
    if not NAS_UPLOAD_SECRET:
        return jsonify({"error": "NAS_UPLOAD_SECRET not configured"}), 500
    return jsonify({
        "endpoint": NAS_UPLOAD_URL,
        "secret": NAS_UPLOAD_SECRET,
        "folder": "creatorgrowth",
    })


@app.route("/api/videos/<int:vid>/upload-complete", methods=["POST"])
def video_upload_complete(vid):
    data = request.get_json(force=True) or {}
    conn = get_db()
    meta, _ = _details_meta(conn, vid)
    if data.get("clear"):
        meta["video_url"] = ""
        meta["video_size"] = None
        meta["publish_status"] = None
        _save_details_meta(conn, vid, meta)
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "url": ""})
    path = (data.get("path") or "").strip()
    if not path or not path.startswith("/uploads/"):
        return jsonify({"error": "invalid path"}), 400
    public_url = NAS_PUBLIC_BASE.rstrip("/") + path
    meta["video_url"] = public_url
    meta["video_uploaded_at"] = datetime.utcnow().isoformat() + "Z"
    meta["video_size"] = data.get("size")
    meta["publish_status"] = None
    _save_details_meta(conn, vid, meta)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "url": public_url})


def _blotato_post(api_key, body):
    req = Request(
        f"{BLOTATO_API}/posts",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        resp = urlopen(req, timeout=120)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _blotato_upload_media(api_key, video_url):
    req = Request(
        f"{BLOTATO_API}/media",
        data=json.dumps({"url": video_url}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    resp = urlopen(req, timeout=300)
    return json.loads(resp.read())


@app.route("/api/videos/<int:vid>/publish-blotato", methods=["POST"])
def video_publish_blotato(vid):
    api_key = os.environ.get("BLOTATO_API_KEY", "")
    if not api_key:
        return jsonify({"error": "BLOTATO_API_KEY not configured"}), 500

    conn = get_db()
    meta, _ = _details_meta(conn, vid)
    video_url = meta.get("video_url", "")
    if not video_url:
        conn.close()
        return jsonify({"error": "no video uploaded yet"}), 400

    # Pull title + caption from request or fall back to video title
    body = request.get_json(silent=True) or {}
    caption = (body.get("caption") or "").strip()
    title = (body.get("title") or "").strip()
    if not title:
        v = conn.execute("SELECT title FROM videos WHERE id = ?", (vid,)).fetchone()
        title = (v["title"] if v else "") or ""
    if not caption:
        caption = title

    # Upload to Blotato media
    try:
        media = _blotato_upload_media(api_key, video_url)
    except Exception as e:
        conn.close()
        return jsonify({"error": f"blotato media upload failed: {e}"}), 502
    media_url = media.get("url", "")
    if not media_url:
        conn.close()
        return jsonify({"error": "blotato did not return media url", "raw": media}), 502

    # Per-platform publish — only platforms with an account ID configured
    accounts = {
        "youtube":   os.environ.get("BLOTATO_YOUTUBE_ACCOUNT_ID", ""),
        "tiktok":    os.environ.get("BLOTATO_TIKTOK_ACCOUNT_ID", ""),
        "facebook":  os.environ.get("BLOTATO_FACEBOOK_ACCOUNT_ID", ""),
        "instagram": os.environ.get("BLOTATO_INSTAGRAM_ACCOUNT_ID", ""),
        "linkedin":  os.environ.get("BLOTATO_LINKEDIN_ACCOUNT_ID", ""),
        "twitter":   os.environ.get("BLOTATO_X_ACCOUNT_ID", ""),
        "pinterest": os.environ.get("BLOTATO_PINTEREST_ACCOUNT_ID", ""),
        "bluesky":   os.environ.get("BLOTATO_BLUESKY_ACCOUNT_ID", ""),
        "threads":   os.environ.get("BLOTATO_THREADS_ACCOUNT_ID", ""),
    }
    fb_page_id = os.environ.get("BLOTATO_FACEBOOK_PAGE_ID", "")
    pin_board_id = os.environ.get("BLOTATO_PINTEREST_BOARD_ID", "")

    privacy = (body.get("privacy") or "private").lower()
    if privacy not in ("public", "private", "unlisted"):
        privacy = "private"
    targets = {
        "youtube":   {"targetType": "youtube", "title": title, "privacyStatus": privacy, "shouldNotifySubscribers": False},
        "tiktok":    {"targetType": "tiktok", "privacyLevel": "PUBLIC_TO_EVERYONE", "disabledComments": False, "disabledDuet": False, "disabledStitch": False, "isBrandedContent": False, "isYourBrand": False, "isAiGenerated": True},
        "facebook":  {"targetType": "facebook", "pageId": fb_page_id} if fb_page_id else None,
        "instagram": {"targetType": "instagram"},
        "linkedin":  {"targetType": "linkedin"},
        "twitter":   {"targetType": "twitter"},
        "pinterest": {"targetType": "pinterest", "boardId": pin_board_id} if pin_board_id else None,
        "bluesky":   {"targetType": "bluesky"},
        "threads":   {"targetType": "threads"},
    }

    results = {}
    for platform, account_id in accounts.items():
        if not account_id:
            results[platform] = {"status": "skipped", "reason": "no account id"}
            continue
        target = targets.get(platform)
        if target is None:
            results[platform] = {"status": "skipped", "reason": "missing config (page/board)"}
            continue
        text = caption + (" #short" if platform == "youtube" else "")
        resp = _blotato_post(api_key, {"post": {
            "accountId": account_id,
            "content": {"text": text, "mediaUrls": [media_url], "platform": platform},
            "target": target,
        }})
        if "postSubmissionId" in resp:
            # Submission accepted; actual posting is async on Blotato's side.
            # Caller should poll /publish-status to resolve.
            results[platform] = {"status": "processing", "id": resp["postSubmissionId"]}
        else:
            results[platform] = {"status": "fail", "raw": resp}

    meta["publish_status"] = results
    meta["published_at"] = datetime.utcnow().isoformat() + "Z"
    meta["blotato_media_url"] = media_url
    _save_details_meta(conn, vid, meta)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "results": results, "media_url": media_url})


@app.route("/api/videos/<int:vid>/publish-status", methods=["POST"])
def video_publish_status(vid):
    """Re-poll Blotato for each platform that returned a postSubmissionId, update DB."""
    api_key = os.environ.get("BLOTATO_API_KEY", "")
    if not api_key:
        return jsonify({"error": "BLOTATO_API_KEY not configured"}), 500

    conn = get_db()
    meta, _ = _details_meta(conn, vid)
    results = meta.get("publish_status") or {}
    if not isinstance(results, dict):
        conn.close()
        return jsonify({"error": "no publish history"}), 400

    updated = False
    for platform, info in list(results.items()):
        if not isinstance(info, dict):
            continue
        if info.get("status") not in ("ok", "submitted", "processing"):
            continue
        sub_id = info.get("id")
        if not sub_id:
            continue
        req = Request(
            f"{BLOTATO_API}/posts/{sub_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            resp = urlopen(req, timeout=30)
            data = json.loads(resp.read())
        except Exception as e:
            results[platform] = {**info, "poll_error": str(e)}
            continue
        status = (data.get("status") or "").lower()
        post_url = data.get("publicUrl") or data.get("postUrl")
        if status in ("published", "succeeded", "completed"):
            results[platform] = {"status": "ok", "id": sub_id, "post_url": post_url}
            updated = True
        elif status == "failed":
            results[platform] = {"status": "fail", "id": sub_id, "error": data.get("errorMessage", "unknown")}
            updated = True
        elif status in ("processing", "queued", "pending", "submitted"):
            results[platform] = {"status": "processing", "id": sub_id}
        else:
            results[platform] = {"status": status or "unknown", "id": sub_id, "raw": data}
            updated = True

    if updated:
        meta["publish_status"] = results
        _save_details_meta(conn, vid, meta)
        conn.commit()
    conn.close()
    return jsonify({"ok": True, "results": results})


# ── Keywords ──────────────────────────────────────────────

@app.route("/api/keywords", methods=["GET"])
def list_keywords():
    filter_type = request.args.get("filter", "all")  # all, favorites, youtube
    conn = get_db()
    if filter_type == "favorites":
        rows = conn.execute("SELECT * FROM keywords WHERE is_favorite = 1 ORDER BY search_volume DESC").fetchall()
    elif filter_type == "youtube":
        rows = conn.execute("SELECT * FROM keywords WHERE is_youtube = 1 ORDER BY search_volume DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM keywords ORDER BY search_volume DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/keywords/<int:kid>/favorite", methods=["POST"])
def toggle_favorite(kid):
    conn = get_db()
    row = conn.execute("SELECT is_favorite FROM keywords WHERE id = ?", (kid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    new_val = 0 if row["is_favorite"] else 1
    conn.execute("UPDATE keywords SET is_favorite = ? WHERE id = ?", (new_val, kid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "is_favorite": new_val})


@app.route("/api/keywords/<int:kid>/youtube", methods=["POST"])
def toggle_youtube(kid):
    conn = get_db()
    row = conn.execute("SELECT is_youtube FROM keywords WHERE id = ?", (kid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    new_val = 0 if row["is_youtube"] else 1
    conn.execute("UPDATE keywords SET is_youtube = ? WHERE id = ?", (new_val, kid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "is_youtube": new_val})


@app.route("/api/keywords/<int:kid>", methods=["DELETE"])
def delete_keyword(kid):
    conn = get_db()
    conn.execute("DELETE FROM keywords WHERE id = ?", (kid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/keywords/import-airtable", methods=["POST"])
def import_keywords_airtable():
    """Import keywords from the Airtable imported table."""
    import urllib.request
    token = os.environ.get("AIRTABLE_TOKEN", "")
    if not token:
        return jsonify({"error": "AIRTABLE_TOKEN not configured"}), 500
    base_id = "appghTjP5qs7AyO4z"
    table_id = "tblFlmtj4GgpS51GF"
    all_records = []
    offset = None
    while True:
        url = f"https://api.airtable.com/v0/{base_id}/{table_id}"
        if offset:
            url += f"?offset={offset}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        all_records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    conn = get_db()
    inserted = skipped = 0
    for r in all_records:
        f = r.get("fields", {})
        keyword = f.get("\ufeffKeyword") or f.get("Keyword", "")
        if not keyword:
            continue
        try:
            conn.execute(
                """INSERT INTO keywords (keyword, search_volume, competition, overall, searches_30d, word_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (keyword.strip(), int(f.get("Search volume", 0)), round(f.get("Competition", 0), 1),
                 round(f.get("Overall", 0), 1), int(f.get("30d ago searches", 0)),
                 int(f.get("Number of words", 0))),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            skipped += 1
    conn.commit()
    conn.close()
    return jsonify({"inserted": inserted, "skipped": skipped, "total": len(all_records)})



# ── Twitter routes ─────────────────────────────────────────────────────────────

@app.route("/api/twitter/post", methods=["POST"])
@login_required
def twitter_post():
    data = request.get_json()
    text = (data or {}).get("text", "").strip()
    media_url = (data or {}).get("media_url", "").strip()
    thread_parts = (data or {}).get("thread", [])  # list of {text, media_url}

    if not text:
        return jsonify({"error": "No text provided"}), 400
    if len(text) > 280:
        return jsonify({"error": "Tweet exceeds 280 characters"}), 400
    if not BLOTATO_API_KEY:
        return jsonify({"error": "BLOTATO_API_KEY not configured"}), 500

    # Build post payload
    def to_full_url(u):
        return "https://creatorgrowth.com" + u if u and u.startswith("/") else (u or "")

    media_urls = [to_full_url(media_url)] if media_url else []

    post_obj = {
        "accountId": BLOTATO_X_ACCOUNT_ID,
        "content": {
            "platform": "twitter",
            "text": text,
            "mediaUrls": media_urls
        },
        "target": {
            "targetType": "twitter"
        }
    }

    # Thread support
    if thread_parts:
        thread_items = []
        for part in thread_parts:
            t = part.get("text", "").strip()
            if t:
                mu = part.get("media_url", "")
                thread_items.append({
                    "text": t,
                    "mediaUrls": [to_full_url(mu)] if mu else []
                })
        if thread_items:
            post_obj["content"]["thread"] = thread_items

    payload = json.dumps({"post": post_obj}).encode()

    req = Request(
        "https://backend.blotato.com/v2/posts",
        data=payload,
        headers={
            "blotato-api-key": BLOTATO_API_KEY,
            "Content-Type": "application/json"
        },
        method="POST"
    )
    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        blotato_id = str(result.get("id", ""))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    conn = get_db()
    conn.execute(
        "INSERT INTO tweets (text, blotato_id, media_url, thread_json) VALUES (?, ?, ?, ?)",
        (text, blotato_id, media_url, json.dumps(thread_parts))
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "blotato_id": blotato_id})


@app.route("/api/twitter/feed", methods=["GET"])
@login_required
def twitter_feed():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, text, posted_at, blotato_id, media_url, thread_json FROM tweets ORDER BY id DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


def _ring_median_color(img, x, y, w, h, pad=8, ring=24):
    """Median RGB of pixels in an annulus around the box (pad outside, ring wide).
    Median resists outliers like the bright text/borders adjacent to box edges that
    would otherwise pull a mean toward foreground colors."""
    px = img.load()
    W, H = img.size
    ox1 = max(0, x - pad - ring); oy1 = max(0, y - pad - ring)
    ox2 = min(W, x + w + pad + ring); oy2 = min(H, y + h + pad + ring)
    ix1 = max(0, x - pad); iy1 = max(0, y - pad)
    ix2 = min(W, x + w + pad); iy2 = min(H, y + h + pad)
    rs = []; gs = []; bs = []
    for yy in range(oy1, oy2):
        in_inner_y = iy1 <= yy < iy2
        for xx in range(ox1, ox2):
            if in_inner_y and ix1 <= xx < ix2:
                continue
            p = px[xx, yy]
            rs.append(p[0]); gs.append(p[1]); bs.append(p[2])
    if not rs:
        return (8, 8, 18)
    rs.sort(); gs.sort(); bs.sort()
    m = len(rs) // 2
    return (rs[m], gs[m], bs[m])


def _despeckle_stars(crop, bg_color, luma_threshold=110, radius=3, dark_ratio=0.80):
    """Remove isolated bright pixels (stars) from a box crop. A pixel is considered
    a 'star' if it's brighter than luma_threshold AND most of its neighbors are dark.
    Replaces star pixels with bg_color. Used for slideshow slides where the crop is
    scaled up — stars would otherwise become very visible dots."""
    W, H = crop.size
    src = crop.load()
    out = crop.copy()
    dst = out.load()
    # Build a luma map first (faster than recomputing for each pixel's neighbors)
    luma = [[0] * H for _ in range(W)]
    for y in range(H):
        for x in range(W):
            p = src[x, y]
            luma[x][y] = 0.3 * p[0] + 0.6 * p[1] + 0.1 * p[2]
    for y in range(H):
        for x in range(W):
            if luma[x][y] < luma_threshold:
                continue
            dark = 0; total = 0
            for dy in range(-radius, radius + 1):
                ny = y + dy
                if ny < 0 or ny >= H: continue
                for dx in range(-radius, radius + 1):
                    if dx == 0 and dy == 0: continue
                    nx = x + dx
                    if nx < 0 or nx >= W: continue
                    if luma[nx][ny] < 60:
                        dark += 1
                    total += 1
            if total > 0 and (dark / total) >= dark_ratio:
                dst[x, y] = bg_color
    return out


def _apply_alpha_mask(crop_rgba, mask_l):
    """Multiply the crop's alpha by an L-mode mask of the same size. Anything black
    in the mask becomes transparent in the crop."""
    from PIL import ImageChops as _IC
    if mask_l is None:
        return crop_rgba
    if crop_rgba.mode != "RGBA":
        crop_rgba = crop_rgba.convert("RGBA")
    if mask_l.size != crop_rgba.size:
        return crop_rgba
    existing_alpha = crop_rgba.split()[3]
    new_alpha = _IC.multiply(existing_alpha, mask_l)
    crop_rgba.putalpha(new_alpha)
    return crop_rgba


def _build_shape_mask(W, H, x, y, shapes_px):
    """Build an L-mode mask of size (W, H) representing the union of shapes_px.
    Each shape is a dict in pixel coords (relative to image origin, NOT bbox).
    The mask is drawn in bbox-local coords (subtract x, y)."""
    from PIL import ImageDraw as _ID
    mask = Image.new("L", (W, H), 0)
    draw = _ID.Draw(mask)
    for s in shapes_px:
        t = s.get("type")
        if t == "poly":
            pts = s.get("points") or []
            if len(pts) >= 3:
                local = [(px - x, py - y) for (px, py) in pts]
                draw.polygon(local, fill=255)
        else:  # rect
            sx = s.get("x", 0); sy = s.get("y", 0)
            sw = s.get("w", 0); sh = s.get("h", 0)
            lx = sx - x; ly = sy - y
            draw.rectangle([lx, ly, lx + sw - 1, ly + sh - 1], fill=255)
    return mask


def _corner_bg_color(img, patch=40):
    """Median RGB sampled from the 4 corner patches — global background guess."""
    px = img.load()
    W, H = img.size
    rs = []; gs = []; bs = []
    for (cx, cy) in ((0, 0), (W - patch, 0), (0, H - patch), (W - patch, H - patch)):
        for yy in range(max(0, cy), min(H, cy + patch)):
            for xx in range(max(0, cx), min(W, cx + patch)):
                p = px[xx, yy]
                rs.append(p[0]); gs.append(p[1]); bs.append(p[2])
    if not rs:
        return (8, 8, 18)
    rs.sort(); gs.sort(); bs.sort()
    m = len(rs) // 2
    return (rs[m], gs[m], bs[m])


REPLICATE_FAST_WHISPER_VERSION = "3ab86df6c8f54c11309d4d1f930ac292bad43ace52d10c80d87eb258b3c9f79c"

def _replicate_whisper_words(audio_bytes, audio_mime):
    """Word-level timestamps via vaibhavs10/incredibly-fast-whisper on Replicate.
    Returns list of {word, start, end}. None on any failure."""
    import base64, time
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
    token = os.environ.get("REPLICATE_API_TOKEN", "")
    if not token:
        app.logger.warning("diagrams: REPLICATE_API_TOKEN not set")
        return None
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    data_uri = f"data:{audio_mime or 'audio/wav'};base64,{audio_b64}"
    body = {
        "version": REPLICATE_FAST_WHISPER_VERSION,
        "input": {
            "audio": data_uri,
            "task": "transcribe",
            "timestamp": "word",
            "batch_size": 24,
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Prefer": "wait=60",
    }
    req = Request("https://api.replicate.com/v1/predictions",
                  data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        app.logger.warning(f"diagrams: replicate http {e.code}: {e.read().decode('utf-8','ignore')[:300]}")
        return None
    except URLError as e:
        app.logger.warning(f"diagrams: replicate network: {e.reason}")
        return None

    poll_url = (data.get("urls") or {}).get("get")
    deadline = time.time() + 90
    while data.get("status") not in ("succeeded", "failed", "canceled") and poll_url and time.time() < deadline:
        time.sleep(1)
        try:
            r2 = Request(poll_url, headers={"Authorization": f"Bearer {token}"})
            with urlopen(r2, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError):
            return None

    if data.get("status") != "succeeded":
        app.logger.warning(f"diagrams: replicate final status {data.get('status')}: {str(data.get('error'))[:200]}")
        return None
    output = data.get("output") or {}
    chunks = output.get("chunks") or []
    words = []
    for c in chunks:
        ts = c.get("timestamp") or []
        text = (c.get("text") or "").strip()
        if not text or len(ts) < 2 or ts[0] is None:
            continue
        start = float(ts[0])
        end = float(ts[1]) if ts[1] is not None else start + 0.3
        words.append({"word": text, "start": start, "end": end})
    return words if words else None


def _gemini_match_boxes_to_words(image_with_boxes_bytes, words, n_boxes):
    """Ask Gemini which word-index in `words` each numbered box's phrase begins at.
    Returns {times: [float|None], descs: [str|None]} indexed by box."""
    import base64
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None
    img_b64 = base64.b64encode(image_with_boxes_bytes).decode("ascii")
    # Compact numbered transcript: [idx]word
    numbered_words = " ".join(f"[{i}]{w['word']}" for i, w in enumerate(words))
    prompt = (
        f"You are aligning visual reveals to a narrated transcript with word-level timing. "
        f"This drives a video where each numbered box appears the instant its KEY descriptive word is spoken.\n\n"
        f"The image has {n_boxes} gold-outlined boxes numbered 1..{n_boxes}. Each covers a visual element.\n\n"
        f"Below is the transcript with each word prefixed by its position [N]:\n{numbered_words}\n\n"
        f"For each numbered box (in order 1..{n_boxes}):\n"
        f"  1. Read the visible TEXT inside the box (use OCR — the boxes contain pixel-art text like "
        f"'COLD OPEN', 'CHAPTER CUT', 'BACKGROUND', subtitles like 'full 6 seconds', 'trimmed to 3 seconds', "
        f"'slowed down in post', etc.). Also note any icon or shape.\n"
        f"  2. Pick the SINGLE word position [N] of the EARLIEST descriptive verb/adjective in the transcript "
        f"that directly describes the box's content. Prefer ACTION VERBS and DISTINCTIVE adjectives over "
        f"the literal noun. Examples:\n"
        f"     - Box about 'CHAPTER CUT — trimmed to 3 seconds' → pick 'trimmed' (verb), NOT 'chapter' (noun later).\n"
        f"     - Box about 'BACKGROUND — slowed down in post' → pick 'slowed' (verb), NOT 'background' (noun later).\n"
        f"     - Box about 'COLD OPEN — full 6 seconds' → pick 'cold' (adjective opening the phrase).\n"
        f"     - Box about 'THE GENERATED CLIP — 6 seconds' → pick 'six' or 'loop' (the descriptor used in audio).\n"
        f"     - Boxes containing a title/heading that's NOT spoken in audio → pick the first word of the audio "
        f"(position 0) for the opening title, or the final phrase for a concluding tagline.\n"
        f"  3. NEVER pick filler words: 'as', 'a', 'the', 'in', 'of', 'and', 'or', 'to', 'for'. Skip these.\n"
        f"  4. Box order must be strictly increasing (box 1's word_index < box 2's < ...). If your picks "
        f"violate this, adjust later boxes forward.\n\n"
        f"Return ONLY valid JSON, no markdown:\n"
        f'{{"boxes":[{{"box":1,"describes":"<short>","key_word":"<the word you picked>","start_word":3}},'
        f'{{"box":2,"describes":"...","key_word":"...","start_word":12}}]}}'
    )
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/png", "data": img_b64}},
        ]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.2},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    req = Request(url, data=json.dumps(body).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        entries = parsed.get("boxes", []) if isinstance(parsed, dict) else []
    except (HTTPError, URLError, KeyError, IndexError, ValueError, TypeError):
        return None
    times = [None] * n_boxes
    descs = [None] * n_boxes
    for e in entries:
        try:
            i = int(e.get("box", 0)) - 1
            widx = int(e.get("start_word", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= i < n_boxes and 0 <= widx < len(words):
            times[i] = float(words[widx]["start"])
            descs[i] = (e.get("describes") or "")[:80]
    return {"times": times, "descs": descs}


def _gemini_align_boxes(image_with_boxes_bytes, audio_bytes, audio_mime, transcript, n_boxes, duration):
    """Ask Gemini 2.5 Flash to assign a reveal time (s) to each numbered box,
    based on the image, audio, and transcript. Returns list[float|None] of length n_boxes."""
    import base64
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None
    img_b64 = base64.b64encode(image_with_boxes_bytes).decode("ascii")
    aud_b64 = base64.b64encode(audio_bytes).decode("ascii")
    prompt = (
        "You are aligning visual reveals to spoken audio for a diagram animation.\n\n"
        f"The image shows a diagram with {n_boxes} gold-outlined boxes, each labeled with a number 1..{n_boxes} on a gold circle. "
        "Each box covers a distinct visual element (a title, a card, a label, etc.).\n\n"
        f"The audio is the narration that plays over the diagram. Total duration: {duration:.2f} seconds.\n\n"
        f"Transcript (may have minor errors): {transcript or '(no transcript provided)'}\n\n"
        f"For each numbered box (in order 1..{n_boxes}), identify which visual element it covers, "
        "then determine the moment in seconds when that element is verbally referenced or becomes thematically relevant in the audio. "
        "Constraints: every time must be >= 0.3 and <= duration; times must be strictly increasing in box order "
        "(box 1 first, box N last); if an element is not directly mentioned, place it at a natural pause near a related phrase.\n\n"
        "Return ONLY a single valid JSON object — no markdown fences, no preamble — in this exact shape:\n"
        '{"boxes":[{"box":1,"describes":"<short element description>","time":0.5},'
        '{"box":2,"describes":"...","time":2.1}]}'
    )
    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": img_b64}},
                {"inline_data": {"mime_type": audio_mime, "data": aud_b64}},
            ]
        }],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.2}
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    req = Request(url, data=json.dumps(body).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        entries = parsed.get("boxes", []) if isinstance(parsed, dict) else []
        out = [None] * n_boxes
        descs = [None] * n_boxes
        for e in entries:
            try:
                idx = int(e.get("box", 0)) - 1
                t = float(e.get("time", 0))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < n_boxes and 0.0 < t <= duration:
                out[idx] = t
                descs[idx] = (e.get("describes") or "")[:80]
        return {"times": out, "descs": descs}
    except (HTTPError, URLError, KeyError, IndexError, ValueError, TypeError):
        return None


# ── Diagrams: persistence ────────────────────────────────────────────────

def _diagrams_dir():
    d = Path(app.root_path) / "static" / "uploads" / "diagrams"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _diagram_dir(video_id, diagram_id):
    d = _diagrams_dir() / str(video_id) / diagram_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _diagram_row_to_dict(row):
    if not row:
        return None
    d = dict(row)
    try:
        d["boxes"] = json.loads(d.pop("boxes_json") or "[]")
    except Exception:
        d["boxes"] = []
    rm = d.pop("result_meta_json", None)
    try:
        d["result_meta"] = json.loads(rm) if rm else None
    except Exception:
        d["result_meta"] = None
    # expose URLs (paths are stored relative to /app, e.g. "static/uploads/diagrams/..")
    for k in ("image_path", "audio_path"):
        v = d.get(k)
        if v and not v.startswith("/"):
            d[k + "_url"] = "/" + v
        else:
            d[k + "_url"] = v
    return d


@app.route("/api/videos/<int:vid>/diagrams", methods=["GET"])
def diagrams_list(vid):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM diagrams WHERE video_id=? ORDER BY position, created_at",
        (vid,)
    ).fetchall()
    conn.close()
    return jsonify([_diagram_row_to_dict(r) for r in rows])


@app.route("/api/videos/<int:vid>/diagrams", methods=["POST"])
def diagrams_create(vid):
    diagram_id = "d" + uuid.uuid4().hex[:14]
    name = (request.json or {}).get("name") if request.is_json else None
    conn = get_db()
    pos_row = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM diagrams WHERE video_id=?", (vid,)).fetchone()
    pos = pos_row[0] if pos_row else 0
    if not name:
        name = f"Diagram {pos + 1}"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO diagrams (id, video_id, name, boxes_json, position, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (diagram_id, vid, name, "[]", pos, now, now)
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM diagrams WHERE id=?", (diagram_id,)).fetchone()
    conn.close()
    return jsonify(_diagram_row_to_dict(row))


@app.route("/api/diagrams/<diagram_id>", methods=["GET"])
def diagrams_get(diagram_id):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM diagrams WHERE id=?", (diagram_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(_diagram_row_to_dict(row))


@app.route("/api/diagrams/<diagram_id>", methods=["PATCH"])
def diagrams_patch(diagram_id):
    payload = request.get_json(force=True, silent=True) or {}
    fields = []
    values = []
    if "name" in payload:
        fields.append("name=?"); values.append(payload["name"])
    if "boxes" in payload:
        fields.append("boxes_json=?"); values.append(json.dumps(payload["boxes"]))
    if "script" in payload:
        fields.append("script=?"); values.append(payload["script"])
    if "mode" in payload:
        mode_v = (payload.get("mode") or "reveal").lower()
        if mode_v not in ("reveal", "slideshow"):
            mode_v = "reveal"
        fields.append("mode=?"); values.append(mode_v)
    if "bg_color_override" in payload:
        raw = payload.get("bg_color_override")
        if raw is None or raw == "":
            fields.append("bg_color_override=?"); values.append(None)
        else:
            s = str(raw).strip()
            if not (s.startswith("#") and len(s) == 7 and all(c in "0123456789abcdefABCDEF" for c in s[1:])):
                return jsonify({"error": "bg_color_override must be a #RRGGBB hex string or empty"}), 400
            fields.append("bg_color_override=?"); values.append(s.lower())
    if not fields:
        return jsonify({"error": "no fields to update"}), 400
    fields.append("updated_at=?")
    values.append(datetime.now(timezone.utc).isoformat())
    values.append(diagram_id)
    conn = get_db()
    conn.execute(f"UPDATE diagrams SET {', '.join(fields)} WHERE id=?", values)
    conn.commit()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM diagrams WHERE id=?", (diagram_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(_diagram_row_to_dict(row))


@app.route("/api/diagrams/<diagram_id>", methods=["DELETE"])
def diagrams_delete(diagram_id):
    import shutil
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT video_id FROM diagrams WHERE id=?", (diagram_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    vid = row["video_id"]
    conn.execute("DELETE FROM diagrams WHERE id=?", (diagram_id,))
    conn.commit()
    conn.close()
    asset_dir = _diagrams_dir() / str(vid) / diagram_id
    try:
        if asset_dir.exists():
            shutil.rmtree(asset_dir)
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/diagrams/<diagram_id>/image", methods=["POST"])
def diagrams_upload_image(diagram_id):
    f = request.files.get("image")
    if not f:
        return jsonify({"error": "no image file"}), 400
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT video_id, image_path FROM diagrams WHERE id=?", (diagram_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    vid = row["video_id"]
    # remove old image
    if row["image_path"]:
        old = Path(app.root_path) / row["image_path"]
        try:
            if old.exists(): old.unlink()
        except Exception: pass
    ext = (f.filename.rsplit(".", 1)[-1] if f.filename and "." in f.filename else "png").lower()
    if ext not in ("png", "jpg", "jpeg", "webp", "gif", "bmp"):
        ext = "png"
    target = _diagram_dir(vid, diagram_id) / f"image.{ext}"
    f.save(str(target))
    rel = str(target.relative_to(Path(app.root_path)))
    # Auto-detect a default canvas/background color from the saved image. Stored so
    # render and the studio UI agree on the "auto" pick. User can override via the
    # eyedropper / hex input (-> bg_color_override).
    bg_auto_hex = None
    try:
        with Image.open(target) as _bg_img:
            _bg_rgb = _bg_img.convert("RGB")
            r, g, b = _corner_bg_color(_bg_rgb, patch=40)
            bg_auto_hex = "#{:02x}{:02x}{:02x}".format(int(r), int(g), int(b))
    except Exception as e:
        app.logger.warning("diagrams: bg color auto-detect failed: %s", e)
    conn.execute(
        "UPDATE diagrams SET image_path=?, bg_color_auto=?, updated_at=? WHERE id=?",
        (rel, bg_auto_hex, datetime.now(timezone.utc).isoformat(), diagram_id),
    )
    conn.commit()
    conn.close()
    return jsonify({
        "image_path": rel,
        "image_path_url": "/" + rel,
        "bg_color_auto": bg_auto_hex,
    })


@app.route("/api/diagrams/<diagram_id>/audio", methods=["POST"])
def diagrams_upload_audio(diagram_id):
    f = request.files.get("audio")
    if not f:
        return jsonify({"error": "no audio file"}), 400
    duration = request.form.get("duration", "")
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT video_id, audio_path FROM diagrams WHERE id=?", (diagram_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404
    vid = row["video_id"]
    if row["audio_path"]:
        old = Path(app.root_path) / row["audio_path"]
        try:
            if old.exists(): old.unlink()
        except Exception: pass
    ext = (f.filename.rsplit(".", 1)[-1] if f.filename and "." in f.filename else "mp3").lower()
    if ext not in ("mp3", "wav", "m4a", "aac", "ogg", "flac"):
        ext = "mp3"
    target = _diagram_dir(vid, diagram_id) / f"audio.{ext}"
    f.save(str(target))
    rel = str(target.relative_to(Path(app.root_path)))
    conn.execute("UPDATE diagrams SET audio_path=?, audio_name=?, audio_duration=?, updated_at=? WHERE id=?",
                 (rel, f.filename or "audio", duration, datetime.now(timezone.utc).isoformat(), diagram_id))
    conn.commit()
    conn.close()
    return jsonify({"audio_path": rel, "audio_path_url": "/" + rel, "audio_name": f.filename, "audio_duration": duration})


def _render_write_on_frames(frames_dir, crop_img, bw, bh, reveal_dur, fps=30):
    """Pre-render PNG frames for a 'write-on' wipe: alpha mask sweeps left → right
    over reveal_dur seconds, with a soft 3%-of-width feather at the leading edge.
    Returns (frames_dir, n_frames)."""
    from PIL import Image as _Image, ImageDraw as _ImageDraw, ImageChops as _ImageChops
    n_frames = max(2, int(round(reveal_dur * fps)))
    frames_dir.mkdir(parents=True, exist_ok=True)
    base = crop_img.convert("RGBA")
    soft = max(6, int(bw * 0.03))
    src_alpha = base.split()[3]
    for i in range(n_frames):
        p = i / max(1, n_frames - 1)
        ease = p * p * (3 - 2 * p)
        mask_x = int(bw * ease)
        # Build a column mask: 255 left of mask_x-soft, gradient down to 0 at mask_x, 0 beyond.
        col_mask = _Image.new("L", (bw, bh), 0)
        d = _ImageDraw.Draw(col_mask)
        if mask_x - soft > 0:
            d.rectangle([0, 0, mask_x - soft - 1, bh - 1], fill=255)
        for x in range(max(0, mask_x - soft), min(bw, mask_x)):
            v = int(round(255 * (mask_x - x) / soft))
            d.line([(x, 0), (x, bh - 1)], fill=max(0, min(255, v)))
        # Combine column mask with the source alpha
        new_alpha = _ImageChops.multiply(src_alpha, col_mask)
        frame = base.copy()
        frame.putalpha(new_alpha)
        frame.save(frames_dir / f"f{i:04d}.png")
    return frames_dir, n_frames


def _render_zoom_frames(frames_dir, crop_img, bw, bh, anim, zoom_dur, fade_dur, fps=30):
    """Pre-render PNG frames for a zoom intro animation. Each frame is a (bw, bh)
    RGBA canvas with the box content scaled per smoothstep ease + alpha fade baked in.
    Returns (frames_dir, n_frames). ffmpeg consumes via image2 input."""
    from PIL import Image as _Image
    start_s = 0.5 if anim == "zoom_in" else 1.3
    end_s = 1.0
    n_frames = max(2, int(round(zoom_dur * fps)))
    frames_dir.mkdir(parents=True, exist_ok=True)
    base = crop_img.convert("RGBA")
    for i in range(n_frames):
        p = i / max(1, n_frames - 1)
        # smootherstep: zero first + second derivative at ends → ultra-soft onset/offset
        ease = p * p * p * (p * (6 * p - 15) + 10)
        s = start_s + (end_s - start_s) * ease
        nw = max(1, int(round(bw * s)))
        nh = max(1, int(round(bh * s)))
        scaled = base.resize((nw, nh), _Image.LANCZOS)
        canvas = _Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        canvas.paste(scaled, ((bw - nw) // 2, (bh - nh) // 2), scaled)
        # Bake the alpha fade-in into the frame
        frame_t = i / fps
        a = min(1.0, frame_t / fade_dur) if fade_dur > 0 else 1.0
        if a < 1.0:
            alpha = canvas.split()[3].point(lambda v, mult=a: int(v * mult))
            canvas.putalpha(alpha)
        canvas.save(frames_dir / f"f{i:04d}.png")
    return frames_dir, n_frames


@app.route("/api/diagrams/render", methods=["POST"])
def diagrams_render():
    """Render image + boxes + audio -> MP4 with timed fade-in reveals.
    Box `anim` field controls each box's entrance: fade, slide directions, zoom_in/out,
    or `hide` (the box stays masked permanently — no reveal).
    Accepts either:
      - form field `diagram_id` (uses persisted image+audio+boxes+script), or
      - inline files (legacy): image, audio, boxes JSON, script."""
    import tempfile, subprocess, shutil
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    diagram_id = request.form.get("diagram_id", "").strip()
    script_input = request.form.get("script", "")
    job_id = uuid.uuid4().hex[:12]
    work_dir = Path(tempfile.mkdtemp(prefix=f"diagrams_{job_id}_"))

    # Resolve inputs from either persisted diagram or inline files
    diagram_row = None
    if diagram_id:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        diagram_row = conn.execute("SELECT * FROM diagrams WHERE id=?", (diagram_id,)).fetchone()
        conn.close()
        if not diagram_row:
            return jsonify({"error": "diagram not found"}), 404

    if diagram_row:
        # Pull from persisted diagram
        if not diagram_row["image_path"] or not diagram_row["audio_path"]:
            return jsonify({"error": "diagram is missing image or audio"}), 400
        src_image = Path(app.root_path) / diagram_row["image_path"]
        src_audio = Path(app.root_path) / diagram_row["audio_path"]
        if not src_image.exists() or not src_audio.exists():
            return jsonify({"error": "stored image/audio file missing on disk"}), 500
        try:
            boxes = json.loads(diagram_row["boxes_json"] or "[]")
        except Exception:
            boxes = []
        if not boxes:
            return jsonify({"error": "diagram has no boxes drawn"}), 400
        if not script_input:
            script_input = diagram_row["script"] or ""
        audio_mime_hint = None  # ffprobe will figure it out
    else:
        # Legacy inline upload
        image_file = request.files.get("image")
        audio_file = request.files.get("audio")
        boxes_json = request.form.get("boxes", "[]")
        if not image_file or not audio_file:
            return jsonify({"error": "image and audio required (or pass diagram_id)"}), 400
        try:
            boxes = json.loads(boxes_json)
        except Exception:
            return jsonify({"error": "invalid boxes JSON"}), 400
        if not boxes:
            return jsonify({"error": "at least one box required"}), 400
        src_image = work_dir / "_inline_image"
        image_file.save(str(src_image))
        a_ext_inline = (audio_file.filename.rsplit(".", 1)[-1] if audio_file.filename and "." in audio_file.filename else "mp3").lower()
        if a_ext_inline not in ("mp3", "wav", "m4a", "aac", "ogg", "flac"):
            a_ext_inline = "mp3"
        src_audio = work_dir / f"_inline_audio.{a_ext_inline}"
        audio_file.save(str(src_audio))
        audio_mime_hint = audio_file.mimetype

    try:
        # Save + normalize image
        img = Image.open(src_image).convert("RGB")
        W, H = img.size
        W = W - (W % 2)
        H = H - (H % 2)
        if W < 64 or H < 64:
            return jsonify({"error": "image too small (min 64x64)"}), 400
        img = img.resize((W, H))
        image_path = work_dir / "input.png"
        img.save(image_path)

        # Copy audio into work dir
        a_ext = src_audio.suffix.lstrip(".").lower() or "mp3"
        if a_ext not in ("mp3", "wav", "m4a", "aac", "ogg", "flac"):
            a_ext = "mp3"
        audio_path = work_dir / f"audio.{a_ext}"
        shutil.copy(str(src_audio), str(audio_path))

        # Probe audio duration
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True, timeout=30,
        )
        try:
            duration = float(probe.stdout.strip())
        except ValueError:
            return jsonify({"error": "ffprobe could not read audio duration"}), 500
        if duration <= 0.1 or duration > 600:
            return jsonify({"error": f"audio duration out of range ({duration:.1f}s, max 600s)"}), 400

        # Compute box rects + kind (reveal vs hide) + anim style + time_override.
        # Each record: (x, y, w, h, anim, original_box_dict, mask_L_or_None).
        # Box variants supported:
        #   - legacy rect:  {x, y, w, h, anim}
        #   - legacy poly:  {type: 'poly', points, anim}
        #   - compound:     {type: 'compound', shapes: [...], anim}  (lasso-added shapes)
        # For poly + compound, a per-box L-mode mask is built so the crop animates
        # only inside the union of its shapes.
        all_box_records = []
        reveal_indices = []
        for b in boxes:
            shapes_px = []  # list of {type:'rect',x,y,w,h} or {type:'poly',points:[(px,py),...]}
            is_poly_legacy = isinstance(b, dict) and b.get("type") == "poly" and isinstance(b.get("points"), list)
            is_compound    = isinstance(b, dict) and b.get("type") == "compound" and isinstance(b.get("shapes"), list)
            if is_compound:
                for s in b["shapes"]:
                    if not isinstance(s, dict):
                        continue
                    st = s.get("type")
                    if st == "poly":
                        pts = s.get("points") or []
                        if len(pts) < 3:
                            continue
                        try:
                            pts_px = [(float(p["x"]) * W, float(p["y"]) * H) for p in pts]
                        except (KeyError, TypeError, ValueError):
                            return jsonify({"error": "compound poly shape has invalid points"}), 400
                        shapes_px.append({"type": "poly", "points": pts_px})
                    else:
                        try:
                            sx = float(s["x"]) * W; sy = float(s["y"]) * H
                            sw = float(s["w"]) * W; sh = float(s["h"]) * H
                        except (KeyError, TypeError, ValueError):
                            return jsonify({"error": "compound rect shape missing x/y/w/h"}), 400
                        shapes_px.append({"type": "rect", "x": sx, "y": sy, "w": sw, "h": sh})
                if not shapes_px:
                    continue
            elif is_poly_legacy:
                pts_frac = b.get("points") or []
                if len(pts_frac) < 3:
                    continue
                try:
                    pts_px = [(float(p["x"]) * W, float(p["y"]) * H) for p in pts_frac]
                except (KeyError, TypeError, ValueError):
                    return jsonify({"error": "poly box has invalid points"}), 400
                shapes_px.append({"type": "poly", "points": pts_px})
            else:
                try:
                    bx = float(b["x"]); by = float(b["y"]); bw = float(b["w"]); bh = float(b["h"])
                except (KeyError, TypeError, ValueError):
                    return jsonify({"error": "box missing x/y/w/h"}), 400
                shapes_px.append({"type": "rect", "x": bx * W, "y": by * H, "w": bw * W, "h": bh * H})
            # Union bbox over every sub-shape.
            xs_lo = []; ys_lo = []; xs_hi = []; ys_hi = []
            for s in shapes_px:
                if s["type"] == "poly":
                    pts = s["points"]
                    xs_lo.append(min(p[0] for p in pts))
                    ys_lo.append(min(p[1] for p in pts))
                    xs_hi.append(max(p[0] for p in pts))
                    ys_hi.append(max(p[1] for p in pts))
                else:
                    xs_lo.append(s["x"]);             ys_lo.append(s["y"])
                    xs_hi.append(s["x"] + s["w"]);    ys_hi.append(s["y"] + s["h"])
            bx_px = min(xs_lo); by_px = min(ys_lo)
            bw_px = max(xs_hi) - bx_px; bh_px = max(ys_hi) - by_px
            x = int(bx_px); y = int(by_px)
            w = int(bw_px); h = int(bh_px)
            x = max(0, min(W - 4, x)); y = max(0, min(H - 4, y))
            w = max(4, min(W - x, w)); h = max(4, min(H - y, h))
            x -= (x % 2); y -= (y % 2)
            w -= (w % 2); h -= (h % 2)
            if w < 2 or h < 2:
                continue
            # Single plain rect → no mask needed (the bbox IS the shape).
            needs_mask = is_poly_legacy or is_compound
            mask_l = _build_shape_mask(w, h, x, y, shapes_px) if needs_mask else None
            anim_val = (b.get("anim") or "fade").lower() if isinstance(b, dict) else "fade"
            all_box_records.append((x, y, w, h, anim_val, b if isinstance(b, dict) else {}, mask_l))
            if anim_val != "hide":
                reveal_indices.append(len(all_box_records) - 1)
        if not all_box_records:
            return jsonify({"error": "no valid boxes after normalization"}), 400

        # Reveal boxes are the ones that animate in. Hide boxes only mask the bg permanently.
        box_rects = [(r[0], r[1], r[2], r[3]) for i, r in enumerate(all_box_records) if i in reveal_indices]
        box_anims = [all_box_records[i][4] for i in reveal_indices]
        # Parallel to box_rects: poly_local_pts (or None) for masking the crop. None = plain rect box.
        box_masks = [all_box_records[i][6] for i in reveal_indices]
        hide_rects = [(r[0], r[1], r[2], r[3]) for i, r in enumerate(all_box_records) if i not in reveal_indices]
        # Manual time overrides per reveal box (None if not set)
        box_overrides = []
        for idx in reveal_indices:
            bd = all_box_records[idx][5]
            ov = bd.get("time_override")
            try:
                box_overrides.append(float(ov) if ov is not None else None)
            except (TypeError, ValueError):
                box_overrides.append(None)

        if not box_rects and not hide_rects:
            return jsonify({"error": "no boxes after kind split"}), 400

        N = len(box_rects)

        # Determine mode early — bg + crops differ between reveal and slideshow.
        mode = "reveal"
        if diagram_row:
            try:
                mode = (diagram_row["mode"] or "reveal").lower()
            except (KeyError, IndexError):
                mode = "reveal"
            if mode not in ("reveal", "slideshow"):
                mode = "reveal"

        # Pre-sample bg-fill color per box rect — used for reveal-mode masking.
        fill_for_rect = {}
        for (x, y, w, h, _anim, _bd, _poly) in all_box_records:
            fill_for_rect[(x, y, w, h)] = _ring_median_color(img, x, y, w, h, pad=8, ring=24)

        if mode == "reveal":
            # Reveal mode: bg = image with every box filled with its local bg color.
            bg = img.copy()
            draw = ImageDraw.Draw(bg)
            for (x, y, w, h, _anim, _bd, _poly) in all_box_records:
                fill = fill_for_rect[(x, y, w, h)]
                draw.rectangle([x, y, x + w - 1, y + h - 1], fill=fill)
        else:
            # Slideshow mode: bg = flat corner-sampled color (slides cover it).
            bg = Image.new("RGB", (W, H), (8, 8, 18))  # placeholder, replaced below

        def _rect_contains(outer, inner, slack=2):
            """outer / inner are (x,y,w,h). True if inner is (mostly) inside outer."""
            ox, oy, ow, oh = outer
            ix, iy, iw, ih = inner
            return (ix >= ox - slack and iy >= oy - slack and
                    (ix + iw) <= (ox + ow) + slack and (iy + ih) <= (oy + oh) + slack)

        # Pad the bottom so the HTML5 video player's control gradient sits over empty bg
        bottom_pad = int(min(120, max(60, H * 0.10)))
        bottom_pad -= bottom_pad % 2
        # Priority: user override (eyedropper / hex) → stored auto → fresh compute.
        canvas_bg_color = None
        if diagram_row is not None:
            for col in ("bg_color_override", "bg_color_auto"):
                try:
                    v = diagram_row[col]
                except (IndexError, KeyError):
                    v = None
                if v and isinstance(v, str) and v.startswith("#") and len(v) == 7:
                    try:
                        canvas_bg_color = (int(v[1:3], 16), int(v[3:5], 16), int(v[5:7], 16))
                        break
                    except ValueError:
                        continue
        if canvas_bg_color is None:
            canvas_bg_color = _corner_bg_color(img, patch=40)
        bg_path = work_dir / "bg.png"
        if mode == "reveal":
            bg_padded = Image.new("RGB", (W, H + bottom_pad), canvas_bg_color)
            bg_padded.paste(bg, (0, 0))
            bg_padded.save(bg_path)
        else:
            # Slideshow bg = flat corner color (the slides cover the whole frame).
            Image.new("RGB", (W, H + bottom_pad), canvas_bg_color).save(bg_path)

        # If no reveal boxes (only hide boxes), short-circuit to a trivial render:
        # just bg.png + audio, no filter graph.
        if N == 0:
            out_path = work_dir / "out.mp4"
            simple_cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-t", f"{duration:.3f}", "-i", str(bg_path),
                "-i", str(audio_path),
                "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-r", "30", "-shortest",
                str(out_path),
            ]
            r = subprocess.run(simple_cmd, capture_output=True, text=True, timeout=240)
            if r.returncode != 0:
                return jsonify({"error": "ffmpeg failed (hide-only)", "stderr": r.stderr[-1500:]}), 500
            # Persist + return
            if diagram_row:
                vid = diagram_row["video_id"]
                out_dir = _diagram_dir(vid, diagram_id)
                final_path = out_dir / "result.mp4"
                shutil.move(str(out_path), str(final_path))
                rel = str(final_path.relative_to(Path(app.root_path)))
                result_url = "/" + rel
            else:
                out_dir = Path(app.root_path) / "static" / "uploads" / "diagrams"
                out_dir.mkdir(parents=True, exist_ok=True)
                final_path = out_dir / f"{job_id}.mp4"
                shutil.move(str(out_path), str(final_path))
                result_url = f"/static/uploads/diagrams/{job_id}.mp4"
            payload = {
                "url": result_url, "duration": round(duration, 2),
                "boxes": 0, "hidden_boxes": len(hide_rects),
                "reveal_times": [], "alignment": "hide_only",
                "descriptions": [], "size": (W, H + bottom_pad),
                "content_size": (W, H), "bottom_pad": bottom_pad,
            }
            if diagram_row:
                conn = get_db()
                conn.execute("UPDATE diagrams SET result_url=?, result_meta_json=?, updated_at=? WHERE id=?",
                             (result_url, json.dumps(payload), datetime.now(timezone.utc).isoformat(), diagram_id))
                conn.commit()
                conn.close()
            return jsonify(payload)

        # Crop preparation is deferred until AFTER the times are computed below,
        # so that nested inner boxes can be masked inside their outer crops.
        FPS = 30
        crop_paths = [None] * N
        zoom_frame_dirs = [None] * N
        zoom_frame_counts = [0] * N

        # ---- Reveal times: AI-aligned if possible, else evenly distributed ----
        first_t = min(0.5, max(0.3, duration * 0.05))
        last_t = max(first_t + 0.5, duration * 0.75)
        if N == 1:
            even_times = [first_t]
        else:
            step = (last_t - first_t) / (N - 1)
            even_times = [first_t + i * step for i in range(N)]

        alignment_mode = "evenly_distributed"
        alignment_descs = [None] * N

        # Build a Gemini-friendly image with numbered boxes drawn on top
        align_img = img.copy()
        d2 = ImageDraw.Draw(align_img)
        # font for badge numbers (try common paths, fall back to default)
        font = None
        badge_radius = max(14, min(28, int(min(W, H) * 0.025)))
        font_size = int(badge_radius * 1.3)
        for fp in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        ):
            try:
                font = ImageFont.truetype(fp, font_size)
                break
            except OSError:
                continue
        if font is None:
            font = ImageFont.load_default()
        for i, (x, y, w, h) in enumerate(box_rects):
            d2.rectangle([x, y, x + w - 1, y + h - 1], outline=(245, 185, 66), width=4)
            cx, cy = x + badge_radius + 4, y + badge_radius + 4
            d2.ellipse([cx - badge_radius, cy - badge_radius, cx + badge_radius, cy + badge_radius], fill=(245, 185, 66))
            label = str(i + 1)
            bbox_ascent = 0
            try:
                bb = d2.textbbox((0, 0), label, font=font)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                bbox_ascent = bb[1]
            except AttributeError:
                if hasattr(font, "getsize"):
                    tw, th = font.getsize(label)
                else:
                    tw, th = 10, 12
            d2.text((cx - tw / 2, cy - th / 2 - bbox_ascent), label, fill=(0, 0, 0), font=font)

        buf = BytesIO()
        align_img.save(buf, format="PNG")
        align_bytes = buf.getvalue()

        script = (script_input or "").strip()
        mime_by_ext = {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
                       "aac": "audio/aac", "ogg": "audio/ogg", "flac": "audio/flac"}
        audio_mime = audio_mime_hint or mime_by_ext.get(a_ext, "audio/mpeg")
        audio_bytes = Path(audio_path).read_bytes()

        # Preferred path: Whisper word timestamps + Gemini phrase matching
        ai_result = None
        if N <= 12 and duration <= 180:
            words = _replicate_whisper_words(audio_bytes, audio_mime)
            if words:
                match = _gemini_match_boxes_to_words(align_bytes, words, N)
                if match and any(t is not None for t in match["times"]):
                    ai_result = match
                    alignment_mode = "whisper_aligned"
            # Fallback: audio-reasoning-only Gemini (no word timestamps)
            if ai_result is None:
                fallback = _gemini_align_boxes(align_bytes, audio_bytes, audio_mime, script, N, duration)
                if fallback and any(t is not None for t in fallback["times"]):
                    ai_result = fallback
                    alignment_mode = "gemini_audio"

        if ai_result and any(t is not None for t in ai_result["times"]):
            ai_times = ai_result["times"][:]
            for i in range(N):
                if ai_times[i] is None:
                    ai_times[i] = even_times[i]
            # 0.18s lead — with eased slide motion the perceived arrival lags the
            # motion onset by ~half the slide duration, so we lean a touch earlier.
            lead = 0.18 if alignment_mode == "whisper_aligned" else 0.0
            for i in range(N):
                ai_times[i] = max(0.3, float(ai_times[i]) - lead)
            # Clamp + enforce strictly increasing (min step 0.25s)
            min_step = 0.25
            for i in range(N):
                t = max(0.3, min(duration - 0.1, float(ai_times[i])))
                if i > 0 and t < ai_times[i - 1] + min_step:
                    t = ai_times[i - 1] + min_step
                if t > duration - 0.05:
                    t = duration - 0.05
                ai_times[i] = t
            times = ai_times
            alignment_descs = ai_result["descs"]
        else:
            times = even_times

        # Apply manual time overrides (per box) AFTER AI alignment, BEFORE monotonic clamp.
        # Manual times are absolute — no lead time applied.
        manual_count = 0
        for i in range(N):
            if box_overrides[i] is not None:
                times[i] = max(0.3, min(duration - 0.05, box_overrides[i]))
                manual_count += 1
        # Re-enforce monotonic if overrides scrambled order (overrides win; AI shifts around them)
        if manual_count > 0:
            min_step = 0.25
            for i in range(N):
                t = max(0.3, min(duration - 0.05, float(times[i])))
                if i > 0 and t < times[i - 1] + min_step:
                    t = times[i - 1] + min_step
                if t > duration - 0.05:
                    t = duration - 0.05
                times[i] = t
            if alignment_mode in ("whisper_aligned", "gemini_audio", "evenly_distributed"):
                alignment_mode = alignment_mode + "_with_manual"

        # Generate the per-box overlay source. Reveal mode = cropped + inner-masked
        # rectangle to overlay at the box's original position. Slideshow mode = the
        # box content scaled up centered on a full 16:9 canvas (the slide IS the frame).
        canvas_w = W
        canvas_h = H + bottom_pad
        for i, ((x, y, w, h), anim_for_box) in enumerate(zip(box_rects, box_anims)):
            mask_l = box_masks[i] if i < len(box_masks) else None
            if mode == "reveal":
                crop = img.crop((x, y, x + w, y + h))
                dc = ImageDraw.Draw(crop)
                for j in range(i + 1, N):
                    jx, jy, jw, jh = box_rects[j]
                    if _rect_contains((x, y, w, h), (jx, jy, jw, jh)):
                        lx, ly = jx - x, jy - y
                        fill = fill_for_rect.get(box_rects[j], (8, 8, 18))
                        dc.rectangle([lx, ly, lx + jw - 1, ly + jh - 1], fill=fill)
                for hr in hide_rects:
                    if _rect_contains((x, y, w, h), hr):
                        hx, hy, hw, hh = hr
                        lx, ly = hx - x, hy - y
                        fill = fill_for_rect.get(hr, (8, 8, 18))
                        dc.rectangle([lx, ly, lx + hw - 1, ly + hh - 1], fill=fill)
                # Polygon / compound shapes: mask out everything outside the
                # union of shapes. Applied AFTER nested-rect fills so they also
                # get clipped to the shape outline.
                if mask_l is not None:
                    crop = _apply_alpha_mask(crop.convert("RGBA"), mask_l)
                cp = work_dir / f"crop_{i:02d}.png"
                crop.save(cp)
                crop_paths[i] = cp
                if anim_for_box in ("zoom_in", "zoom_out"):
                    fdir, nf = _render_zoom_frames(
                        work_dir / f"zoom_{i:02d}", crop, w, h, anim_for_box,
                        zoom_dur=0.45, fade_dur=0.35, fps=FPS,
                    )
                    zoom_frame_dirs[i] = fdir
                    zoom_frame_counts[i] = nf
                elif anim_for_box == "write_on":
                    fdir, nf = _render_write_on_frames(
                        work_dir / f"writeon_{i:02d}", crop.convert("RGBA"),
                        w, h, reveal_dur=0.8, fps=FPS,
                    )
                    zoom_frame_dirs[i] = fdir
                    zoom_frame_counts[i] = nf
            else:
                # SLIDESHOW: the box content becomes a full 16:9 slide.
                box_crop = img.crop((x, y, x + w, y + h))
                # Remove isolated bright pixels (stars) before upscaling — otherwise
                # a 1-2px star becomes a 5-10px dot in the slide.
                box_crop = _despeckle_stars(box_crop, canvas_bg_color)
                # Mask is applied AFTER despeckle (despeckle needs RGB).
                if mask_l is not None:
                    box_crop = _apply_alpha_mask(box_crop.convert("RGBA"), mask_l)
                margin = 0.85   # leave 15% breathing room
                sx_ = (canvas_w * margin) / w
                sy_ = (H * margin) / h   # vertical center within the content area (top H of canvas)
                s = min(sx_, sy_)
                nw = max(2, int(w * s)); nh = max(2, int(h * s))
                nw -= nw % 2; nh -= nh % 2
                scaled = box_crop.resize((nw, nh), Image.LANCZOS)
                slide = Image.new("RGB", (canvas_w, canvas_h), canvas_bg_color)
                ox = (canvas_w - nw) // 2
                oy = (H - nh) // 2   # vertical center within top H, leaves bottom_pad clear
                # Use the scaled image as its own mask so polygon-transparent pixels
                # fall through to the slide's bg color instead of showing as black.
                if scaled.mode == "RGBA":
                    slide.paste(scaled, (ox, oy), scaled)
                else:
                    slide.paste(scaled, (ox, oy))
                sp = work_dir / f"slide_{i:02d}.png"
                slide.save(sp)
                crop_paths[i] = sp
                # Zoom in slideshow = "Ken Burns" feel on the full slide
                if anim_for_box in ("zoom_in", "zoom_out"):
                    fdir, nf = _render_zoom_frames(
                        work_dir / f"zoom_{i:02d}", slide, canvas_w, canvas_h, anim_for_box,
                        zoom_dur=0.45, fade_dur=0.35, fps=FPS,
                    )
                    zoom_frame_dirs[i] = fdir
                    zoom_frame_counts[i] = nf
                elif anim_for_box == "write_on":
                    fdir, nf = _render_write_on_frames(
                        work_dir / f"writeon_{i:02d}", slide.convert("RGBA"),
                        canvas_w, canvas_h, reveal_dur=0.8, fps=FPS,
                    )
                    zoom_frame_dirs[i] = fdir
                    zoom_frame_counts[i] = nf

        fade_dur = 0.35           # entrance fade duration
        exit_fade_dur = 0.55      # exit fade — longer so fade-only exits don't feel abrupt
        slide_dur = 0.50
        zoom_dur = 0.45
        slide_dist = int(min(60, max(22, (H + bottom_pad) * 0.04)))

        # Legacy alias map. Zoom is now working again via pre-rendered frames.
        LEGACY_ANIM_MAP = {
            "from_right_fade": "from_right",
            "from_left_fade": "from_left",
            "from_above_fade": "from_above",
            "from_below_fade": "from_below",
        }
        valid_anims = {"fade", "from_right", "from_left", "from_above", "from_below",
                       "zoom_in", "zoom_out", "write_on"}

        # Resolve each reveal box's effective anim
        resolved_anims = []
        for i in range(N):
            raw = (box_anims[i] or "fade").lower()
            a = LEGACY_ANIM_MAP.get(raw, raw)
            resolved_anims.append(a if a in valid_anims else "fade")

        # Build ffmpeg inputs
        inputs = ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(bg_path)]
        for i in range(N):
            if resolved_anims[i] in ("zoom_in", "zoom_out", "write_on"):
                # image sequence input (fade is baked into the PNG frames)
                inputs += ["-framerate", f"{FPS}",
                           "-i", str(zoom_frame_dirs[i] / "f%04d.png")]
            else:
                inputs += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(crop_paths[i])]
        inputs += ["-i", str(audio_path)]

        # mode was determined at the top of the render (slideshow vs reveal)
        # Slide-distance: bigger in slideshow (slides cover the whole frame)
        if mode == "slideshow":
            slide_dist = int(min(220, max(60, (H + bottom_pad) * 0.10)))

        def _box_filter_segs(prev_lbl, in_idx, out_lbl, bx, by, bw, bh, t, anim,
                             n_zoom_frames, t_exit=None):
            """Return filter-graph segments for one box's entrance (+ optional exit fade).
            When t_exit is set, an alpha fade-out is added starting at t_exit, and the
            overlay's enable gate becomes 'between(t, t, t_exit + exit_fade_dur)'."""
            src = f"[s{in_idx}]"
            segs = []
            exit_filter = (f",fade=t=out:st={t_exit:.3f}:d={exit_fade_dur:.3f}:alpha=1"
                           if t_exit is not None else "")
            if anim in ("zoom_in", "zoom_out", "write_on"):
                zoom_clip_len = n_zoom_frames / FPS
                stop_extra = max(0.0, duration - t - zoom_clip_len)
                segs.append(
                    f"[{in_idx}:v]format=rgba,"
                    f"setpts=PTS+{t:.3f}/TB,"
                    f"tpad=stop_mode=clone:stop_duration={stop_extra:.3f}"
                    f"{exit_filter}{src}"
                )
                x_expr, y_expr = str(bx), str(by)
            elif anim == "fade":
                segs.append(
                    f"[{in_idx}:v]format=rgba,"
                    f"fade=t=in:st={t:.3f}:d={fade_dur:.3f}:alpha=1"
                    f"{exit_filter}{src}"
                )
                x_expr, y_expr = str(bx), str(by)
            else:
                # Slide variants — eased entrance, optionally eased exit in the same
                # direction (only when t_exit is set, i.e. slideshow inter-slide transitions).
                p_in = f"min(1,max(0,(t-{t:.3f})/{slide_dur:.3f}))"
                ease_in = f"({p_in}*{p_in}*(3-2*{p_in}))"
                remaining_in = f"(1-{ease_in})"
                if t_exit is not None:
                    p_out = f"min(1,max(0,(t-{t_exit:.3f})/{slide_dur:.3f}))"
                    ease_out = f"({p_out}*{p_out}*(3-2*{p_out}))"
                    exit_offset = f"({slide_dist}*{ease_out})"
                else:
                    exit_offset = "0"
                segs.append(
                    f"[{in_idx}:v]format=rgba,"
                    f"fade=t=in:st={t:.3f}:d={fade_dur:.3f}:alpha=1"
                    f"{exit_filter}{src}"
                )
                if anim == "from_right":
                    # enters from right (x = bx + slide_dist), moves left; exit continues left.
                    x_expr = f"{bx}+{slide_dist}*{remaining_in}-{exit_offset}"
                    y_expr = str(by)
                elif anim == "from_left":
                    # enters from left, moves right; exit continues right.
                    x_expr = f"{bx}-{slide_dist}*{remaining_in}+{exit_offset}"
                    y_expr = str(by)
                elif anim == "from_below":
                    # enters from below, moves up; exit continues up.
                    x_expr = str(bx)
                    y_expr = f"{by}+{slide_dist}*{remaining_in}-{exit_offset}"
                elif anim == "from_above":
                    # enters from above, moves down; exit continues down.
                    x_expr = str(bx)
                    y_expr = f"{by}-{slide_dist}*{remaining_in}+{exit_offset}"
                else:
                    x_expr, y_expr = str(bx), str(by)
            if t_exit is not None:
                # The overlay is enabled from reveal through fade-out completion.
                enable_expr = f"between(t,{t:.3f},{(t_exit + exit_fade_dur):.3f})"
            else:
                enable_expr = f"gte(t,{t:.3f})"
            segs.append(
                f"{prev_lbl}{src}overlay=x='{x_expr}':y='{y_expr}':"
                f"enable='{enable_expr}'{out_lbl}"
            )
            return segs

        filter_parts = []
        last_label = "[0:v]"
        for i, ((bx, by, bw, bh), t) in enumerate(zip(box_rects, times)):
            anim = resolved_anims[i]
            out_label = "[vout]" if i == N - 1 else f"[v{i}]"
            t_exit = None
            if mode == "slideshow" and i + 1 < N:
                # Force slide N's exit to fully complete BEFORE slide N+1 enters,
                # with a small gap between them so they don't overlap on screen.
                inter_slide_gap = 0.10
                t_exit = times[i + 1] - exit_fade_dur - inter_slide_gap
                t_exit = max(t + 0.15, t_exit)
            # In slideshow mode the overlay is the full 16:9 canvas, positioned at (0,0).
            if mode == "slideshow":
                ovx, ovy = 0, 0
                ovw, ovh = canvas_w, canvas_h
            else:
                ovx, ovy, ovw, ovh = bx, by, bw, bh
            filter_parts.extend(_box_filter_segs(
                last_label, i + 1, out_label,
                ovx, ovy, ovw, ovh, t, anim,
                zoom_frame_counts[i], t_exit=t_exit,
            ))
            last_label = out_label

        filter_complex = ";".join(filter_parts)

        out_path = work_dir / "out.mp4"
        cmd = ["ffmpeg", "-y"] + inputs + [
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", f"{N+1}:a",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-r", "30",
            "-shortest",
            str(out_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if r.returncode != 0:
            return jsonify({"error": "ffmpeg failed", "stderr": r.stderr[-1500:], "cmd": " ".join(cmd[:5])}), 500

        # Store result: under the diagram's folder if persisted, else flat
        if diagram_row:
            vid = diagram_row["video_id"]
            out_dir = _diagram_dir(vid, diagram_id)
            final_path = out_dir / "result.mp4"
            shutil.move(str(out_path), str(final_path))
            rel = str(final_path.relative_to(Path(app.root_path)))
            result_url = "/" + rel
        else:
            out_dir = Path(app.root_path) / "static" / "uploads" / "diagrams"
            out_dir.mkdir(parents=True, exist_ok=True)
            final_path = out_dir / f"{job_id}.mp4"
            shutil.move(str(out_path), str(final_path))
            result_url = f"/static/uploads/diagrams/{job_id}.mp4"

        result_payload = {
            "url": result_url,
            "duration": round(duration, 2),
            "boxes": N,
            "hidden_boxes": len(hide_rects),
            "reveal_times": [round(t, 2) for t in times],
            "manual_flags": [box_overrides[i] is not None for i in range(N)],
            "alignment": alignment_mode,
            "descriptions": alignment_descs,
            "size": (W, H + bottom_pad),
            "content_size": (W, H),
            "bottom_pad": bottom_pad,
            "anims": resolved_anims,
            "mode": mode,
        }

        if diagram_row:
            conn = get_db()
            conn.execute("UPDATE diagrams SET result_url=?, result_meta_json=?, updated_at=? WHERE id=?",
                         (result_url, json.dumps(result_payload), datetime.now(timezone.utc).isoformat(), diagram_id))
            conn.commit()
            conn.close()

        return jsonify(result_payload)
    except Exception as e:
        return jsonify({"error": f"render exception: {type(e).__name__}: {e}"}), 500
    finally:
        try:
            shutil.rmtree(work_dir)
        except Exception:
            pass


@app.route("/api/diagrams/transcribe", methods=["POST"])
def diagrams_transcribe():
    """Transcribe an uploaded audio clip via Gemini. Returns {transcript: str}."""
    import base64
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError

    f = request.files.get("audio")
    if not f:
        return jsonify({"error": "no audio file"}), 400
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set"}), 500

    audio_bytes = f.read()
    if len(audio_bytes) > 18 * 1024 * 1024:
        return jsonify({"error": "audio too large (max 18MB inline)"}), 413
    mime = f.mimetype or "audio/mpeg"
    b64 = base64.b64encode(audio_bytes).decode("ascii")

    body = {
        "contents": [{
            "parts": [
                {"text": "Transcribe this audio verbatim. Return only the transcript text — no preamble, no quotes, no markdown, no labels."},
                {"inline_data": {"mime_type": mime, "data": b64}}
            ]
        }]
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    req = Request(url, data=json.dumps(body).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return jsonify({"error": f"gemini http {e.code}", "detail": e.read().decode("utf-8", "ignore")[:500]}), 502
    except URLError as e:
        return jsonify({"error": f"gemini network: {e.reason}"}), 502

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        return jsonify({"error": "gemini response malformed", "raw": data}), 502
    return jsonify({"transcript": text})



# ── Chapter Studio: list-driven chapter clip renderer ───────────────────

def _chapters_dir():
    d = Path(app.root_path) / "static" / "uploads" / "chapters"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _chapter_video_dir(video_id):
    d = _chapters_dir() / str(video_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _chapter_row_to_dict(row):
    if not row:
        return None
    d = dict(row)
    try:
        d["items"] = json.loads(d.pop("items_json") or "[]")
    except (TypeError, ValueError):
        d["items"] = []
    rz = d.get("result_zip_path")
    d["result_zip_url"] = ("/" + rz) if rz and not rz.startswith("/") else rz
    return d


_CS_WRAP_VALUES = {"off", "on"}

# Reveal style → ffmpeg xfade transition name.
_CS_REVEAL_TO_XFADE = {
    "blur_fade":   "fade",
    "dissolve":    "dissolve",
    "wipe_lr":     "wiperight",
    "wipe_rl":     "wipeleft",
    "slide_left":  "slideright",   # item enters from left side
    "slide_right": "slideleft",    # item enters from right side
    "circle_open": "circleopen",
    "pixelize":    "pixelize",
}


def _cs_clamp_size(val, lo, hi, default="auto"):
    """Accept 'auto' or an integer string in [lo, hi]. Returns the canonical string."""
    if val == "auto" or val is None:
        return "auto"
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    n = max(lo, min(hi, n))
    return str(n)


def _cs_clamp_int(val, lo, hi, default):
    try:
        n = int(val)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


@app.route("/api/videos/<int:vid>/chapter", methods=["GET"])
def chapter_get(vid):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM chapters WHERE video_id=?", (vid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({
            "video_id": vid, "items": [], "style": "blue_glass", "numbering": "none",
            "box_size": "auto", "text_size": "auto", "wrap_mode": "off",
            "pad_top": 60, "pad_right": 80, "pad_bottom": 60, "pad_left": 80,
            "reveal_style": "blur_fade",
            "result_zip_url": None,
        })
    return jsonify(_chapter_row_to_dict(row))


@app.route("/api/videos/<int:vid>/chapter", methods=["PUT"])
def chapter_put(vid):
    payload = request.get_json(force=True, silent=True) or {}
    items = payload.get("items", [])
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400
    items = [str(x or "")[:500] for x in items]
    style = (payload.get("style") or "blue_glass")
    if style not in ("blue_glass",):
        style = "blue_glass"
    numbering = (payload.get("numbering") or "none")
    if numbering not in ("none", "number", "step"):
        numbering = "none"
    box_size = _cs_clamp_size(payload.get("box_size"), 50, 100)
    text_size = _cs_clamp_size(payload.get("text_size"), 24, 120)
    wrap_mode = (payload.get("wrap_mode") or "off")
    if wrap_mode not in _CS_WRAP_VALUES: wrap_mode = "off"
    pad_top    = _cs_clamp_int(payload.get("pad_top"),    0, 300, 60)
    pad_right  = _cs_clamp_int(payload.get("pad_right"),  0, 300, 80)
    pad_bottom = _cs_clamp_int(payload.get("pad_bottom"), 0, 300, 60)
    pad_left   = _cs_clamp_int(payload.get("pad_left"),   0, 300, 80)
    reveal_style = (payload.get("reveal_style") or "blur_fade")
    if reveal_style not in _CS_REVEAL_TO_XFADE: reveal_style = "blur_fade"
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.execute(
        """INSERT INTO chapters
             (video_id, items_json, style, numbering, box_size, text_size, wrap_mode,
              pad_top, pad_right, pad_bottom, pad_left, reveal_style, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(video_id) DO UPDATE SET
             items_json=excluded.items_json,
             style=excluded.style,
             numbering=excluded.numbering,
             box_size=excluded.box_size,
             text_size=excluded.text_size,
             wrap_mode=excluded.wrap_mode,
             pad_top=excluded.pad_top,
             pad_right=excluded.pad_right,
             pad_bottom=excluded.pad_bottom,
             pad_left=excluded.pad_left,
             reveal_style=excluded.reveal_style,
             updated_at=excluded.updated_at""",
        (vid, json.dumps(items), style, numbering, box_size, text_size, wrap_mode,
         pad_top, pad_right, pad_bottom, pad_left, reveal_style, now, now)
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM chapters WHERE video_id=?", (vid,)).fetchone()
    conn.close()
    return jsonify(_chapter_row_to_dict(row))


@app.route("/api/videos/<int:vid>/chapter/from-doc", methods=["POST"])
def chapter_from_doc(vid):
    """Pull chapter titles from the linked content doc.

    Looks at video_details.custom_fields for "Content Doc" path,
    reads the markdown, and extracts `### Step N - <title>` lines."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT custom_fields FROM video_details WHERE video_id=?", (vid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "no video_details for this video"}), 404
    try:
        fields = json.loads(row["custom_fields"] or "[]")
    except (TypeError, ValueError):
        fields = []
    doc_path = ""
    for f in fields:
        if (f.get("key") or "").strip().lower() == "content doc":
            doc_path = (f.get("value") or "").strip()
            break
    if not doc_path:
        return jsonify({"error": "no Content Doc field set on this video"}), 404
    # Field may hold an absolute path, "content_docs/foo.md", or just "foo.md".
    # Canonical prod layout: CONTENT_DIR/content_docs/<filename>. Try a few resolutions.
    fname = Path(doc_path).name
    candidates = []
    p0 = Path(doc_path)
    if p0.is_absolute():
        candidates.append(p0)
    candidates += [
        CONTENT_DIR / doc_path,                # CONTENT_DIR + "content_docs/foo.md"
        CONTENT_DIR / "content_docs" / fname,  # CONTENT_DIR/content_docs/foo.md
        CONTENT_DIR / fname,                   # CONTENT_DIR/foo.md
    ]
    p = next((c for c in candidates if c.exists()), None)
    if not p:
        return jsonify({"error": f"content doc not found. tried: {[str(c) for c in candidates]}"}), 404
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return jsonify({"error": f"could not read content doc: {e}"}), 500
    # Match step headings in either content-doc style (`### Step N - Title`) or
    # show-doc style (`STEP N — TITLE`). Line must START with optional `###` + Step,
    # so narration like "And now let's get into Step 1..." doesn't match.
    step_re = re.compile(
        r"^\s*(?:#{2,4}\s+)?Step\s+\d+\s*[-–—:]\s*(.+?)(?:\s*[✅✓☑])?\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    matches = step_re.findall(text)
    # Dedupe consecutive identical titles (case-insensitive), preserving order
    items = []
    seen = set()
    for m in matches:
        t = m.strip()
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(t)
    return jsonify({"items": items, "source": str(p)})


@app.route("/api/videos/<int:vid>/chapter/render", methods=["POST"])
def chapter_render(vid):
    """Accept 2N PNGs (before/after for each clip) and assemble N MP4 clips.

    Form fields:
      count: int N
      clip_K_before, clip_K_after: PNG files for K in 1..N
    Each clip: 4.5s hold before → 1s xfade → 4.5s hold after = 10s @ 30fps."""
    import tempfile, subprocess, shutil, zipfile
    try:
        n = int(request.form.get("count", "0"))
    except ValueError:
        return jsonify({"error": "invalid count"}), 400
    if n < 1 or n > 20:
        return jsonify({"error": "count must be between 1 and 20"}), 400

    # Pull the saved reveal_style for this video; default to crossfade.
    conn = get_db()
    conn.row_factory = sqlite3.Row
    crow = conn.execute("SELECT reveal_style FROM chapters WHERE video_id=?", (vid,)).fetchone()
    conn.close()
    reveal_style = (crow["reveal_style"] if crow and "reveal_style" in crow.keys() else "blur_fade")
    xfade_name = _CS_REVEAL_TO_XFADE.get(reveal_style, "fade")

    work = Path(tempfile.mkdtemp(prefix=f"chapter_render_{vid}_"))
    try:
        # Save uploads, validate
        for k in range(1, n + 1):
            for phase in ("before", "after"):
                f = request.files.get(f"clip_{k}_{phase}")
                if not f:
                    return jsonify({"error": f"missing clip_{k}_{phase}"}), 400
                f.save(str(work / f"clip_{k}_{phase}.png"))

        # Output dir under static
        out_dir = _chapter_video_dir(vid) / "clips"
        # Clean prior renders for this video
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        clips = []
        for k in range(1, n + 1):
            before = work / f"clip_{k}_before.png"
            after = work / f"clip_{k}_after.png"
            out = out_dir / f"chapter_{k:02d}.mp4"
            # 4.5s hold before, 1s xfade, 4.5s hold after = 10s. Offset of xfade = 4.5.
            # `xfade` requires both inputs as video streams of equal duration parts; we use
            # -loop on the still images and -t to clip them.
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-t", "5.5", "-i", str(before),
                "-loop", "1", "-t", "5.5", "-i", str(after),
                "-filter_complex",
                "[0:v]scale=1920:1080,format=yuv420p,fps=30,setsar=1[v0];"
                "[1:v]scale=1920:1080,format=yuv420p,fps=30,setsar=1[v1];"
                f"[v0][v1]xfade=transition={xfade_name}:duration=1:offset=4.5,format=yuv420p[v]",
                "-map", "[v]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(out),
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return jsonify({
                    "error": f"ffmpeg failed for clip {k}",
                    "stderr": r.stderr[-1500:],
                }), 500
            rel = str(out.relative_to(Path(app.root_path)))
            clips.append({"k": k, "name": out.name, "url": "/" + rel})

        # Zip all clips
        zip_path = out_dir / f"chapters_video_{vid}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for c in clips:
                zf.write(out_dir / c["name"], arcname=c["name"])
        rel_zip = str(zip_path.relative_to(Path(app.root_path)))

        now = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        conn.execute(
            "UPDATE chapters SET result_zip_path=?, last_rendered_at=?, updated_at=? WHERE video_id=?",
            (rel_zip, now, now, vid)
        )
        conn.commit()
        conn.close()

        return jsonify({
            "ok": True,
            "count": n,
            "clips": clips,
            "zip_url": "/" + rel_zip,
            "rendered_at": now,
        })
    finally:
        try:
            shutil.rmtree(work)
        except OSError:
            pass


# ── SAY doc (ElevenLabs-ready plain narration) ───────────────────────────

VOCAL_DOC_SYSTEM_PROMPT = """You extract the SAY doc from a content doc — ONLY the words Andy actually says aloud, in narration order, as plain prose. This text goes directly into ElevenLabs for TTS, so any markdown markers (asterisks, underscores, pipes, dashes) would be SPOKEN ALOUD literally. Do not use any.

INCLUDE (every word Andy speaks, in order):
- The hook / intro spoken paragraph (typically under "🎤 SAY THIS")
- The body of every spoken block (typically under "🗣️ Say:")
- Any bridges, transitions, or asides that are clearly spoken
- The closing / CTA spoken content

STRIP (do not include any of this):
- Title, packaging variants, thumbnail copy, video description
- 5P framework labels, 💎 BENEFITS prep, 📋 STEPS recap lists, 🔒 WHY STAY notes, 🎁 GIFT prep
- 🖥️ Show cues, B-roll cues, image prompts, frame notes, stage directions
- Chapter labels, STEP headings, section dividers
- Sources, references, checklists
- ALL emoji
- ALL markdown headings (# ## ###)
- ALL list bullets (-, *, 1.)
- ALL bold/italic/underline markers (**, *, __)
- ALL code fences and inline code
- Metadata blocks (CHAPTERS, CODES, INTRO labels)

OUTPUT FORMAT:
Plain prose with ONE permitted structural marker: section divider lines so the editor can see where steps begin. The marker lines are stripped before the text reaches ElevenLabs — they exist purely for visual structure while editing.

Use exactly these marker forms, each on its own line, with a blank line above and below:
- === HOOK === (above the intro / hook section)
- === STEP 1: <step name> === (above each step's spoken content; number them 1, 2, 3, …)
- === CLOSING === (above the final CTA / closing section, if there is one distinct from the last step)

Example shape:

=== HOOK ===

[hook paragraph(s)]

=== STEP 1: Setup ===

[step 1 paragraph(s)]

=== STEP 2: First Build ===

[step 2 paragraph(s)]

=== CLOSING ===

[closing paragraph(s)]

Other rules:
- Natural paragraph breaks (one blank line) between distinct beats within a section.
- Spell out symbols that would be read awkwardly: "30 percent" not "30%", "and" not "&", "for example" not "e.g."
- Keep natural contractions ("it's", "we'll", "don't").
- Use commas and ellipses for natural pauses — never insert codes like "|||" or "[pause]".
- No headings beyond the === markers, no bold/italic/underline, no bullets, no emoji, no other markdown.

CRITICAL: Return ONLY the spoken text plus the === markers. No preamble, no code fence, no commentary. Start with === HOOK === on the first line and end with the final spoken word of the last section. The output should be fully self-contained — do not truncate mid-sentence."""


@app.route("/api/videos/<int:vid>/vocal-doc", methods=["GET"])
def get_vocal_doc(vid):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT vocal_doc FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "video not found"}), 404
    return jsonify({"vocal_doc": row["vocal_doc"] or ""})


@app.route("/api/videos/<int:vid>/vocal-doc", methods=["POST"])
def save_vocal_doc(vid):
    body = request.get_json(force=True, silent=True) or {}
    text = body.get("vocal_doc", "")
    conn = get_db()
    conn.execute("UPDATE videos SET vocal_doc=? WHERE id=?", (text, vid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/videos/<int:vid>/vocal-doc/generate", methods=["POST"])
def generate_vocal_doc(vid):
    """Take supplied content doc text (or fall back to stored script col) and
    produce a Benson-coded vocal doc via Gemini 2.5 Flash."""
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set"}), 500

    body = request.get_json(force=True, silent=True) or {}
    content_doc = (body.get("content_doc") or "").strip()
    if not content_doc:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT script FROM videos WHERE id=?", (vid,)).fetchone()
        conn.close()
        content_doc = (row["script"] or "").strip() if row else ""
    if not content_doc:
        return jsonify({"error": "no content doc supplied and no stored script"}), 400

    payload = {
        "contents": [{
            "parts": [{"text": VOCAL_DOC_SYSTEM_PROMPT + "\n\n---\nCONTENT DOC TO EXTRACT FROM:\n\n" + content_doc}]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 32768,
        }
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    req = Request(url, data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return jsonify({"error": f"gemini http {e.code}", "detail": e.read().decode("utf-8", "ignore")[:500]}), 502
    except URLError as e:
        return jsonify({"error": f"gemini network: {e.reason}"}), 502

    try:
        candidate = data["candidates"][0]
        text = candidate["content"]["parts"][0]["text"].strip()
        finish = candidate.get("finishReason", "")
    except (KeyError, IndexError, TypeError):
        return jsonify({"error": "gemini response malformed", "raw": data}), 502
    if finish and finish not in ("STOP", "MODEL_LENGTH"):
        return jsonify({
            "error": f"gemini incomplete (finishReason={finish})",
            "partial": text,
            "hint": "Output was cut off. Content doc may be too long for one call.",
        }), 502

    # Strip an accidental ```markdown fence if Gemini adds one
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"): lines = lines[1:]
        if lines and lines[-1].strip() == "```": lines = lines[:-1]
        text = "\n".join(lines).strip()

    conn = get_db()
    conn.execute("UPDATE videos SET vocal_doc=? WHERE id=?", (text, vid))
    conn.commit()
    conn.close()
    return jsonify({"vocal_doc": text})


# ---------------------------------------------------------------------------
# Visuals doc — vocal_doc augmented with inline [AVATAR]/[DIAGRAM]/[SCREEN] tags
# that drive what plays over the avatar at each point in the timeline.
# ---------------------------------------------------------------------------

VISUALS_DOC_SYSTEM_PROMPT = """You are tagging a YouTube script with VISUAL MODE markers.

Given a vocal doc (already split into === STEP N: TITLE === sections), you insert ONE of three tags on its own line before each paragraph block:

[AVATAR]   — the talking-head avatar carries this beat; no extra visual needed. Use for hooks, transitions, personal anecdotes, opinion lines.
[DIAGRAM]  — a pre-built diagram should be on screen. Use for any moment where the speaker is explaining a concept, listing items, comparing options, walking through a framework, or showing the structure of an idea.
[SCREEN]   — the speaker is referring to a live screen-recording / app demo. Use whenever the script says "let me show you", "watch this", "here's what it looks like", or describes clicking, typing, navigating, opening tabs, or any literal on-screen interaction.

RULES:
1. Output the ENTIRE vocal doc back, unchanged, with tags inserted. Do NOT rewrite, summarize, or skip text.
2. Preserve every `=== STEP N: TITLE ===` marker exactly as-is.
3. Each tag goes on its own line, with a blank line before and after. Place tags BEFORE the paragraph block they apply to.
4. Every paragraph block must have exactly one tag. Multiple paragraphs in a row with the same intended visual should be wrapped under ONE tag (don't repeat the tag).
5. Default to [AVATAR] when uncertain. Only use [DIAGRAM] or [SCREEN] when the text clearly calls for it.
6. Do not invent text. Do not add commentary, code fences, or preamble.

Output ONLY the tagged vocal doc."""


@app.route("/api/videos/<int:vid>/visuals-doc", methods=["GET"])
def get_visuals_doc(vid):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT visuals_doc FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "video not found"}), 404
    return jsonify({"visuals_doc": row["visuals_doc"] or ""})


@app.route("/api/videos/<int:vid>/visuals-doc", methods=["POST"])
def save_visuals_doc(vid):
    body = request.get_json(force=True, silent=True) or {}
    text = body.get("visuals_doc", "")
    conn = get_db()
    conn.execute("UPDATE videos SET visuals_doc=? WHERE id=?", (text, vid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/videos/<int:vid>/visuals-doc/generate", methods=["POST"])
def generate_visuals_doc(vid):
    """Read the stored vocal_doc and run a single Gemini pass that inserts
    [AVATAR]/[DIAGRAM]/[SCREEN] tags before each paragraph block. Saves to
    visuals_doc and returns the tagged text."""
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set"}), 500

    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT vocal_doc FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    vocal_doc = (row["vocal_doc"] or "").strip() if row else ""
    if not vocal_doc:
        return jsonify({"error": "vocal_doc is empty — generate the Say doc first"}), 400

    payload = {
        "contents": [{
            "parts": [{"text": VISUALS_DOC_SYSTEM_PROMPT + "\n\n---\nVOCAL DOC TO TAG:\n\n" + vocal_doc}]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 32768,
        }
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    req = Request(url, data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return jsonify({"error": f"gemini http {e.code}", "detail": e.read().decode("utf-8", "ignore")[:500]}), 502
    except URLError as e:
        return jsonify({"error": f"gemini network: {e.reason}"}), 502

    try:
        candidate = data["candidates"][0]
        text = candidate["content"]["parts"][0]["text"].strip()
        finish = candidate.get("finishReason", "")
    except (KeyError, IndexError, TypeError):
        return jsonify({"error": "gemini response malformed", "raw": data}), 502
    if finish and finish not in ("STOP", "MODEL_LENGTH"):
        return jsonify({
            "error": f"gemini incomplete (finishReason={finish})",
            "partial": text,
        }), 502

    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"): lines = lines[1:]
        if lines and lines[-1].strip() == "```": lines = lines[:-1]
        text = "\n".join(lines).strip()

    conn = get_db()
    conn.execute("UPDATE videos SET visuals_doc=? WHERE id=?", (text, vid))
    conn.commit()
    conn.close()
    return jsonify({"visuals_doc": text})


VISUAL_TAG_RE = re.compile(r"^\s*\[(AVATAR|DIAGRAM|SCREEN)\]\s*$", re.IGNORECASE)


def _parse_visuals_blocks(text):
    """Split a tagged visuals_doc into ordered [TAG] blocks with char offsets
    into the STRIPPED text (no === STEP N === lines, no [TAG] lines, just the
    spoken body — same shape as `_strip_and_segment` produces).

    Returns (cleaned_text, blocks) where blocks is a list of:
        {tag, step, char_start, char_end, text}

    `step` is the 1-based step number this block falls under (1 if before the
    first marker; matches the numbering in `Step N: Title` diagram names).
    Untagged paragraphs (before the first tag in a section) implicitly inherit
    AVATAR so timing math still covers them.
    """
    if not text:
        return "", []

    blocks = []
    out_lines = []
    joined_len = 0
    current_tag = "AVATAR"
    current_step = 0  # 0 = pre-step (treated as step 1 below)
    block_open = False

    def open_block(tag, step, start):
        nonlocal block_open
        if block_open and blocks and blocks[-1].get("char_end") is None:
            blocks[-1]["char_end"] = start
        blocks.append({
            "tag": tag.upper(),
            "step": max(1, step),
            "char_start": start,
            "char_end": None,
        })
        block_open = True

    for line in text.split("\n"):
        # === STEP N: TITLE === marker
        m_step = re.match(r"^\s*=+\s*STEP\s*(\d+)[:\s]([^=]*?)\s*=+\s*$", line, re.IGNORECASE)
        if m_step:
            current_step = int(m_step.group(1))
            # close any open block at this boundary so the new step starts fresh
            marker_pos = joined_len + (1 if out_lines else 0)
            if block_open and blocks and blocks[-1].get("char_end") is None:
                blocks[-1]["char_end"] = marker_pos
                block_open = False
            continue
        # Non-step `=== HOOK ===` style marker (skip but don't reset step)
        if re.match(r"^\s*=+\s*[A-Z0-9][^=]*?\s*=+\s*$", line):
            marker_pos = joined_len + (1 if out_lines else 0)
            if block_open and blocks and blocks[-1].get("char_end") is None:
                blocks[-1]["char_end"] = marker_pos
                block_open = False
            continue
        # [TAG] line
        m_tag = VISUAL_TAG_RE.match(line)
        if m_tag:
            current_tag = m_tag.group(1).upper()
            marker_pos = joined_len + (1 if out_lines else 0)
            open_block(current_tag, current_step, marker_pos)
            continue
        # Body line — if we haven't opened a block yet, open one with current_tag
        if not block_open:
            marker_pos = joined_len + (1 if out_lines else 0)
            open_block(current_tag, current_step, marker_pos)
        sep = 1 if out_lines else 0
        out_lines.append(line)
        joined_len += sep + len(line)

    if block_open and blocks and blocks[-1].get("char_end") is None:
        blocks[-1]["char_end"] = joined_len

    cleaned = "\n".join(out_lines)

    # Mirror the lstrip the Say pipeline does so offsets line up with the
    # take's char-aligned timing.
    prefix = len(cleaned) - len(cleaned.lstrip())
    if prefix:
        cleaned = cleaned[prefix:]
        for b in blocks:
            b["char_start"] = max(0, b["char_start"] - prefix)
            if b["char_end"] is not None:
                b["char_end"] = max(0, b["char_end"] - prefix)

    cleaned = cleaned.rstrip()
    n = len(cleaned)
    for b in blocks:
        b["char_start"] = min(b["char_start"], n)
        if b["char_end"] is not None:
            b["char_end"] = min(b["char_end"], n)
    blocks = [b for b in blocks if (b["char_end"] or 0) > b["char_start"]]

    # Attach the body text for each block so the editor can show a preview.
    for b in blocks:
        b["text"] = cleaned[b["char_start"]:b["char_end"]].strip()

    return cleaned, blocks


@app.route("/api/videos/<int:vid>/visuals-doc/compute-blocks", methods=["POST"])
def compute_visuals_blocks(vid):
    """Parse the saved visuals_doc and return time-aligned blocks ready for
    timeline placement. Uses the latest voiceover take's duration via linear
    interpolation (matches compute-segments fallback)."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT visuals_doc FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    text = (row["visuals_doc"] or "") if row else ""
    if not text.strip():
        return jsonify({"error": "visuals_doc is empty"}), 400

    cleaned, blocks = _parse_visuals_blocks(text)
    if not blocks:
        return jsonify({"error": "no [TAG] blocks found in visuals_doc"}), 400

    # Pull the most recent take that has a duration (or segments we can derive
    # duration from). Fall back to a synthetic 60s/step estimate so the editor
    # gets *something* back even pre-synth.
    state = _load_voiceover_state(vid)
    gens = state.get("generations", [])
    duration = 0.0
    for g in reversed(gens):
        d = g.get("duration") or g.get("audio_duration") or 0
        if d:
            duration = float(d)
            break
        segs = g.get("segments") or []
        if segs:
            duration = float(segs[-1].get("end", 0)) or duration
            if duration:
                break

    if duration <= 0:
        # Last-resort estimate: 13 chars/sec speaking rate.
        duration = max(30.0, len(cleaned) / 13.0)
        method = "estimate-13cps"
    else:
        method = "linear-from-take-duration"

    total_chars = max(1, len(cleaned))
    out = []
    for b in blocks:
        start = duration * (b["char_start"] / total_chars)
        end = duration * (b["char_end"] / total_chars)
        out.append({
            "tag": b["tag"],
            "step": b["step"],
            "char_start": b["char_start"],
            "char_end": b["char_end"],
            "start": round(start, 3),
            "end": round(end, 3),
            "text": b["text"][:200],
        })

    return jsonify({
        "blocks": out,
        "duration": duration,
        "method": method,
        "total_chars": total_chars,
    })


# ---------------------------------------------------------------------------
# Visual tags — human-tagged char ranges in the cleaned vocal_doc that map
# selections in the script to visuals on the timeline. Andy selects text in
# the Visuals tab and right-clicks → Diagram / Avatar / Text anim / Screen.
# Each tag stores: {id, char_start, char_end, type, asset_id?, label?, color?}.
# ---------------------------------------------------------------------------

_VISUAL_TAG_TYPES = {
    "diagram", "avatar", "text_anim", "screen", "chapter",
}


def _sanitize_visual_tags(raw):
    """Trust nothing; normalize what the client posts."""
    out = []
    if not isinstance(raw, list):
        return out
    for t in raw:
        if not isinstance(t, dict):
            continue
        ttype = (t.get("type") or "").lower()
        if ttype not in _VISUAL_TAG_TYPES:
            continue
        try:
            cs = int(t.get("char_start"))
            ce = int(t.get("char_end"))
        except (TypeError, ValueError):
            continue
        if ce <= cs:
            continue
        out.append({
            "id": str(t.get("id") or ("vt" + uuid.uuid4().hex[:12])),
            "char_start": cs,
            "char_end": ce,
            "type": ttype,
            "asset_id": (str(t["asset_id"]) if t.get("asset_id") else None),
            "label": (str(t["label"])[:120] if t.get("label") else None),
            "color": (str(t["color"])[:24] if t.get("color") else None),
        })
    # Sort by start so consumers don't have to.
    out.sort(key=lambda x: (x["char_start"], x["char_end"]))
    return out


@app.route("/api/videos/<int:vid>/visual-tags", methods=["GET"])
def get_visual_tags(vid):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT visual_tags FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "video not found"}), 404
    try:
        tags = json.loads(row["visual_tags"]) if row["visual_tags"] else []
    except (TypeError, ValueError):
        tags = []
    return jsonify({"tags": tags if isinstance(tags, list) else []})


@app.route("/api/videos/<int:vid>/visual-tags", methods=["POST"])
def save_visual_tags(vid):
    body = request.get_json(force=True, silent=True) or {}
    tags = _sanitize_visual_tags(body.get("tags"))
    conn = get_db()
    conn.execute(
        "UPDATE videos SET visual_tags=? WHERE id=?",
        (json.dumps(tags), vid),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "tags": tags})


def _gemini_diagram_keywords(image_bytes, api_key):
    """Ask Gemini 2.5 Flash to look at a diagram image and return a list of
    concept keywords (1-3 words each) describing what's visible. Used to
    score where in the script a diagram should be placed.

    Returns list[str] or None on error. Cheap call (~$0.0001 per diagram).
    """
    import base64
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
    img_b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = (
        "Look at this diagram. Return 5-12 short keywords (1-3 words each) "
        "describing what concepts, named tools, processes, or text elements "
        "are visible in it. Focus on things a viewer would say out loud when "
        "explaining the diagram. Skip generic words like 'diagram', 'image', "
        "'box'. Return ONLY a JSON array of lowercase strings — no commentary, "
        "no code fence. Example: "
        '["thumbnail generation", "midjourney", "canva", "workflow", "slash thumb"]'
    )
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/png", "data": img_b64}},
        ]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1,
            "maxOutputTokens": 256,
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    req = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
    except (HTTPError, URLError, KeyError, IndexError, ValueError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    out = []
    for k in parsed:
        if isinstance(k, str):
            kw = k.strip().lower()
            if kw and len(kw) <= 40:
                out.append(kw)
    return out[:12] if out else None


_SCREEN_PHRASE_RE = re.compile(
    r"\b("
    r"let me show you|"
    r"i'?ll show you|"
    r"here'?s what (?:it|this|that) looks like|"
    r"watch this(?: clip| video)?|"
    r"i'?ll demonstrate|"
    r"on my screen|"
    r"in my (?:browser|terminal|editor)|"
    r"on the screen here|"
    r"check this out"
    r")\b",
    re.IGNORECASE,
)


def _sentence_spans(cleaned, seg):
    """Return list of (char_start, char_end) for each sentence inside `seg`,
    in coords of the cleaned vocal_doc. Uses simple `.!?` splitting which is
    good enough for narration."""
    cs0 = int(seg["char_start"])
    ce0 = int(seg["char_end"])
    body = cleaned[cs0:ce0]
    out = []
    i = 0
    n = len(body)
    while i < n:
        while i < n and body[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        # Walk forward to next sentence terminator (with min length so
        # initialisms like "I." don't split prematurely).
        j = i + 8
        while j < n and body[j] not in ".!?":
            j += 1
        if j < n:
            j += 1  # include punctuation
        end = min(n, j)
        if end <= start:
            break
        out.append((cs0 + start, cs0 + end))
        i = end
    return out


@app.route("/api/videos/<int:vid>/visual-tags/auto-suggest", methods=["POST"])
def auto_suggest_visual_tags(vid):
    """Generate a sprinkled mix of diagram + avatar + text_anim + screen tags.

    Rules:
      - Diagrams: only RENDERED diagrams (skips not-rendered). Each lands on
        one sentence; first sentence of the step matched by `Step N` /
        `Diagram N` in the name, or position+1 as fallback. Extras (more
        diagrams than steps) land at later sentences of the same step.
      - Avatar: maximum ONE sentence per step (Andy's "salt" rule), preferring
        a mid-step sentence that's not already tagged.
      - Screen: any sentence containing a screen-recording trigger phrase
        ("let me show", "watch this", "click on", "type into", etc).
      - Text_anim: up to N sentences sprinkled in remaining un-tagged spots
        across the doc (default ~3 + 1 per 4 steps).
      - All other text stays untagged.

    Body: {replace: bool=true}  // replace prior auto-tag types (diagram,
    avatar, screen, text_anim). Manual `chapters` tags are preserved.
    """
    body = request.get_json(force=True, silent=True) or {}
    replace = bool(body.get("replace", True))

    conn = get_db()
    conn.row_factory = sqlite3.Row
    vrow = conn.execute(
        "SELECT vocal_doc, visual_tags FROM videos WHERE id=?", (vid,)
    ).fetchone()
    drows = conn.execute(
        "SELECT id, name, position, script, image_path, result_url, vision_keywords FROM diagrams "
        "WHERE video_id=? ORDER BY position, created_at",
        (vid,)
    ).fetchall()
    conn.close()
    if not vrow:
        return jsonify({"error": "video not found"}), 404

    raw_text = (vrow["vocal_doc"] or "")
    cleaned, doc_segments = _strip_and_segment(raw_text)
    if not cleaned:
        return jsonify({"error": "vocal_doc has no spoken text"}), 400

    def _diagram_available(r):
        return bool(r["result_url"]) or bool(r["image_path"])

    # Drop INTRO if it sits before any real step segment with body — most
    # narrations open straight into Step 1 with no separate hook.
    real_segs = [s for s in doc_segments if (s.get("char_end") or 0) > s["char_start"]]

    def _ord(s):
        m = re.search(r"(?:step|diagram)\s*(\d+)", s or "", re.IGNORECASE)
        return int(m.group(1)) if m else None

    step_segs_by_num = {}
    for s in real_segs:
        n = _ord(s.get("name") or "")
        if n is not None and n not in step_segs_by_num:
            step_segs_by_num[n] = s

    # Ordered list of "real step" segments — excludes HOOK/INTRO/OUTRO/etc.
    # Lets us map `Diagram N` → the Nth real step even when segment names
    # don't carry an explicit "Step N:" prefix.
    _META_NAME_RE = re.compile(
        r"^\s*(hook|intro|outro|cta|wrap|wrap\s*up|end|conclusion)\s*$",
        re.IGNORECASE,
    )
    step_segs_in_order = [
        s for s in real_segs
        if not _META_NAME_RE.match(s.get("name") or "")
    ]

    # All sentences in the doc, grouped by which segment they fall in.
    seg_sentences = []  # list[(seg, [(cs, ce), ...])]
    for s in real_segs:
        sents = _sentence_spans(cleaned, s)
        seg_sentences.append((s, sents))

    # Track which (char_start, char_end) ranges are already claimed.
    claimed = []
    def claim(cs, ce):
        claimed.append((cs, ce))
    def overlaps_claimed(cs, ce):
        return any(cs < e and ce > s for (s, e) in claimed)

    suggested = []
    skipped = []
    used_steps_for_diagram = {}  # step_num → count of diagrams placed in this step
    chapter_sentence_by_seg = {}  # id(seg) → index of the step-intro sentence

    def _is_step_intro(text: str) -> bool:
        t = text.lstrip().lower()
        if re.search(r"\b(?:step|chapter)\s*\d+\b", t):
            return True
        if t.startswith(("and now", "now let", "first up", "first,", "next up", "next,")):
            return True
        return False

    # 1) CHAPTER intros — the literal "And now let's get into Step N — TITLE"
    #    sentence at the start of each non-HOOK segment. This is the chapter
    #    card moment. Runs FIRST so diagrams know to skip past it.
    for seg, sents in seg_sentences:
        if not sents:
            continue
        if _META_NAME_RE.match(seg.get("name") or ""):
            # HOOK / INTRO doesn't get a chapter card.
            continue
        # Find the first intro-shaped sentence within the first ~3 sentences.
        intro_idx = None
        for i, (cs, ce) in enumerate(sents[:3]):
            if _is_step_intro(cleaned[cs:ce]):
                intro_idx = i
                break
        if intro_idx is None:
            # No explicit intro — use the first sentence by default.
            intro_idx = 0
        cs, ce = sents[intro_idx]
        if overlaps_claimed(cs, ce):
            continue
        claim(cs, ce)
        chapter_sentence_by_seg[id(seg)] = intro_idx
        suggested.append({
            "id": "vt" + uuid.uuid4().hex[:12],
            "char_start": cs,
            "char_end": ce,
            "type": "chapter",
            "label": seg.get("name") or None,
        })

    # 2) Diagrams — span a 3-5 sentence PARAGRAPH inside the step body. Pick
    #    the window by keyword-matching the diagram's name + vision keywords
    #    (extracted from the actual image via Gemini Flash, cached on the row)
    #    against the script.
    rendered_diagrams = [r for r in drows if _diagram_available(r)]
    for r in drows:
        if not _diagram_available(r):
            skipped.append({
                "diagram_id": r["id"],
                "name": r["name"],
                "reason": "no image_path or result_url — skipped",
            })

    # --- Gemini vision keyword extraction (cached on diagrams.vision_keywords).
    # For diagrams with image_path but no cached keywords, call Gemini in
    # parallel and persist. ~$0.0001 per diagram, ~2-4s for 7 diagrams.
    vkw_by_id = {}
    for r in rendered_diagrams:
        cached = r["vision_keywords"]
        if cached:
            try:
                parsed = json.loads(cached)
                if isinstance(parsed, list):
                    vkw_by_id[r["id"]] = [str(k).lower() for k in parsed if k]
            except (TypeError, ValueError):
                pass
    need_vision = [
        r for r in rendered_diagrams
        if r["id"] not in vkw_by_id and r["image_path"]
    ]
    vision_api_key = os.environ.get("GEMINI_API_KEY", "")
    if need_vision and vision_api_key:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch_and_describe(row):
            try:
                img_full = Path(app.root_path) / row["image_path"]
                if not img_full.exists():
                    return row["id"], None
                with open(img_full, "rb") as f:
                    img_bytes = f.read()
                kws = _gemini_diagram_keywords(img_bytes, vision_api_key)
                return row["id"], kws
            except Exception as e:
                app.logger.warning(f"diagram-vision: {row['id']} failed: {e}")
                return row["id"], None

        with ThreadPoolExecutor(max_workers=min(8, len(need_vision))) as ex:
            futs = {ex.submit(_fetch_and_describe, r): r for r in need_vision}
            for fut in as_completed(futs):
                did, kws = fut.result()
                if kws:
                    vkw_by_id[did] = kws

        # Persist newly-computed keywords so the next click is instant.
        if vkw_by_id:
            conn2 = get_db()
            for did in [r["id"] for r in need_vision if r["id"] in vkw_by_id]:
                conn2.execute(
                    "UPDATE diagrams SET vision_keywords=? WHERE id=?",
                    (json.dumps(vkw_by_id[did]), did),
                )
            conn2.commit()
            conn2.close()

    # Global window pool: every 3-sentence window across the WHOLE doc,
    # excluding any that overlap a chapter-intro sentence already claimed.
    _STOP_WORDS = {
        "step", "diagram", "chapter", "with", "from", "into", "this",
        "that", "the", "and", "for", "your", "you", "have", "what",
        "when", "where", "will", "just", "like", "more",
    }
    all_windows = []  # list of (cs, ce, seg_id, start_idx_in_seg)
    for seg, sents in seg_sentences:
        # Skip past chapter intro and trailing-most sentence.
        body_start_idx = chapter_sentence_by_seg.get(id(seg), -1) + 1
        body = sents[body_start_idx:]
        if len(body) > 3:
            body = body[:-1]
        for s in range(max(1, len(body) - 2)):
            window = body[s:s + 3]
            if not window:
                continue
            if any(overlaps_claimed(c, e) for (c, e) in window):
                continue
            all_windows.append((window[0][0], window[-1][1], id(seg), s, seg))

    def _kws_for_diagram(r):
        name = r["name"] or ""
        kw_source = re.sub(r"[^a-zA-Z\s]", " ", name)
        name_kws = [
            w.lower()
            for w in kw_source.split()
            if len(w) >= 4 and w.lower() not in _STOP_WORDS
        ]
        vision_kws = vkw_by_id.get(r["id"]) or []
        vision_kws = [
            v for v in vision_kws
            if v and not all(w in _STOP_WORDS for w in v.split())
        ]
        return name_kws, vision_kws

    # For each diagram, find its top scoring window in the WHOLE doc. Then
    # greedy-assign: sort by best-score desc, place each at its best
    # unclaimed window.
    diagram_candidates = []
    for r in rendered_diagrams:
        name = r["name"] or ""
        name_kws, vision_kws = _kws_for_diagram(r)
        step_n = _ord(name) or ((r["position"] or 0) + 1)
        # Compute "named step" segment id for soft tiebreaker.
        named_seg = step_segs_by_num.get(step_n)
        if named_seg is None and 0 <= (step_n - 1) < len(step_segs_in_order):
            named_seg = step_segs_in_order[step_n - 1]
        named_seg_id = id(named_seg) if named_seg else None
        # Score every window for this diagram.
        scored = []
        for (wcs, wce, seg_id, _s, _seg) in all_windows:
            window_text = cleaned[wcs:wce].lower()
            score = 0
            for kw in name_kws:
                hits = window_text.count(kw)
                if hits:
                    score += 15 + min(hits, 3) * 5
            for vkw in vision_kws:
                hits = window_text.count(vkw)
                if hits:
                    score += 30 + min(hits, 3) * 10
            # Soft bonus if the window is inside the diagram's named step.
            if seg_id == named_seg_id:
                score += 8
            scored.append((score, wcs, wce, seg_id))
        scored.sort(key=lambda x: -x[0])
        diagram_candidates.append({
            "row": r,
            "ranked": scored,  # list of (score, cs, ce, seg_id) desc
        })

    # Greedy assignment by top-score across diagrams. The diagram with the
    # strongest signal places first; later diagrams skip claimed windows.
    diagram_candidates.sort(
        key=lambda d: -(d["ranked"][0][0] if d["ranked"] else 0)
    )
    for cand in diagram_candidates:
        r = cand["row"]
        ranked = cand["ranked"]
        placed = False
        for (_score, cs, ce, _seg_id) in ranked:
            if overlaps_claimed(cs, ce):
                continue
            claim(cs, ce)
            suggested.append({
                "id": "vt" + uuid.uuid4().hex[:12],
                "char_start": cs,
                "char_end": ce,
                "type": "diagram",
                "asset_id": r["id"],
                "label": r["name"] or None,
            })
            placed = True
            break
        if not placed:
            skipped.append({
                "diagram_id": r["id"],
                "name": r["name"],
                "reason": "no free paragraph anywhere in script",
            })

    # 2) Screen tags from trigger phrases — any sentence containing one.
    for seg, sents in seg_sentences:
        for (cs, ce) in sents:
            if overlaps_claimed(cs, ce):
                continue
            sentence_text = cleaned[cs:ce]
            if _SCREEN_PHRASE_RE.search(sentence_text):
                claim(cs, ce)
                suggested.append({
                    "id": "vt" + uuid.uuid4().hex[:12],
                    "char_start": cs,
                    "char_end": ce,
                    "type": "screen",
                    "label": None,
                })

    # 3) Avatar sprinkle — at most ONE per step. Avatar is "salt"; we want a
    #    short, punchy one-liner (≤ 100 chars) ending in a strong terminator.
    #    Score candidates and pick the best.
    # Sweet spot for avatar: ~4-7s spoken = 50-100 chars at typical pace.
    # Up to 130 chars (~10s) is acceptable. Avoid very short (<35) and very
    # long (>130) sentences.
    _AVATAR_OPENER_RE = re.compile(
        r"^\s*(but|that's|that is|here's|here is|the truth|the trick|"
        r"the reality|the result|the difference|the point|the thing|"
        r"the magic|the moment|the way|what (?:happens|matters)|"
        r"think about|imagine|now think|now imagine)\b",
        re.IGNORECASE,
    )
    _AVATAR_EMPHATIC_RE = re.compile(
        r"\b(?:never|always|literally|every|nobody|nothing|fast|free|"
        r"forever|exactly|actually|really|truly|finally|magic|"
        r"changes everything|game[- ]?changer|here'?s why)\b",
        re.IGNORECASE,
    )
    for seg, sents in seg_sentences:
        if not sents:
            continue
        best = None  # (score, idx, cs, ce)
        for idx, (cs, ce) in enumerate(sents):
            if overlaps_claimed(cs, ce):
                continue
            length = ce - cs
            if length < 35 or length > 130:
                continue
            text = cleaned[cs:ce].strip()
            score = 0
            # Reward sentences near the 70-char sweet spot.
            score += 100 - abs(length - 75)
            # Reward strong terminators.
            if text.endswith("!"):
                score += 40
            elif text.endswith("."):
                score += 15
            # Big reward for emphatic openers.
            if _AVATAR_OPENER_RE.search(text):
                score += 50
            # Reward emphatic content words.
            if _AVATAR_EMPHATIC_RE.search(text):
                score += 30
            # Penalize first sentence of a step (likely chapter intro).
            if idx == 0:
                score -= 60
            # Penalize last sentence (might be chapter lead).
            if idx == len(sents) - 1:
                score -= 30
            if best is None or score > best[0]:
                best = (score, idx, cs, ce)
        if best is None:
            continue
        _score, _idx, cs, ce = best
        claim(cs, ce)
        suggested.append({
            "id": "vt" + uuid.uuid4().hex[:12],
            "char_start": cs,
            "char_end": ce,
            "type": "avatar",
            "label": None,
        })

    # 4) Text-anim sprinkle: punctuation/emphasis ONLY — a handful of
    #    sentences with a strong feel ("...", em-dashes, exclamation, all-caps
    #    words) get tagged for an animated text overlay. Cap at 20.
    text_anim_cap = 20
    text_anim_count = 0
    _TEXT_ANIM_HINT_RE = re.compile(
        r"(?:\.{3}|!|\b(?:never|always|literally|every single|nobody|nothing)\b|"
        r"—\s*[A-Z]|\b[A-Z]{4,}\b)",
    )
    for (_seg, sents) in seg_sentences:
        if text_anim_count >= text_anim_cap:
            break
        for (cs, ce) in sents:
            if text_anim_count >= text_anim_cap:
                break
            if overlaps_claimed(cs, ce):
                continue
            if (ce - cs) < 20:
                continue
            if not _TEXT_ANIM_HINT_RE.search(cleaned[cs:ce]):
                continue
            claim(cs, ce)
            suggested.append({
                "id": "vt" + uuid.uuid4().hex[:12],
                "char_start": cs,
                "char_end": ce,
                "type": "text_anim",
                "label": None,
            })
            text_anim_count += 1

    # 5) Screen as default — every remaining unclaimed sentence becomes a
    #    screen tag. Andy wants the full script colored end-to-end; untagged
    #    space looks empty in the Visuals tab even though it'd render fine.
    for (_seg, sents) in seg_sentences:
        for (cs, ce) in sents:
            if overlaps_claimed(cs, ce):
                continue
            if (ce - cs) < 6:
                continue
            claim(cs, ce)
            suggested.append({
                "id": "vt" + uuid.uuid4().hex[:12],
                "char_start": cs,
                "char_end": ce,
                "type": "screen",
                "label": None,
            })

    # Merge with existing tags. Preserve manual `chapters` tags entirely;
    # drop the other auto types if replace=true.
    try:
        existing = json.loads(vrow["visual_tags"]) if vrow["visual_tags"] else []
    except (TypeError, ValueError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    auto_types = {"diagram", "avatar", "screen", "text_anim", "chapter"}
    if replace:
        existing = [t for t in existing if t.get("type") not in auto_types]

    def _overlaps_tag(a, b):
        try:
            return int(a["char_start"]) < int(b["char_end"]) and int(a["char_end"]) > int(b["char_start"])
        except (TypeError, ValueError, KeyError):
            return False
    keep = [t for t in existing if not any(_overlaps_tag(t, s) for s in suggested)]

    merged = _sanitize_visual_tags(keep + suggested)

    conn = get_db()
    conn.execute(
        "UPDATE videos SET visual_tags=? WHERE id=?",
        (json.dumps(merged), vid),
    )
    conn.commit()
    conn.close()

    counts = {}
    for t in suggested:
        counts[t["type"]] = counts.get(t["type"], 0) + 1
    return jsonify({
        "tags": merged,
        "suggested_count": len(suggested),
        "counts": counts,
        "skipped": skipped,
    })


@app.route("/api/videos/<int:vid>/visual-tags/apply", methods=["POST"])
def apply_visual_tags(vid):
    """Resolve each tag to a concrete timeline placement using the latest
    voiceover take's duration. Char offsets are in the CLEANED vocal_doc
    (no `=== STEP N ===` lines, no normalization beyond strip), matching the
    same space the editor renders text in."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    vrow = conn.execute(
        "SELECT vocal_doc, visual_tags FROM videos WHERE id=?", (vid,)
    ).fetchone()
    drows = conn.execute(
        "SELECT id, name, image_path, result_url, audio_duration FROM diagrams WHERE video_id=?",
        (vid,)
    ).fetchall()
    conn.close()
    if not vrow:
        return jsonify({"error": "video not found"}), 404

    raw_text = (vrow["vocal_doc"] or "")
    cleaned, _ = _strip_and_segment(raw_text)
    total_chars = max(1, len(cleaned))

    try:
        tags = json.loads(vrow["visual_tags"]) if vrow["visual_tags"] else []
    except (TypeError, ValueError):
        tags = []
    if not isinstance(tags, list) or not tags:
        return jsonify({"placements": [], "tag_count": 0})

    state = _load_voiceover_state(vid)
    gens = state.get("generations", [])
    duration = 0.0
    for g in reversed(gens):
        d = g.get("duration") or g.get("audio_duration") or 0
        if d:
            duration = float(d)
            break
        segs = g.get("segments") or []
        if segs:
            duration = float(segs[-1].get("end", 0)) or duration
            if duration:
                break
    if duration <= 0:
        duration = max(30.0, total_chars / 13.0)
        method = "estimate-13cps"
    else:
        method = "linear-from-take-duration"

    diagram_by_id = {d["id"]: d for d in drows}

    placements = []
    for t in tags:
        try:
            cs = max(0, int(t.get("char_start", 0)))
            ce = max(cs + 1, int(t.get("char_end", 0)))
        except (TypeError, ValueError):
            continue
        cs = min(cs, total_chars)
        ce = min(ce, total_chars)
        if ce <= cs:
            continue
        start = duration * (cs / total_chars)
        end = duration * (ce / total_chars)
        p = {
            "tag_id": t.get("id"),
            "type": (t.get("type") or "").lower(),
            "char_start": cs,
            "char_end": ce,
            "start": round(start, 3),
            "end": round(end, 3),
            "label": t.get("label"),
            "matched": True,
        }
        if p["type"] == "diagram":
            d = diagram_by_id.get(t.get("asset_id") or "")
            if not d:
                p["matched"] = False
                p["reason"] = "diagram not found"
            elif not (d["result_url"] or d["image_path"]):
                p["matched"] = False
                p["reason"] = "diagram has no rendered MP4 or image"
            else:
                # Prefer rendered MP4 (with animation), fall back to still image.
                if d["result_url"]:
                    p["result_url"] = _proxy_asset_url(d["result_url"])
                    p["asset_kind"] = "video"
                else:
                    rel = d["image_path"]
                    p["result_url"] = ("/" + rel) if rel and not rel.startswith("/") else rel
                    p["asset_kind"] = "image"
                p["diagram_id"] = d["id"]
                p["diagram_name"] = d["name"]
        placements.append(p)

    return jsonify({
        "placements": placements,
        "duration": duration,
        "method": method,
        "total_chars": total_chars,
        "tag_count": len(tags),
    })


# ---------------------------------------------------------------------------
# Auto-place diagrams on the editor timeline by verbatim-matching each
# diagram's stored `script` against the spoken vocal_doc.
# ---------------------------------------------------------------------------

def _normalize_for_match(s: str) -> str:
    """Lowercase, collapse whitespace, strip most punctuation. Returns a
    (normalized_text, idx_map) pair via the wrapper below. This helper just
    produces the normalized string; positions are computed by the wrapper."""
    out = []
    for ch in s.lower():
        if ch.isalnum() or ch == " ":
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
        # drop everything else (punctuation, symbols)
    # collapse runs of spaces
    norm = re.sub(r"\s+", " ", "".join(out)).strip()
    return norm


def _normalize_with_index_map(s: str):
    """Like _normalize_for_match but also returns a parallel array `idx_map`
    where idx_map[i] = the index in the ORIGINAL string corresponding to the
    i-th char of the normalized string. Used to map a match back to the
    original cleaned vocal_doc's char offsets."""
    norm_chars = []
    idx_map = []
    last_was_space = True  # so leading whitespace is collapsed away
    for i, ch in enumerate(s):
        if ch.isalnum():
            norm_chars.append(ch.lower())
            idx_map.append(i)
            last_was_space = False
        elif ch.isspace() or not ch.isalnum():
            if not last_was_space:
                norm_chars.append(" ")
                idx_map.append(i)
                last_was_space = True
            # else skip (collapse)
    # trim trailing space
    while norm_chars and norm_chars[-1] == " ":
        norm_chars.pop()
        idx_map.pop()
    return "".join(norm_chars), idx_map


def _find_script_in_doc(script: str, cleaned_doc: str):
    """Locate `script` inside `cleaned_doc` with whitespace/punctuation-tolerant
    matching. Returns (char_start, char_end) into `cleaned_doc`, or None if no
    confident match.

    Strategy: normalize both, search for the full normalized script first; if
    not found, fall back to the first ~12 words of the script as a key (handles
    diagrams whose `script` drifted slightly from the final vocal doc)."""
    if not script or not cleaned_doc:
        return None
    norm_doc, doc_map = _normalize_with_index_map(cleaned_doc)
    norm_script_full = _normalize_for_match(script)
    if not norm_script_full:
        return None

    def locate(needle: str):
        if not needle or len(needle) < 8:
            return None
        idx = norm_doc.find(needle)
        if idx < 0:
            return None
        end_idx = idx + len(needle) - 1
        char_start = doc_map[idx] if idx < len(doc_map) else None
        char_end = doc_map[end_idx] + 1 if end_idx < len(doc_map) else None
        if char_start is None or char_end is None:
            return None
        return (char_start, char_end)

    # 1) Full script
    hit = locate(norm_script_full)
    if hit:
        return hit
    # 2) First 12 words as anchor — extend end to match the script's word count
    words = norm_script_full.split()
    if len(words) >= 4:
        anchor = " ".join(words[:min(12, len(words))])
        anchor_hit = locate(anchor)
        if anchor_hit:
            # Estimate end by walking the same number of normalized words as
            # the full script. Caps at end-of-doc.
            start_in_norm = norm_doc.find(anchor)
            if start_in_norm >= 0:
                target_word_count = len(words)
                remaining = norm_doc[start_in_norm:]
                rem_words = remaining.split()
                consumed_chars = 0
                taken = 0
                for w in rem_words:
                    if taken >= target_word_count:
                        break
                    next_idx = remaining.find(w, consumed_chars)
                    if next_idx < 0:
                        break
                    consumed_chars = next_idx + len(w)
                    taken += 1
                end_in_norm = start_in_norm + consumed_chars - 1
                if end_in_norm < len(doc_map):
                    end_char = doc_map[end_in_norm] + 1
                    return (anchor_hit[0], end_char)
            return anchor_hit
    return None


@app.route("/api/videos/<int:vid>/diagrams/auto-place", methods=["POST"])
def diagrams_auto_place(vid):
    """For each diagram with a stored `script`, verbatim-match it against the
    cleaned vocal_doc and return its timestamped placement on the timeline.

    Uses the latest voiceover take's stored duration for char→time mapping
    (linear interpolation against cleaned_doc length). If you have segments
    with `char_start_times_seconds` arrays we could swap to exact alignment;
    for now the linear pass matches the rest of the editor's timing math
    and ships in seconds per video."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    vrow = conn.execute(
        "SELECT vocal_doc FROM videos WHERE id=?", (vid,)
    ).fetchone()
    drows = conn.execute(
        "SELECT * FROM diagrams WHERE video_id=? ORDER BY position, created_at",
        (vid,)
    ).fetchall()
    conn.close()

    raw_text = (vrow["vocal_doc"] or "") if vrow else ""
    if not raw_text.strip():
        return jsonify({"error": "vocal_doc is empty — generate the Say doc first"}), 400
    cleaned, doc_segments = _strip_and_segment(raw_text)
    if not cleaned:
        return jsonify({"error": "vocal_doc has no spoken text"}), 400

    # Build a step-number → segment lookup for fallback placement when a
    # diagram has no `script`. Also extract N from "Diagram N" so manually-
    # created diagrams (which never get a Step N: prefix) can still land.
    def _ordinal(s: str):
        m = re.search(r"(?:step|diagram)\s*(\d+)", s or "", re.IGNORECASE)
        return int(m.group(1)) if m else None
    step_segments = {}
    for s in doc_segments:
        n = _ordinal(s.get("name") or "")
        if n is not None and n not in step_segments:
            step_segments[n] = s
    # Build a fallback "even distribution" map: when no script + no matching
    # step segment, we'll place diagrams proportionally by position so they at
    # least spread across the take instead of all stacking at t=0.
    total_diagrams = len(drows)
    # Compute the position rank (sorted by position, then created_at) for each
    # diagram so deletions don't leave gaps in the even distribution.
    sorted_by_pos = sorted(
        drows, key=lambda r: (r["position"] or 0, r["created_at"] or "")
    )
    rank_by_id = {r["id"]: idx for idx, r in enumerate(sorted_by_pos)}

    state = _load_voiceover_state(vid)
    gens = state.get("generations", [])
    duration = 0.0
    for g in reversed(gens):
        d = g.get("duration") or g.get("audio_duration") or 0
        if d:
            duration = float(d)
            break
        segs = g.get("segments") or []
        if segs:
            duration = float(segs[-1].get("end", 0)) or duration
            if duration:
                break
    if duration <= 0:
        duration = max(30.0, len(cleaned) / 13.0)
        method = "estimate-13cps"
    else:
        method = "linear-from-take-duration"

    total_chars = max(1, len(cleaned))

    placements = []
    for r in drows:
        d = _diagram_row_to_dict(r)
        script = (d.get("script") or "").strip()
        result_url = _proxy_asset_url(d.get("result_url") or "")
        if not result_url:
            placements.append({
                "diagram_id": d["id"],
                "name": d.get("name"),
                "matched": False,
                "reason": "no rendered result_url (diagram not rendered yet)",
            })
            continue
        match_method = None
        hit = None
        if script:
            hit = _find_script_in_doc(script, cleaned)
            if hit:
                match_method = "verbatim_script"
        # Fallback 1: no script (or script didn't match) → look up the step
        # segment from "Step N" / "Diagram N" pattern in name → step N span.
        if not hit:
            step_n = _ordinal(d.get("name") or "")
            seg = step_segments.get(step_n) if step_n is not None else None
            if seg is not None and seg.get("char_end") is not None:
                hit = (int(seg["char_start"]), int(seg["char_end"]))
                match_method = "step_fallback"
        # Fallback 2: still no hit → even distribution across take by position
        # rank. Better than skipping; Andy can drag clips to refine.
        if not hit:
            rank = rank_by_id.get(d["id"], 0)
            denom = max(1, total_diagrams)
            slot_chars = total_chars / denom
            cs = int(rank * slot_chars)
            ce = int(min(total_chars, cs + slot_chars))
            if ce > cs:
                hit = (cs, ce)
                match_method = "even_distribution"
        if not hit:
            reason = (
                "script not found in vocal_doc"
                if script
                else f"could not place '{d.get('name')}'"
            )
            placements.append({
                "diagram_id": d["id"],
                "name": d.get("name"),
                "matched": False,
                "reason": reason,
                "script_preview": script[:120] if script else None,
            })
            continue
        char_start, char_end = hit
        start_sec = duration * (char_start / total_chars)
        # Prefer the diagram's own audio_duration when known so the MP4 plays
        # its full length even if the vocal_doc match is shorter. Fall back to
        # the matched span otherwise.
        own_dur = 0.0
        try:
            own_dur = float(d.get("audio_duration") or 0)
        except (TypeError, ValueError):
            own_dur = 0.0
        span_sec = duration * ((char_end - char_start) / total_chars)
        end_sec = start_sec + max(own_dur, span_sec, 1.0)
        placements.append({
            "diagram_id": d["id"],
            "name": d.get("name"),
            "matched": True,
            "match_method": match_method,
            "result_url": result_url,
            "char_start": char_start,
            "char_end": char_end,
            "start": round(start_sec, 3),
            "end": round(end_sec, 3),
            "snippet": cleaned[char_start:min(char_end, char_start + 160)],
        })

    return jsonify({
        "placements": placements,
        "duration": duration,
        "method": method,
        "total_chars": total_chars,
        "matched": sum(1 for p in placements if p.get("matched")),
        "skipped": sum(1 for p in placements if not p.get("matched")),
    })


def _split_text_for_tts(text, max_chars=9500):
    """Split into chunks <= max_chars, preferring paragraph > sentence > word boundaries.
    ElevenLabs free/Creator/Pro endpoint caps each request at 10k chars; we chunk and stitch."""
    if len(text) <= max_chars:
        return [text]

    def by_sentence(p):
        sentences = re.split(r'(?<=[.!?…])\s+', p)
        out, cur = [], ""
        for s in sentences:
            cand = (cur + " " + s) if cur else s
            if len(cand) <= max_chars:
                cur = cand
            else:
                if cur: out.append(cur)
                if len(s) <= max_chars:
                    cur = s
                else:
                    # Final fallback: word-by-word, with hard char-slice for pathological single words
                    words, sub = s.split(), ""
                    for w in words:
                        if len(w) > max_chars:
                            if sub: out.append(sub); sub = ""
                            for i in range(0, len(w), max_chars):
                                out.append(w[i:i+max_chars])
                            continue
                        c2 = (sub + " " + w) if sub else w
                        if len(c2) <= max_chars:
                            sub = c2
                        else:
                            if sub: out.append(sub)
                            sub = w
                    cur = sub
        if cur: out.append(cur)
        return out

    chunks, cur = [], ""
    for p in text.split("\n\n"):
        if not p.strip():
            continue
        cand = (cur + "\n\n" + p) if cur else p
        if len(cand) <= max_chars:
            cur = cand
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        if len(p) <= max_chars:
            cur = p
        else:
            chunks.extend(by_sentence(p))
            cur = ""
    if cur:
        chunks.append(cur)
    return chunks


def _strip_say_markers(text):
    """Drop === HOOK / STEP N / CLOSING === lines and collapse blank runs."""
    out = []
    for line in (text or "").split("\n"):
        if re.match(r"^\s*=+\s*[A-Z0-9].*=+\s*$", line):
            continue
        out.append(line)
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _strip_and_segment(text):
    """Like _strip_say_markers but also returns step boundaries as character offsets
    into the stripped text.

    Returns (stripped_text, segments) where segments is a list of
    {name, char_start, char_end}. char_end is the offset of the char AFTER the
    segment's last char (so cleaned[char_start:char_end] is the segment body).

    A leading body before the first marker is labelled "INTRO" so it isn't lost.
    """
    if not text:
        return "", []

    raw_segments = []
    out_lines = []
    joined_len = 0  # always equals len("\n".join(out_lines))

    def push_segment(name, start):
        if raw_segments and raw_segments[-1].get("char_end") is None:
            raw_segments[-1]["char_end"] = start
        raw_segments.append({"name": name, "char_start": start, "char_end": None})

    for line in text.split("\n"):
        m = re.match(r"^\s*=+\s*([A-Z0-9][^=]*?)\s*=+\s*$", line)
        if m:
            # Next non-marker char will land at: joined_len + (1 if there's already
            # a line, since "\n".join inserts a newline before appending).
            marker_pos = joined_len + (1 if out_lines else 0)
            push_segment(m.group(1).strip(), marker_pos)
            continue
        sep = 1 if out_lines else 0
        out_lines.append(line)
        joined_len += sep + len(line)

    if raw_segments and raw_segments[-1].get("char_end") is None:
        raw_segments[-1]["char_end"] = joined_len

    cleaned = "\n".join(out_lines)

    # If the doc has body before the first marker, prepend an INTRO segment so
    # the marker timing covers the full audio.
    if (not raw_segments) or (raw_segments and raw_segments[0]["char_start"] > 0):
        raw_segments.insert(0, {
            "name": "INTRO",
            "char_start": 0,
            "char_end": raw_segments[0]["char_start"] if raw_segments else len(cleaned),
        })

    # Drop empty segments (consecutive markers with nothing between them).
    raw_segments = [s for s in raw_segments
                    if (s.get("char_end") or 0) > s["char_start"]]

    # Mirror the lstrip() that the original cleaner does, shifting offsets.
    prefix = len(cleaned) - len(cleaned.lstrip())
    if prefix:
        cleaned = cleaned[prefix:]
        for s in raw_segments:
            s["char_start"] = max(0, s["char_start"] - prefix)
            if s["char_end"] is not None:
                s["char_end"] = max(0, s["char_end"] - prefix)

    # Trailing rstrip — clamp to cleaned length.
    cleaned = cleaned.rstrip()
    n = len(cleaned)
    for s in raw_segments:
        s["char_start"] = min(s["char_start"], n)
        if s["char_end"] is not None:
            s["char_end"] = min(s["char_end"], n)
    raw_segments = [s for s in raw_segments if s["char_end"] > s["char_start"]]

    return cleaned, raw_segments


def _segments_to_times(segments, char_start_seconds, char_end_seconds, total_chars):
    """Convert character-offset segments into time-offset segments using an
    ElevenLabs alignment.

    char_start_seconds / char_end_seconds are parallel arrays whose length matches
    the total character count sent to TTS. total_chars is len(char_start_seconds).
    Returns segments with extra fields start (s), end (s), and a clipped char range.
    """
    if not segments or not char_start_seconds:
        return []
    out = []
    for s in segments:
        cs = max(0, min(s["char_start"], total_chars - 1))
        ce = max(cs + 1, min(s["char_end"], total_chars))
        out.append({
            "name": s["name"],
            "char_start": cs,
            "char_end": ce,
            "start": float(char_start_seconds[cs]),
            "end": float(char_end_seconds[ce - 1]),
        })
    return out


def _load_voiceover_state(vid):
    """Return state dict with a `generations` list. Migrates old single-record
    shape `{url, voice_id, ...}` to `{generations: [{...}]}` transparently."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT voiceover_state FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not row or not row["voiceover_state"]:
        return {"generations": []}
    try:
        state = json.loads(row["voiceover_state"])
    except Exception:
        return {"generations": []}
    if isinstance(state, dict) and "generations" not in state:
        # Legacy single-record shape
        if state.get("url"):
            state = {"generations": [state]}
        else:
            state = {"generations": []}
    if "generations" not in state:
        state["generations"] = []
    return state


def _save_voiceover_state(vid, state):
    conn = get_db()
    conn.execute("UPDATE videos SET voiceover_state=? WHERE id=?",
                 (json.dumps(state), vid))
    conn.commit()
    conn.close()


@app.route("/api/videos/<int:vid>/say/voiceover", methods=["GET"])
def get_say_voiceover(vid):
    return jsonify(_load_voiceover_state(vid))


@app.route("/api/videos/<int:vid>/say/voiceover/<int:idx>/compute-segments",
           methods=["POST"])
def compute_voiceover_segments(vid, idx):
    """Backfill step segments on an existing voiceover take without re-calling
    ElevenLabs. Uses ffprobe for duration + linear char→time interpolation
    against the current vocal_doc. Marker accuracy: roughly within speech-rate
    variation (~1-2s drift per step). Good enough for placement; not exact."""
    state = _load_voiceover_state(vid)
    gens = state.get("generations", [])
    if idx < 0 or idx >= len(gens):
        return jsonify({"error": "take not found"}), 404
    gen = gens[idx]

    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT vocal_doc FROM videos WHERE id=?", (vid,)
    ).fetchone()
    conn.close()
    raw_text = (row["vocal_doc"] or "") if row else ""
    if not raw_text:
        return jsonify({"error": "vocal_doc is empty"}), 400

    cleaned, segments = _strip_and_segment(raw_text)

    # If this take was max-words-truncated, clip the doc to the same length so
    # the linear interpolation maps onto the audio that actually exists.
    target_words = int(gen.get("words") or 0)
    if target_words > 0:
        words = cleaned.split()
        if len(words) > target_words:
            count = 0
            in_word = False
            cut_at = len(cleaned)
            for i, ch in enumerate(cleaned):
                if ch.isspace():
                    if in_word:
                        count += 1
                        if count >= target_words:
                            cut_at = i
                            break
                    in_word = False
                else:
                    in_word = True
            cleaned = cleaned[:cut_at]
            segments = [s for s in segments if s["char_start"] < cut_at]
            for s in segments:
                s["char_end"] = min(s["char_end"], cut_at)
            segments = [s for s in segments if s["char_end"] > s["char_start"]]

    if not segments:
        return jsonify({"error": "no === STEP === markers in vocal_doc"}), 400

    audio_rel = (gen.get("url") or "").lstrip("/")
    if not audio_rel:
        return jsonify({"error": "take has no audio url"}), 400
    audio_path = Path(app.root_path) / audio_rel
    if not audio_path.exists():
        return jsonify({"error": f"audio file missing: {audio_rel}"}), 404
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(r.stdout.strip())
    except Exception as e:
        return jsonify({"error": f"ffprobe failed: {e}"}), 500

    total_chars = max(1, len(cleaned))
    timed = []
    for s in segments:
        timed.append({
            "name": s["name"],
            "char_start": s["char_start"],
            "char_end": s["char_end"],
            "start": round(s["char_start"] / total_chars * duration, 3),
            "end": round(s["char_end"] / total_chars * duration, 3),
        })

    gen["segments"] = timed
    gen["duration"] = duration
    gen["segments_method"] = "linear-approx"
    _save_voiceover_state(vid, state)
    return jsonify({"ok": True, "segments": timed, "duration": duration})


@app.route("/api/videos/<int:vid>/say/voiceover/<int:idx>", methods=["DELETE"])
def delete_say_voiceover(vid, idx):
    state = _load_voiceover_state(vid)
    gens = state.get("generations", [])
    if idx < 0 or idx >= len(gens):
        return jsonify({"error": "index out of range"}), 404
    removed = gens.pop(idx)
    # Best-effort delete of the file
    try:
        rel = (removed.get("url") or "").lstrip("/")
        if rel:
            p = Path(app.root_path) / rel
            if p.exists() and p.is_file():
                p.unlink()
    except Exception:
        pass
    _save_voiceover_state(vid, state)
    return jsonify(state)


def _tts_job_set(job_id, **fields):
    fields["updated_at"] = time.time()
    keys = list(fields.keys())
    set_clause = ", ".join(f"{k}=?" for k in keys)
    vals = [fields[k] for k in keys] + [job_id]
    conn = get_db()
    conn.execute(f"UPDATE tts_jobs SET {set_clause} WHERE id=?", vals)
    conn.commit()
    conn.close()


def _tts_job_get(job_id):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tts_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _tts_run(job_id, vid, text, voice_id, model_id, api_key, segments=None):
    """Background worker — synthesize chunks via ElevenLabs (with-timestamps),
    stitch, save, persist voiceover_state including step segments + alignment.

    `segments` is a list of {name, char_start, char_end} into `text` (the same
    text we send to TTS). When provided, we compute time-offsets per segment
    using the alignment returned by /v1/text-to-speech/<v>/with-timestamps.
    """
    import base64
    from urllib.error import HTTPError, URLError
    try:
        chunks = _split_text_for_tts(text, max_chars=9500)
        _tts_job_set(job_id, status="synthesizing", chunks_total=len(chunks))

        base_voice_settings = {
            "stability": 0.45,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        }
        chunk_audio = []
        # Parallel arrays, one entry per character ElevenLabs reported (across all chunks).
        all_chars = []
        all_char_starts = []   # global seconds
        all_char_ends = []     # global seconds
        time_offset = 0.0
        prev_ids = []
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps"
        for i, chunk in enumerate(chunks):
            payload = {
                "text": chunk,
                "model_id": model_id,
                "voice_settings": base_voice_settings,
            }
            if prev_ids:
                payload["previous_request_ids"] = prev_ids[-3:]
            req = Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                          headers={
                              "xi-api-key": api_key,
                              "Content-Type": "application/json",
                              "Accept": "application/json",
                          })
            try:
                with urlopen(req, timeout=180) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    rid = resp.getheader("request-id") or resp.getheader("x-request-id") or ""
                    if rid:
                        prev_ids.append(rid)
            except HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")[:600]
                _tts_job_set(job_id, status="error",
                             error=f"elevenlabs http {e.code}", detail=detail)
                return
            except URLError as e:
                _tts_job_set(job_id, status="error",
                             error=f"elevenlabs network: {e.reason}")
                return

            try:
                chunk_audio.append(base64.b64decode(body["audio_base64"]))
            except Exception as e:
                _tts_job_set(job_id, status="error",
                             error=f"audio decode failed: {e}",
                             detail=json.dumps(body)[:600])
                return

            align = body.get("alignment") or {}
            chars = align.get("characters") or []
            starts = align.get("character_start_times_seconds") or []
            ends = align.get("character_end_times_seconds") or []
            if chars and starts and ends and len(chars) == len(starts) == len(ends):
                all_chars.extend(chars)
                all_char_starts.extend(s + time_offset for s in starts)
                all_char_ends.extend(e + time_offset for e in ends)
                time_offset += float(ends[-1])
            _tts_job_set(job_id, chunks_done=i + 1)

        _tts_job_set(job_id, status="stitching")

        out_dir = Path(app.root_path) / "static" / "uploads" / "voiceover"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_id = uuid.uuid4().hex[:10]
        out_path = out_dir / f"v{vid}_{out_id}.mp3"

        if len(chunk_audio) == 1:
            out_path.write_bytes(chunk_audio[0])
            audio_bytes = chunk_audio[0]
        else:
            import tempfile, shutil
            work = Path(tempfile.mkdtemp(prefix=f"tts_stitch_{out_id}_"))
            try:
                parts = []
                for k, b in enumerate(chunk_audio):
                    p = work / f"part_{k}.mp3"
                    p.write_bytes(b)
                    parts.append(p)
                list_file = work / "concat.txt"
                list_file.write_text("\n".join(
                    "file '" + str(p).replace("'", "'\\''") + "'" for p in parts
                ))
                cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                       "-i", str(list_file), "-c", "copy", str(out_path)]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                if r.returncode != 0:
                    _tts_job_set(job_id, status="error",
                                 error="ffmpeg concat failed",
                                 detail=r.stderr[-1500:])
                    return
                audio_bytes = out_path.read_bytes()
            finally:
                shutil.rmtree(work, ignore_errors=True)

        rel = str(out_path.relative_to(Path(app.root_path)))
        audio_url = "/" + rel

        duration_s = float(all_char_ends[-1]) if all_char_ends else 0.0
        timed_segments = []
        if segments and all_char_starts and all_char_ends:
            timed_segments = _segments_to_times(
                segments, all_char_starts, all_char_ends, len(all_chars),
            )

        generation = {
            "url": audio_url,
            "voice_id": voice_id,
            "model_id": model_id,
            "words": len(text.split()),
            "chars": len(text),
            "bytes": len(audio_bytes),
            "chunks": len(chunks),
            "duration": duration_s,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "preview": text[:140] + ("…" if len(text) > 140 else ""),
            "segments": timed_segments,
            # Alignment kept for future per-word features (captions, hover-to-seek).
            # Compact form: skip ends (start[i+1] is a fine approximation).
            "alignment": {
                "characters": all_chars,
                "char_start_seconds": [round(s, 3) for s in all_char_starts],
            } if all_chars else None,
        }
        state = _load_voiceover_state(vid)
        state.setdefault("generations", []).append(generation)
        _save_voiceover_state(vid, state)

        _tts_job_set(job_id, status="done", generation_json=json.dumps(generation))
    except Exception as e:
        _tts_job_set(job_id, status="error", error=f"{type(e).__name__}: {e}")


@app.route("/api/videos/<int:vid>/say/synthesize", methods=["POST"])
def synthesize_say(vid):
    """Kick off an async ElevenLabs synthesis. Returns {job_id} immediately;
    client polls /status/<job_id> for progress and final result.

    Body: {text?, max_words?: int (default 200), voice_id?, model_id?}
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        return jsonify({"error": "ELEVENLABS_API_KEY not set on server"}), 500

    body = request.get_json(force=True, silent=True) or {}
    raw_text = (body.get("text") or "").strip()
    if not raw_text:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT vocal_doc FROM videos WHERE id=?", (vid,)).fetchone()
        conn.close()
        raw_text = (row["vocal_doc"] or "") if row else ""

    text, segments = _strip_and_segment(raw_text)
    if not text:
        return jsonify({"error": "no SAY text to synthesize"}), 400

    max_words = int(body.get("max_words") or 200)
    if max_words > 0:
        words = text.split()
        if len(words) > max_words:
            # Find where the max_words-th word ends so we can clip segments cleanly.
            count = 0
            in_word = False
            cut_at = len(text)
            for idx, ch in enumerate(text):
                if ch.isspace():
                    if in_word:
                        count += 1
                        if count >= max_words:
                            cut_at = idx
                            break
                    in_word = False
                else:
                    in_word = True
            text = text[:cut_at]
            segments = [s for s in segments if s["char_start"] < cut_at]
            for s in segments:
                s["char_end"] = min(s["char_end"], cut_at)

    voice_id = (body.get("voice_id") or os.environ.get("ELEVENLABS_VOICE_ID")
                or "21m00Tcm4TlvDq8ikWAM").strip()
    model_id = (body.get("model_id") or os.environ.get("ELEVENLABS_MODEL_ID")
                or "eleven_multilingual_v2").strip()

    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn = get_db()
    conn.execute("""
        INSERT INTO tts_jobs (id, video_id, status, chunks_done, chunks_total, started_at, updated_at)
        VALUES (?, ?, 'queued', 0, 0, ?, ?)
    """, (job_id, vid, now, now))
    conn.commit()
    conn.close()

    t = threading.Thread(target=_tts_run,
                         args=(job_id, vid, text, voice_id, model_id, api_key, segments),
                         daemon=True)
    t.start()

    return jsonify({
        "job_id": job_id,
        "status": "queued",
        "text_chars": len(text),
        "text_words": len(text.split()),
    })


@app.route("/api/videos/<int:vid>/say/synthesize/status/<job_id>", methods=["GET"])
def synthesize_status(vid, job_id):
    job = _tts_job_get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    out = {
        "status": job.get("status"),
        "chunks_done": job.get("chunks_done") or 0,
        "chunks_total": job.get("chunks_total") or 0,
        "error": job.get("error"),
        "detail": job.get("detail"),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
    }
    if job.get("status") == "done" and job.get("generation_json"):
        try:
            out["generation"] = json.loads(job["generation_json"])
        except Exception:
            pass
        state = _load_voiceover_state(vid)
        out["generations"] = state.get("generations", [])
    return jsonify(out)


# ── Editor (CapCut-style timeline) ───────────────────────────────────────

@app.route("/api/videos/<int:vid>/assets", methods=["GET"])
def video_assets(vid):
    """List all rendered assets (diagrams + chapters) for this video."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    out = []
    try:
        diagrams = conn.execute(
            "SELECT id, name, result_url, result_meta_json, updated_at, position FROM diagrams "
            "WHERE video_id=? AND result_url IS NOT NULL ORDER BY position, updated_at",
            (vid,)
        ).fetchall()
        for d in diagrams:
            try:
                meta = json.loads(d["result_meta_json"] or "{}")
            except Exception:
                meta = {}
            out.append({
                "id": "diag_" + d["id"],
                "type": "diagram",
                "source_id": d["id"],
                "name": d["name"] or "diagram",
                "url": d["result_url"],
                "duration": meta.get("duration", 0),
                "updated_at": d["updated_at"],
            })
    except sqlite3.OperationalError:
        pass
    try:
        chapters = conn.execute(
            "SELECT id, name, result_url, result_meta_json, updated_at, position FROM chapters "
            "WHERE video_id=? AND result_url IS NOT NULL ORDER BY position, updated_at",
            (vid,)
        ).fetchall()
        for c in chapters:
            try:
                meta = json.loads(c["result_meta_json"] or "{}")
            except Exception:
                meta = {}
            out.append({
                "id": "chap_" + c["id"],
                "type": "chapter",
                "source_id": c["id"],
                "name": c["name"] or "chapter",
                "url": c["result_url"],
                "duration": meta.get("duration", 0),
                "updated_at": c["updated_at"],
            })
    except sqlite3.OperationalError:
        pass
    conn.close()
    return jsonify(out)


def _read_doc_field(custom_fields, key_name):
    """Resolve a 'Content Doc' / 'Bullet Doc' custom_fields entry to {text, rel_path}.
    rel_path is what PUT /api/content/file expects (relative to CONTENT_DIR). Returns
    {'text':'', 'rel_path':''} if absent or unreadable."""
    target = None
    for f in custom_fields or []:
        if (f.get("key") or "").strip().lower() == key_name.strip().lower():
            target = (f.get("value") or "").strip()
            break
    if not target:
        return {"text": "", "rel_path": ""}
    fname = Path(target).name
    p0 = Path(target)
    candidates = []
    if p0.is_absolute():
        candidates.append(p0)
    candidates += [
        CONTENT_DIR / target,
        CONTENT_DIR / "content_docs" / fname,
        CONTENT_DIR / fname,
    ]
    p = next((c for c in candidates if c.exists()), None)
    if not p:
        return {"text": "", "rel_path": target}
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""
    try:
        rel = str(p.resolve().relative_to(CONTENT_DIR.resolve()))
    except ValueError:
        rel = target
    return {"text": text, "rel_path": rel}


_ASSET_PROXY_ALLOWED_HOSTS = {"media.agentflow.net"}


def _proxy_asset_url(url):
    """If url is on a CORS-blocked allowlisted host, route through /api/asset-proxy so the editor can fetch bytes."""
    from urllib.parse import urlparse, quote
    if not url:
        return url
    try:
        host = urlparse(url).hostname
    except ValueError:
        return url
    if host in _ASSET_PROXY_ALLOWED_HOSTS:
        return "/api/asset-proxy?u=" + quote(url, safe="")
    return url


@app.route("/api/asset-proxy", methods=["GET"])
def asset_proxy():
    """Stream a remote asset back to the browser so cross-origin fetches work.
    Same-origin to the editor (creatorgrowth.com); avoids needing CORS on media subdomain.
    Allowlisted to media.agentflow.net to prevent SSRF."""
    from urllib.parse import urlparse
    from flask import Response, stream_with_context
    url = (request.args.get("u") or "").strip()
    if not url:
        return jsonify({"error": "missing u"}), 400
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in _ASSET_PROXY_ALLOWED_HOSTS:
        return jsonify({"error": "host not allowed"}), 403
    try:
        upstream = urlopen(Request(url, headers={"User-Agent": "creatorgrowth-asset-proxy"}), timeout=30)
    except Exception as e:
        return jsonify({"error": f"upstream fetch failed: {e}"}), 502
    content_type = upstream.headers.get("Content-Type", "application/octet-stream")
    content_length = upstream.headers.get("Content-Length")
    def gen():
        try:
            while True:
                chunk = upstream.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()
    headers = {"Content-Type": content_type, "Cache-Control": "private, max-age=300"}
    if content_length:
        headers["Content-Length"] = content_length
    return Response(stream_with_context(gen()), headers=headers)


# ── Diagram batch generation (Gemini Nano Banana / Image gen) ────────────
# `Generate all` produces 16-bit pixel art illustrations directly from each
# step title — channel signature style. The per-diagram /pixel-art endpoint
# below is a separate transform for already-existing images.

# LOCKED PIXEL_STYLE — copy verbatim from skills DRAWING_TO_PIXEL_ART_SOP.md.
# Never modify per-request.
_DIAGRAM_PIXEL_STYLE = (
    "16-bit SNES pixel art, 16:9 aspect ratio (1920x1080). "
    "Background: deep dark navy blue (#0b1220) with subtle sparse pixel star dots -- "
    "small single white pixels scattered across it like a night sky. "
    "All titles and headers: bold GOLD/YELLOW pixel font (#ffd700). "
    "Labels and body text: white pixel font. "
    "Accent colors for panels: neon green (#00ff88) for positive/right, "
    "coral red (#ff4444) for negative/wrong. "
    "Section borders: thick neon pixel borders matching accent color. "
    "Visible chunky pixel blocks. No gradients, no photography, no smooth lines. "
    "Retro game UI feel -- dramatic, high contrast, readable. "
)

# Planner prompt — Gemini text generates a SOP-compliant image-gen prompt
# per concept, then we feed it to the image model. This mirrors the SOP's
# "Step 2 — Design Each Concept (Mental Only)" stage that a human would do.
_DIAGRAM_PLANNER_PROMPT = (
    "You are the planner for the 'Drawing to Pixel Art' SOP for a YouTube "
    "tutorial video. For ONE step, design a detailed image-generation "
    "prompt that will produce a striking pixel-art DIAGRAM SLIDE. Output "
    "ONLY the prompt body — no markdown, no explanation, no preamble.\n\n"
    "STEP TITLE: {title}\n\n"
    "STEP CONTENT:\n{body}\n\n"
    "==== HARD RULES (NEVER VIOLATE) ====\n\n"
    "**The slide is a DIAGRAM, not a poster.** It MUST have at least 3 "
    "distinct visual elements arranged with spatial separation. NEVER "
    "output a layout that is just 'title + one centered photo/illustration', "
    "and NEVER stop at 2 callouts that only restate the title's words. "
    "Break the concept into a comparison, a multi-stage flow, a hub with "
    "satellites, a process pipeline, or an annotated scene with 3-6 callouts.\n\n"
    "**COVER THE WHOLE STEP, not just the title.** The diagram must "
    "represent the FULL scope of what STEP CONTENT below describes — the "
    "process, the input, the output, the cost, the trick, the contrast. "
    "Read the body content and turn its key beats into visual elements. "
    "If the body has 5 distinct talking points, the diagram should show "
    "all 5 (as pipeline stages, callouts, or panel rows). A 'title plus "
    "two restated buzzwords' is FAILURE — that just regurgitates the "
    "title and ignores 95% of the actual content.\n\n"
    "**If the step describes a PROCESS (command → action → output → "
    "iterate), default to a left-to-right pipeline with 3-5 stages, each "
    "stage labeled with its action and connected by arrows. Show inputs, "
    "outputs, and any cost/time badge in the gap.**\n\n"
    "If you can't think of how to split it, default to: a centered SCENE "
    "with 3-6 labeled callout boxes pointing at parts of the scene. Each "
    "callout = small bordered pixel panel with a pixel-font label + arrow.\n\n"
    "==== LAYOUT PICKER ====\n\n"
    "Pick the layout that fits THIS concept (do NOT default to 'two-panel "
    "bullet list'):\n"
    "- side-by-side split → wrong vs right, before vs after, with/without\n"
    "- left-to-right pipeline → setup steps, multi-stage process, "
    "input→processing→output\n"
    "- top-to-bottom flow → cause→effect, hierarchy\n"
    "- three columns → 3 distinct items or options\n"
    "- **hub-and-spoke** → MULTIPLE tools/brands/options converging on one "
    "thing. ALWAYS pick this when the step talks about chaos, multiple "
    "apps, juggling tools, or 'too many options'. Brand logos go around "
    "the center with connecting lines.\n"
    "- comparison table → feature-by-feature\n"
    "- annotated scene → ONE scene with multiple labeled callouts (use this "
    "INSTEAD of single-photo when the concept is one focal idea)\n\n"
    "==== STRUCTURE EVERY OUTPUT FOLLOWS ====\n\n"
    "1. `TOP CENTER: large bold gold pixel font title: '{title}'.` (mandatory)\n"
    "2. Optional subtitle in white pixel font.\n"
    "3. `LAYOUT: <picked layout>.`\n"
    "4. Per-panel/element descriptions using compass labels (LEFT PANEL, "
    "RIGHT PANEL, CENTER, TOP LEFT, etc.). Each panel/element gets a neon "
    "border color spec like `(neon green border #00ff88, dark green tint)`.\n"
    "5. `BOTTOM CENTER: small white pixel font: <one-sentence takeaway>.` "
    "(mandatory)\n\n"
    "==== BORDER COLORS BY MEANING ====\n"
    "- neon green #00ff88 = positive/right/with-system\n"
    "- coral red #ff4444 = negative/wrong/without-system\n"
    "- gold #ffd700 = neutral/highlight/featured\n"
    "- neon blue #00aaff = stage/neutral flow\n"
    "- neon purple #cc88ff = secondary concepts\n"
    "- neon teal #00ddcc = variety\n\n"
    "==== BRAND LOGOS YOU CAN REFERENCE ====\n"
    "Gemini knows these — describe each as 'pixel art [LOGO NAME] logo':\n"
    "Midjourney (spiral/boat), Canva (rainbow 'C'), Runway (orange play "
    "wedge), Replicate (purple infinity), ChatGPT (green/teal swirl), "
    "OpenAI (white flower), Anthropic Claude (orange leaf), GitHub "
    "(octocat/branch), Photoshop (blue square 'Ps'), DaVinci Resolve "
    "(rainbow camera), CapCut (black scissors), Adobe (red 'A'), Figma "
    "(four colored shapes), Gmail (red M), Slack (multi-color hash), "
    "Notion (white 'N'), Airtable (red/blue/yellow), n8n (orange "
    "hexagons), Loom (blue play), Final Cut (X), Procreate (orange P).\n\n"
    "==== ICONS / SCENES PALETTE ====\n"
    "pixel art terminal with cursor, pixel art robot with glowing eyes, "
    "pixel art creator stick figure (Andy: brown hair, beard, white shirt), "
    "cloud server, lightning bolt, gear, brain, neon green checkmark, "
    "coral red X, clock, dollar sign, pipeline progress bar, ZIP file, "
    "waveform bars, magnifying glass, mountain, fire, key.\n\n"
    "==== EXAMPLE for a 'tools cause chaos' step ====\n"
    "TOP CENTER: large bold gold pixel font title: 'THE PROBLEM'. Subtitle "
    "in white pixel font: 'every visual is a different app'. LAYOUT: "
    "hub-and-spoke. CENTER: a pixel art creator (Andy stick figure: brown "
    "hair, beard, white shirt) at a glowing pixel computer, stressed "
    "expression, red zigzag lightning bolts radiating outward. AROUND "
    "the creator: 4 pixel art brand logos arranged in a circle — TOP "
    "LEFT pixel art Midjourney spiral logo, TOP RIGHT pixel art Runway "
    "orange play wedge, BOTTOM LEFT pixel art Canva rainbow C, BOTTOM "
    "RIGHT pixel art stock-image grid. Each logo sits in a small coral "
    "red (#ff4444) pixel border and a thin coral red dotted line connects "
    "back to the creator. Small white pixel-font label under each logo "
    "naming it. To the right of the hub: a coral red pixel clock reading "
    "'22 MIN' and a coral red dollar-sign with '$$$'. BOTTOM CENTER: small "
    "white pixel font: 'Every visual = a different app. Logins, prompts, "
    "subscriptions, 20+ minutes burned.'\n\n"
    "Now output the prompt body for THIS step, following the rules above. "
    "Just the prompt text — nothing else."
)


# Image-gen prompt is the locked PIXEL_STYLE prefix + the planner output +
# a final hard-rule reminder so the model doesn't degenerate to "title + one photo".
_DIAGRAM_PIXEL_GEN_PROMPT_TPL = (
    "{style}\n\n"
    "{body}\n\n"
    "HARD RULES (apply on top of everything above):\n"
    "- This is a DIAGRAM slide. It MUST contain at least 2 distinct visual "
    "elements with clear spatial separation — never one centered image with "
    "just a title.\n"
    "- Every panel/element must have its own pixel border in a neon color.\n"
    "- Real labels in pixel font, not placeholder text.\n"
    "- If the layout calls for brand logos, render each as recognizable "
    "pixel art (Midjourney spiral, Canva rainbow C, Runway orange wedge, "
    "etc.).\n"
    "- Background is exactly #0b1220.\n"
)

# Flash is what Andy prefers — faster, and quality is fine when the planner
# pre-stages a strong prompt (the 2-step pipeline carries most of the load).
_DIAGRAM_GEMINI_MODEL = "gemini-3.1-flash-image-preview"


# Several patterns Andy's docs use for step headers — we try them in order
# and keep whichever yields the most matches.
_STEP_PATTERNS = [
    # content-doc style: `### Step 1 - Title` / `## Step 1 — Title` / `Step 1: Title`
    re.compile(
        r"^\s*(?:#{2,4}\s+)?Step\s+\d+\s*[-–—:]\s*(.+?)(?:\s*[✅✓☑])?\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # show-doc style: `========== STEP 1 — TITLE ==========` or just `STEP 1 — TITLE`
    re.compile(
        r"^\s*={2,}\s*STEP\s+\d+\s*[-–—:]\s*(.+?)\s*={2,}\s*$",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*STEP\s+\d+\s*[-–—:]\s*(.+?)\s*$",
        re.MULTILINE,
    ),
    # part-style: `Part one: …` / `Part 1 — …`
    re.compile(
        r"^\s*Part\s+(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s*[-–—:]\s*(.+?)\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # Last-resort: Say-doc prose like "let's get into Step 2 — The 2-Minute Setup."
    # Captures from `Step N -` to the next sentence end. Permissive — only used
    # if every line-anchored pattern above produced nothing.
    re.compile(
        r"Step\s+\d+\s*[-–—:]\s*([A-Z][^.!?\n]{2,80}?)(?=[.!?\n])",
        re.IGNORECASE,
    ),
]


def _extract_steps_from_text(text):
    """Pull step titles. Tries multiple patterns; picks the one with the most hits."""
    if not text:
        return []
    best = []
    for pat in _STEP_PATTERNS:
        matches = [m.strip() for m in pat.findall(text) if m.strip()]
        if len(matches) > len(best):
            best = matches
    items, seen = [], set()
    for t in best:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        items.append(t)
    return items


# Header patterns that also capture body (everything until the next header).
# Tried in order; first one that finds 2+ matches wins.
_STEP_SECTION_PATTERNS = [
    # show-doc / "STEP 1 — TITLE" bare uppercase
    re.compile(
        r"^[ \t]*STEP\s+\d+\s*[-–—:]\s*(?P<title>.+?)[ \t]*\n(?P<body>.*?)(?=^[ \t]*STEP\s+\d+\s*[-–—:]|\Z)",
        re.MULTILINE | re.DOTALL,
    ),
    # content-doc "### Step 1 - Title" / "## Step 1 — Title"
    re.compile(
        r"^[ \t]*#{2,4}\s+Step\s+\d+\s*[-–—:]\s*(?P<title>.+?)[ \t]*\n(?P<body>.*?)(?=^[ \t]*#{2,4}\s+Step\s+\d+|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    ),
    # bullet-doc escaped markdown: `\### **Step 1 — Title**`
    re.compile(
        r"^[ \t]*\\?#{2,4}\s+\*+Step\s+\d+\s*[-–—:]\s*(?P<title>.+?)\*+[ \t]*\n(?P<body>.*?)(?=^[ \t]*\\?#{2,4}\s+\*+Step\s+\d+|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    ),
]


def _extract_step_sections_from_text(text):
    """Return [(title, body)] — body is the prose between this step header and the next.
    Body is capped at ~1800 chars so we keep image prompts focused."""
    if not text:
        return []
    best = []
    for pat in _STEP_SECTION_PATTERNS:
        found = []
        for m in pat.finditer(text):
            title = (m.group("title") or "").strip().rstrip("*").strip()
            body = (m.group("body") or "").strip()
            if title:
                found.append((title, body[:1800]))
        if len(found) > len(best):
            best = found
    # Dedupe by title (case-insensitive)
    out, seen = [], set()
    for title, body in best:
        k = title.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append((title, body))
    return out


def _briefs_diagnostic(vid):
    """Counts for each fallback source — used in the 400-error body so the UI can tell the user what to fix."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM chapters WHERE video_id=?", (vid,)).fetchone()
    chapters = 0
    if row:
        try:
            chapters = len(json.loads(row["items_json"] or "[]"))
        except (TypeError, ValueError):
            chapters = 0
    drow = conn.execute(
        "SELECT custom_fields FROM video_details WHERE video_id=?", (vid,)
    ).fetchone()
    conn.close()
    try:
        fields = json.loads(drow["custom_fields"]) if drow and drow["custom_fields"] else []
    except (TypeError, ValueError):
        fields = []
    content_steps = len(_extract_steps_from_text(
        _read_doc_field(fields, "Content Doc").get("text", "")
    ))
    bullet_steps = len(_extract_steps_from_text(
        _read_doc_field(fields, "Bullet Doc").get("text", "")
    ))
    conn2 = get_db()
    conn2.row_factory = sqlite3.Row
    vrow = conn2.execute("SELECT vocal_doc FROM videos WHERE id=?", (vid,)).fetchone()
    conn2.close()
    say_steps = len(_extract_steps_from_text(
        (vrow["vocal_doc"] or "") if vrow else ""
    ))
    return {"chapters": chapters, "content": content_steps, "bullet": bullet_steps, "say": say_steps}


def _derive_diagram_briefs(vid):
    """Return [{name, brief}] in this order of fallback:
    1) chapters.items_json (manually curated)
    2) Steps parsed from the Content Doc file
    3) Steps parsed from the Bullet Doc file
    """
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM chapters WHERE video_id=?", (vid,)).fetchone()
    items = []
    if row:
        try:
            items = json.loads(row["items_json"] or "[]")
        except (TypeError, ValueError):
            items = []
    if not items:
        drow = conn.execute(
            "SELECT custom_fields FROM video_details WHERE video_id=?", (vid,)
        ).fetchone()
        try:
            fields = json.loads(drow["custom_fields"]) if drow and drow["custom_fields"] else []
        except (TypeError, ValueError):
            fields = []
        for key in ("Content Doc", "Bullet Doc"):
            doc = _read_doc_field(fields, key)
            extracted = _extract_steps_from_text(doc.get("text", ""))
            if extracted:
                items = extracted
                break
        if not items:
            # Last resort: try the vocal/say doc (stored in videos.vocal_doc).
            vrow = conn.execute("SELECT vocal_doc FROM videos WHERE id=?", (vid,)).fetchone()
            extracted = _extract_steps_from_text((vrow["vocal_doc"] or "") if vrow else "")
            if extracted:
                items = extracted
    conn.close()
    if not items:
        return []
    # Re-fetch fields here too (the earlier scope only triggers on the fallback path).
    # We attach a body to each title by re-extracting sections from whichever doc has them.
    conn2 = get_db()
    conn2.row_factory = sqlite3.Row
    drow2 = conn2.execute(
        "SELECT custom_fields FROM video_details WHERE video_id=?", (vid,)
    ).fetchone()
    conn2.close()
    try:
        fields2 = json.loads(drow2["custom_fields"]) if drow2 and drow2["custom_fields"] else []
    except (TypeError, ValueError):
        fields2 = []
    sections_by_title = {}
    for key in ("Content Doc", "Bullet Doc"):
        doc_text = _read_doc_field(fields2, key).get("text", "")
        if not doc_text:
            continue
        for (title, body) in _extract_step_sections_from_text(doc_text):
            sections_by_title.setdefault(title.lower(), body)

    def _resolve_body(item_title):
        """Match chapter title (short) to section title (often long, e.g. with
        ': subtitle' suffix). Try exact, then prefix, then either-contains."""
        key = item_title.lower().strip()
        if not key:
            return ""
        if key in sections_by_title:
            return sections_by_title[key]
        # Prefix: chapter "THE PROBLEM" vs section "THE PROBLEM: EVERY VISUAL..."
        for sk, sb in sections_by_title.items():
            if sk.startswith(key):
                return sb
        # Either contains
        for sk, sb in sections_by_title.items():
            if key in sk or sk in key:
                return sb
        return ""

    out = []
    for i, t in enumerate(items):
        title = (t or "").strip()
        body = _resolve_body(title)
        out.append({
            "name": f"Step {i+1}: {title}" if title else f"Step {i+1}",
            "brief": title,
            "body": body,
        })
    return out


def _gemini_plan_diagram_prompt(title, body, api_key):
    """Step 1 of the SOP: Gemini text picks the layout + writes the
    image-gen prompt body. Returns the prompt body string, or None on failure."""
    from urllib.error import HTTPError, URLError
    planner_text = _DIAGRAM_PLANNER_PROMPT.format(title=title, body=body)
    payload = {
        "contents": [{"parts": [{"text": planner_text}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1500,
        },
    }
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.5-flash:generateContent?key=" + api_key)
    req = Request(url, data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        app.logger.warning(f"diagrams.plan: gemini http {e.code}: {detail}")
        return None
    except URLError as e:
        app.logger.warning(f"diagrams.plan: gemini network: {e.reason}")
        return None
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return text or None
    except (KeyError, IndexError, TypeError):
        app.logger.warning(f"diagrams.plan: malformed planner response: {str(data)[:300]}")
        return None


def _gemini_generate_diagram_image(brief_obj, api_key):
    """Two-step pipeline per SOP: planner (text) → renderer (image).
    Returns (PNG bytes, planned_prompt str) or (None, planned_prompt|None) on failure."""
    title = (brief_obj.get("brief") or brief_obj.get("name") or "").upper()
    body = (brief_obj.get("body") or "").strip()
    app.logger.info(
        f"diagrams.gen: title={title!r} body_chars={len(body)} body_preview={body[:160]!r}"
    )
    if not body:
        body = ("(no extra script content — design something visual that fits "
                "the title)")
    planned = _gemini_plan_diagram_prompt(title, body, api_key)
    if planned:
        app.logger.info(
            f"diagrams.plan: title={title!r} planned_chars={len(planned)}\n"
            f"PLANNED PROMPT >>>\n{planned}\n<<<"
        )
    else:
        app.logger.warning(f"diagrams.plan: planner returned NOTHING for title={title!r}")
        planned = (f"TOP CENTER: large bold gold pixel font title: '{title}'. "
                   f"Design a pixel art slide for this step. Pick the layout "
                   f"that best fits the content (hub-and-spoke / split / "
                   f"pipeline / focal point). Use brand logos and scenes "
                   f"where relevant.\n\nCONTENT:\n{body}\n\n"
                   f"BOTTOM CENTER: small white pixel font: one-sentence takeaway.")
    full_prompt = _DIAGRAM_PIXEL_GEN_PROMPT_TPL.format(
        style=_DIAGRAM_PIXEL_STYLE,
        body=planned,
    )
    png = _gemini_image_call(
        [{"text": full_prompt}],
        api_key,
        _DIAGRAM_GEMINI_MODEL,
        "diagrams.gen",
    )
    return png, planned


@app.route("/api/videos/<int:vid>/diagrams/generate-batch", methods=["POST"])
def diagrams_generate_batch(vid):
    """Generate N diagrams in one shot. Body: {briefs?: [{name, brief}]}.
    If briefs missing, derive from chapter items. Runs in parallel."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set on server"}), 500
    body = request.get_json(force=True, silent=True) or {}
    briefs = body.get("briefs") or _derive_diagram_briefs(vid)
    briefs = [b for b in briefs if isinstance(b, dict) and (b.get("brief") or "").strip()]
    if not briefs:
        # Give the UI a useful diagnostic so the user knows WHICH fallback failed.
        diag = _briefs_diagnostic(vid)
        return jsonify({
            "error": (
                "No steps found to generate diagrams from. "
                "Looked in: chapters ({chapters}), Content Doc ({content}), "
                "Bullet Doc ({bullet}), Say Doc ({say}). "
                "Add `Step N - Title` / `STEP N — TITLE` headers, or populate chapters."
            ).format(**diag),
            "diagnostic": diag,
        }), 400
    if len(briefs) > 12:
        briefs = briefs[:12]

    # Replace prior auto-generated diagrams for this video (anything named
    # "Step N: ...") so we don't accumulate stale duplicates each click.
    # Manually-named diagrams (Diagram 1, etc.) are left untouched.
    replace = body.get("replace", True)
    deleted_count = 0
    if replace:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, image_path FROM diagrams WHERE video_id=? AND name LIKE 'Step %:%'",
            (vid,),
        ).fetchall()
        for r in rows:
            # Best-effort: remove on-disk file too
            try:
                ip = r["image_path"]
                if ip:
                    fp = Path(app.root_path) / ip
                    if fp.exists():
                        fp.unlink()
            except Exception:
                pass
        if rows:
            conn.executemany(
                "DELETE FROM diagrams WHERE id=?",
                [(r["id"],) for r in rows],
            )
            conn.commit()
            deleted_count = len(rows)
        conn.close()

    from concurrent.futures import ThreadPoolExecutor, as_completed
    out_root = UPLOAD_DIR / "diagrams" / str(vid)
    out_root.mkdir(parents=True, exist_ok=True)

    def _worker(idx, b):
        png, _planned = _gemini_generate_diagram_image(b, api_key)
        if not png:
            return idx, b, None, None
        diagram_id = "d" + uuid.uuid4().hex[:14]
        fname = f"{diagram_id}.png"
        out_path = out_root / fname
        out_path.write_bytes(png)
        rel = str(out_path.relative_to(Path(app.root_path)))
        return idx, b, diagram_id, rel

    results = [None] * len(briefs)
    with ThreadPoolExecutor(max_workers=min(7, len(briefs))) as ex:
        futures = {ex.submit(_worker, i, b): i for i, b in enumerate(briefs)}
        for fut in as_completed(futures):
            try:
                idx, b, diagram_id, rel = fut.result()
            except Exception as e:
                app.logger.exception(f"diagrams.gen: worker crashed: {e}")
                continue
            results[idx] = (b, diagram_id, rel)

    # Persist successful ones as diagram rows
    conn = get_db()
    conn.row_factory = sqlite3.Row
    pos_row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM diagrams WHERE video_id=?",
        (vid,)
    ).fetchone()
    base_pos = pos_row[0] if pos_row else 0
    created = []
    failures = 0
    now = datetime.now(timezone.utc).isoformat()
    for i, r in enumerate(results):
        if not r or not r[1]:
            failures += 1
            continue
        b, diagram_id, rel = r
        conn.execute(
            "INSERT INTO diagrams (id, video_id, name, boxes_json, image_path, position, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (diagram_id, vid, b["name"], "[]", rel, base_pos + i, now, now),
        )
        row = conn.execute("SELECT * FROM diagrams WHERE id=?", (diagram_id,)).fetchone()
        created.append(_diagram_row_to_dict(row))
    conn.commit()
    conn.close()

    return jsonify({
        "created": created,
        "requested": len(briefs),
        "failed": failures,
        "replaced": deleted_count,
    })


def _gemini_image_call(parts_payload, api_key, model, tag):
    """Shared Gemini image-gen REST call. parts_payload is a list of v1beta parts
    (each {text:...} or {inline_data:{mime_type,data}}). Returns PNG bytes or None."""
    from urllib.error import HTTPError, URLError
    import base64
    payload = {
        "contents": [{"parts": parts_payload}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={api_key}")
    req = Request(url, data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:500]
        except Exception:
            pass
        app.logger.warning(f"{tag}: gemini http {e.code}: {detail}")
        return None
    except URLError as e:
        app.logger.warning(f"{tag}: gemini network: {e.reason}")
        return None
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        app.logger.warning(f"{tag}: malformed gemini response: {str(data)[:300]}")
        return None
    for p in parts:
        inline = p.get("inlineData") or p.get("inline_data")
        if inline and inline.get("data"):
            try:
                return base64.b64decode(inline["data"])
            except Exception as e:
                app.logger.warning(f"{tag}: b64 decode failed: {e}")
                return None
    app.logger.warning(f"{tag}: no image part in response")
    return None


def _gemini_image_call_detail(parts_payload, api_key, model, tag):
    """Like _gemini_image_call but returns (bytes_or_None, error_reason_or_None)
    so callers can surface why a call failed."""
    from urllib.error import HTTPError, URLError
    import base64
    payload = {
        "contents": [{"parts": parts_payload}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={api_key}")
    req = Request(url, data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        reason = f"http {e.code}: {detail}"
        app.logger.warning(f"{tag}: gemini {reason}")
        return None, reason
    except URLError as e:
        reason = f"network: {e.reason}"
        app.logger.warning(f"{tag}: gemini {reason}")
        return None, reason
    except Exception as e:
        reason = f"unexpected: {type(e).__name__}: {e}"
        app.logger.warning(f"{tag}: gemini {reason}")
        return None, reason
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        finish = ""
        try:
            finish = data["candidates"][0].get("finishReason", "")
        except (KeyError, IndexError, TypeError):
            pass
        reason = f"no candidate parts (finishReason={finish or 'unknown'}, raw={str(data)[:200]})"
        app.logger.warning(f"{tag}: {reason}")
        return None, reason
    for p in parts:
        inline = p.get("inlineData") or p.get("inline_data")
        if inline and inline.get("data"):
            try:
                return base64.b64decode(inline["data"]), None
            except Exception as e:
                reason = f"b64 decode: {e}"
                app.logger.warning(f"{tag}: {reason}")
                return None, reason
    finish = ""
    try:
        finish = data["candidates"][0].get("finishReason", "")
    except (KeyError, IndexError, TypeError):
        pass
    reason = f"no image in response (finishReason={finish or 'unknown'})"
    app.logger.warning(f"{tag}: {reason}")
    return None, reason


_DIAGRAM_PIXEL_PROMPT = (
    "Transform this diagram into 16-bit SNES pixel art. Keep every box, arrow, "
    "label, and the overall layout EXACTLY where they are — do not rearrange "
    "anything. Render everything with chunky visible pixels, a flat limited "
    "color palette, no anti-aliasing, no smoothing, no photorealism — pure "
    "retro pixel art aesthetic.\n\n"
    "Color palette (pixel art version of the dark-mode diagram colors): "
    "muted blue fills (#1e3a5f) with light blue borders (#74b9ff), muted amber "
    "(#5c4813 / #ffec99), muted green (#1e4d2b / #8ce99a), muted red (#5c1a1a "
    "/ #ff8787), muted purple (#3b2d6b / #b197fc). Background is a flat dark "
    "navy (#121212). Text is blocky white (#f8f9fa) pixel font. Shape borders "
    "are wobbly 2-3px pixel outlines.\n\n"
    "Sprinkle a few pixel sparkles scattered around the diagram for that "
    "16-bit game-screenshot feel. Output 16:9 aspect ratio (1920x1080)."
)


@app.route("/api/diagrams/<diagram_id>/regenerate", methods=["POST"])
def diagram_regenerate(diagram_id):
    """Redo a single diagram's source image via the same 2-step SOP pipeline
    used by /generate-batch. Reuses the chapter brief associated with this
    diagram's name (Step N: TITLE). Replaces image_path on success."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set"}), 500
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM diagrams WHERE id=?", (diagram_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "diagram not found"}), 404
    vid = row["video_id"]
    name = row["name"] or ""
    conn.close()

    # Find the brief in the current chapter/step list whose title matches.
    briefs = _derive_diagram_briefs(vid)
    match = None
    # `Step N: TITLE` → derive position + title
    m = re.match(r"^\s*Step\s+(\d+)\s*:\s*(.+?)\s*$", name, re.IGNORECASE)
    if m:
        step_num = int(m.group(1))
        title_lc = m.group(2).strip().lower()
        # Prefer position match (1-indexed)
        if 1 <= step_num <= len(briefs):
            cand = briefs[step_num - 1]
            if (cand.get("brief") or "").strip().lower() == title_lc:
                match = cand
        if not match:
            # Fall back to title-only match
            for b in briefs:
                if (b.get("brief") or "").strip().lower() == title_lc:
                    match = b
                    break
    if not match:
        # Last resort: synthesize a brief from the diagram's name
        match = {"name": name, "brief": name.split(":", 1)[-1].strip() or name, "body": ""}

    png, planned = _gemini_generate_diagram_image(match, api_key)
    if not png:
        return jsonify({
            "error": "generation failed (see server logs)",
            "debug": {
                "body_chars": len((match.get("body") or "")),
                "body_preview": (match.get("body") or "")[:300],
                "planned_prompt": planned,
            },
        }), 502
    out_root = UPLOAD_DIR / "diagrams" / str(vid)
    out_root.mkdir(parents=True, exist_ok=True)
    out_path = out_root / f"{diagram_id}_{int(time.time())}.png"
    out_path.write_bytes(png)
    rel = str(out_path.relative_to(Path(app.root_path)))
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    conn.row_factory = sqlite3.Row
    conn.execute("UPDATE diagrams SET image_path=?, updated_at=? WHERE id=?",
                 (rel, now, diagram_id))
    conn.commit()
    row = conn.execute("SELECT * FROM diagrams WHERE id=?", (diagram_id,)).fetchone()
    conn.close()
    out = _diagram_row_to_dict(row)
    # Echo back what the planner saw + produced so the UI can show it for debugging.
    out["debug"] = {
        "body_chars": len((match.get("body") or "")),
        "body_preview": (match.get("body") or "")[:300],
        "planned_prompt": planned,
    }
    return jsonify(out)


@app.route("/api/diagrams/<diagram_id>/pixel-art", methods=["POST"])
def diagram_pixel_art(diagram_id):
    """Transform the diagram's current image into 16-bit pixel art via Nano Banana Pro
    (`gemini-3-pro-image-preview`). Replaces image_path with the new pixel version."""
    import base64
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set"}), 500
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM diagrams WHERE id=?", (diagram_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "diagram not found"}), 404
    img_rel = row["image_path"]
    if not img_rel:
        conn.close()
        return jsonify({"error": "diagram has no source image — generate one first"}), 400
    img_path = Path(app.root_path) / img_rel
    if not img_path.exists():
        conn.close()
        return jsonify({"error": f"image file missing on disk: {img_rel}"}), 404
    src_bytes = img_path.read_bytes()
    parts_payload = [
        {"inline_data": {
            "mime_type": "image/png",
            "data": base64.b64encode(src_bytes).decode("ascii"),
        }},
        {"text": _DIAGRAM_PIXEL_PROMPT},
    ]
    new_bytes = _gemini_image_call(parts_payload, api_key,
                                   "gemini-3-pro-image-preview",
                                   "diagrams.pixel")
    if not new_bytes:
        conn.close()
        return jsonify({"error": "pixel-art generation failed (see server logs)"}), 502
    vid = row["video_id"]
    out_root = UPLOAD_DIR / "diagrams" / str(vid)
    out_root.mkdir(parents=True, exist_ok=True)
    out_path = out_root / f"{diagram_id}_pixel.png"
    out_path.write_bytes(new_bytes)
    rel = str(out_path.relative_to(Path(app.root_path)))
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE diagrams SET image_path=?, updated_at=? WHERE id=?",
                 (rel, now, diagram_id))
    conn.commit()
    row = conn.execute("SELECT * FROM diagrams WHERE id=?", (diagram_id,)).fetchone()
    conn.close()
    return jsonify(_diagram_row_to_dict(row))


_FACELESS_SOP_PATH = Path(__file__).resolve().parent / "prompts" / "thumbnail_faceless.md"
_PIXEL_FACE_SOP_PATH = Path(__file__).resolve().parent / "prompts" / "thumbnail_pixel_face.md"
_FACE_REFS_DIR = Path(__file__).resolve().parent / "assets" / "face_references"
_LOGOS_DIR = Path(__file__).resolve().parent / "assets" / "logos"


def _load_face_refs():
    """Return list of (mime_type, image_bytes) for every PNG/JPG in the bundled
    face_references dir (alphabetical). Andy curates this folder directly —
    drop in / rename / delete files and the next request picks up the change."""
    import mimetypes
    refs = []
    if not _FACE_REFS_DIR.exists():
        return refs
    for p in sorted(_FACE_REFS_DIR.iterdir()):
        if p.name.startswith("."):
            continue
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        refs.append((mime, p.read_bytes()))
    return refs


def _load_logo(name):
    """Return (mime_type, png_bytes) for a logo, or None if missing."""
    import mimetypes
    p = _LOGOS_DIR / f"{name}.png"
    if not p.exists():
        return None
    mime = mimetypes.guess_type(str(p))[0] or "image/png"
    return (mime, p.read_bytes())


_PIXEL_FACE_PROMPT_TEMPLATE = """\
These are reference photos. The first [N_FACES] image(s) are my face —
I have long flowing brown hair that falls past my shoulders and a full
beard. The final image is the [BRAND NAME]: [BRAND DESCRIPTION].

BRAND LOGO PLACEMENT RULE (critical): The [BRAND NAME] MUST appear in
the final image EXACTLY as it looks in the reference image — identical
colors, identical shape, identical [BRAND KEY FEATURES], identical
outline. Treat it like a sticker being pasted onto the scene. DO NOT
redraw it in pixel art style. DO NOT add visible pixels to it. DO NOT
change its proportions. DO NOT change its expression. The logo stays
clean, flat, and crisp — it is the only element in the image that is
NOT pixel art.

Everything ELSE in the image is 16-bit SNES pixel art: chunky visible
pixels, flat limited color palette, no anti-aliasing, no smoothing,
no photorealism, pure retro pixel art aesthetic.

STRICT LAYOUT (follow exactly):

LEFT THIRD (left side of the 16:9 frame): a LARGE close-up pixel art
portrait of me — head and just the tops of my shoulders, zoomed IN.
My head and hair FILL almost the entire left third vertically
(top-to-bottom). This is a close-up portrait, NOT a small distant
avatar. Long flowing brown hair past the shoulders (flowing down to
the bottom of the frame on both sides of the face). Full brown beard.
Plain white t-shirt (only neckline and shoulder tops visible). The
face LIKENESS must match the reference photos — same eyes, nose,
beard shape, recognizable as the same person. Confident excited
expression, slight smile, looking directly at the viewer. Pixel art
— chunky pixels, flat colors, but detailed enough at this close-up
size to capture the likeness. Must still read at 320×180 preview
size (YouTube thumbnail scale).

TOP RIGHT (upper half of the right two-thirds): bold 16-bit pixel art
text [TITLE_INSTRUCTION] on two lines. Vertical gradient from bright
yellow (#FFD700) to deep gold (#E8A800). Thick dark navy pixel shadow
behind the text. Text fills the upper right portion of the frame.

BOTTOM RIGHT (lower half of the right two-thirds): the [BRAND NAME]
PASTED AS-IS from the reference image, NOT redrawn in pixel art.
Covering approximately ONE QUARTER (25%) of the total screen area.
[BRAND KEY FEATURES]. Looks like the reference PNG placed directly
onto the scene.

BACKGROUND: dark navy pixel art with subtle blue glow, a few pixel
sparkles scattered around the title text and the brand logo. Pure
pixel art background, no gradients.

Output must be 16:9 aspect ratio (1920x1080). The brand logo in the
bottom right must look IDENTICAL to the reference image (flat, crisp,
not pixelated) — every other element must be pure 16-bit SNES pixel
art style.
"""


_DEFAULT_BRAND = {
    "logo_name": "claude_code",
    "brand_name": "Claude Code logo character",
    "brand_description": (
        "a chunky flat orange creature with a rounded rectangular body, "
        "'>' and '<' black eyes, short stubby legs, and a crisp white outline"
    ),
    "brand_key_features": (
        "orange body, '>' and '<' eyes, white outline, stubby legs"
    ),
}


def _build_pixel_face_prompt(video_title, brand=None):
    """Return (prompt_text, [(mime,bytes), ...]) for the pixel-face direct call.
    Substitutes the locked SOP template with brand info + tells Gemini to
    pick its own 2-4 word [TITLE TEXT] from the full video title."""
    brand = brand or _DEFAULT_BRAND
    title_instruction = (
        f"(pick a punchy 2-4 word version yourself of this full video title: "
        f"\"{video_title}\" — examples: \"71,000 Creators Installed This Skill\" "
        f"→ \"71K INSTALLS\"; \"You Can Get Claude Code Free Now\" → \"NOW FREE\")"
    )
    face_refs = _load_face_refs()
    template = (_PIXEL_FACE_PROMPT_TEMPLATE
                .replace("[N_FACES]", str(len(face_refs)))
                .replace("[BRAND NAME]", brand["brand_name"])
                .replace("[BRAND DESCRIPTION]", brand["brand_description"])
                .replace("[BRAND KEY FEATURES]", brand["brand_key_features"])
                .replace("[TITLE_INSTRUCTION]", title_instruction))
    image_refs = list(face_refs)
    logo = _load_logo(brand["logo_name"])
    if logo:
        image_refs.append(logo)
    return template, image_refs


def _allocate_thumb_slots(thumbs, titles, n):
    """Walk thumbs left-to-right, return n empty indices, extending the array by 9
    (and padding titles to match) as needed. Mutates thumbs and titles in place."""
    empty_slots = []
    i = 0
    while len(empty_slots) < n:
        if i >= len(thumbs):
            thumbs.extend([""] * 9)
        if not thumbs[i]:
            empty_slots.append(i)
        i += 1
    while len(titles) < len(thumbs):
        titles.append("")
    return empty_slots


def _read_video_thumb_state(conn, vid):
    """Return (title, thumbs_list, titles_list, drow_exists)."""
    conn.row_factory = sqlite3.Row
    vrow = conn.execute("SELECT title FROM videos WHERE id=?", (vid,)).fetchone()
    if not vrow:
        return None
    title = (vrow["title"] or "").strip()
    drow = conn.execute(
        "SELECT original_thumbs, original_titles FROM video_details WHERE video_id=?",
        (vid,),
    ).fetchone()
    if drow and drow["original_thumbs"]:
        thumbs = json.loads(drow["original_thumbs"])
    else:
        thumbs = ["", "", "", "", "", "", "", "", ""]
    if not isinstance(thumbs, list):
        thumbs = ["", "", "", "", "", "", "", "", ""]
    if drow and drow["original_titles"]:
        titles = json.loads(drow["original_titles"])
    else:
        titles = ["", "", "", "", "", "", "", "", ""]
    if not isinstance(titles, list):
        titles = ["", "", "", "", "", "", "", "", ""]
    return title, thumbs, titles, bool(drow)


def _persist_thumb_state(conn, vid, thumbs, titles, drow_exists):
    if drow_exists:
        conn.execute(
            "UPDATE video_details SET original_thumbs=?, original_titles=? WHERE video_id=?",
            (json.dumps(thumbs), json.dumps(titles), vid),
        )
    else:
        conn.execute(
            "INSERT INTO video_details (video_id, original_thumbs, original_titles) VALUES (?, ?, ?)",
            (vid, json.dumps(thumbs), json.dumps(titles)),
        )
    conn.commit()


def _gemini_thumb_direct(vid, *, kind, prompt_builder, slug):
    """Shared direct-to-Gemini thumbnail generator.

    kind: short identifier used in filenames + telemetry (e.g. 'faceless', 'pixel_face')
    prompt_builder: callable(video_title) -> (prompt_text, [(mime,bytes), ...])
    slug: filename prefix for outputs
    """
    from concurrent.futures import ThreadPoolExecutor
    import base64
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set"}), 500

    body = request.get_json(silent=True) or {}
    try:
        n = int(body.get("count", 3))
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(n, 6))

    conn = get_db()
    state = _read_video_thumb_state(conn, vid)
    if state is None:
        conn.close()
        return jsonify({"error": "video not found"}), 404
    title, thumbs, titles, drow_exists = state
    if not title:
        conn.close()
        return jsonify({"error": "video has no title"}), 400

    empty_slots = _allocate_thumb_slots(thumbs, titles, n)

    prompt, image_refs = prompt_builder(title)

    image_parts = [
        {"inline_data": {
            "mime_type": mime,
            "data": base64.b64encode(b).decode("ascii"),
        }}
        for mime, b in image_refs
    ]

    def _gen(variant_idx, attempt):
        parts_payload = image_parts + [{"text": prompt}]
        return _gemini_image_call_detail(
            parts_payload, api_key, "gemini-3-pro-image-preview",
            f"thumb.gemini-{kind}.v{variant_idx}.a{attempt}",
        )

    # Pass 1: fire all n in parallel
    results = [None] * n  # each entry: {"png": bytes|None, "error": str|None, "attempts": int}
    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = {ex.submit(_gen, i, 1): i for i in range(n)}
        for fut in futures:
            i = futures[fut]
            png, err = fut.result()
            results[i] = {"png": png, "error": err, "attempts": 1}

    # Pass 2: retry each failed slot once
    retry_idxs = [i for i, r in enumerate(results) if r["png"] is None]
    if retry_idxs:
        with ThreadPoolExecutor(max_workers=len(retry_idxs)) as ex:
            futures = {ex.submit(_gen, i, 2): i for i in retry_idxs}
            for fut in futures:
                i = futures[fut]
                png, err = fut.result()
                results[i] = {"png": png, "error": err, "attempts": 2}

    out_root = UPLOAD_DIR / "thumbs" / str(vid)
    out_root.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    written = []
    failures = []
    for variant_idx, res in enumerate(results):
        slot = empty_slots[variant_idx]
        if res["png"]:
            out_path = out_root / f"{slug}_{ts}_{variant_idx + 1}.png"
            out_path.write_bytes(res["png"])
            rel_url = "/" + str(out_path.relative_to(Path(app.root_path))).replace(os.sep, "/")
            thumbs[slot] = rel_url
            written.append({
                "slot_index": slot,
                "slot_label": slot + 1,
                "url": rel_url,
                "attempts": res["attempts"],
            })
        else:
            failures.append({
                "slot_label": slot + 1,
                "attempts": res["attempts"],
                "error": res["error"] or "unknown",
            })

    if not written:
        conn.close()
        return jsonify({
            "error": "all gemini calls failed",
            "failures": failures,
        }), 502

    _persist_thumb_state(conn, vid, thumbs, titles, drow_exists)
    conn.close()
    return jsonify({
        "ok": True,
        "kind": kind,
        "count": len(written),
        "requested": n,
        "thumbs": written,
        "failures": failures,
        "original_thumbs": thumbs,
        "original_titles": titles,
        "prompt_chars": len(prompt),
        "image_refs": len(image_refs),
    })


def _faceless_prompt_builder(video_title):
    """Build the faceless prompt — embeds the full faceless SOP since it's
    layout-pattern-driven (Gemini picks one of the 6)."""
    sop_text = _FACELESS_SOP_PATH.read_text() if _FACELESS_SOP_PATH.exists() else ""
    prompt = (
        f"Make a YouTube thumbnail for a video titled: \"{video_title}\".\n\n"
        f"Follow this SOP exactly — the channel signature, locked style "
        f"constants, and one of the 6 layout patterns. Pick the layout pattern "
        f"that best fits the title. Output 16:9, 1920x1080.\n\n"
        f"=== SOP START ===\n{sop_text}\n=== SOP END ==="
    )
    return prompt, []


@app.route("/api/videos/<int:vid>/gemini-faceless", methods=["POST"])
@login_required
def gemini_faceless(vid):
    """Direct-to-Gemini faceless thumbnails — no Claude judgment, no face refs."""
    if not _FACELESS_SOP_PATH.exists():
        return jsonify({"error": f"SOP missing: {_FACELESS_SOP_PATH}"}), 500
    return _gemini_thumb_direct(
        vid,
        kind="faceless",
        prompt_builder=_faceless_prompt_builder,
        slug="gemini_faceless",
    )


@app.route("/api/videos/<int:vid>/gemini-pixel-face", methods=["POST"])
@login_required
def gemini_pixel_face(vid):
    """Direct-to-Gemini pixel-face — sends the locked SOP prompt template (not
    the full SOP) substituted with Claude Code brand info, plus 4 face refs + 1
    brand logo."""
    if not _load_face_refs():
        return jsonify({"error": "no face references found in assets/face_references/"}), 500
    if not _load_logo(_DEFAULT_BRAND["logo_name"]):
        return jsonify({"error": f"brand logo missing: {_DEFAULT_BRAND['logo_name']}"}), 500
    return _gemini_thumb_direct(
        vid,
        kind="pixel_face",
        prompt_builder=_build_pixel_face_prompt,
        slug="gemini_pixel_face",
    )


@app.route("/videos/<int:vid>/thumb-edit/<int:slot>", methods=["GET"])
@login_required
def thumb_editor_page(vid, slot):
    """Serve the miniPaint-wrapped thumbnail editor for a single slot.
    Opens in a new tab from the studio. Pre-loads the slot's current PNG
    (if any) as layer 1, and writes back on Save."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    vrow = conn.execute("SELECT title FROM videos WHERE id=?", (vid,)).fetchone()
    if not vrow:
        conn.close()
        return "video not found", 404
    title = (vrow["title"] or "").strip() or "(untitled)"
    drow = conn.execute(
        "SELECT original_thumbs FROM video_details WHERE video_id=?", (vid,)
    ).fetchone()
    thumb_url = ""
    if drow and drow["original_thumbs"]:
        try:
            thumbs = json.loads(drow["original_thumbs"])
            if isinstance(thumbs, list) and 0 <= slot < len(thumbs):
                thumb_url = thumbs[slot] or ""
        except Exception:
            pass
    conn.close()
    return render_template(
        "thumb_editor.html",
        vid=vid,
        slot=slot,
        slot_label=slot + 1,
        video_title=title,
        thumb_url=thumb_url,
    )


@app.route("/diagrams/<diagram_id>/image-edit", methods=["GET"])
@login_required
def diagram_image_editor_page(diagram_id):
    """miniPaint-wrapped editor for a single diagram's source image. Opens in a
    new tab from Diagram Studio. Pre-loads the diagram's current image as layer 1
    and writes back to /api/diagrams/<id>/image on save (existing endpoint)."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    drow = conn.execute(
        "SELECT id, video_id, name, image_path FROM diagrams WHERE id=?",
        (diagram_id,),
    ).fetchone()
    if not drow:
        conn.close()
        return "diagram not found", 404
    vrow = conn.execute("SELECT title FROM videos WHERE id=?", (drow["video_id"],)).fetchone()
    conn.close()
    video_title = ((vrow["title"] if vrow else "") or "").strip() or "(untitled)"
    diagram_name = (drow["name"] or "").strip() or "untitled diagram"
    image_url = ("/" + drow["image_path"]) if drow["image_path"] else ""
    return render_template(
        "diagram_image_editor.html",
        diagram_id=diagram_id,
        video_id=drow["video_id"],
        diagram_name=diagram_name,
        video_title=video_title,
        image_url=image_url,
    )


@app.route("/api/videos/<int:vid>/thumb-slot/<int:slot>", methods=["POST"])
@login_required
def thumb_slot_save(vid, slot):
    """Receive an edited PNG from the miniPaint wrapper, save it to disk, and
    write the URL into original_thumbs[slot] (overwriting that slot only)."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file in multipart upload"}), 400
    conn = get_db()
    state = _read_video_thumb_state(conn, vid)
    if state is None:
        conn.close()
        return jsonify({"error": "video not found"}), 404
    _title, thumbs, titles, drow_exists = state
    while len(thumbs) <= slot:
        thumbs.append("")
    while len(titles) < len(thumbs):
        titles.append("")

    out_root = UPLOAD_DIR / "thumbs" / str(vid)
    out_root.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    out_path = out_root / f"edited_slot{slot + 1}_{ts}.png"
    f.save(str(out_path))
    rel_url = "/" + str(out_path.relative_to(Path(app.root_path))).replace(os.sep, "/")
    thumbs[slot] = rel_url
    _persist_thumb_state(conn, vid, thumbs, titles, drow_exists)
    conn.close()
    return jsonify({
        "ok": True,
        "slot_index": slot,
        "slot_label": slot + 1,
        "url": rel_url,
        "original_thumbs": thumbs,
    })


@app.route("/api/videos/<int:vid>/editor-bootstrap", methods=["GET"])
def editor_bootstrap(vid):
    """Single payload the OpenCut bridge page needs to seed a new project:
    title, content/bullet doc text, and the rendered asset list."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    vrow = conn.execute("SELECT id, video_id, title FROM videos WHERE id=?", (vid,)).fetchone()
    if not vrow:
        conn.close()
        return jsonify({"error": "video not found"}), 404
    drow = conn.execute("SELECT custom_fields FROM video_details WHERE video_id=?", (vid,)).fetchone()
    try:
        custom_fields = json.loads(drow["custom_fields"]) if drow and drow["custom_fields"] else []
    except (TypeError, ValueError):
        custom_fields = []
    # Reuse assets logic inline (kept small to avoid a refactor)
    assets = []
    try:
        diagrams = conn.execute(
            "SELECT id, name, result_url, result_meta_json FROM diagrams "
            "WHERE video_id=? AND result_url IS NOT NULL ORDER BY position, updated_at",
            (vid,),
        ).fetchall()
        for d in diagrams:
            try:
                meta = json.loads(d["result_meta_json"] or "{}")
            except Exception:
                meta = {}
            assets.append({
                "id": "diag_" + d["id"],
                "type": "diagram",
                "name": d["name"] or "diagram",
                "url": _proxy_asset_url(d["result_url"]),
                "duration": meta.get("duration", 0),
            })
    except sqlite3.OperationalError:
        pass
    try:
        chapters = conn.execute(
            "SELECT id, name, result_url, result_meta_json FROM chapters "
            "WHERE video_id=? AND result_url IS NOT NULL ORDER BY position, updated_at",
            (vid,),
        ).fetchall()
        for c in chapters:
            try:
                meta = json.loads(c["result_meta_json"] or "{}")
            except Exception:
                meta = {}
            assets.append({
                "id": "chap_" + c["id"],
                "type": "chapter",
                "name": c["name"] or "chapter",
                "url": _proxy_asset_url(c["result_url"]),
                "duration": meta.get("duration", 0),
            })
    except sqlite3.OperationalError:
        pass
    conn.close()
    todo_doc = _read_doc_field(custom_fields, "Screen Share To-Do")
    return jsonify({
        "vid": vrow["id"],
        "video_id": vrow["video_id"],
        "title": vrow["title"] or f"Video {vid}",
        "content_doc": _read_doc_field(custom_fields, "Content Doc"),
        "bullet_doc": _read_doc_field(custom_fields, "Bullet Doc"),
        "screen_share_todo": {
            "text": todo_doc.get("text", ""),
            "rel_path": todo_doc.get("rel_path", ""),
            "scenes": _parse_todo_scenes(todo_doc.get("text", "")),
        },
        # Back-compat with the first bridge build that read text-only fields.
        "content_doc_text": _read_doc_field(custom_fields, "Content Doc")["text"],
        "bullet_doc_text": _read_doc_field(custom_fields, "Bullet Doc")["text"],
        "assets": assets,
    })


@app.route("/api/videos/<int:vid>/editor", methods=["GET"])
def get_editor_state(vid):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT editor_state FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "video not found"}), 404
    raw = row["editor_state"]
    if not raw:
        return jsonify({"timeline": []})
    try:
        return jsonify(json.loads(raw))
    except Exception:
        return jsonify({"timeline": []})


@app.route("/api/videos/<int:vid>/editor", methods=["POST"])
def save_editor_state(vid):
    body = request.get_json(force=True, silent=True) or {}
    payload = json.dumps(body)
    conn = get_db()
    conn.execute("UPDATE videos SET editor_state=? WHERE id=?", (payload, vid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


def _editor_resolve(url):
    rel = (url or "").lstrip("/")
    if not rel:
        return None
    p = Path(app.root_path) / rel
    return p if p.exists() else None


def _editor_normalize_state(state):
    """Ensure tracks shape. Migrate legacy flat `timeline` array into video track."""
    tracks = state.get("tracks") or {}
    tracks.setdefault("video", [])
    tracks.setdefault("audio", [])
    legacy = state.get("timeline") or []
    if legacy and not tracks["video"]:
        t = 0.0
        for c in legacy:
            dur = float(c.get("duration") or 0)
            tracks["video"].append({
                **c, "kind": "video",
                "start_time": t, "in": 0.0, "out": dur,
            })
            t += dur
    state["tracks"] = tracks
    state.pop("timeline", None)
    return state


@app.route("/api/videos/<int:vid>/editor/render", methods=["POST"])
def render_editor_timeline(vid):
    """Compose timeline into a single MP4.
    - Video lane: per-clip trim (in/out), gaps padded to black, sequential concat
    - Audio lane: per-clip trim, gaps padded with silence, mixed/replaces video audio
    Both lanes time-aligned via the clip's `start_time`. Final length = longest lane."""
    import tempfile, shutil

    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT editor_state FROM videos WHERE id=?", (vid,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "video not found"}), 404
    try:
        state = json.loads(row["editor_state"] or "{}")
    except Exception:
        return jsonify({"error": "editor state corrupt"}), 500
    state = _editor_normalize_state(state)
    video_clips = state["tracks"]["video"]
    audio_clips = state["tracks"]["audio"]
    if not video_clips and not audio_clips:
        return jsonify({"error": "timeline is empty"}), 400

    def total_end(clips):
        end = 0.0
        for c in clips:
            e = float(c.get("start_time") or 0) + max(0.0, float(c.get("out") or 0) - float(c.get("in") or 0))
            if e > end: end = e
        return end
    total_dur = max(total_end(video_clips), total_end(audio_clips))
    if total_dur <= 0:
        return jsonify({"error": "timeline has zero duration"}), 400

    job_id = uuid.uuid4().hex[:12]
    work = Path(tempfile.mkdtemp(prefix=f"editor_{job_id}_"))
    try:
        # ── Build the video lane as a single MP4 (gaps padded with black) ──
        video_lane_path = None
        if video_clips:
            # Sort by start_time
            ordered = sorted(video_clips, key=lambda c: float(c.get("start_time") or 0))
            parts = []  # list of (in_path or None for gap, duration_sec, in_offset)
            cursor = 0.0
            for c in ordered:
                st = float(c.get("start_time") or 0)
                inP = float(c.get("in") or 0)
                outP = float(c.get("out") or 0)
                clip_len = max(0.0, outP - inP)
                if clip_len <= 0:
                    continue
                if st > cursor + 0.01:
                    parts.append(("gap", st - cursor, 0.0))
                src = _editor_resolve(c.get("asset_url"))
                if not src:
                    continue
                parts.append((src, clip_len, inP))
                cursor = st + clip_len
            # Pad video lane to total_dur with black if shorter
            if total_dur > cursor + 0.01:
                parts.append(("gap", total_dur - cursor, 0.0))

            # Convert each part to a normalized 1920x1080 30fps mp4 (re-encode), then concat
            normalized = []
            for i, (src, dur, off) in enumerate(parts):
                out_part = work / f"vpart_{i}.mp4"
                if src == "gap":
                    # Black filler with silent audio so concat stays aligned
                    cmd = [
                        "ffmpeg", "-y",
                        "-f", "lavfi", "-i", f"color=c=black:s=1920x1080:d={dur:.3f}:r=30",
                        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
                        "-shortest",
                        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30",
                        "-c:a", "aac", "-b:a", "128k",
                        str(out_part),
                    ]
                else:
                    cmd = [
                        "ffmpeg", "-y",
                        "-ss", f"{off:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1",
                        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30",
                        "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-ar", "44100",
                        str(out_part),
                    ]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if r.returncode != 0:
                    return jsonify({"error": "ffmpeg video-part failed",
                                    "stderr": r.stderr[-2000:]}), 500
                normalized.append(out_part)

            video_lane_path = work / "video_lane.mp4"
            list_file = work / "vconcat.txt"
            list_file.write_text("\n".join(
                "file '" + str(p).replace("'", "'\\''") + "'" for p in normalized
            ))
            r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                                "-i", str(list_file), "-c", "copy", str(video_lane_path)],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                return jsonify({"error": "ffmpeg video-concat failed",
                                "stderr": r.stderr[-2000:]}), 500

        # ── Build the audio lane (gaps = silence), full timeline length ──
        audio_lane_path = None
        if audio_clips:
            inputs = []     # ffmpeg -i list
            filter_in = []  # delayed+trimmed labels
            for i, c in enumerate(audio_clips):
                src = _editor_resolve(c.get("asset_url"))
                if not src: continue
                st = float(c.get("start_time") or 0)
                inP = float(c.get("in") or 0)
                outP = float(c.get("out") or 0)
                if outP - inP <= 0: continue
                inputs += ["-i", str(src)]
                # Trim, then delay so the clip lands at its timeline position
                delay_ms = int(st * 1000)
                filter_in.append(
                    f"[{len(inputs)//2 - 1}:a]atrim={inP:.3f}:{outP:.3f},asetpts=PTS-STARTPTS"
                    f",adelay={delay_ms}|{delay_ms}[a{i}]"
                )
            if filter_in:
                audio_lane_path = work / "audio_lane.m4a"
                amix = "".join(f"[a{i}]" for i in range(len(filter_in)))
                # apad ensures the mix is at least total_dur long; atrim caps it
                filter_complex = ";".join(filter_in) + ";" + \
                    f"{amix}amix=inputs={len(filter_in)}:normalize=0,apad,atrim=0:{total_dur:.3f}[aout]"
                cmd = ["ffmpeg", "-y"] + inputs + [
                    "-filter_complex", filter_complex,
                    "-map", "[aout]",
                    "-c:a", "aac", "-b:a", "192k",
                    str(audio_lane_path),
                ]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if r.returncode != 0:
                    return jsonify({"error": "ffmpeg audio-lane failed",
                                    "stderr": r.stderr[-2000:]}), 500

        # ── Combine: video lane + audio lane (audio lane replaces clip audio) ──
        out_dir = Path(app.root_path) / "static" / "uploads" / "editor"
        out_dir.mkdir(parents=True, exist_ok=True)
        final_path = out_dir / f"v{vid}_{job_id}.mp4"
        if video_lane_path and audio_lane_path:
            cmd = [
                "ffmpeg", "-y",
                "-i", str(video_lane_path),
                "-i", str(audio_lane_path),
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                str(final_path),
            ]
        elif video_lane_path:
            shutil.move(str(video_lane_path), str(final_path))
            cmd = None
        elif audio_lane_path:
            # No video — produce a black-video MP4 the length of the audio
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"color=c=black:s=1920x1080:d={total_dur:.3f}:r=30",
                "-i", str(audio_lane_path),
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-shortest",
                str(final_path),
            ]
        else:
            return jsonify({"error": "no resolvable clips"}), 400
        if cmd:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                return jsonify({"error": "ffmpeg final mux failed",
                                "stderr": r.stderr[-2000:]}), 500

        rel = str(final_path.relative_to(Path(app.root_path)))
        result_url = "/" + rel
        state["last_render"] = {
            "url": result_url,
            "at": datetime.now(timezone.utc).isoformat(),
            "video_clips": len(video_clips),
            "audio_clips": len(audio_clips),
            "duration": round(total_dur, 2),
        }
        conn = get_db()
        conn.execute("UPDATE videos SET editor_state=? WHERE id=?",
                     (json.dumps(state), vid))
        conn.commit()
        conn.close()
        return jsonify({"url": result_url,
                        "video_clips": len(video_clips),
                        "audio_clips": len(audio_clips),
                        "duration": round(total_dur, 2)})
    except Exception as e:
        import traceback
        return jsonify({"error": f"render exception: {type(e).__name__}: {e}",
                        "trace": traceback.format_exc()[-1500:]}), 500
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ── Screen-Share Slicer ─────────────────────────────────────────────────────
# Raw screen-share mp4 → N segments where N = scene count in the card's
# Screen Share To-Do. v1: uniform-time boundaries; user nudges in the slicer
# UI. v2 may add ffmpeg scene-detect snap once we know how far off uniform is.

_SCENE_HEADER_RE = re.compile(r"^###\s+Scene\s+(\d+):\s*(.*?)\s*$", re.MULTILINE)


def _parse_todo_scenes(md_text):
    """Pull scene headers out of a Screen Share To-Do markdown.

    Returns [{"index": int, "title": str}, ...]. Empty list if no headers
    or only the unfilled template placeholder is present.
    """
    if not md_text:
        return []
    scenes = []
    for m in _SCENE_HEADER_RE.finditer(md_text):
        idx = int(m.group(1))
        title = (m.group(2) or "").strip()
        if title.startswith("[") and "tied to a brief field" in title.lower():
            title = f"Scene {idx}"
        scenes.append({"index": idx, "title": title or f"Scene {idx}"})
    return scenes


def _ffprobe_duration(src):
    """Float seconds for an mp4 (local path or https URL), or None on failure."""
    cmd = ["ffprobe", "-v", "error",
           "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1",
           str(src)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return float(r.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


def _uniform_boundaries(duration, n_segments):
    """For N segments, return N-1 boundary timestamps at equal intervals."""
    if n_segments < 2 or duration <= 0:
        return []
    return [round(duration * (i + 1) / n_segments, 2) for i in range(n_segments - 1)]


@app.route("/api/videos/<int:vid>/screen-share/detect", methods=["POST"])
def screen_share_detect(vid):
    """Detect cut boundaries for a raw screen-share video.

    Body: {"src_url": "<https url or local path>"}
    Returns: {src_url, duration, scenes:[{index,title}], boundaries:[float]}
    Boundaries are uniformly spaced; scene count comes from the card's
    Screen Share To-Do. The slicer UI lets the user nudge each boundary.
    """
    body = request.get_json(silent=True) or {}
    src = (body.get("src_url") or "").strip()
    if not src:
        return jsonify({"error": "src_url required"}), 400

    conn = get_db()
    det = conn.execute(
        "SELECT custom_fields FROM video_details WHERE video_id = ?", (vid,)
    ).fetchone()
    conn.close()
    fields = json.loads(det["custom_fields"]) if det and det["custom_fields"] else []
    todo_doc = _read_doc_field(fields, "Screen Share To-Do")
    scenes = _parse_todo_scenes(todo_doc.get("text", ""))
    if not scenes:
        return jsonify({"error": "no Screen Share To-Do scenes found for this card"}), 400

    duration = _ffprobe_duration(src)
    if duration is None or duration <= 0:
        return jsonify({"error": "ffprobe could not read duration from src",
                        "src_url": src}), 400

    return jsonify({
        "src_url": src,
        "duration": round(duration, 2),
        "scenes": scenes,
        "boundaries": _uniform_boundaries(duration, len(scenes)),
    })


@app.route("/api/videos/<int:vid>/screen-share/cut", methods=["POST"])
def screen_share_cut(vid):
    """Cut the raw screen-share into N segments at user-accepted boundaries.

    Body: {"src_url": "...", "boundaries": [t1, t2, ...], "scenes": [...]}
    Returns: {job_id, clips: [{url, start, end, duration, scene_index, scene_title}]}
    Uses -c copy → fast, but cuts snap to nearest keyframe (±2s typical for
    screen recordings). Re-encode mode can be added if frame-exact cuts matter.
    """
    body = request.get_json(silent=True) or {}
    src = (body.get("src_url") or "").strip()
    raw_boundaries = body.get("boundaries") or []
    scenes = body.get("scenes") or []
    if not src:
        return jsonify({"error": "src_url required"}), 400

    duration = _ffprobe_duration(src)
    if duration is None or duration <= 0:
        return jsonify({"error": "ffprobe could not read duration from src"}), 400

    cuts = sorted({round(float(b), 3) for b in raw_boundaries if 0 < float(b) < duration})
    edges = [0.0] + cuts + [float(duration)]
    segments = list(zip(edges[:-1], edges[1:]))

    job_id = uuid.uuid4().hex[:12]
    out_dir = Path(app.root_path) / "static" / "uploads" / "screen-share" / f"v{vid}" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    clips = []
    for i, (start, end) in enumerate(segments):
        dur = end - start
        if dur <= 0.05:
            continue
        out_path = out_dir / f"clip_{i+1:02d}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", src,
            "-t", f"{dur:.3f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(out_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            return jsonify({"error": f"ffmpeg cut segment {i+1} failed",
                            "stderr": r.stderr[-1500:]}), 500
        rel = str(out_path.relative_to(Path(app.root_path)))
        scene = scenes[i] if i < len(scenes) else {}
        clips.append({
            "url": "/" + rel,
            "start": round(start, 2),
            "end": round(end, 2),
            "duration": round(dur, 2),
            "scene_index": scene.get("index", i + 1),
            "scene_title": scene.get("title", f"Scene {i+1}"),
        })

    return jsonify({"job_id": job_id, "clips": clips})


# ---- miniPaint inpaint (Remove / Generative fill) via Replicate Flux Fill Pro ----

def _replicate_flux_fill(image_bytes, mask_bytes, prompt):
    """Inpaint via black-forest-labs/flux-fill-pro.
    image_bytes: full-canvas PNG. mask_bytes: PNG where WHITE = fill, BLACK = keep.
    prompt: text (empty/None = content-aware remove). Returns PNG bytes or None."""
    import base64, time
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
    token = os.environ.get("REPLICATE_API_TOKEN", "")
    if not token:
        app.logger.warning("flux-fill: REPLICATE_API_TOKEN not set")
        return None

    image_uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    mask_uri = "data:image/png;base64," + base64.b64encode(mask_bytes).decode("ascii")

    body = {
        "input": {
            "image": image_uri,
            "mask": mask_uri,
            "prompt": prompt or "",
            "steps": 50,
            "guidance": 60,
            "output_format": "png",
            "safety_tolerance": 6,
            "prompt_upsampling": False,
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Prefer": "wait=60",
    }
    url = "https://api.replicate.com/v1/models/black-forest-labs/flux-fill-pro/predictions"
    req = Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        app.logger.warning(f"flux-fill: replicate http {e.code}: {e.read().decode('utf-8','ignore')[:300]}")
        return None
    except URLError as e:
        app.logger.warning(f"flux-fill: replicate network: {e.reason}")
        return None

    poll_url = (data.get("urls") or {}).get("get")
    deadline = time.time() + 120
    while data.get("status") not in ("succeeded", "failed", "canceled") and poll_url and time.time() < deadline:
        time.sleep(1.0)
        try:
            r2 = Request(poll_url, headers={"Authorization": f"Bearer {token}"})
            with urlopen(r2, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError):
            return None

    if data.get("status") != "succeeded":
        app.logger.warning(f"flux-fill: final status {data.get('status')}: {str(data.get('error'))[:200]}")
        return None

    out = data.get("output")
    img_url = out if isinstance(out, str) else (out[0] if isinstance(out, list) and out else None)
    if not img_url:
        return None
    try:
        with urlopen(img_url, timeout=60) as resp:
            return resp.read()
    except (HTTPError, URLError) as e:
        app.logger.warning(f"flux-fill: download failed: {e}")
        return None


@app.route("/api/thumb-edit/inpaint", methods=["POST"])
def thumb_edit_inpaint():
    """Right-click inpaint from miniPaint.
    Multipart: image (PNG), mask (PNG, white=fill), prompt (str, optional).
    Returns PNG bytes."""
    img_f = request.files.get("image")
    mask_f = request.files.get("mask")
    if not img_f or not mask_f:
        return jsonify({"error": "missing image or mask"}), 400
    prompt = (request.form.get("prompt") or "").strip()
    image_bytes = img_f.read()
    mask_bytes = mask_f.read()
    out = _replicate_flux_fill(image_bytes, mask_bytes, prompt)
    if not out:
        return jsonify({"error": "inpaint failed (check server logs)"}), 502
    from flask import send_file
    import io
    return send_file(io.BytesIO(out), mimetype="image/png", download_name="inpaint.png")


if __name__ == "__main__":
    import webbrowser, threading
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5050")).start()
    print("\n   Content Mate: Ideas Dashboard → http://127.0.0.1:5050\n")
    app.run(host="127.0.0.1", port=5050, debug=True)
