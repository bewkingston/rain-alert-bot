-- Cache สำหรับ channel access token ที่ออกอัตโนมัติผ่าน LINE OAuth
-- (client_credentials — อายุ 30 วัน ต่ออายุเองเมื่อใกล้หมด)

CREATE TABLE kv_cache (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
