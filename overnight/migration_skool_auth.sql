-- Skool OAuth + per-user data scoping migration
-- Applied to a copy of videos.db (videos_test.db) in this branch.
-- Live DB gets it via init_db() on the next deploy, gated on schema-version detection.
--
-- All ALTERs are idempotent via the pre-flight check in init_db(); raw SQL here
-- runs once against the test copy with `sqlite3 ... < migration_skool_auth.sql`.

BEGIN;

-- 1. Extend users with Skool fields ─────────────────────────────────────────
ALTER TABLE users ADD COLUMN skool_handle TEXT;
ALTER TABLE users ADD COLUMN skool_member_id TEXT;
ALTER TABLE users ADD COLUMN access_token TEXT;
ALTER TABLE users ADD COLUMN token_expires_at TEXT;
CREATE INDEX IF NOT EXISTS idx_users_skool_handle ON users(skool_handle);

-- 2. Add user_id to top-level scoped tables ─────────────────────────────────
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

-- 3. Backfill: all existing rows belong to Andy ─────────────────────────────
INSERT INTO users (email, last_login)
VALUES ('andhaf94@gmail.com', CURRENT_TIMESTAMP)
ON CONFLICT(email) DO NOTHING;

UPDATE videos             SET user_id = (SELECT id FROM users WHERE email='andhaf94@gmail.com') WHERE user_id IS NULL;
UPDATE show_docs          SET user_id = (SELECT id FROM users WHERE email='andhaf94@gmail.com') WHERE user_id IS NULL;
UPDATE channels           SET user_id = (SELECT id FROM users WHERE email='andhaf94@gmail.com') WHERE user_id IS NULL;
UPDATE keywords           SET user_id = (SELECT id FROM users WHERE email='andhaf94@gmail.com') WHERE user_id IS NULL;
UPDATE my_channel_videos  SET user_id = (SELECT id FROM users WHERE email='andhaf94@gmail.com') WHERE user_id IS NULL;
UPDATE tweets             SET user_id = (SELECT id FROM users WHERE email='andhaf94@gmail.com') WHERE user_id IS NULL;

COMMIT;

-- Verification queries (run manually after migration):
-- SELECT name FROM pragma_table_info('users')         WHERE name IN ('skool_handle','skool_member_id','access_token','token_expires_at');
-- SELECT name FROM pragma_table_info('videos')        WHERE name='user_id';
-- SELECT COUNT(*) AS unscoped_videos FROM videos WHERE user_id IS NULL;
