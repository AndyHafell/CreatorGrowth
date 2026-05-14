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
    if new_status not in ("options", "best", "packaging", "script", "edited", "archived", "published"):
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
        "SELECT id FROM thumb_queue WHERE video_id = ? AND clicked_by_email = ? AND status = 'queued'",
        (vid, email),
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"ok": True, "queued": True, "queue_id": existing["id"], "already": True})
    cur = conn.execute(
        "INSERT INTO thumb_queue (video_id, title, clicked_by_email) VALUES (?, ?, ?)",
        (vid, title, email),
    )
    conn.commit()
    qid = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "queued": True, "queue_id": qid})


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
        "SELECT id, video_id, title, created_at FROM thumb_queue "
        "WHERE status = 'queued' AND clicked_by_email = ? ORDER BY id ASC",
        (user_email,),
    ).fetchall()
    conn.close()
    return jsonify([{"id": r["id"], "video_id": r["video_id"], "title": r["title"], "created_at": r["created_at"]} for r in rows])


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
        "original_thumbs": _pad(json.loads(row["original_thumbs"]), 9),
        "original_titles": _pad(json.loads(row["original_titles"]), 9),
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
    conn.execute("UPDATE diagrams SET image_path=?, updated_at=? WHERE id=?",
                 (rel, datetime.now(timezone.utc).isoformat(), diagram_id))
    conn.commit()
    conn.close()
    return jsonify({"image_path": rel, "image_path_url": "/" + rel})


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
        ease = p * p * (3 - 2 * p)
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

        # Compute box rects + kind (reveal vs hide) + anim style + time_override
        all_box_records = []   # each: (x, y, w, h, anim, original_box_dict)
        reveal_indices = []
        for b in boxes:
            try:
                bx = float(b["x"]); by = float(b["y"]); bw = float(b["w"]); bh = float(b["h"])
            except (KeyError, TypeError, ValueError):
                return jsonify({"error": "box missing x/y/w/h"}), 400
            x = int(bx * W); y = int(by * H)
            w = int(bw * W); h = int(bh * H)
            x = max(0, min(W - 4, x)); y = max(0, min(H - 4, y))
            w = max(4, min(W - x, w)); h = max(4, min(H - y, h))
            x -= (x % 2); y -= (y % 2)
            w -= (w % 2); h -= (h % 2)
            if w < 2 or h < 2:
                continue
            anim_val = (b.get("anim") or "fade").lower() if isinstance(b, dict) else "fade"
            all_box_records.append((x, y, w, h, anim_val, b if isinstance(b, dict) else {}))
            if anim_val != "hide":
                reveal_indices.append(len(all_box_records) - 1)
        if not all_box_records:
            return jsonify({"error": "no valid boxes after normalization"}), 400

        # Reveal boxes are the ones that animate in. Hide boxes only mask the bg permanently.
        box_rects = [(r[0], r[1], r[2], r[3]) for i, r in enumerate(all_box_records) if i in reveal_indices]
        box_anims = [all_box_records[i][4] for i in reveal_indices]
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
        for (x, y, w, h, _anim, _bd) in all_box_records:
            fill_for_rect[(x, y, w, h)] = _ring_median_color(img, x, y, w, h, pad=8, ring=24)

        if mode == "reveal":
            # Reveal mode: bg = image with every box filled with its local bg color.
            bg = img.copy()
            draw = ImageDraw.Draw(bg)
            for (x, y, w, h, _anim, _bd) in all_box_records:
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
            else:
                # SLIDESHOW: the box content becomes a full 16:9 slide.
                box_crop = img.crop((x, y, x + w, y + h))
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

        fade_dur = 0.35
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
                       "zoom_in", "zoom_out"}

        # Resolve each reveal box's effective anim
        resolved_anims = []
        for i in range(N):
            raw = (box_anims[i] or "fade").lower()
            a = LEGACY_ANIM_MAP.get(raw, raw)
            resolved_anims.append(a if a in valid_anims else "fade")

        # Build ffmpeg inputs
        inputs = ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(bg_path)]
        for i in range(N):
            if resolved_anims[i] in ("zoom_in", "zoom_out"):
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
            overlay's enable gate becomes 'between(t, t, t_exit + fade_dur)'."""
            src = f"[s{in_idx}]"
            segs = []
            exit_filter = (f",fade=t=out:st={t_exit:.3f}:d={fade_dur:.3f}:alpha=1"
                           if t_exit is not None else "")
            if anim in ("zoom_in", "zoom_out"):
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
                enable_expr = f"between(t,{t:.3f},{(t_exit + fade_dur):.3f})"
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
                t_exit = times[i + 1]
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


if __name__ == "__main__":
    import webbrowser, threading
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5050")).start()
    print("\n   Content Mate: Ideas Dashboard → http://127.0.0.1:5050\n")
    app.run(host="127.0.0.1", port=5050, debug=True)
