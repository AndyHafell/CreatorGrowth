# Skool gate — blockers to flip from "scaffolded" to "live for paying members"

Branch: `skool-auth` (this PR). Most of the gate ships in this branch; the
items below are what still needs human action or a real-world data source
before opening to AI Mate members.

---

## 1. Decide the data source for the member roster

**Critical.** Per memory `project_creatorgrowth_skool_auth.md`, **Skool exposes
no public admin API and no public OAuth.** This branch ships two
data-source paths and the gate works either way:

- **Path A (manual, ships today):** `scripts/skool_allowlist.py add/revoke/list`
  — Mike runs this when onboarding/churning members. Zero infra dependencies.
  Risk: Mike has to remember. Lag between Skool churn and revocation is human.

- **Path B (Chrome-ext scrape, half-built):** `scripts/skool_member_sync.py`
  reads `overnight/skool_members_snapshot.json` (extension writes it when Andy
  has Skool open) or `overnight/skool_members_manual.json` (we edit by hand).
  TODO: write the extension JS that produces the snapshot.

Decision needed: which path do we run for the first cohort? Recommend Path A
for the first 1-2 weeks (low risk), then Path B as automation. Either way,
the nightly cron in `scripts/skool_cron.sh` is wired and idempotent — it just
needs a roster file.

## 2. Env vars on the VPS

Set in `.env` (template at `.env.example`):

```bash
SECRET_KEY=<from 1Password — generate fresh once, never rotate without warning users>
DM_VERIFY_TOKEN=<from 1Password — bearer for /api/auth/skool/dm-code/admin-verify>
SKOOL_DM_TARGET=theaiandy
DM_CODE_TTL_SEC=600
```

OAuth env vars stay empty (Skool has no OAuth):
```
SKOOL_API_KEY=
SKOOL_CLIENT_ID=
SKOOL_CLIENT_SECRET=
```

If/when Skool ships OAuth, fill all three → callback automatically switches to
real token exchange and rejects `user_email` requests.

## 3. Chrome extension hook (to fully automate DM-code verify)

Today the DM-code flow ends at `/api/auth/skool/dm-code/admin-verify`, which
needs Andy or Mike to run `scripts/skool_dm_verify.py CODE --sender HANDLE`
whenever a member DMs a code.

To fully automate: extend `skool/skool-extension/content.js` so when Andy's
Skool DM panel surfaces a message that's exactly a 6-digit code, the extension
POSTs to `/api/auth/skool/dm-code/admin-verify` with the bearer token and the
sender's handle. Then members get logged in within seconds of DMing.

Until that lands, the manual CLI step is the bottleneck — fine for the first
~20 members, not fine for scale.

## 4. Final scoping sweep verification

The decorator sweep applied `@video_owner_required` / `@diagram_owner_required`
/ `@channel_owner_required` / `@keyword_owner_required` / `@show_doc_owner_required`
to 68 routes. The high-risk batch endpoints (`/api/videos/clear`,
`/api/videos/backfill-*`, `/api/videos/sync-*`, `scrape_all_channels`) got
explicit `_request_user_id()` scoping.

Before launch, **run this grep and confirm zero hits**:

```bash
grep -nE 'SELECT \* FROM (videos|show_docs|channels|keywords) (ORDER|WHERE (?!.*user_id))' app.py
grep -nE '(UPDATE|DELETE FROM) (videos|show_docs|channels|keywords) WHERE' app.py | grep -v 'user_id\|@video_owner_required\|@channel_owner_required\|@keyword_owner_required\|@show_doc_owner_required'
```

Any hits without an upstream `@*_owner_required` are a cross-tenant leak. The
known-safe pattern is: decorator at top of route → `WHERE id = ?` inside is fine
because ownership was already proven.

## 5. SECRET_KEY pinning (done in code, needs prod env)

The app now logs a `WARNING: SECRET_KEY not set` at boot when missing. Before
prod restart: store a permanent 32-byte hex in 1Password, set in VPS env.
Without it, every container restart logs every user out AND breaks every
in-flight DM code.

## 6. Smoke test (`overnight/skool_smoke_test.md`)

Run that runbook end-to-end with Mike before merging. The runbook tests:
- Deploy + migration (no errors, 0 unscoped rows)
- Mike logs in via DM-code (humans-in-loop)
- Andy + Mike see only their own videos (read isolation)
- Mike adds a Rickroll → both can have it (UNIQUE drop works)
- Non-member rejected at /login (403)
- Revoke Mike → next request 401 (live re-check)

The merge to main is gated on this passing.

## 7. Auto-clear stale dm_codes

`dm_codes` rows with `status='expired'` or `status='consumed'` accumulate
forever. Add a tiny cron OR a periodic background task to DELETE rows older
than 7 days. Not blocking launch, but nice for housekeeping.

## 8. Frontend "Sign in with Skool" button

`templates/index.html` still shows only the email/password form. Add a
"Continue with Skool" button that:
1. Prompts for handle.
2. Calls `POST /api/auth/skool/dm-code/start` with the handle.
3. Shows the code + "DM @theaiandy this number" instructions.
4. Polls `GET /api/auth/skool/dm-code/poll?code=XXXXXX` every 3 seconds.
5. On 200 with `ok:true`, reloads the SPA — they're logged in.

UI-only change; backend contract is in place.

## 9. Tests against live data (manual)

`tests/test_skool_gate.py` runs against an in-memory-ish fresh DB. Before
launch, run one manual integration test against a *copy* of prod
(`videos_test.db = cp videos.db`) to confirm the migration is idempotent on
real data and doesn't break the existing 424 rows.

```bash
cp videos.db videos_test.db
DB_PATH="$PWD/videos_test.db" python3 -c "import os; os.environ['DB_PATH']='$PWD/videos_test.db'; import app"
sqlite3 videos_test.db "SELECT COUNT(*) FROM videos WHERE user_id IS NULL"   # must be 0
```

## 10. Once paying members log in

- Watch logs for `not on allowlist` (false negatives = real members getting
  blocked because Mike forgot to add them).
- Watch logs for `access revoked` (false positives = real members getting
  kicked because the sync had stale data).
- Adjust allowlist via CLI as needed.

---

## Summary — what's done vs what's blocking

**Done in this branch:**
- Per-user `user_id` scoping on videos/show_docs/channels/keywords/my_channel_videos/tweets
- Column-level UNIQUE dropped on videos.video_id, channels.url, keywords.keyword + composite UNIQUEs with user_id
- `users.allowlisted` + manual CLI (`skool_allowlist.py`)
- Nightly sync script (`skool_member_sync.py` + `skool_cron.sh`) — needs roster file
- Callback gates on allowlist (403 for non-members)
- DM-code start/poll/admin-verify endpoints + CLI (`skool_dm_verify.py`)
- Live allowlist re-check in `@app.before_request` (revoked users 401 on next hit)
- Ownership decorators on 68 routes
- `pytest tests/test_skool_gate.py` — 12 passing
- `.env.example` + SECRET_KEY warning at boot
- Smoke-test runbook (`overnight/skool_smoke_test.md`)

**Blocking merge-to-main:**
- Smoke test (#6) with Mike — needs his real Skool handle + interactive DM
- Prod env vars (#2, #5) set in 1Password + VPS
- Roster data source decision (#1) — Path A is enough for v1

**Nice-to-have (post-launch):**
- Chrome ext hook (#3)
- Frontend Skool button (#8)
- Auto-clear stale dm_codes (#7)
