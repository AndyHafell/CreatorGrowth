# Skool OAuth + per-user data scoping — plan

Branch: `skool-auth`
DB: `videos.db` (the live SQLite at the repo root; `dashboard.db` is 0 bytes and unused)
Source file: `app.py` (9,611 lines, Flask)

## What's already in place

App.py already has email-only login (no OAuth):
- `app.secret_key` from env (`SECRET_KEY`)
- `app.permanent_session_lifetime = 30 days`
- `users` table: `(id, email UNIQUE, last_login, created_at)`
- `POST /api/login` — email + shared `DASHBOARD_PASSWORD`, sets `session["authenticated"]` + `session["email"]`
- `POST /api/logout`
- `GET /api/auth-status`
- `@app.before_request require_auth` — global gate with an `open_paths` allow-list
- `@login_required` decorator (used in ~10 routes)
- `THUMB_QUEUE_USERS` map (env `THUMB_QUEUE_USERS=email1:secret1,...`) for the Mac
  thumb-poll Bearer auth (separate channel, not session)

So this work **adds** Skool OAuth as a second login path, then bolts a `user_id`
column onto the data tables and a scoping wrapper onto reads/writes. The existing
email+password login keeps working (used by `/api/login`); the Skool path adds a
`/api/auth/skool/callback` that resolves a Skool member to a row in `users` and
populates the same session keys.

Per memory `project_creatorgrowth_skool_auth.md`: handle (not email) is the
intended canonical identity in the long run, with manual allow-list (Phase 1) →
DM-code verify + nightly cancel poll (Phase 2). This plan implements Phase 1 in a
shape that lets us add the handle/DM-code layer later without re-migrating data.

---

## 1. OAuth flow

```
+--------+                                +-----------+              +-------+
| Browser|                                | Flask app |              | Skool |
+---+----+                                +-----+-----+              +---+---+
    |                                           |                        |
    | 1. GET /login  (no session)               |                        |
    |------------------------------------------>|                        |
    |   200 HTML login page                     |                        |
    |<------------------------------------------|                        |
    |                                                                    |
    | 2. user clicks "Sign in with Skool"                                |
    | 2a. GET /api/auth/skool/start                                      |
    |------------------------------------------>|                        |
    |   200 { auth_url, state }                 |                        |
    |   (state stored in session)               |                        |
    |<------------------------------------------|                        |
    |                                           |                        |
    | 3. browser redirects to auth_url ───────────────────────────────-->|
    |                                                                    |
    | 4. user approves on Skool, Skool redirects back with ?code&state   |
    |<-------------------------------------------------------------------|
    |                                           |                        |
    | 5. POST /api/auth/skool/callback          |                        |
    |    body: { code, state }                  |                        |
    |------------------------------------------>|                        |
    |                              5a. POST https://skool/oauth/token    |
    |                                  { code, client_id, secret }       |
    |                                  -->                               |
    |                              5b. <-- { access_token, member }      |
    |                              5c. UPSERT into users                 |
    |                                  session["user_id"] = users.id     |
    |                                  session["email"]   = member.email |
    |                                  session["authenticated"] = True   |
    |   200 { ok, user: {id, email, handle} }   |                        |
    |<------------------------------------------|                        |
    |                                           |                        |
    | 6. GET /api/videos  (session cookie)      |                        |
    |------------------------------------------>|                        |
    |   filtered by user_id = session["user_id"]                         |
    |   200 [...]                               |                        |
    |<------------------------------------------|                        |
```

### Mock mode (no `SKOOL_API_KEY` in env)

Step 5a is skipped. The callback accepts:
```
POST /api/auth/skool/callback
{ "code": "test123", "user_email": "test@example.com" }
```
…and synthesizes the member object as `{email: user_email, handle: derived from email}`.
This is the path the curl-test in the goal exercises. The mock is gated on the
absence of `SKOOL_API_KEY` — set the env var and the route refuses
`user_email` and only accepts a real `code`.

### Why callback is POST not GET

Skool's redirect would normally hit a GET. We're stubbing a POST callback because
(a) the curl-test in the goal expects POST, (b) in production the browser
redirect will land on a GET shim that re-POSTs to this endpoint server-side (or
swaps to a GET handler — same logic). Keeping the contract POST/JSON makes the
test ergonomic and avoids `redirect_uri` registration churn for the stub.

---

## 2. DB schema delta

### Add `users.skool_handle` and `users.skool_member_id` (optional, for Phase 2)

```sql
ALTER TABLE users ADD COLUMN skool_handle TEXT;
ALTER TABLE users ADD COLUMN skool_member_id TEXT;
ALTER TABLE users ADD COLUMN access_token TEXT;
ALTER TABLE users ADD COLUMN token_expires_at TEXT;
CREATE INDEX IF NOT EXISTS idx_users_skool_handle ON users(skool_handle);
```

### Add `user_id` FK to top-level data tables

Top-level tables (own data directly):
- `videos` — root of most data
- `show_docs` — independent
- `channels` — tracked-channels list (per-user)
- `keywords` — keyword research (per-user)
- `my_channel_videos` — "my own YouTube channel" mirror (per-user)
- `tweets` — posted tweets (per-user)

Cascading-from-videos tables (scoped via JOIN, no `user_id` needed):
- `video_details` — FK to `videos.id`
- `chapters` — FK to `videos.id`
- `diagrams` — FK to `videos.id`
- `thumb_queue` — FK to `videos.id` (already has `clicked_by_email` for thumb-poll Bearer routing — keeps that as-is)
- `tts_jobs` — `video_id` (no formal FK but practical)

```sql
-- Top-level scoped tables
ALTER TABLE videos             ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE show_docs          ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE channels           ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE keywords           ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE my_channel_videos  ADD COLUMN user_id INTEGER REFERENCES users(id);
ALTER TABLE tweets             ADD COLUMN user_id INTEGER REFERENCES users(id);

CREATE INDEX IF NOT EXISTS idx_videos_user_id            ON videos(user_id);
CREATE INDEX IF NOT EXISTS idx_show_docs_user_id         ON show_docs(user_id);
CREATE INDEX IF NOT EXISTS idx_channels_user_id          ON channels(user_id);
CREATE INDEX IF NOT EXISTS idx_keywords_user_id          ON keywords(user_id);
CREATE INDEX IF NOT EXISTS idx_my_channel_videos_user_id ON my_channel_videos(user_id);
CREATE INDEX IF NOT EXISTS idx_tweets_user_id            ON tweets(user_id);
```

### Backfill (one-time)

All existing rows are Andy's. UPSERT his row into users first, then backfill:

```sql
INSERT INTO users (email, last_login)
VALUES ('andhaf94@gmail.com', CURRENT_TIMESTAMP)
ON CONFLICT(email) DO NOTHING;

UPDATE videos             SET user_id = (SELECT id FROM users WHERE email='andhaf94@gmail.com') WHERE user_id IS NULL;
UPDATE show_docs          SET user_id = (SELECT id FROM users WHERE email='andhaf94@gmail.com') WHERE user_id IS NULL;
UPDATE channels           SET user_id = (SELECT id FROM users WHERE email='andhaf94@gmail.com') WHERE user_id IS NULL;
UPDATE keywords           SET user_id = (SELECT id FROM users WHERE email='andhaf94@gmail.com') WHERE user_id IS NULL;
UPDATE my_channel_videos  SET user_id = (SELECT id FROM users WHERE email='andhaf94@gmail.com') WHERE user_id IS NULL;
UPDATE tweets             SET user_id = (SELECT id FROM users WHERE email='andhaf94@gmail.com') WHERE user_id IS NULL;
```

### NOT enforcing `NOT NULL` yet

The migration leaves `user_id` nullable. Once the app is writing `user_id` on
every insert (Phase 1 done) and we've verified no nulls remain, a follow-up
migration tightens it. Premature `NOT NULL` would crash any path that
forgot to wire the wrapper.

The full migration lives in `overnight/migration_skool_auth.sql` (applied to a
copy of `videos.db` in this task, not the live file).

---

## 3. SELECT queries that need `WHERE user_id = ?`

There are 76 `conn.execute("SELECT ...` sites in `app.py`. The full grep is at
`overnight/_selects_raw.txt`. Of those:

### Must add `user_id` filter (top-level tables, direct reads)

| Line | Query | Notes |
| --- | --- | --- |
| 833  | `SELECT * FROM videos ORDER BY added_at DESC` | `/api/videos` list — primary target |
| 878  | `SELECT id FROM videos WHERE video_id = ?` | dedupe-on-add — scope to user (same `video_id` can be tracked by two users) |
| 901  | `SELECT * FROM videos WHERE video_id = ?` | re-fetch after insert |
| 931  | `SELECT tucked FROM videos WHERE id = ?` | tuck toggle — must verify ownership |
| 962  | `SELECT transformed FROM videos WHERE id = ?` | transformed toggle — ownership |
| 986  | `SELECT title FROM videos WHERE id = ?` | title read — ownership |
| 1115 | `SELECT * FROM show_docs ORDER BY date DESC, id DESC` | show-docs list |
| 1153 | `SELECT * FROM show_docs WHERE id = ?` | show-doc detail |
| 1167 | `SELECT id FROM show_docs WHERE id = ?` | show-doc existence |
| 1290 | `SELECT * FROM channels ORDER BY added_at DESC` | channels list |
| 1312 | `SELECT * FROM channels WHERE url = ?` | channel dedupe-on-add |
| 1452 | `SELECT * FROM channels WHERE id = ?` | channel detail |
| 1461 | `SELECT * FROM channels WHERE id = ?` | channel detail (post-update) |
| 1469 | `SELECT * FROM channels ORDER BY added_at ASC` | channels list (reorder source) |
| 1485 | `SELECT * FROM channels ORDER BY added_at DESC` | channels list |
| 1499 | `SELECT * FROM channels ORDER BY added_at ASC` | channels list |
| 1962 | `SELECT * FROM videos WHERE video_id = ?` | video by yt-id |
| 2092 | `SELECT * FROM videos WHERE id = ?` | ownership check |
| 2538 | `SELECT id, title FROM videos WHERE id = ?` | ownership check |
| 2715 | `SELECT * FROM videos WHERE id = ?` | ownership check |
| 2834 | `SELECT * FROM videos WHERE id = ?` | ownership check |
| 3015 | `SELECT * FROM videos WHERE id = ?` | ownership check |
| 3419 | `SELECT title FROM videos WHERE id = ?` | ownership check |
| 3558–3562 | `SELECT * FROM keywords ...` | keywords list — scope |
| 3570 | `SELECT is_favorite FROM keywords WHERE id = ?` | keyword detail |
| 3584 | `SELECT is_youtube FROM keywords WHERE id = ?` | keyword detail |
| 5519, 5634, 5666, 5825, 5930, 7066, 7390, 7921 | `SELECT vocal_doc / visuals_doc / visual_tags / voiceover_state FROM videos WHERE id=?` | content-doc & visuals-doc — these are "content_docs" in the goal's language; scope via parent video ownership |
| 8578, 8786, 8828, 8953 | `SELECT title FROM videos WHERE id=?` | various route ownership checks |
| 9028, 9090 | `SELECT editor_state FROM videos WHERE id=?` | editor state — ownership |

### Cascading reads (filter via parent video ownership, not own `user_id`)

These tables don't get a `user_id` column. They are scoped by joining/checking
the parent `videos.user_id`:

| Line | Query | Scoping strategy |
| --- | --- | --- |
| 1753, 1770, 1787, 1827, 1845, 2218, 2809, 2977, 3310, 3321, 5282, 8957 | `SELECT * FROM video_details WHERE video_id = ?` | guard by `videos.user_id` check first |
| 1756, 1772 | `SELECT ... FROM videos WHERE id = ?` (inside details handler) | combine ownership + read |
| 4107, 4118, 4127, 4168, 4180, 4204, 4254, 4359, 8157, 8304, 8356, 8378, 8414 | `SELECT * FROM diagrams WHERE id=?` / `WHERE video_id=?` | guard parent video ownership |
| 5208, 5269, 7859, 7898 | `SELECT * FROM chapters WHERE video_id=?` | guard parent video ownership |
| 7219 | `SELECT * FROM tts_jobs WHERE id=?` | join via tts_jobs.video_id → videos.user_id |
| 404, 407, 409 | `SELECT/UPDATE/INSERT users` | unchanged — auth itself |

### Out of scope for v1 (Bearer-authed or shared)

These two endpoint families remain Bearer-authed (not session) and stay scoped
by their own keys:

- `/api/thumb-queue/*` — uses `THUMB_QUEUE_USERS` env map + `clicked_by_email`
  (the Mac poller per memory `project_thumb_button.md`). Already user-scoped via
  `clicked_by_email`. Skip for v1.
- `/api/show-docs/*` GET-by-share — open via existing allowlist. Listing
  `/api/show-docs` is session-authed and gets scoped above.

### Single-user shared (no scoping)

- `my_channel_videos` is the YouTube-mirror of Andy's own channel — for now
  multi-user means each user mirrors *their* channel, so it gets `user_id` and
  is filtered by it.

---

## 4. Scoping wrapper

```python
def current_user_id():
    """Return session user_id or None. Reads from session set by /api/login or
    /api/auth/skool/callback. After we add a backfill UPSERT in /api/login (any
    user already authenticated by password should resolve to a users.id), this
    is always set when authenticated=True."""
    return session.get("user_id")

def scoped_videos_select(conn, where_extra="", params=()):
    uid = current_user_id()
    if uid is None:
        return []  # unauthenticated never reaches here (before_request), but defensive
    q = "SELECT * FROM videos WHERE user_id = ?"
    if where_extra:
        q += f" AND {where_extra}"
    q += " ORDER BY added_at DESC"
    return conn.execute(q, (uid, *params)).fetchall()

def owns_video(conn, vid: int) -> bool:
    uid = current_user_id()
    if uid is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM videos WHERE id = ? AND user_id = ?", (vid, uid)
    ).fetchone()
    return row is not None
```

v1 applies `scoped_videos_select` to `GET /api/videos` (line 833) and adds
`owns_video()` guards as I touch other endpoints in follow-up commits. The
plan tracks the full set so nothing's missed; this branch ships the wrapper +
videos list + ownership-on-insert. Everything else gets a follow-up sweep.

---

## 5. /api/login backfill

The existing email-only `/api/login` already UPSERTs into `users`. I add one
line to also populate `session["user_id"]` so the wrapper works for the
password path too — no behavior change for existing usage.

```python
# after the INSERT/UPDATE, before session.permanent = True:
row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
session["user_id"] = row["id"]
```

---

## 6. What does NOT change in v1

- The 9 other write/read sites on `videos` past `/api/videos` keep working
  against the global table — they'll be unscoped until the follow-up sweep.
  This is acceptable for the curl-test goal (which only exercises `/api/videos`)
  and explicitly called out as scope in the goal ("at least /api/videos").
- The `users` table keeps its existing email-only path; Skool is an
  additional login route.
- No frontend changes — login page already exists; the OAuth button is a
  follow-up. The curl-test hits the JSON endpoints directly.
- No data deletion or `NOT NULL` enforcement.

---

## 7. Migration application

In this task we copy `videos.db → videos_test.db` and apply the migration
against the copy, leaving the live DB untouched. The live DB gets the same
migration on deploy (`./rebuild.sh` after the branch lands), inside a one-shot
function gated on schema-version detection.
