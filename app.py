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
THUMB_QUEUE_SECRET = os.environ.get("THUMB_QUEUE_SECRET", "")
CONTENT_DIR = Path(os.environ.get("CONTENT_DIR", str(Path(__file__).resolve().parent.parent.parent / "content" / "content_docs")))
CONTENT_DIR.mkdir(parents=True, exist_ok=True)


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
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
            original_thumbs TEXT DEFAULT '["","",""]',
            original_titles TEXT DEFAULT '["","",""]',
            custom_fields TEXT DEFAULT '[]',
            FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
        )
    """)
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
            FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
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
    if new_status not in ("options", "best", "in_progress", "archived", "done"):
        return jsonify({"error": "Invalid status"}), 400
    conn = get_db()
    conn.execute("UPDATE videos SET status = ? WHERE id = ?", (new_status, vid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "status": new_status})


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
    """Queue a thumbnail-generation task for the Mac poller to pick up."""
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
        "SELECT id FROM thumb_queue WHERE video_id = ? AND status = 'queued'",
        (vid,),
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"ok": True, "queued": True, "queue_id": existing["id"], "already": True})
    cur = conn.execute(
        "INSERT INTO thumb_queue (video_id, title) VALUES (?, ?)",
        (vid, title),
    )
    conn.commit()
    qid = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "queued": True, "queue_id": qid})


def _thumb_queue_auth_ok():
    if not THUMB_QUEUE_SECRET:
        return False
    header = request.headers.get("Authorization", "")
    return header == f"Bearer {THUMB_QUEUE_SECRET}"


@app.route("/api/thumb-queue", methods=["GET"])
def thumb_queue_list():
    """Mac poller: list pending queued tasks. Auth via Bearer secret."""
    if not _thumb_queue_auth_ok():
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    rows = conn.execute(
        "SELECT id, video_id, title, created_at FROM thumb_queue WHERE status = 'queued' ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return jsonify([{"id": r["id"], "video_id": r["video_id"], "title": r["title"], "created_at": r["created_at"]} for r in rows])


@app.route("/api/thumb-queue/<int:qid>/done", methods=["POST"])
def thumb_queue_done(qid):
    """Mac poller: mark task as picked up / done."""
    if not _thumb_queue_auth_ok():
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db()
    conn.execute(
        "UPDATE thumb_queue SET status = 'done', picked_up_at = CURRENT_TIMESTAMP WHERE id = ?",
        (qid,),
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
    """Backfill missing/zero view counts using yt-dlp individual lookups."""
    limit = request.args.get("limit", 10, type=int)
    conn = get_db()
    missing = conn.execute(
        "SELECT id, video_id FROM videos WHERE view_count IS NULL OR view_count = 0 LIMIT ?",
        (limit,),
    ).fetchall()
    if not missing:
        conn.close()
        return jsonify({"backfilled": 0, "remaining": 0})
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
        default_fields = json.dumps([{"key": "Content Doc", "value": ""}])
        conn.execute(
            """INSERT INTO video_details (video_id, inspo_thumbs, inspo_titles, original_thumbs, original_titles, custom_fields)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (vid, default_inspo, default_inspo_titles, default_empty3, default_empty3, default_fields),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM video_details WHERE video_id = ?", (vid,)).fetchone()
    conn.close()
    return jsonify({
        "video_id": row["video_id"],
        "inspo_thumbs": json.loads(row["inspo_thumbs"]),
        "inspo_titles": json.loads(row["inspo_titles"]),
        "original_thumbs": json.loads(row["original_thumbs"]),
        "original_titles": json.loads(row["original_titles"]),
        "custom_fields": json.loads(row["custom_fields"]),
    })


@app.route("/api/videos/<int:vid>/details", methods=["POST"])
def save_video_details(vid):
    data = request.get_json(force=True)
    conn = get_db()
    # Upsert
    existing = conn.execute("SELECT id FROM video_details WHERE video_id = ?", (vid,)).fetchone()
    values = {
        "inspo_thumbs": json.dumps(data.get("inspo_thumbs", ["", "", ""])),
        "inspo_titles": json.dumps(data.get("inspo_titles", ["", "", ""])),
        "original_thumbs": json.dumps(data.get("original_thumbs", ["", "", ""])),
        "original_titles": json.dumps(data.get("original_titles", ["", "", ""])),
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
    # If a custom thumbnail (My Thumbnails slot 1) is set, update the card thumbnail
    original_thumbs = data.get("original_thumbs", ["", "", ""])
    if original_thumbs and original_thumbs[0]:
        conn.execute("UPDATE videos SET thumbnail_url = ? WHERE id = ?", (original_thumbs[0], vid))

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
           VALUES (?, ?, ?, ?, '', 0, '', '', 'in_progress', 0, 0)""",
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


if __name__ == "__main__":
    import webbrowser, threading
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5050")).start()
    print("\n   Content Mate: Ideas Dashboard → http://127.0.0.1:5050\n")
    app.run(host="127.0.0.1", port=5050, debug=True)
