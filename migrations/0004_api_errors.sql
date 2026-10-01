-- บันทึก LINE API error ที่เดิม console.error เฉย ๆ แล้วหายไปเงียบ ๆ
-- (เช่น 429 quota exceeded ตอน push) — ให้เช็คย้อนหลังได้ผ่าน /api/errors

CREATE TABLE api_errors (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  occurred_at  TEXT NOT NULL,
  endpoint     TEXT NOT NULL,
  line_user_id TEXT,
  status_code  INTEGER,
  detail       TEXT
);

CREATE INDEX idx_api_errors_occurred_at ON api_errors (occurred_at);
