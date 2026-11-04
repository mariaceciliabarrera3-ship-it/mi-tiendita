ALTER TABLE tenants ADD COLUMN timezone TEXT NOT NULL DEFAULT 'America/Argentina/Buenos_Aires';
CREATE TABLE auth_rate_limits (rate_key TEXT PRIMARY KEY, window_start INTEGER NOT NULL, attempts INTEGER NOT NULL);
CREATE TABLE auth_action_tokens (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), purpose TEXT NOT NULL CHECK(purpose IN ('verify_email','reset_password')), expires_at INTEGER NOT NULL, used_at INTEGER, created_at INTEGER NOT NULL);
CREATE INDEX idx_auth_action_user ON auth_action_tokens(user_id,purpose);
