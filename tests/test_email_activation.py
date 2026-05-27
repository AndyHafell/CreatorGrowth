"""Email-based activation + paid-member webhook tests.

The new default auth path:
  /api/auth/skool/activate {email} → 7-day trial, session, allowlisted=1
  /api/admin/paid-members  {email, name} → UPSERT, flips to 'paid', NULL trial_end

before_request enforces trial-expiry: if trial_end < now and source still
'trial', flip to 'trial:expired' and 401.
"""


def test_activate_new_user_starts_trial(client, db):
    r = client.post("/api/auth/skool/activate", json={"email": "new@example.com"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["email"] == "new@example.com"

    row = db.execute(
        "SELECT allowlisted, allowlist_source, trial_end FROM users WHERE email = ?",
        ("new@example.com",),
    ).fetchone()
    assert row["allowlisted"] == 1
    assert row["allowlist_source"] == "trial"
    assert row["trial_end"] is not None

    # Session is live: subsequent authed request succeeds
    r = client.get("/api/auth-status")
    assert r.status_code == 200
    assert r.get_json()["authenticated"] is True


def test_activate_rejects_invalid_email(client):
    r = client.post("/api/auth/skool/activate", json={"email": "not-an-email"})
    assert r.status_code == 400


def test_activate_blocks_revoked_user(client, db):
    db.execute(
        "INSERT INTO users (email, allowlisted, allowlist_source, revoked_at) "
        "VALUES ('churned@example.com', 0, 'trial:expired', datetime('now'))"
    )
    db.commit()
    r = client.post("/api/auth/skool/activate", json={"email": "churned@example.com"})
    assert r.status_code == 403
    assert "join_url" in r.get_json()


def test_activate_logs_in_existing_paid_user(client, db):
    db.execute(
        "INSERT INTO users (email, allowlisted, allowlist_source, allowlisted_at) "
        "VALUES ('paid@example.com', 1, 'paid', datetime('now'))"
    )
    db.commit()
    r = client.post("/api/auth/skool/activate", json={"email": "paid@example.com"})
    assert r.status_code == 200
    # Source stays 'paid' (we don't downgrade a paid user to trial)
    row = db.execute(
        "SELECT allowlist_source FROM users WHERE email = ?", ("paid@example.com",)
    ).fetchone()
    assert row["allowlist_source"] == "paid"


def test_paid_webhook_requires_bearer(client):
    r = client.post("/api/admin/paid-members", json={"email": "x@y.com"})
    assert r.status_code == 401


def test_paid_webhook_creates_new_paid_user(client, db):
    r = client.post(
        "/api/admin/paid-members",
        headers={"Authorization": "Bearer pytest-paid-token"},
        json={"email": "fresh@example.com", "name": "Fresh Convert"},
    )
    assert r.status_code == 200

    row = db.execute(
        "SELECT allowlisted, allowlist_source, trial_end, display_name "
        "FROM users WHERE email = ?", ("fresh@example.com",)
    ).fetchone()
    assert row["allowlisted"] == 1
    assert row["allowlist_source"] == "paid"
    assert row["trial_end"] is None
    assert row["display_name"] == "Fresh Convert"

    log = db.execute(
        "SELECT email, name FROM paid_webhook_log WHERE email = ?",
        ("fresh@example.com",),
    ).fetchone()
    assert log is not None
    assert log["name"] == "Fresh Convert"


def test_paid_webhook_upgrades_trial_user_to_paid(client, db):
    # User starts a trial, then Skool fires new_paid_member
    client.post("/api/auth/skool/activate", json={"email": "convert@example.com"})
    row = db.execute(
        "SELECT allowlist_source, trial_end FROM users WHERE email = ?",
        ("convert@example.com",),
    ).fetchone()
    assert row["allowlist_source"] == "trial"
    assert row["trial_end"] is not None

    r = client.post(
        "/api/admin/paid-members",
        headers={"Authorization": "Bearer pytest-paid-token"},
        json={"email": "convert@example.com", "name": "Conversion Name"},
    )
    assert r.status_code == 200

    row = db.execute(
        "SELECT allowlisted, allowlist_source, trial_end, revoked_at "
        "FROM users WHERE email = ?", ("convert@example.com",),
    ).fetchone()
    assert row["allowlisted"] == 1
    assert row["allowlist_source"] == "paid"
    assert row["trial_end"] is None
    assert row["revoked_at"] is None


def test_paid_webhook_unrevokes_churned_user(client, db):
    # Old user was revoked; Skool fires new_paid_member (they re-subscribed)
    db.execute(
        "INSERT INTO users (email, allowlisted, allowlist_source, revoked_at) "
        "VALUES ('back@example.com', 0, 'trial:expired', datetime('now'))"
    )
    db.commit()
    r = client.post(
        "/api/admin/paid-members",
        headers={"Authorization": "Bearer pytest-paid-token"},
        json={"email": "back@example.com", "name": "Back Again"},
    )
    assert r.status_code == 200
    row = db.execute(
        "SELECT allowlisted, allowlist_source, revoked_at "
        "FROM users WHERE email = ?", ("back@example.com",),
    ).fetchone()
    assert row["allowlisted"] == 1
    assert row["allowlist_source"] == "paid"
    assert row["revoked_at"] is None


def test_trial_expiry_revokes_on_next_request(client, db):
    # Start trial then artificially backdate trial_end to the past
    client.post("/api/auth/skool/activate", json={"email": "stale@example.com"})
    db.execute(
        "UPDATE users SET trial_end = datetime('now', '-1 hour') WHERE email = ?",
        ("stale@example.com",),
    )
    db.commit()

    # Next authed request should 401 with 'trial expired'
    r = client.get("/api/videos")
    assert r.status_code == 401
    body = r.get_json()
    assert body["error"] == "trial expired"
    assert body["join_url"] == "https://www.skool.com/aimate"

    # And the row is flipped
    row = db.execute(
        "SELECT allowlisted, allowlist_source FROM users WHERE email = ?",
        ("stale@example.com",),
    ).fetchone()
    assert row["allowlisted"] == 0
    assert row["allowlist_source"] == "trial:expired"


def test_trial_expiry_does_not_affect_paid_user(client, db):
    # Paid user has trial_end NULL — expiry sweep must skip them.
    client.post(
        "/api/admin/paid-members",
        headers={"Authorization": "Bearer pytest-paid-token"},
        json={"email": "loyal@example.com", "name": "Loyal Customer"},
    )
    # Now do an authed action as Loyal
    client.post("/api/auth/skool/activate", json={"email": "loyal@example.com"})
    r = client.get("/api/videos")
    assert r.status_code == 200

    row = db.execute(
        "SELECT allowlisted, allowlist_source FROM users WHERE email = ?",
        ("loyal@example.com",),
    ).fetchone()
    assert row["allowlisted"] == 1
    assert row["allowlist_source"] == "paid"


def test_trial_still_valid_inside_window(client, db):
    # Fresh trial — trial_end is +7d. Next request should pass.
    client.post("/api/auth/skool/activate", json={"email": "fresh@example.com"})
    r = client.get("/api/videos")
    assert r.status_code == 200
