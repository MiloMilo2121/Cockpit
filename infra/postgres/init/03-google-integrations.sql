CREATE TABLE IF NOT EXISTS cockpit_google_oauth_states (
  state TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  scopes JSONB NOT NULL,
  redirect_uri TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  consumed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS cockpit_google_accounts (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT 'google',
  google_email TEXT NOT NULL,
  google_subject TEXT,
  display_name TEXT,
  access_token TEXT NOT NULL,
  refresh_token TEXT,
  token_type TEXT,
  token_expiry TIMESTAMPTZ,
  scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (provider, user_id, google_email)
);

CREATE INDEX IF NOT EXISTS idx_cockpit_google_accounts_user_id
ON cockpit_google_accounts (user_id);

CREATE TABLE IF NOT EXISTS cockpit_sync_cursors (
  account_id BIGINT NOT NULL REFERENCES cockpit_google_accounts(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  cursor_key TEXT NOT NULL,
  cursor_value TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (account_id, provider, cursor_key)
);

CREATE TABLE IF NOT EXISTS cockpit_raw_events (
  event_uid TEXT PRIMARY KEY,
  account_id BIGINT NOT NULL REFERENCES cockpit_google_accounts(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  external_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  source_cursor TEXT NOT NULL DEFAULT '',
  payload JSONB NOT NULL,
  occurred_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cockpit_external_documents (
  account_id BIGINT NOT NULL REFERENCES cockpit_google_accounts(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  external_document_id TEXT NOT NULL,
  title TEXT NOT NULL,
  mime_type TEXT,
  content TEXT NOT NULL DEFAULT '',
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (account_id, provider, external_document_id)
);
