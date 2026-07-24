-- Rain Alert Worker — initial schema
-- Ported from database.py's SQLAlchemy models. SQLite/D1 has no native
-- boolean type — is_active/alert_enabled/is_primary use INTEGER 0/1.

CREATE TABLE users (
  line_user_id   TEXT PRIMARY KEY,
  display_name   TEXT,
  is_active      INTEGER NOT NULL DEFAULT 1,
  alert_enabled  INTEGER NOT NULL DEFAULT 1,
  alert_cooldown INTEGER NOT NULL DEFAULT 30,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE user_locations (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  line_user_id TEXT NOT NULL,
  label        TEXT NOT NULL DEFAULT 'ตำแหน่งปัจจุบัน',
  latitude     REAL NOT NULL,
  longitude    REAL NOT NULL,
  is_primary   INTEGER NOT NULL DEFAULT 1,
  updated_at   TEXT NOT NULL
);

CREATE INDEX idx_user_locations_line_user_id ON user_locations (line_user_id);
CREATE INDEX idx_user_locations_primary ON user_locations (line_user_id, is_primary);

CREATE TABLE alert_logs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  line_user_id    TEXT NOT NULL,
  rain_intensity  TEXT,
  minutes_to_rain INTEGER,
  source          TEXT,
  message_sent    TEXT,
  sent_at         TEXT NOT NULL
);

CREATE INDEX idx_alert_logs_line_user_id ON alert_logs (line_user_id);
CREATE INDEX idx_alert_logs_user_sent_at ON alert_logs (line_user_id, sent_at DESC);
