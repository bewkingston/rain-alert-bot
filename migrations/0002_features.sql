-- Feature parity with github.com/bewkingston/rain-alert-bot @ c7e89ae:
-- alert time preferences, alert feedback, user feedback channel.

ALTER TABLE users ADD COLUMN alert_start_hour INTEGER NOT NULL DEFAULT 6;
ALTER TABLE users ADD COLUMN alert_end_hour   INTEGER NOT NULL DEFAULT 22;

ALTER TABLE alert_logs ADD COLUMN user_feedback TEXT;
ALTER TABLE alert_logs ADD COLUMN feedback_at   TEXT;

CREATE TABLE feedbacks (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  line_user_id TEXT NOT NULL,
  message      TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE INDEX idx_feedbacks_line_user_id ON feedbacks (line_user_id);
