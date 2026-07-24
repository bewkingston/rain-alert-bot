export interface Env {
  DB: D1Database;
  LINE_CHANNEL_SECRET: string;
  /** Channel ID (ตัวเลข) — ถ้าตั้งไว้ บอทจะออก access token เองอัตโนมัติผ่าน OAuth */
  LINE_CHANNEL_ID?: string;
  /** ทางเลือก: token แบบ long-lived ที่ออกเองจาก console (ใช้แทน LINE_CHANNEL_ID ได้) */
  LINE_CHANNEL_ACCESS_TOKEN?: string;
  TOMORROW_IO_API_KEY: string;
  TMD_API_KEY: string;
  /** ตั้งไว้ถ้าอยากให้ forward ข้อความ 'ติชม ...' เข้า LINE ส่วนตัว (optional) */
  ADMIN_LINE_USER_ID?: string;
}
