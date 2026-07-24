/**
 * line.ts — Rain Alert Worker
 * LINE webhook signature verification, Messaging API calls (fetch-based,
 * no @line/bot-sdk — avoids Node-shim compatibility risk on Workers),
 * Flex Message builders, and webhook event handling.
 * Ported from github.com/bewkingston/rain-alert-bot @ c7e89ae
 * (main.py webhook route + line_handler.py). LIFF/Rain Route ถูกตัดออก —
 * ไม่เคยตั้งค่า LIFF_ID จริง (placeholder ใน rich-menu.json).
 */

import type { Env } from "./types";
import {
  getOrCreateUser,
  getUser,
  getPrimaryLocation,
  upsertLocation,
  setUserActive,
  setAlertEnabled,
  setAlertHours,
  recordAlertFeedback,
  insertFeedback,
} from "./db";
import {
  getRainForecast,
  getRainForecastAtTime,
  buildAlertRecommendation,
  type RainForecast,
} from "./weather";

// ─────────────────────────────────────────────
//  Signature verification
// ─────────────────────────────────────────────

/** LINE signs the raw UTF-8 request body, HMAC-SHA256, base64, header x-line-signature */
export async function verifyLineSignature(
  rawBody: ArrayBuffer,
  sigB64: string,
  secret: string
): Promise<boolean> {
  if (!sigB64 || !secret) return false;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"]
  );
  let sig: Uint8Array;
  try {
    sig = Uint8Array.from(atob(sigB64), (c) => c.charCodeAt(0));
  } catch {
    return false;
  }
  return crypto.subtle.verify("HMAC", key, sig, rawBody);
}

// ─────────────────────────────────────────────
//  Access token — ออกอัตโนมัติผ่าน OAuth (client_credentials)
// ─────────────────────────────────────────────

const TOKEN_CACHE_KEY = "line_access_token";

/**
 * คืน channel access token:
 * 1. ถ้าตั้ง LINE_CHANNEL_ACCESS_TOKEN (long-lived จาก console) → ใช้ตัวนั้น
 * 2. ไม่งั้นใช้ token จาก D1 cache (ออกผ่าน OAuth, อายุ 30 วัน)
 * 3. ถ้าหมดอายุ/ยังไม่มี → ขอใหม่ด้วย Channel ID + secret แล้ว cache
 */
export async function getAccessToken(env: Env, forceRefresh = false): Promise<string> {
  if (env.LINE_CHANNEL_ACCESS_TOKEN) return env.LINE_CHANNEL_ACCESS_TOKEN;
  if (!env.LINE_CHANNEL_ID) {
    throw new Error("ต้องตั้ง LINE_CHANNEL_ID หรือ LINE_CHANNEL_ACCESS_TOKEN อย่างใดอย่างหนึ่ง");
  }

  if (!forceRefresh) {
    const cached = await env.DB
      .prepare("SELECT value FROM kv_cache WHERE key = ? AND expires_at > ?")
      .bind(TOKEN_CACHE_KEY, new Date().toISOString())
      .first<{ value: string }>();
    if (cached) return cached.value;
  }

  const resp = await fetch("https://api.line.me/v2/oauth/accessToken", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "client_credentials",
      client_id: env.LINE_CHANNEL_ID,
      client_secret: env.LINE_CHANNEL_SECRET,
    }),
  });
  if (!resp.ok) {
    throw new Error(`LINE token issue failed: ${resp.status} ${await resp.text()}`);
  }
  const data: { access_token: string; expires_in: number } = await resp.json();

  // ต่ออายุก่อนหมดจริง 1 วัน กันขาดช่วง
  const expiresAt = new Date(Date.now() + (data.expires_in - 86_400) * 1000).toISOString();
  await env.DB
    .prepare("INSERT OR REPLACE INTO kv_cache (key, value, expires_at) VALUES (?, ?, ?)")
    .bind(TOKEN_CACHE_KEY, data.access_token, expiresAt)
    .run();

  console.log("✅ Issued new LINE access token via OAuth");
  return data.access_token;
}

// ─────────────────────────────────────────────
//  Messaging API (reply / push)
// ─────────────────────────────────────────────

async function callLineApi(path: "reply" | "push", body: unknown, env: Env): Promise<void> {
  let token = await getAccessToken(env);
  let resp = await fetch(`https://api.line.me/v2/bot/message/${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });

  // token ใน cache ถูกเพิกถอน/หมดอายุก่อนเวลา → ออกใหม่แล้วลองซ้ำครั้งเดียว
  if (resp.status === 401 && !env.LINE_CHANNEL_ACCESS_TOKEN && env.LINE_CHANNEL_ID) {
    token = await getAccessToken(env, true);
    resp = await fetch(`https://api.line.me/v2/bot/message/${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
    });
  }

  if (!resp.ok) {
    console.error(`LINE API error (${path}): ${resp.status} ${await resp.text()}`);
  }
}

/** Quick Reply ปุ่ม 'อัพเดทตำแหน่ง' — LINE เปิด location picker ให้เอง */
const LOCATION_QUICK_REPLY = {
  items: [{ type: "action", action: { type: "location", label: "📍 อัพเดทตำแหน่งของฉัน" } }],
};

function textMessage(text: string) {
  return { type: "text", text };
}

function flexMessage(altText: string, contents: unknown, withLocationQuickReply = false) {
  return {
    type: "flex",
    altText,
    contents,
    ...(withLocationQuickReply ? { quickReply: LOCATION_QUICK_REPLY } : {}),
  };
}

async function reply(replyToken: string, messages: unknown[], env: Env) {
  await callLineApi("reply", { replyToken, messages }, env);
}

async function push(to: string, messages: unknown[], env: Env) {
  await callLineApi("push", { to, messages }, env);
}

// ─────────────────────────────────────────────
//  Time formatting (Asia/Bangkok, no DST)
// ─────────────────────────────────────────────

function bangkokParts(date: Date) {
  const fmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Bangkok",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
  const parts: Record<string, string> = {};
  for (const p of fmt.formatToParts(date)) parts[p.type] = p.value;
  return parts;
}

function formatDateTimeTh(date: Date): string {
  const p = bangkokParts(date);
  return `${p.day}/${p.month} ${p.hour}:${p.minute} น.`;
}

function formatTimeTh(date: Date): string {
  const p = bangkokParts(date);
  return `${p.hour}:${p.minute}`;
}

export function bangkokHour(date: Date = new Date()): number {
  return parseInt(bangkokParts(date).hour, 10);
}

// ─────────────────────────────────────────────
//  Shared Flex pieces
// ─────────────────────────────────────────────

const COLOR_MAP: Record<string, string> = {
  none: "#2E7D32",
  light: "#1565C0",
  moderate: "#E65100",
  heavy: "#B71C1C",
  violent: "#6A1B9A",
};

/** คืน [minsText, durText] — 'อีก X นาที (~HH:MM น.)' และ '~X นาที (หยุด ~HH:MM น.)' */
export function rainTimingTexts(forecast: RainForecast): [string, string | null] {
  const now = new Date();
  const mins = forecast.minutesToRain;

  let minsText: string;
  if (!forecast.willRain) {
    minsText = "ไม่มีฝนใน 1 ชม. ☀️";
  } else if (mins === 0 || mins === null) {
    // tomorrow_io/open_meteo เป็นข้อมูล minutely — mins 0/null แปลว่าตกอยู่ตอนนี้
    minsText = forecast.source !== "tmd" ? "ตกอยู่เลยนะ ระวังด้วย ☔" : "อีกซักครู่นึง (1-2 ชม.)";
  } else {
    const start = formatTimeTh(new Date(now.getTime() + mins * 60_000));
    minsText = `อีก ${mins} นาที (~${start} น.)`;
  }

  let durText: string | null = null;
  if (forecast.willRain) {
    const dur = forecast.rainDurationMin;
    if (dur) {
      const end = formatTimeTh(new Date(now.getTime() + ((mins ?? 0) + dur) * 60_000));
      durText = `~${dur} นาที (หยุด ~${end} น.)`;
    } else if (forecast.source === "tomorrow_io" || forecast.source === "open_meteo") {
      durText = "ต่อเนื่องเกิน 1 ชม. ☔";
    }
  }
  return [minsText, durText];
}

function infoRow(label: string, value: string) {
  return {
    type: "box",
    layout: "horizontal",
    contents: [
      { type: "text", text: label, color: "#888888", size: "sm", flex: 2 },
      { type: "text", text: value, color: "#111111", size: "sm", weight: "bold", flex: 3, wrap: true },
    ],
  };
}

function howRow(icon: string, label: string, desc: string) {
  return {
    type: "box",
    layout: "horizontal",
    spacing: "md",
    alignItems: "center",
    contents: [
      { type: "text", text: icon, size: "md", flex: 0 },
      {
        type: "box",
        layout: "vertical",
        flex: 1,
        contents: [
          { type: "text", text: label, size: "sm", color: "#111111", weight: "bold", wrap: true },
          { type: "text", text: desc, size: "xs", color: "#888888", wrap: true, margin: "xs" },
        ],
      },
    ],
  };
}

// ─────────────────────────────────────────────
//  Flex builders
// ─────────────────────────────────────────────

export function buildRainFlex(forecast: RainForecast, recommend: string, locationLabel?: string | null) {
  const color = COLOR_MAP[forecast.intensity] ?? "#1565C0";
  const [minsText, durText] = rainTimingTexts(forecast);
  const nowStr = formatDateTimeTh(new Date());

  return {
    type: "bubble",
    size: "mega",
    header: {
      type: "box",
      layout: "vertical",
      backgroundColor: color,
      paddingAll: "20px",
      contents: [
        { type: "text", text: `${forecast.emoji} ${forecast.intensityTh}`, color: "#FFFFFF", weight: "bold", size: "xxl" },
        {
          type: "text",
          text: locationLabel ? `📍 ${locationLabel}  •  ${nowStr}` : nowStr,
          color: "#FFFFFFCC",
          size: "xs",
          margin: "sm",
          wrap: true,
        },
      ],
    },
    body: {
      type: "box",
      layout: "vertical",
      spacing: "md",
      contents: [
        infoRow("⏱ ฝนจะตก", minsText),
        ...(durText ? [infoRow("⏳ ตกนาน", durText)] : []),
        { type: "separator", margin: "md" },
        { type: "text", text: `💡 ${recommend}`, wrap: true, color: "#333333", size: "sm", margin: "md" },
      ],
    },
    footer: {
      type: "box",
      layout: "horizontal",
      spacing: "sm",
      contents: [
        {
          type: "button",
          style: "secondary",
          height: "sm",
          flex: 1,
          action: { type: "message", label: "ขอเช็กอีกรอบ", text: "ฝนตกไหม" },
        },
        {
          type: "button",
          style: "secondary",
          height: "sm",
          flex: 1,
          action: { type: "message", label: "⏰ ตั้งเวลาแจ้งเตือน", text: "แจ้งเตือน" },
        },
      ],
    },
  };
}

/** Flex สำหรับ auto-push alert — มี badge + ปุ่ม feedback ✅/❌ */
export function buildPushAlertFlex(
  forecast: RainForecast,
  recommend: string,
  locLabel: string,
  minsText: string,
  alertLogId: number,
  durText: string | null
) {
  const color = COLOR_MAP[forecast.intensity] ?? "#1565C0";
  const nowStr = formatDateTimeTh(new Date());

  return {
    type: "bubble",
    size: "mega",
    header: {
      type: "box",
      layout: "vertical",
      backgroundColor: color,
      paddingAll: "20px",
      contents: [
        { type: "text", text: "แค่อยากบอกว่าฝนจะมาแล้วนะ ☔", color: "#FFFFFF99", size: "xs", weight: "bold" },
        { type: "text", text: `${forecast.emoji} ${forecast.intensityTh}`, color: "#FFFFFF", weight: "bold", size: "xxl", margin: "sm" },
        { type: "text", text: `📍 ${locLabel}  •  ${nowStr}`, color: "#FFFFFFCC", size: "xs", margin: "sm" },
      ],
    },
    body: {
      type: "box",
      layout: "vertical",
      spacing: "md",
      contents: [
        infoRow("⏱ ฝนจะตก", minsText),
        ...(durText ? [infoRow("⏳ ตกนาน", durText)] : []),
        { type: "separator", margin: "md" },
        { type: "text", text: `💡 ${recommend}`, wrap: true, color: "#333333", size: "sm", margin: "md" },
      ],
    },
    footer: {
      type: "box",
      layout: "vertical",
      spacing: "sm",
      contents: [
        {
          type: "button",
          style: "secondary",
          height: "sm",
          action: { type: "message", label: "👀 ขอเช็กอีกรอบ", text: "ฝน" },
        },
        {
          type: "box",
          layout: "horizontal",
          spacing: "sm",
          contents: [
            {
              type: "button",
              style: "secondary",
              height: "sm",
              flex: 1,
              color: "#4CAF50",
              action: {
                type: "postback",
                label: "✅ ฝนตกจริง",
                data: `action=feedback&log_id=${alertLogId}&result=rain`,
                displayText: "✅ ฝนตกจริง",
              },
            },
            {
              type: "button",
              style: "secondary",
              height: "sm",
              flex: 1,
              color: "#F44336",
              action: {
                type: "postback",
                label: "❌ ไม่มีฝน",
                data: `action=feedback&log_id=${alertLogId}&result=no_rain`,
                displayText: "❌ ไม่มีฝน",
              },
            },
          ],
        },
      ],
    },
  };
}

/** Flex สรุปอากาศเช้า 07:00 น. — ส่งทุกวันไม่ว่าฝนจะตกหรือไม่ */
export function buildDailyWeatherFlex(forecast: RainForecast, recommend: string, locLabel: string) {
  const color = COLOR_MAP[forecast.intensity] ?? "#2E7D32";
  const nowStr = formatDateTimeTh(new Date());
  const [minsText, durText] = rainTimingTexts(forecast);

  return {
    type: "bubble",
    size: "mega",
    header: {
      type: "box",
      layout: "vertical",
      backgroundColor: color,
      paddingAll: "20px",
      contents: [
        { type: "text", text: "🌅 สรุปอากาศเช้านี้", color: "#FFFFFF99", size: "xs", weight: "bold" },
        { type: "text", text: `${forecast.emoji} ${forecast.intensityTh}`, color: "#FFFFFF", weight: "bold", size: "xxl", margin: "sm" },
        { type: "text", text: `📍 ${locLabel}  •  ${nowStr}`, color: "#FFFFFFCC", size: "xs", margin: "sm" },
      ],
    },
    body: {
      type: "box",
      layout: "vertical",
      spacing: "md",
      contents: [
        infoRow("⏱ สถานะฝน", minsText),
        ...(durText ? [infoRow("⏳ ตกนาน", durText)] : []),
        { type: "separator", margin: "md" },
        { type: "text", text: `💡 ${recommend}`, wrap: true, color: "#333333", size: "sm", margin: "md" },
      ],
    },
    footer: {
      type: "box",
      layout: "horizontal",
      spacing: "sm",
      contents: [
        {
          type: "button",
          style: "secondary",
          height: "sm",
          flex: 1,
          action: { type: "message", label: "👀 ขอเช็กอีกรอบ", text: "ฝน" },
        },
        {
          type: "button",
          style: "secondary",
          height: "sm",
          flex: 1,
          action: { type: "message", label: "⏰ ตั้งเวลาแจ้งเตือน", text: "แจ้งเตือน" },
        },
      ],
    },
  };
}

export function buildWelcomeFlex() {
  return {
    type: "bubble",
    size: "mega",
    header: {
      type: "box",
      layout: "vertical",
      backgroundColor: "#1565C0",
      paddingAll: "20px",
      contents: [
        { type: "text", text: "🌧️ Rain Alert", color: "#FFFFFF", weight: "bold", size: "xxl" },
        { type: "text", text: "เดี๋ยวทักให้เองนะ ไม่ต้องห่วง 🌂", color: "#BBDEFB", size: "sm", margin: "sm" },
      ],
    },
    body: {
      type: "box",
      layout: "vertical",
      spacing: "lg",
      contents: [
        {
          type: "box",
          layout: "vertical",
          backgroundColor: "#EBF4FF",
          cornerRadius: "4px",
          paddingAll: "14px",
          contents: [
            { type: "text", text: "เซ็ตแป๊บนึง แค่นั้นเอง", weight: "bold", size: "md", color: "#1565C0" },
            { type: "text", text: "แชร์ location ให้ก่อนนะ เดี๋ยวดูแลให้เอง", size: "sm", color: "#555555", wrap: true, margin: "sm" },
          ],
        },
        { type: "separator" },
        howRow("🌧️", "แจ้งก่อนเปียก", "ถ้าฝนจะมา เดี๋ยวบอกเองนะ ไม่ต้องเป็นห่วง"),
        howRow("💬", "พิมพ์ 'ฝน'", "อยากเช็คเองก็พิมพ์มาได้เลย"),
        howRow("⏰", "ตั้งเวลาแจ้งเตือน", "พิมพ์ 'แจ้งเตือน' เลือกช่วงเวลาที่สะดวก"),
      ],
    },
    footer: {
      type: "box",
      layout: "vertical",
      spacing: "sm",
      contents: [
        {
          type: "button",
          style: "secondary",
          height: "sm",
          action: { type: "message", label: "⏰ ตั้งเวลาแจ้งเตือน", text: "แจ้งเตือน" },
        },
      ],
    },
  };
}

export function buildHelpFlex() {
  return {
    type: "bubble",
    size: "mega",
    header: {
      type: "box",
      layout: "vertical",
      paddingAll: "16px",
      contents: [{ type: "text", text: "🌧️ ใช้ง่ายกว่าที่คิดนะ", weight: "bold", size: "lg" }],
    },
    body: {
      type: "box",
      layout: "vertical",
      spacing: "md",
      contents: [
        howRow("📍", "ส่ง location", "ส่งมาได้เลย เดี๋ยวเช็คให้"),
        howRow("💬", "พิมพ์ 'ฝน'", "พิมพ์แค่คำเดียวก็รู้เลย"),
        howRow("⏰", "พิมพ์ 'แจ้งเตือน'", "ตั้งช่วงเวลาที่อยากได้รับแจ้งเตือน"),
        howRow("🕗", "พิมพ์ 'ออกบ้าน 8.00'", "เช็คฝนล่วงหน้าตามเวลา (สูงสุด 6 ชม.)"),
        howRow("💬", "พิมพ์ 'ติชม ...'", "บอกปัญหา/ไอเดียมาได้เลย อ่านทุกข้อความ"),
      ],
    },
  };
}

export function buildNoLocationFlex() {
  return {
    type: "bubble",
    size: "kilo",
    body: {
      type: "box",
      layout: "vertical",
      spacing: "md",
      paddingAll: "20px",
      contents: [
        { type: "text", text: "ยังไม่รู้ว่าอยู่ที่ไหนเลยนะ 📍", weight: "bold", size: "lg", color: "#333333" },
        {
          type: "text",
          text: "กด + เลือก 'ตำแหน่ง' ส่งมาก่อนได้เลยนะ แล้วค่อยพิมพ์ 'ฝน' ใหม่",
          size: "sm",
          color: "#555555",
          wrap: true,
          margin: "md",
        },
      ],
    },
  };
}

export function buildTimeRainFlex(forecast: RainForecast, timeStr: string, timeLabel: string) {
  const color = COLOR_MAP[forecast.intensity] ?? "#1565C0";

  let header: string, advice: string;
  if (!forecast.willRain) {
    header = `☀️ ${timeStr} น. — ฟ้าใส`;
    advice = "ฟ้าใสเลย ออกได้สบายๆ ☀️";
  } else if (forecast.intensity === "light") {
    header = `🌦️ ${timeStr} น. — ฝนนิดหน่อย`;
    advice = "ฝนนิดหน่อยนะ เอาร่มไปด้วยก็ดี 🌂";
  } else if (forecast.intensity === "moderate") {
    header = `🌧️ ${timeStr} น. — ฝนพอสมควร`;
    advice = "ฝนพอสมควรเลยนะ เอาเสื้อกันฝนไปด้วยก็ดี 🧥";
  } else {
    header = `⛈️ ${timeStr} น. — ฝนหนักมาก`;
    advice = "ฝนหนักมากเลยนะ รอซาหน่อยก็ดีกว่า ⛈️";
  }

  return {
    type: "bubble",
    size: "mega",
    header: {
      type: "box",
      layout: "vertical",
      backgroundColor: color,
      paddingAll: "20px",
      contents: [
        { type: "text", text: header, color: "#FFFFFF", weight: "bold", size: "lg", wrap: true },
        { type: "text", text: `(${timeLabel})`, color: "#FFFFFFCC", size: "xs", margin: "sm" },
      ],
    },
    body: {
      type: "box",
      layout: "vertical",
      spacing: "md",
      contents: [
        { type: "separator", margin: "md" },
        { type: "text", text: `💡 ${advice}`, wrap: true, color: "#333333", size: "sm", margin: "md" },
      ],
    },
    footer: {
      type: "box",
      layout: "horizontal",
      spacing: "sm",
      contents: [
        {
          type: "button",
          style: "secondary",
          height: "sm",
          flex: 1,
          action: { type: "message", label: "🌧️ ฝนตอนนี้", text: "ฝน" },
        },
      ],
    },
  };
}

export function buildAlertTimeFlex(startHour: number, endHour: number) {
  const preset = (label: string, text: string) => ({
    type: "button",
    style: "primary",
    color: "#1565C0",
    height: "sm",
    action: { type: "message", label, text },
  });

  return {
    type: "bubble",
    size: "mega",
    header: {
      type: "box",
      layout: "vertical",
      backgroundColor: "#1565C0",
      paddingAll: "16px",
      contents: [
        { type: "text", text: "⏰ เวลาแจ้งเตือน", color: "#FFFFFF", weight: "bold", size: "lg" },
        {
          type: "text",
          text: `ตอนนี้: ${String(startHour).padStart(2, "0")}:00 - ${String(endHour).padStart(2, "0")}:00 น.`,
          color: "#BBDEFB",
          size: "sm",
          margin: "sm",
        },
      ],
    },
    body: {
      type: "box",
      layout: "vertical",
      spacing: "md",
      paddingAll: "16px",
      contents: [
        { type: "text", text: "เช่น: แจ้งเตือน 6:00-22:00", size: "xs", color: "#999999" },
        {
          type: "box",
          layout: "vertical",
          spacing: "sm",
          contents: [
            preset("🌅 6:00 - 22:00 น.", "แจ้งเตือน 6:00-22:00"),
            preset("🌄 7:00 - 21:00 น.", "แจ้งเตือน 7:00-21:00"),
            preset("💼 8:00 - 20:00 น.", "แจ้งเตือน 8:00-20:00"),
            preset("🌙 ตลอด 24 ชม.", "แจ้งเตือน 0:00-23:59"),
          ],
        },
      ],
    },
  };
}

// ─────────────────────────────────────────────
//  Webhook event types (minimal — only what we use)
// ─────────────────────────────────────────────

export interface LineEvent {
  type: string;
  replyToken?: string;
  source: { userId: string; type: string };
  message?: {
    type: string;
    text?: string;
    latitude?: number;
    longitude?: number;
    address?: string;
    title?: string;
  };
  postback?: { data: string };
}

const weatherKeys = (env: Env) => ({ tomorrowApiKey: env.TOMORROW_IO_API_KEY, tmdApiKey: env.TMD_API_KEY });

// ─────────────────────────────────────────────
//  Event handling
// ─────────────────────────────────────────────

/** Parses a LINE webhook request body ({destination, events: [...]}) and dispatches each event. */
export async function handleWebhookBody(bodyText: string, env: Env): Promise<void> {
  const parsed: { events?: LineEvent[] } = JSON.parse(bodyText);
  const events = parsed.events ?? [];
  await Promise.all(events.map((event) => handleLineEvent(event, env)));
}

export async function handleLineEvent(event: LineEvent, env: Env): Promise<void> {
  const uid = event.source?.userId;
  if (!uid) return;

  try {
    switch (event.type) {
      case "follow":
        await getOrCreateUser(env.DB, uid);
        if (event.replyToken) {
          await reply(
            event.replyToken,
            [flexMessage("ยินดีต้อนรับสู่ Rain Alert 🌧️", buildWelcomeFlex(), true)], env);
        }
        return;

      case "unfollow":
        await setUserActive(env.DB, uid, false);
        return;

      case "postback":
        await handlePostback(event, uid, env);
        return;

      case "message":
        if (event.message?.type === "location") {
          await handleLocationMessage(event, uid, env);
        } else if (event.message?.type === "text") {
          await handleTextMessage(event, uid, env);
        }
        return;

      default:
        return;
    }
  } catch (e) {
    console.error(`handleLineEvent error for ${uid}: ${e}`);
  }
}

async function handleLocationMessage(event: LineEvent, uid: string, env: Env): Promise<void> {
  const lat = event.message!.latitude!;
  const lon = event.message!.longitude!;
  // ใช้ address จาก LINE message เป็น label (ย่อให้สั้นลง)
  const addr = event.message!.address || event.message!.title || "ตำแหน่งปัจจุบัน";
  const label = addr.includes(",") ? addr.split(",")[0].trim().slice(0, 64) : addr.slice(0, 64);

  await getOrCreateUser(env.DB, uid);
  await upsertLocation(env.DB, uid, lat, lon, label);

  const forecast = await getRainForecast(lat, lon, weatherKeys(env));
  const recommend = buildAlertRecommendation(forecast);

  if (event.replyToken) {
    await reply(
      event.replyToken,
      [flexMessage(`${forecast.emoji} ${forecast.intensityTh}`, buildRainFlex(forecast, recommend, label))], env);
  }
}

/** แยกเวลาจากข้อความ '7.00' / '08:30' / '19.30' → [hour, minute] หรือ null */
export function parseTime(text: string): [number, number] | null {
  const m = text.match(/\b(\d{1,2})[.:](\d{2})\b/);
  if (!m) return null;
  const h = parseInt(m[1], 10);
  const mn = parseInt(m[2], 10);
  if (h >= 0 && h <= 23 && mn >= 0 && mn <= 59) return [h, mn];
  return null;
}

const RAIN_KEYWORDS = ["ฝน", "ฝนตกไหม", "ฝนไหม", "ฝนตก", "rain", "🌧️", "🌧"];
const ON_KEYWORDS = ["เปิด", "เปิดแจ้งเตือน", "on"];
const OFF_KEYWORDS = ["ปิด", "ปิดแจ้งเตือน", "off"];
const ALERT_TIME_KEYWORDS = ["แจ้งเตือน", "เวลาแจ้ง", "ตั้งเวลา"];
const FEEDBACK_PREFIXES = ["ติชม", "ฟีดแบค", "feedback", "แนะนำ"];
const TIME_CHECK_KEYWORDS = ["ออก", "ไป", "กลับ", "เดินทาง", "เช้า", "เย็น", "ถึง"];

async function handleTextMessage(event: LineEvent, uid: string, env: Env): Promise<void> {
  const text = (event.message!.text ?? "").trim();
  const tl = text.toLowerCase();
  const replyToken = event.replyToken;
  if (!replyToken) return;
  

  // ── เช็คฝนตอนนี้ ─────────────────────────────
  if (RAIN_KEYWORDS.includes(tl)) {
    await replyCurrentRain(replyToken, uid, env);
    return;
  }

  // ── เปิด/ปิดแจ้งเตือน (คงไว้จากเวอร์ชันเดิม + ปุ่มปิดใน flex เก่า) ──
  if (ON_KEYWORDS.includes(tl)) {
    await setAlertEnabled(env.DB, uid, true);
    await reply(replyToken, [textMessage("✅ เปิดการแจ้งเตือนฝนแล้วครับ")], env);
    return;
  }
  if (OFF_KEYWORDS.includes(tl)) {
    await setAlertEnabled(env.DB, uid, false);
    await reply(replyToken, [textMessage("🔕 ปิดการแจ้งเตือนแล้วครับ\nพิมพ์ 'เปิด' เมื่อต้องการเปิดอีกครั้ง")], env);
    return;
  }

  // ── ติชม / feedback ──────────────────────────
  // (เช็คก่อนคำสั่งตั้งเวลา — 'ติชม แจ้งเตือนช้าไป' ต้องเข้าที่ feedback
  //  ไม่ใช่เมนูตั้งเวลา; เวอร์ชัน Python เดิม route ผิดตรงนี้)
  const fbPrefix = FEEDBACK_PREFIXES.find((p) => tl.startsWith(p));
  if (fbPrefix) {
    await handleFeedbackText(text, fbPrefix, replyToken, uid, env);
    return;
  }

  // ── ตั้งค่าเวลาแจ้งเตือน เช่น "แจ้งเตือน 6:00-22:00" ────────
  if (ALERT_TIME_KEYWORDS.some((kw) => tl.includes(kw))) {
    const m = text.match(/(\d{1,2})[.:](\d{2})\s*-\s*(\d{1,2})[.:](\d{2})/);
    if (m) {
      const startH = parseInt(m[1], 10);
      const endH = parseInt(m[3], 10);
      if (startH >= endH) {
        await reply(replyToken, [textMessage("เวลาเริ่มต้นต้องเร็วกว่าเวลาสิ้นสุดนะครับ 😅")], env);
        return;
      }
      await getOrCreateUser(env.DB, uid);
      await setAlertHours(env.DB, uid, startH, endH);
      await reply(
        replyToken,
        [textMessage(`✅ ตั้งค่าแจ้งเตือนแล้ว\n⏰ ${String(startH).padStart(2, "0")}:00 - ${String(endH).padStart(2, "0")}:00 น.`)],
        env
      );
    } else {
      const user = await getUser(env.DB, uid);
      await reply(
        replyToken,
        [flexMessage("ตั้งค่าเวลาแจ้งเตือน", buildAlertTimeFlex(user?.alertStartHour ?? 6, user?.alertEndHour ?? 22))],
        env
      );
    }
    return;
  }

  // ── เช็คฝนตามเวลา เช่น "ออกบ้าน 8.00" ────────
  const parsedTime = parseTime(tl);
  if (parsedTime !== null && TIME_CHECK_KEYWORDS.some((kw) => tl.includes(kw))) {
    await replyRainAtTime(replyToken, uid, parsedTime[0], parsedTime[1], env);
    return;
  }

  // ── ทุกอย่างอื่น → help ───────────────────────
  await reply(replyToken, [flexMessage("วิธีใช้ Rain Alert 🌧️", buildHelpFlex(), true)], env);
}

async function handleFeedbackText(
  text: string,
  fbPrefix: string,
  replyToken: string,
  uid: string,
  env: Env
): Promise<void> {
  
  const content = text.slice(fbPrefix.length).replace(/^[\s:：,]+/, "").trim();
  if (!content) {
    await reply(
      replyToken,
      [textMessage("💬 อยากบอกอะไรพิมพ์ต่อท้ายได้เลย เช่น\n\nติชม แจ้งเตือนช้าไปนิดนึง\nติชม อยากได้เมนูภาษาอังกฤษ\n\nอ่านทุกข้อความแน่นอน ขอบคุณนะ 🙏")],
      env
    );
    return;
  }
  await insertFeedback(env.DB, uid, content);
  await reply(replyToken, [textMessage("🙏 ได้รับข้อความแล้ว ขอบคุณมากนะ\nทุกความเห็นช่วยให้บอทดีขึ้นจริงๆ 🌧️")], env);
  if (env.ADMIN_LINE_USER_ID) {
    await push(
      env.ADMIN_LINE_USER_ID,
      [textMessage(`💬 Feedback ใหม่\nจาก: ${uid.slice(0, 12)}...\n\n${content}`)], env).catch((e) => console.error(`forward feedback failed: ${e}`));
  }
}

async function replyCurrentRain(replyToken: string, uid: string, env: Env): Promise<void> {
  const loc = await getPrimaryLocation(env.DB, uid);
  if (!loc) {
    await reply(
      replyToken,
      [flexMessage("ส่ง location ก่อนนะครับ", buildNoLocationFlex(), true)], env);
    return;
  }
  const forecast = await getRainForecast(loc.latitude, loc.longitude, weatherKeys(env));
  const recommend = buildAlertRecommendation(forecast);
  await reply(
    replyToken,
    [flexMessage(`${forecast.emoji} ${forecast.intensityTh}`, buildRainFlex(forecast, recommend, loc.label), true)], env);
}

async function replyRainAtTime(
  replyToken: string,
  uid: string,
  hour: number,
  minute: number,
  env: Env
): Promise<void> {
  
  const loc = await getPrimaryLocation(env.DB, uid);
  const timeStr = `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;

  if (!loc) {
    await reply(replyToken, [flexMessage("ส่ง location ก่อนนะครับ", buildNoLocationFlex(), true)], env);
    return;
  }

  const forecast = await getRainForecastAtTime(loc.latitude, loc.longitude, hour, minute, env.TOMORROW_IO_API_KEY);

  // นาทีจากตอนนี้ถึงเวลาเป้าหมาย (เวลาไทย)
  const nowMs = Date.now();
  const bp = bangkokParts(new Date());
  const nowMins = parseInt(bp.hour, 10) * 60 + parseInt(bp.minute, 10);
  let deltaMins = hour * 60 + minute - nowMins;
  if (deltaMins <= 0) deltaMins += 24 * 60;

  if (forecast.intensity === "out_of_range") {
    await reply(
      replyToken,
      [textMessage(`⏰ ${timeStr} น. ยังอีก ${Math.floor(deltaMins / 60)} ชม. ${deltaMins % 60} นาทีนะ\n\nดูล่วงหน้าได้แค่ 6 ชม. ค่อยเช็คใหม่ทีหลังได้เลย 😅`)],
      env
    );
    return;
  }

  const timeLabel = deltaMins >= 60 ? `อีก ${Math.floor(deltaMins / 60)} ชม. ${deltaMins % 60} นาที` : `อีก ${deltaMins} นาที`;

  await reply(
    replyToken,
    [flexMessage(`${forecast.emoji} เวลา ${timeStr} น. — ${forecast.intensityTh}`, buildTimeRainFlex(forecast, timeStr, timeLabel), true)],
    env
  );
}

async function handlePostback(event: LineEvent, uid: string, env: Env): Promise<void> {
  const data = event.postback?.data ?? "";
  const params = new URLSearchParams(data);

  if (params.get("action") === "feedback") {
    const logId = parseInt(params.get("log_id") ?? "0", 10);
    const result = params.get("result");
    if (result !== "rain" && result !== "no_rain") return;

    const status = await recordAlertFeedback(env.DB, logId, result);
    const msg =
      status === "saved"
        ? result === "rain"
          ? "✅ ขอบคุณนะ! จะได้ปรับให้แม่นยิ่งขึ้น 🌧️"
          : "👍 โอเค บันทึกแล้ว จะได้รู้ว่าพยากรณ์พลาด"
        : status === "already"
          ? "บันทึกไว้แล้วนะ ขอบคุณ! 😊"
          : "หาข้อมูลไม่เจอแล้ว แต่ขอบคุณนะ!";

    if (event.replyToken) {
      await reply(event.replyToken, [textMessage(msg)], env);
    }
  }
}

// ─────────────────────────────────────────────
//  Push (เรียกจาก scheduled handler)
// ─────────────────────────────────────────────

export async function pushRainAlertMessage(
  env: Env,
  uid: string,
  forecast: RainForecast,
  locLabel: string,
  alertLogId: number
): Promise<void> {
  const recommend = buildAlertRecommendation(forecast);
  const [minsText, durText] = rainTimingTexts(forecast);
  const alt = `⚠️ ${forecast.emoji} ฝนจะตก${minsText} — ${locLabel}`;
  await push(
    uid,
    [flexMessage(alt, buildPushAlertFlex(forecast, recommend, locLabel, minsText, alertLogId, durText), true)], env);
}

export async function pushDailyWeather(env: Env, uid: string, forecast: RainForecast, locLabel: string): Promise<void> {
  const recommend = buildAlertRecommendation(forecast);
  const alt = `🌅 สรุปอากาศเช้านี้ ${forecast.emoji} ${forecast.intensityTh} — ${locLabel}`;
  await push(
    uid,
    [flexMessage(alt, buildDailyWeatherFlex(forecast, recommend, locLabel), true)], env);
}
