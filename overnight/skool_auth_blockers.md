# Skool OAuth — blockers to flip mock → production

Branch: `skool-auth` (this PR). The callback at `POST /api/auth/skool/callback`
runs in **mock mode** today because `SKOOL_API_KEY` is unset in the env. To
flip to a real Skool OAuth exchange, the following must land.

## 1. Decide whether Skool OAuth actually exists

**Memory `project_creatorgrowth_skool_auth.md` (2026-05-22) says: "No Skool
OAuth exists."** Skool's public surface as of last check has no member-facing
OAuth — only the admin-API key. Before we wire token exchange, verify the
current state:

- Open a ticket / DM with Skool support (Mike has the relationship): "Is
  there a public OAuth flow members can use to sign into a third-party app
  with their Skool account?"
- If yes, get the URLs and the developer console for client credentials.
- If no, the planned Phase 2 (DM-code verify + nightly cancel poll, per the
  memory) becomes the production path and we replace the OAuth route with
  a `POST /api/auth/skool/dm-code` endpoint that:
  1. Asks the member to message the Clawdia/Onboarding bot with a 6-digit code shown on the login screen.
  2. Server polls the Skool admin-API for the latest DM to the bot account from that handle.
  3. Match → mint session.

Both shapes plug into the same downstream code path (the UPSERT into
`users` + session population in `_upsert_skool_user`).

## 2. Env vars to set on the VPS

```bash
SKOOL_API_KEY=<admin api key (used for member-lookup calls, distinct from OAuth)>
SKOOL_CLIENT_ID=<oauth client id from Skool dev console>
SKOOL_CLIENT_SECRET=<oauth client secret>
SKOOL_REDIRECT_URI=https://creatorgrowth.com/api/auth/skool/oauth-redirect
# Optional overrides if Skool's URLs differ from defaults:
SKOOL_AUTH_URL=https://www.skool.com/oauth/authorize
SKOOL_TOKEN_URL=https://www.skool.com/oauth/token
SKOOL_MEMBER_URL=https://www.skool.com/api/v1/member/me
```

Once `SKOOL_API_KEY` is present, `_skool_oauth_configured()` returns True and
the callback rejects `user_email` in the request body — it only honors `code`
and exchanges it server-to-server.

## 3. A real redirect handler

The branch only adds the JSON `POST /api/auth/skool/callback`. Production
needs an additional `GET /api/auth/skool/oauth-redirect` that:
- Reads `code` and `state` from the query string (where Skool's browser redirect lands).
- Calls `auth_skool_callback()` internally (or re-uses its body).
- Redirects the browser to `/` with the session cookie set.

I left this out of the branch on purpose: the curl test in the goal is POST/JSON,
and the GET-shim depends on the real Skool `redirect_uri` (which we don't have
yet). Trivial to add once #2 is settled.

## 4. Frontend "Sign in with Skool" button

`templates/index.html` currently renders an email/password form for the
existing email-only login (`/api/login`). To use OAuth:

1. Add a "Continue with Skool" button next to the email form.
2. Click handler hits `GET /api/auth/skool/start`, then `window.location =
   response.auth_url`.
3. After Skool redirects back, the GET-shim from #3 lands the user back on `/`
   with `session["authenticated"] = True`, so the existing SPA boots normally.

## 5. Allowlist enforcement (Phase 1 retention guardrail)

The branch UPSERTs *any* email that comes back from Skool. For early access,
we want only paid Skool members through:

```python
# In auth_skool_callback, after the member identity is known:
if email not in load_allowlist_emails():
    return jsonify({"error": "not on allowlist"}), 403
```

Allowlist source options:
- `overnight/allowlist.txt` (one email per line) — checked into the repo.
- `users.allowlisted` BOOLEAN column — manual UPDATE by Andy/Mike.
- Nightly `pull_skool_members.py` cron that syncs paid-status from Skool admin API.

Option C is the long-term answer (matches Phase 2 in the memory). For the
first cohort, option A is fine.

## 6. Drop the global UNIQUE on `videos.video_id`

Schema today:
```sql
video_id TEXT UNIQUE NOT NULL,
```

That blocks two users from each tracking the same YouTube ID. v1's per-user
dedupe (`WHERE video_id = ? AND user_id = ?`) only fires correctly *because*
no second user has hit a collision yet. Before we onboard real users, run:

```sql
-- SQLite has no DROP CONSTRAINT — recreate the index without UNIQUE.
CREATE TABLE videos_new (... same cols, no UNIQUE on video_id ...);
INSERT INTO videos_new SELECT * FROM videos;
DROP TABLE videos;
ALTER TABLE videos_new RENAME TO videos;
CREATE UNIQUE INDEX idx_videos_video_id_user ON videos(video_id, user_id);
```

Or simpler if we're willing to live with a recreate cost: prefix `video_id`
on insert with the `user_id` (`f"u{uid}_{video_id}"`) and strip on read. Less
invasive but ugly. The recreate is the right call.

## 7. Sweep the remaining ~70 SELECT/UPDATE sites

`overnight/skool_auth_plan.md` §3 lists every SELECT that still reads the
global table. v1 only scoped `/api/videos` (the goal's "at least" target).
Before turning multi-tenant on for real users, every site that touches
`videos` / `show_docs` / `channels` / `keywords` / `tweets` / `my_channel_videos`
needs a `WHERE user_id = ?` filter, and every direct `WHERE id = ?` write on
those tables needs an `owns_video()` (or analogous) ownership check.

Recommended sweep order (highest blast radius first):
1. All `UPDATE videos SET ... WHERE id = ?` sites — add `AND user_id = ?`.
2. All `SELECT * FROM video_details WHERE video_id = ?` — add ownership check via parent.
3. Cascading-from-videos tables (chapters, diagrams, tts_jobs).
4. show_docs, channels, keywords, tweets, my_channel_videos.

## 8. `NOT NULL` on `user_id` after sweep

Once #7 is done and grep confirms no INSERT path leaves `user_id` NULL, run:

```sql
-- SQLite again can't ALTER ADD NOT NULL on existing columns; same recreate
-- dance, or add a trigger that rejects NULL user_id inserts as a stopgap.
CREATE TRIGGER trg_videos_user_id_not_null
  BEFORE INSERT ON videos
  WHEN NEW.user_id IS NULL
  BEGIN
    SELECT RAISE(ABORT, 'user_id is required');
  END;
```

## 9. Session storage

Flask's default itsdangerous-signed cookie holds the session client-side.
With one user it's fine; with many, rotation of `SECRET_KEY` invalidates
everyone simultaneously. Before launch:
- Generate a permanent `SECRET_KEY`, store in 1Password.
- Set it in the VPS env (currently the app falls back to a per-process
  `secrets.token_hex(32)` — restarts log everyone out).

Optional follow-up: move to server-side sessions (Flask-Session + Redis or
sqlite-backed) so we can revoke individual sessions.

## 10. Tests to add before turning on for real users

- pytest fixture that boots Flask + a temp DB, runs the migration, asserts:
  - Two users via mock callback get distinct session user_ids.
  - GET /api/videos as user A never returns a row owned by user B.
  - POST /api/videos as user A creates a row with user_id=A.
  - UPDATE/DELETE of A's video by user B returns 404 (after sweep #7).

## Summary — minimum to ship

To flip from mock → prod without onboarding real users yet:
- #2 (env vars) + #3 (GET shim) + #4 (button) + #9 (permanent SECRET_KEY).

To open the door to real Skool members:
- All of the above + #5 (allowlist) + #6 (drop UNIQUE) + #7 (sweep) + #10 (tests).
