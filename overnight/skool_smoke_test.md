# Prod smoke test — Skool gate + per-user isolation

## NEW (Andy-only path, no Mike needed)

There's a self-contained smoke test you can run yourself. It plays both
roles (Andy + a test handle) and asserts every gate behavior. Use it as
the gate-check before merging.

### Local pre-flight (do this first — proves the code works on a copy of your real DB):
```bash
cd ~/dev/creatorgrowth
./scripts/smoke_test.sh local
# Expect: "RESULT: ALL 14 ASSERTIONS PASS ✓"
```

### Prod equivalent (run after deploy, replace BASE):
```bash
# After deploy steps 0–2 below, with DM_VERIFY_TOKEN set in your shell env:
DM_VERIFY_TOKEN="<from VPS .env>" ./scripts/smoke_test.sh prod https://creatorgrowth.com
# Same 14 assertions, against the live server.
```

If both passes, item #9 is satisfied — no need for Mike to be online.
You merge per step 9 of this runbook. If you want Mike to validate his
own handle separately later, that's a nice-to-have, not a blocker.

The detailed manual runbook below remains for the case where the
automated script can't be used (e.g., need to debug a specific step).

---

## Detailed manual runbook

Run this BEFORE merging `skool-auth` to `main`. Requires:
- Mike awake and at his laptop (real Skool handle needed) — OR use Andy
  + a second test handle (the new `smoke_test.sh prod` path covers this).
- VPS DB backup taken (this changes schema + drops UNIQUEs).
- `SECRET_KEY`, `DM_VERIFY_TOKEN` set in VPS env (see `.env.example`).

## 0. Pre-flight

```bash
# On VPS — back up the live DB before the migration runs on next deploy.
ssh root@148.230.108.170 'cp /opt/idea_dashboard/videos.db /opt/idea_dashboard/videos.db.pre_skool_auth.$(date +%Y%m%d_%H%M%S)'

# Confirm env vars are set:
ssh root@148.230.108.170 'cd /opt/idea_dashboard && grep -E "^(SECRET_KEY|DM_VERIFY_TOKEN)=" .env | sed s/=.*/=...SET/'
```

## 1. Deploy

```bash
cd ~/dev/creatorgrowth
git push vps skool-auth         # triggers rebuild.sh
ssh root@148.230.108.170 'cd /opt/idea_dashboard && git checkout skool-auth && ./rebuild.sh'
# Watch the container come up:
ssh root@148.230.108.170 'docker logs -f --tail 50 idea_dashboard' &
```

Verify the migration ran without errors. You should see `migrate_schema()` complete and no `OperationalError` in the logs. The 424 (or however many) videos should all have `user_id = 2` (Andy).

```bash
ssh root@148.230.108.170 'docker exec idea_dashboard sqlite3 /opt/idea_dashboard/videos.db \
  "SELECT COUNT(*) AS total, SUM(user_id IS NULL) AS unscoped FROM videos"'
# Expect: total>0, unscoped=0
```

## 2. Seed the allowlist

```bash
# Andy is auto-seeded by migrate_schema. Add Mike:
ssh root@148.230.108.170 'docker exec idea_dashboard python3 /opt/idea_dashboard/scripts/skool_allowlist.py \
  add --handle MIKES_REAL_HANDLE --email mike@example.com'

# Verify:
ssh root@148.230.108.170 'docker exec idea_dashboard python3 /opt/idea_dashboard/scripts/skool_allowlist.py list'
# Should show Andy + Mike, allowlisted=1
```

## 3. Mike logs in (DM-code flow)

From Mike's browser:

1. Visit https://creatorgrowth.com/login
2. Click "Sign in with Skool" → enter handle `MIKES_REAL_HANDLE`
3. Frontend hits `POST /api/auth/skool/dm-code/start` → shows 6-digit code XXXXXX
4. Mike DMs `@theaiandy` on Skool with code XXXXXX
5. Andy approves via CLI (since the Chrome ext hook isn't wired yet):
   ```bash
   ssh root@148.230.108.170 'DM_VERIFY_TOKEN=$DM_VERIFY_TOKEN python3 /opt/idea_dashboard/scripts/skool_dm_verify.py XXXXXX --sender MIKES_REAL_HANDLE'
   ```
6. Mike's browser polls and gets logged in.

Expected: Mike sees an empty card grid (he owns 0 videos).

## 4. Andy logs in

In Andy's separate browser session (incognito), same flow but with handle `theaiandy`.

Expected: Andy sees his 424 videos. Mike's session in step 3 still works and still shows 0 videos.

## 5. Cross-tenant write test

As Mike:
1. Add a YouTube URL via the UI: paste `https://youtu.be/dQw4w9WgXcQ`
2. Mike now sees 1 video.

As Andy:
1. Andy still sees his 424 videos. Mike's 1 video is NOT there.
2. Try opening one of Andy's video detail modals — works.
3. Inspect: `ssh root@148.230.108.170 'docker exec idea_dashboard sqlite3 /opt/idea_dashboard/videos.db "SELECT id, video_id, user_id FROM videos WHERE video_id=\"dQw4w9WgXcQ\""'`
   - Expect: 2 rows now — Andy's (if he tracked Rickroll) and Mike's, with different user_ids.

## 6. Non-member rejection

Have someone (anyone outside AI Mate) visit /login and try a handle that isn't on the allowlist. Expected: `403 not on allowlist`. Confirm in logs:
```bash
ssh root@148.230.108.170 'docker logs idea_dashboard 2>&1 | grep "not on allowlist" | tail -5'
```

## 7. Revocation test

```bash
ssh root@148.230.108.170 'docker exec idea_dashboard python3 /opt/idea_dashboard/scripts/skool_allowlist.py \
  revoke --handle MIKES_REAL_HANDLE --reason "smoke test"'
```

Mike refreshes his browser. Expected: `401 access revoked`, gets bounced to /login.

Re-add Mike:
```bash
ssh root@148.230.108.170 'docker exec idea_dashboard python3 /opt/idea_dashboard/scripts/skool_allowlist.py \
  add --handle MIKES_REAL_HANDLE --email mike@example.com'
```

## 8. Rollback (if anything broke)

```bash
ssh root@148.230.108.170 '
  cd /opt/idea_dashboard
  cp videos.db.pre_skool_auth.* videos.db
  git checkout main
  ./rebuild.sh
'
```

## 9. If all of 1–7 pass

```bash
cd ~/dev/creatorgrowth
git checkout main
git merge --no-ff skool-auth -m "merge skool-auth: Skool gate + per-user data scoping"
git push origin main
git push vps main
```

Then mark the goal complete.

---

## What this smoke test does NOT cover

- Cancel-poll automation (the nightly sync depends on either Chrome-ext
  scraping or the manual roster file — see `overnight/skool_auth_blockers.md`
  for the data-source decision).
- Mass DM-code verification under load — single-Mike test only.
- A real Skool OAuth handshake — `SKOOL_API_KEY` is empty in prod today, so
  callback is in mock mode (which the allowlist+DM-code combo neutralizes
  for security: even mock mode can't grant a session to a non-allowlisted
  handle).
