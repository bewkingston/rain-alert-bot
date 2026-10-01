-- alert_cooldown เดิมไม่เคยมีผลจริง: INSERT ฝัง 5 นาทีตรงๆ (ดู db.ts getOrCreateUser)
-- ทั้งที่ schema กำหนด default ไว้ 30 อยู่แล้ว แต่ทั้งสองค่าน้อยกว่าหน้าต่าง
-- "ฝนรอบเดียวกัน" 180 นาทีที่ฮาร์ดโค้ดแยกไว้ใน index.ts เสมอ เลยไม่เคยถูกใช้งาน
-- ตอนนี้ผูก alert_cooldown เป็นหน้าต่างเดียวกันจริงๆ (ดู autoRainAlert) และยกเป็น
-- 360 นาที — backfill user เดิมที่ยังติดค่าบั๊ก (5 หรือ 30) ให้ได้ค่าใหม่ด้วย
UPDATE users SET alert_cooldown = 360 WHERE alert_cooldown <= 30;
