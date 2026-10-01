/**
 * index.ts — Rain Alert Worker
 * fetch() router: / , /health, /webhook
 * scheduled() cron handler:
 *   - "*\/5 * * * *"  ตรวจฝนทุก 5 นาที (แทน APScheduler auto_rain_alert) — ส่งเฉพาะตอนฝนจะตกจริง
 * (สรุปอากาศเช้า 07:00 ทุกวันถูกถอดออก 2026-07-29 ตามคำขอ user — กิน quota push ฟรี
 * ทุกวันไม่ว่าฝนจะตกหรือไม่ ตอนที่ quota เหลือแค่ 300/เดือนและมี user เพิ่มขึ้นแล้ว)
 * Ported from github.com/bewkingston/rain-alert-bot @ c7e89ae (main.py + scheduler.py).
 */

import type { Env } from "./types";
import {
  verifyLineSignature,
  handleWebhookBody,
  pushRainAlertMessage,
  bangkokHour,
} from "./line";
import {
  getActiveUsersWithPrimaryLocation,
  getLastAlert,
  insertAlertLog,
  getRecentApiErrors,
  getPrimaryLocation,
  getOrCreateUser,
  upsertLocation,
  type ActiveUserLocation,
} from "./db";
import { getRainForecast, buildAlertRecommendation, SEVERITY } from "./weather";
import { getConditions } from "./conditions";
import { renderStatusPage } from "./web";
import { handleChatCommand } from "./chat";
import { renderLiffPage } from "./liffPage";
import {
  analyzeRoute,
  placesAutocomplete,
  geocodePlaceId,
  reverseGeocode,
  type RouteWeatherResult,
  RouteValidationError,
  RouteNotFoundError,
} from "./rainRoute";

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

    if (request.method === "GET" && url.pathname === "/api/errors") {
      return handleErrorsRequest(request, env);
    }

    if (request.method === "POST" && url.pathname === "/api/chat") {
      return handleChatRequest(request, env);
    }

    if (request.method === "POST" && url.pathname === "/webhook") {
      return handleWebhookRequest(request, env, ctx);
    }

    if (request.method === "GET" && url.pathname === "/liff") {
      return new Response(renderLiffPage(env.LIFF_ID ?? ""), {
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    }

    if (request.method === "POST" && url.pathname === "/api/route-weather") {
      return handleRouteWeatherRequest(request, env);
    }

    if (request.method === "GET" && url.pathname === "/api/places-autocomplete") {
      const q = url.searchParams.get("q") ?? "";
      const results = await placesAutocomplete(env.GOOGLE_MAPS_API_KEY ?? "", q);
      return Response.json(results);
    }

    if (request.method === "GET" && url.pathname === "/api/geocode") {
      const placeId = url.searchParams.get("place_id") ?? "";
      if (!placeId) return Response.json({ detail: "place_id required" }, { status: 422 });
      const loc = await geocodePlaceId(env.GOOGLE_MAPS_API_KEY ?? "", placeId);
      if (!loc) return Response.json({ detail: "ไม่พบสถานที่นี้" }, { status: 404 });
      return Response.json(loc);
    }

    if (request.method === "GET" && url.pathname === "/api/reverse-geocode") {
      const lat = Number(url.searchParams.get("lat"));
      const lon = Number(url.searchParams.get("lon"));
      const address = await reverseGeocode(env.GOOGLE_MAPS_API_KEY ?? "", lat, lon);
      if (!address) return Response.json({ detail: "ไม่พบข้อมูลตำแหน่งนี้" }, { status: 404 });
      return Response.json({ address });
    }

    if (request.method === "POST" && url.pathname === "/api/update-location") {
      return handleUpdateLocationRequest(request, env);
    }

    return new Response("Not found", { status: 404 });
  },

  async scheduled(controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(autoRainAlert(env));
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
 * ไม่ระบุ lat/lon → ใช้ primary location ของเจ้าของแอป (ADMIN_LINE_USER_ID) โดยเฉพาะ
 * ไม่ใช่ user คนแรกที่เจอใน DB แบบเดิม (บั๊ก: พอมี user คนอื่นเพิ่มเข้ามา ลำดับที่ query
 * ได้ไม่การันตีว่าเป็นเจ้าของแอปเอง — dashboard ส่วนตัวเคยโชว์ตำแหน่งของ user คนอื่นแทน)
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
    const adminLocation = env.ADMIN_LINE_USER_ID
      ? await getPrimaryLocation(env.DB, env.ADMIN_LINE_USER_ID)
      : null;

    if (adminLocation) {
      ({ latitude: lat, longitude: lon, label } = adminLocation);
    } else {
      const locations = await getActiveUsersWithPrimaryLocation(env.DB);
      if (!locations.length) {
        return Response.json({ status: "error", detail: "ยังไม่มีโลเคชันตั้งไว้" }, { status: 404 });
      }
      ({ latitude: lat, longitude: lon, label } = locations[0]);
    }
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
 * GET /api/errors?limit= — external API errors ที่โดนปฏิเสธ (เช่น LINE push
 * โดน 429 quota exceeded) ซึ่งเดิมหายไปเงียบ ๆ ใน console.error เท่านั้น
 */
async function handleErrorsRequest(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  const limit = Math.min(Number(url.searchParams.get("limit") ?? "50") || 50, 200);
  const errors = await getRecentApiErrors(env.DB, limit);
  return Response.json({ count: errors.length, errors });
}

/**
 * POST /api/chat {text} — คำสั่ง keyword เดียวกับ LINE bot (เช็คฝน/เปิดปิด/ตั้งเวลา/ติชม)
 * ผ่านช่องแชทในเว็บแทน ไม่มี LLM ตีความประโยคอิสระ (นั่นคือ Phase 3 ที่ยังไม่ทำ)
 */
async function handleChatRequest(request: Request, env: Env): Promise<Response> {
  const body = await request.json<{ text?: string }>().catch(() => ({}) as { text?: string });
  const text = body.text ?? "";
  const reply = await handleChatCommand(text, env);
  return Response.json({ reply });
}

interface RouteWeatherBody {
  origin?: string;
  destination?: string;
  departure_iso?: string;
  uid?: string | null;
  origin_lat?: number | null;
  origin_lon?: number | null;
  dest_lat?: number | null;
  dest_lon?: number | null;
}

/** POST /api/route-weather — Rain Route: วิเคราะห์ฝนตลอดเส้นทาง origin → destination */
async function handleRouteWeatherRequest(request: Request, env: Env): Promise<Response> {
  const body = await request.json<RouteWeatherBody>().catch(() => ({}) as RouteWeatherBody);
  const { origin, destination, departure_iso: departureIso } = body;
  if (!origin || !destination || !departureIso) {
    return Response.json({ detail: "origin, destination, departure_iso required" }, { status: 422 });
  }

  try {
    const result = await analyzeRoute(
      env.GOOGLE_MAPS_API_KEY ?? "",
      env.TOMORROW_IO_API_KEY,
      origin,
      destination,
      departureIso,
      body.origin_lat,
      body.origin_lon,
      body.dest_lat,
      body.dest_lon
    );
    return Response.json(serializeRouteWeatherResult(result));
  } catch (e) {
    if (e instanceof RouteValidationError) return Response.json({ detail: e.message }, { status: 422 });
    if (e instanceof RouteNotFoundError) return Response.json({ detail: e.message }, { status: 404 });
    console.error(`route-weather error: ${e}`);
    return Response.json({ detail: "วิเคราะห์เส้นทางไม่สำเร็จ กรุณาลองใหม่" }, { status: 500 });
  }
}

/** แปลง camelCase (ฝั่ง TS) → snake_case ให้ตรงกับที่ liff.html คาดหวัง (ported จาก Python response เดิม) */
function serializeRouteWeatherResult(r: RouteWeatherResult) {
  return {
    origin: r.origin,
    destination: r.destination,
    departure_str: r.departureStr,
    duration_min: r.durationMin,
    risk_pct: r.riskPct,
    max_intensity: r.maxIntensity,
    max_emoji: r.maxEmoji,
    recommendation: r.recommendation,
    best_alt_departure: r.bestAltDeparture,
    waypoints: r.waypoints.map((w) => ({
      name: w.name,
      lat: w.lat,
      lon: w.lon,
      eta_str: w.etaStr,
      intensity: w.intensity,
      intensity_th: w.intensityTh,
      emoji: w.emoji,
      mm: w.mm,
    })),
    rain_waypoints: r.rainWaypoints.map((w) => ({
      name: w.name,
      eta_str: w.etaStr,
      emoji: w.emoji,
      mm: w.mm,
    })),
  };
}

/** POST /api/update-location — LIFF เรียกทุกครั้งที่เปิดแอป เพื่ออัพเดท primary location ของ user */
async function handleUpdateLocationRequest(request: Request, env: Env): Promise<Response> {
  const body = await request
    .json<{ uid?: string; lat?: number; lon?: number; label?: string }>()
    .catch(() => ({}) as { uid?: string; lat?: number; lon?: number; label?: string });
  const { uid, lat, lon } = body;
  if (!uid || lat == null || lon == null) {
    return Response.json({ detail: "uid, lat, lon required" }, { status: 422 });
  }
  try {
    await getOrCreateUser(env.DB, uid);
    await upsertLocation(env.DB, uid, lat, lon, body.label ?? "ตำแหน่งปัจจุบัน");
    return Response.json({ status: "ok" });
  } catch (e) {
    console.error(`update-location error: ${e}`);
    return Response.json({ detail: "บันทึกตำแหน่งไม่สำเร็จ" }, { status: 500 });
  }
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

// ระยะห่างขั้นต่ำระหว่างแจ้งเตือน escalation สองครั้ง แม้ฝนจะแรงขึ้นจริงก็ตาม —
// กันกรณีข้อมูลพยากรณ์กระตุกข้ามเกณฑ์ severity หลายระดับใน cron รอบติดๆ กัน
// (ทุก 5 นาที) จนแจ้งเตือนรัวภายในไม่กี่นาที
const ESCALATION_MIN_GAP_MIN = 30;

// ต้องมีสัญญาณฝนต่อเนื่องอย่างน้อยเท่านี้ (นาที) ถึงจะเชื่อว่าเป็นฝนจริง ไม่ใช่
// noise ชั่วครู่ของโมเดล — ใช้ rainDurationMin ที่ detectRainWindow คำนวณไว้แล้ว
// (null = ฝนตกต่อเนื่องจนสุดหน้าต่างพยากรณ์ ถือว่าผ่านเสมอ)
const MIN_SUSTAINED_DURATION_MIN = 10;

async function checkAndPushForUser(env: Env, user: ActiveUserLocation): Promise<void> {
  const last = await getLastAlert(env.DB, user.lineUserId);

  // ต้องเช็ค forecast ก่อนเสมอ — ห้าม return จาก cooldown ก่อนดึงข้อมูลใหม่
  // (บั๊กเดิม: cooldown gate ทำงานก่อน fetch ทำให้ escalation logic ด้านล่าง
  //  ไม่มีทางถูกเรียกเลยตอนอยู่ในช่วง cooldown ที่สั้นกว่าหน้าต่างฝนรอบเดียวกัน —
  //  ฝนที่แรงขึ้นกะทันหันระหว่าง cooldown เลยไม่เคยถูกตรวจพบ)
  const forecast = await getRainForecast(user.latitude, user.longitude, weatherKeys(env));
  if (!forecast.willRain || forecast.intensity === "none") return;

  // กรอง false alarm: สัญญาณสั้นๆ ที่โมเดลเห็นแล้วหายไปเองภายในไม่กี่นาที ไม่ใช่ฝนจริง
  if (forecast.rainDurationMin !== null && forecast.rainDurationMin < MIN_SUSTAINED_DURATION_MIN) {
    return;
  }

  // alert ล่าสุด < alertCooldown (นาที ต่อ user) = ถือว่าเป็นฝนรอบเดียวกัน →
  // แจ้งซ้ำได้เฉพาะฝนแรงขึ้นกว่าที่แจ้งครั้งก่อน (escalation) โดยไม่รอ cooldown
  // หมดก่อน เพราะฝนแรงขึ้นกะทันหันต้องแจ้งทันที — แต่ยังต้องเว้น
  // ESCALATION_MIN_GAP_MIN ขั้นต่ำ กันแจ้งรัวตอนข้อมูลกระตุกข้ามหลาย severity
  // ติดๆ กัน ฝนคนละรอบ (ผ่านไปแล้ว ≥ alertCooldown) ไม่ต้องเช็คซ้ำ เพราะการที่
  // เข้าเงื่อนไขนี้ไม่ได้ (sameStorm=false) หมายความว่าพ้น cooldown มาแล้วเสมอ
  const sameStorm = last !== null && last.minutesAgo < user.alertCooldown;

  if (sameStorm) {
    const lastSev = SEVERITY[last!.rainIntensity ?? "light"] ?? 1;
    const nowSev = SEVERITY[forecast.intensity] ?? 0;
    if (nowSev <= lastSev) return;
    if (last!.minutesAgo < ESCALATION_MIN_GAP_MIN) return;
    console.log(`Escalation for ${user.lineUserId}: ${last!.rainIntensity} → ${forecast.intensity}`);
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
