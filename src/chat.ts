/**
 * chat.ts — Web chat input สำหรับ กุชชี่ dashboard (go.forsi.co/app)
 * คำสั่ง keyword เดียวกับที่ LINE bot รองรับใน line.ts (handleTextMessage) แต่ตอบเป็น
 * plain text แทน LINE Flex Message — ไม่มีแนวคิด "หลาย user" ในหน้าเว็บนี้ ทุกคำสั่ง
 * ผูกกับ ADMIN_LINE_USER_ID (เจ้าของแอป) โดยตรง ไม่เรียก LLM ตีความ (ยังไม่ใช่ Phase 3)
 */
import type { Env } from "./types";
import { getPrimaryLocation, setAlertEnabled, setAlertHours, insertFeedback, getUser } from "./db";
import { getRainForecast, getRainForecastAtTime, buildAlertRecommendation } from "./weather";
import { parseTime, bangkokHour } from "./line";

const weatherKeys = (env: Env) => ({ tomorrowApiKey: env.TOMORROW_IO_API_KEY, tmdApiKey: env.TMD_API_KEY });

const RAIN_KEYWORDS = ["ฝน", "ฝนตกไหม", "ฝนไหม", "ฝนตก", "rain", "🌧️", "🌧"];
const ON_KEYWORDS = ["เปิด", "เปิดแจ้งเตือน", "on"];
const OFF_KEYWORDS = ["ปิด", "ปิดแจ้งเตือน", "off"];
const ALERT_TIME_KEYWORDS = ["แจ้งเตือน", "เวลาแจ้ง", "ตั้งเวลา"];
const FEEDBACK_PREFIXES = ["ติชม", "ฟีดแบค", "feedback", "แนะนำ"];
const TIME_CHECK_KEYWORDS = ["ออก", "ไป", "กลับ", "เดินทาง", "เช้า", "เย็น", "ถึง"];

const HELP_TEXT = `พิมพ์คำสั่งพวกนี้ได้เลย:

🌧️ "ฝนตกไหม" — เช็คฝนตอนนี้
✅ "เปิด" / 🔕 "ปิด" — เปิด/ปิดแจ้งเตือนฝน
⏰ "แจ้งเตือน 6:00-22:00" — ตั้งช่วงเวลาแจ้งเตือน
🚶 "ออกบ้าน 8.00" — เช็คฝนล่วงหน้าตามเวลา
💬 "ติชม ..." — ส่งความเห็น`;

export async function handleChatCommand(text: string, env: Env): Promise<string> {
  const uid = env.ADMIN_LINE_USER_ID;
  if (!uid) return "ยังไม่ได้ตั้งค่าเจ้าของแอปเลย (ADMIN_LINE_USER_ID)";

  const trimmed = text.trim();
  const tl = trimmed.toLowerCase();
  if (!trimmed) return HELP_TEXT;

  // ── เช็คฝนตอนนี้ ─────────────────────────────
  if (RAIN_KEYWORDS.includes(tl)) {
    const loc = await getPrimaryLocation(env.DB, uid);
    if (!loc) return "ยังไม่มีตำแหน่งที่ตั้งไว้เลย ส่ง location มาก่อนนะ";
    const forecast = await getRainForecast(loc.latitude, loc.longitude, weatherKeys(env));
    const recommend = buildAlertRecommendation(forecast);
    return `${forecast.emoji} ${forecast.intensityTh}\n📍 ${loc.label}\n\n${recommend}`;
  }

  // ── เปิด/ปิดแจ้งเตือน ─────────────────────────
  if (ON_KEYWORDS.includes(tl)) {
    await setAlertEnabled(env.DB, uid, true);
    return "✅ เปิดการแจ้งเตือนฝนแล้วครับ";
  }
  if (OFF_KEYWORDS.includes(tl)) {
    await setAlertEnabled(env.DB, uid, false);
    return "🔕 ปิดการแจ้งเตือนแล้วครับ\nพิมพ์ 'เปิด' เมื่อต้องการเปิดอีกครั้ง";
  }

  // ── ติชม / feedback (เช็คก่อนคำสั่งตั้งเวลา เหมือน line.ts) ──
  const fbPrefix = FEEDBACK_PREFIXES.find((p) => tl.startsWith(p));
  if (fbPrefix) {
    const content = trimmed.slice(fbPrefix.length).replace(/^[\s:：,]+/, "").trim();
    if (!content) return "💬 อยากบอกอะไรพิมพ์ต่อท้ายได้เลย เช่น 'ติชม แจ้งเตือนช้าไปนิดนึง'";
    await insertFeedback(env.DB, uid, content);
    return "🙏 ได้รับข้อความแล้ว ขอบคุณมากนะ";
  }

  // ── ตั้งค่าเวลาแจ้งเตือน เช่น "แจ้งเตือน 6:00-22:00" ────────
  if (ALERT_TIME_KEYWORDS.some((kw) => tl.includes(kw))) {
    const m = trimmed.match(/(\d{1,2})[.:](\d{2})\s*-\s*(\d{1,2})[.:](\d{2})/);
    if (m) {
      const startH = parseInt(m[1], 10);
      const endH = parseInt(m[3], 10);
      if (startH >= endH) return "เวลาเริ่มต้นต้องเร็วกว่าเวลาสิ้นสุดนะครับ 😅";
      await setAlertHours(env.DB, uid, startH, endH);
      return `✅ ตั้งค่าแจ้งเตือนแล้ว\n⏰ ${String(startH).padStart(2, "0")}:00 - ${String(endH).padStart(2, "0")}:00 น.`;
    }
    const user = await getUser(env.DB, uid);
    return `⏰ ช่วงเวลาแจ้งเตือนตอนนี้: ${String(user?.alertStartHour ?? 6).padStart(2, "0")}:00 - ${String(user?.alertEndHour ?? 22).padStart(2, "0")}:00 น.\nพิมพ์ "แจ้งเตือน 6:00-22:00" เพื่อเปลี่ยน`;
  }

  // ── เช็คฝนตามเวลา เช่น "ออกบ้าน 8.00" ────────
  const parsedTime = parseTime(tl);
  if (parsedTime !== null && TIME_CHECK_KEYWORDS.some((kw) => tl.includes(kw))) {
    const [hour, minute] = parsedTime;
    const loc = await getPrimaryLocation(env.DB, uid);
    if (!loc) return "ยังไม่มีตำแหน่งที่ตั้งไว้เลย ส่ง location มาก่อนนะ";

    const forecast = await getRainForecastAtTime(loc.latitude, loc.longitude, hour, minute, env.TOMORROW_IO_API_KEY);
    const timeStr = `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
    // นาทีของเวลาไทยเท่ากับนาทีของ UTC เสมอ (ต่างกันแค่ชั่วโมงเต็ม ๆ ไม่มี DST)
    const nowMins = bangkokHour() * 60 + new Date().getUTCMinutes();
    let deltaMins = hour * 60 + minute - nowMins;
    if (deltaMins <= 0) deltaMins += 24 * 60;

    if (forecast.intensity === "out_of_range") {
      return `⏰ ${timeStr} น. ยังอีก ${Math.floor(deltaMins / 60)} ชม. ${deltaMins % 60} นาทีนะ\n\nดูล่วงหน้าได้แค่ 6 ชม. ค่อยเช็คใหม่ทีหลังได้เลย 😅`;
    }
    const timeLabel = deltaMins >= 60 ? `อีก ${Math.floor(deltaMins / 60)} ชม. ${deltaMins % 60} นาที` : `อีก ${deltaMins} นาที`;
    return `${forecast.emoji} เวลา ${timeStr} น. (${timeLabel}) — ${forecast.intensityTh}\n📍 ${loc.label}`;
  }

  // ── ทุกอย่างอื่น → help ───────────────────────
  return HELP_TEXT;
}
