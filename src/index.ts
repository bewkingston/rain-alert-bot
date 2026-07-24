/**
 * index.ts — Rain Alert Worker
 * fetch() router: / , /health, /webhook
 * scheduled() cron handlers:
 *   - "*\/5 * * * *"  ตรวจฝนทุก 5 นาที (แทน APScheduler auto_rain_alert)
 *   - "0 0 * * *"     สรุปอากาศเช้า 07:00 น. ไทย = 00:00 UTC (แทน daily_weather_summary)
 * Ported from github.com/bewkingston/rain-alert-bot @ c7e89ae (main.py + scheduler.py).
 */

import type { Env } from "./types";
import {
  verifyLineSignature,
  handleWebhookBody,
  pushRainAlertMessage,
  pushDailyWeather,
  bangkokHour,
} from "./line";
import { getActiveUsersWithPrimaryLocation, getLastAlert, insertAlertLog, type ActiveUserLocation } from "./db";
import { getRainForecast, buildAlertRecommendation, SEVERITY } from "./weather";
import { getConditions } from "./conditions";
import { renderStatusPage } from "./web";

const DAILY_SUMMARY_CRON = "0 0 * * *";

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      return Response.json({ status: "ok", service: "Rain Alert Bot 🌧️" });
    }

    if (request.method === "GET" && url.pathname === "/health") {
      return Response.json({ status: "healthy" });
    }

    if (request.method === "GET" && url.pathname === "/app") {
      return new Response(renderStatusPage(), { headers: { "content-type": "text/html; charset=utf-8" } });
    }

    if (request.method === "GET" && url.pathname === "/api/status") {
      return handleStatusRequest(request, env);
    }

    if (request.method === "POST" && url.pathname === "/webhook") {
      return handleWebhookRequest(request, env, ctx);
    }

    return new Response("Not found", { status: 404 });
  },

  async scheduled(controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    if (controller.cron === DAILY_SUMMARY_CRON) {
      ctx.waitUntil(dailyWeatherSummary(env));
    } else {
      ctx.waitUntil(autoRainAlert(env));
    }
  },
} satisfies ExportedHandler<Env>;

async function handleWebhookRequest(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const signature = request.headers.get("x-line-signature") ?? "";
  const rawBody = await request.arrayBuffer();

  const valid = await verifyLineSignature(rawBody, signature, env.LINE_CHANNEL_SECRET);
  if (!valid) {
    return Response.json({ status: "error", detail: "Invalid signature" }, { status: 400 });
  }

  const bodyText = new TextDecoder().decode(rawBody);

  // ตอบ 200 ให้ LINE ทันที แล้วประมวลผล event เบื้องหลัง —
  // LINE ตัดการเชื่อมต่อเร็วมาก ถ้ารอประมวลผลเสร็จก่อนตอบ request จะโดน
  // cancel กลางทางและคำตอบไม่ถูกส่ง (waitUntil ทำงานต่อได้แม้ client ตัดแล้ว)
  ctx.waitUntil(
    handleWebhookBody(bodyText, env).catch((e) => console.error(`Webhook handler error: ${e}`))
  );

  return Response.json({ status: "ok" });
}

const weatherKeys = (env: Env) => ({ tomorrowApiKey: env.TOMORROW_IO_API_KEY, tmdApiKey: env.TMD_API_KEY });

/**
 * GET /api/status?lat=&lon=&label= — สถานะฝนปัจจุบันสำหรับ web app (กุชชี่ dashboard)
 * ไม่ระบุ lat/lon → ใช้ primary location ของ user คนแรกที่ active ใน DB
 */
async function handleStatusRequest(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const latParam = url.searchParams.get("lat");
  const lonParam = url.searchParams.get("lon");

  let lat: number;
  let lon: number;
  let label: string;

  if (latParam && lonParam) {
    lat = Number(latParam);
    lon = Number(lonParam);
    label = url.searchParams.get("label") ?? `${lat}, ${lon}`;
  } else {
    const locations = await getActiveUsersWithPrimaryLocation(env.DB);
    if (!locations.length) {
      return Response.json({ status: "error", detail: "ยังไม่มีโลเคชันตั้งไว้" }, { status: 404 });
    }
    ({ latitude: lat, longitude: lon, label } = locations[0]);
  }

  const [forecast, conditions] = await Promise.all([
    getRainForecast(lat, lon, weatherKeys(env)),
    getConditions(lat, lon),
  ]);
  const message = buildAlertRecommendation(forecast);

  return Response.json({
    label,
    latitude: lat,
    longitude: lon,
    forecast,
    conditions,
    message,
    updatedAt: new Date().toISOString(),
  });
}

/**
 * ตรวจฝนล่วงหน้า 1 ชม. สำหรับผู้ใช้ทุกคน — ส่งเฉพาะที่จะตกจริง
 * และในช่วงเวลาแจ้งเตือนที่ผู้ใช้ตั้งไว้ พร้อมกันแจ้งซ้ำระหว่างฝนเหตุการณ์เดียวกัน
 */
async function autoRainAlert(env: Env): Promise<void> {
  const locations = await getActiveUsersWithPrimaryLocation(env.DB);
  if (!locations.length) return;

  const currentHour = bangkokHour();
  const eligible = locations.filter((u) => u.alertStartHour <= currentHour && currentHour < u.alertEndHour);

  console.log(`🔍 Checking rain for ${eligible.length}/${locations.length} user(s) (hour=${currentHour})...`);

  const results = await Promise.allSettled(eligible.map((u) => checkAndPushForUser(env, u)));
  const errors = results.filter((r) => r.status === "rejected");
  if (errors.length) {
    console.error(`Scheduler: ${errors.length} error(s) during push`);
  }
}

async function checkAndPushForUser(env: Env, user: ActiveUserLocation): Promise<void> {
  const last = await getLastAlert(env.DB, user.lineUserId);

  // ต้องเช็ค forecast ก่อนเสมอ — ห้าม return จาก cooldown ก่อนดึงข้อมูลใหม่
  // (บั๊กเดิม: cooldown gate ทำงานก่อน fetch ทำให้ escalation logic ด้านล่าง
  //  ไม่มีทางถูกเรียกเลยตอนอยู่ในช่วง cooldown ที่สั้นกว่า 180 นาที — ฝนที่แรงขึ้น
  //  กะทันหันระหว่าง cooldown เลยไม่เคยถูกตรวจพบ)
  const forecast = await getRainForecast(user.latitude, user.longitude, weatherKeys(env));
  if (!forecast.willRain || forecast.intensity === "none") return;

  // alert ล่าสุด <180 นาที = ถือว่าเป็นฝนรอบเดียวกัน → แจ้งซ้ำได้เฉพาะฝนแรงขึ้น
  // กว่าที่แจ้งครั้งก่อน (escalation) โดยไม่ผูกกับ cooldown เลย เพราะฝนแรงขึ้น
  // กะทันหันต้องแจ้งทันที ไม่รอ cooldown หมด
  const sameStorm = last !== null && last.minutesAgo < 180;

  if (sameStorm) {
    const lastSev = SEVERITY[last!.rainIntensity ?? "light"] ?? 1;
    const nowSev = SEVERITY[forecast.intensity] ?? 0;
    if (nowSev <= lastSev) return;
    console.log(`Escalation for ${user.lineUserId}: ${last!.rainIntensity} → ${forecast.intensity}`);
  } else if (last && last.minutesAgo < user.alertCooldown) {
    // ฝนคนละรอบ (ผ่านมา ≥180 นาที) แต่ยังไม่พ้น cooldown ที่ผู้ใช้ตั้งไว้
    return;
  }

  // บันทึก log ก่อนเพื่อให้ได้ id สำหรับปุ่ม feedback แล้วค่อย push
  const logId = await insertAlertLog(env.DB, {
    lineUserId: user.lineUserId,
    rainIntensity: forecast.intensity,
    minutesToRain: forecast.minutesToRain,
    source: forecast.source,
    messageSent: `${forecast.emoji} ${forecast.intensityTh}`,
  });

  await pushRainAlertMessage(env, user.lineUserId, forecast, user.label, logId);
  console.log(`✅ Pushed alert → ${user.lineUserId}: ${forecast.intensityTh}`);
}

/** สรุปสภาพอากาศตอน 07:00 น. — ส่งให้ทุก user ที่ active ทุกวัน ไม่ว่าฝนจะตกหรือไม่ */
async function dailyWeatherSummary(env: Env): Promise<void> {
  const locations = await getActiveUsersWithPrimaryLocation(env.DB);
  if (!locations.length) return;

  console.log(`🌅 Daily weather summary for ${locations.length} user(s)...`);

  const results = await Promise.allSettled(
    locations.map(async (u) => {
      const forecast = await getRainForecast(u.latitude, u.longitude, weatherKeys(env));
      await pushDailyWeather(env, u.lineUserId, forecast, u.label);
    })
  );
  const errors = results.filter((r) => r.status === "rejected");
  if (errors.length) {
    console.error(`Daily summary: ${errors.length} error(s)`);
  }
}
