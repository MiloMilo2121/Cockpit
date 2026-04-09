CREATE TABLE IF NOT EXISTS cockpit_message_events (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  source_message_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  payload JSONB NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (source, source_message_id)
);

CREATE TABLE IF NOT EXISTS cockpit_message_jobs (
  source TEXT NOT NULL,
  source_message_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (source, source_message_id)
);

CREATE TABLE IF NOT EXISTS cockpit_dead_letter_events (
  id BIGSERIAL PRIMARY KEY,
  stage TEXT NOT NULL,
  reason TEXT NOT NULL,
  payload JSONB NOT NULL,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
