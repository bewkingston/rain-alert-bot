/**
 * weather.ts — Rain Alert Worker
 * Tomorrow.io (hyperlocal minutely) + TMD Open Data API
 * Ported from weather.py, with the rain-detection algorithm fixed
 * (see detectRainWindow — the Python version required 2 *consecutive*
 * minutes above threshold, which misses real jittery rain data).
 */

export interface RainForecast {
  willRain: boolean;
  minutesToRain: number | null;
  intensity: "none" | "light" | "moderate" | "heavy" | "violent" | "out_of_range";
  intensityTh: string;
  precipitationMm: number;
  source: "tomorrow_io" | "open_meteo" | "tmd" | "none";
  description: string;
  emoji: string;
  rainDurationMin: number | null;
}

const TOMORROW_URL = "https://api.tomorrow.io/v4/timelines";
const TMD_FORECAST_URL = "https://data.tmd.go.th/nwpapi/v1/forecast/location/hourly/at";

// เกณฑ์ขั้นต่ำที่ถือว่า "ฝนตกจริง" (mm/hr) — ต้องตรงกับขอบเขต "none" ใน
// classifyIntensity (< 0.1 = ไม่มีฝน) ไม่งั้นฝนเล็กน้อยจริง (0.1–0.49 mm/hr)
// จะถูกจัดเป็น willRain=false อยู่ดี ทำให้พลาดแจ้งเตือน (false negative)
const RAIN_THRESHOLD_MM = 0.1;

// ─────────────────────────────────────────────
//  Rain-window detection (the fix)
// ─────────────────────────────────────────────

/**
 * A minute is part of "real" rain if at least 2 of the 3 minutes starting
 * there are >= threshold. This tolerates the minute-to-minute jitter that's
 * normal in real precipitation-intensity data, while still filtering a lone
 * single-minute noise blip (which only ever satisfies 1-of-3).
 */
function isWindowWet(vals: number[], i: number, threshold: number, size = 3): boolean {
  const w = vals.slice(i, i + size);
  return w.length >= 2 && w.filter((v) => v >= threshold).length >= 2;
}

export function detectRainWindow(
  vals: number[],
  threshold: number = RAIN_THRESHOLD_MM
): { willRain: boolean; startMinute: number | null; durationMinutes: number | null; maxIntensity: number } {
  const n = vals.length;
  const maxIntensity = n ? Math.max(...vals) : 0;
  const startMinute = vals.findIndex((_, i) => isWindowWet(vals, i, threshold));

  if (startMinute === -1) {
    return { willRain: false, startMinute: null, durationMinutes: null, maxIntensity };
  }

  let durationMinutes: number | null = null;
  for (let j = startMinute + 1; j < n; j++) {
    const w = vals.slice(j, j + 3);
    if (w.length < 2) break; // insufficient trailing data — rain "continues past window"
    if (w.filter((v) => v >= threshold).length < 2) {
      durationMinutes = j - startMinute;
      break;
    }
  }

  return { willRain: true, startMinute, durationMinutes, maxIntensity };
}

// ─────────────────────────────────────────────
//  Tomorrow.io
// ─────────────────────────────────────────────

export async function getTomorrowForecast(
  lat: number,
  lon: number,
  apiKey: string
): Promise<RainForecast | null> {
  if (!apiKey) {
    console.warn("TOMORROW_IO_API_KEY ไม่ได้ตั้งค่า");
    return null;
  }

  const params = new URLSearchParams({
    location: `${lat},${lon}`,
    units: "metric",
    startTime: "now",
    endTime: "nowPlus60m",
    apikey: apiKey,
  });
  params.append("fields", "precipitationIntensity");
  params.append("fields", "precipitationType");
  params.append("timesteps", "1m");

  try {
    const resp = await fetch(`${TOMORROW_URL}?${params.toString()}`);
    if (!resp.ok) {
      console.error(`Tomorrow.io HTTP error: ${resp.status} — ${await resp.text()}`);
      return null;
    }
    const data: any = await resp.json();
    const intervals = data?.data?.timelines?.[0]?.intervals ?? [];

    if (!intervals.length) return noRainForecast("tomorrow_io");

    const vals: number[] = intervals.map(
      (iv: any) => iv?.values?.precipitationIntensity ?? 0
    );

    const { willRain, startMinute, durationMinutes, maxIntensity } = detectRainWindow(vals);

    if (!willRain) return noRainForecast("tomorrow_io");

    const [level, levelTh, emoji] = classifyIntensity(maxIntensity);

    return {
      willRain: true,
      minutesToRain: startMinute,
      intensity: level,
      intensityTh: levelTh,
      precipitationMm: Math.round(maxIntensity * 100) / 100,
      source: "tomorrow_io",
      description: `ข้อมูลจาก Tomorrow.io — ความเข้มฝนสูงสุด ${maxIntensity.toFixed(1)} mm/hr`,
      emoji,
      rainDurationMin: durationMinutes,
    };
  } catch (e) {
    console.error(`Tomorrow.io error: ${e}`);
    return null;
  }
}

// ─────────────────────────────────────────────
//  Open-Meteo (ฟรี ไม่ต้องมี API key)
// ─────────────────────────────────────────────

const OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast";

/**
 * แปลงข้อมูลฝนราย 15 นาที (mm ต่อ step) เป็น array ราย 1 นาที (mm/hr)
 * เพื่อใช้ร่วมกับ detectRainWindow ตัวเดียวกับ Tomorrow.io
 * — step ที่กำลังดำเนินอยู่จะถูกตัดนาทีที่ผ่านไปแล้วออก ให้ index 0 = ตอนนี้
 */
export function minutely15ToPerMinute(
  steps: { startMs: number; mmPer15: number }[],
  nowMs: number,
  windowMinutes = 60
): number[] {
  const vals: number[] = [];
  for (const step of steps) {
    const endMs = step.startMs + 15 * 60_000;
    if (endMs <= nowMs) continue;
    const mmPerHr = step.mmPer15 * 4;
    const skip = step.startMs < nowMs ? Math.floor((nowMs - step.startMs) / 60_000) : 0;
    for (let i = skip; i < 15 && vals.length < windowMinutes; i++) {
      vals.push(mmPerHr);
    }
    if (vals.length >= windowMinutes) break;
  }
  return vals;
}

export async function getOpenMeteoForecast(lat: number, lon: number): Promise<RainForecast | null> {
  const params = new URLSearchParams({
    latitude: String(lat),
    longitude: String(lon),
    minutely_15: "precipitation",
    forecast_minutely_15: "8", // 2 ชม. — เผื่อ step แรกผ่านไปแล้วบางส่วน
    timezone: "UTC",
  });

  try {
    const resp = await fetch(`${OPEN_METEO_URL}?${params.toString()}`);
    if (!resp.ok) {
      console.error(`Open-Meteo HTTP error: ${resp.status}`);
      return null;
    }
    const data: any = await resp.json();
    const times: string[] = data?.minutely_15?.time ?? [];
    const precs: number[] = data?.minutely_15?.precipitation ?? [];
    if (!times.length || times.length !== precs.length) return noRainForecast("open_meteo");

    const steps = times.map((t, i) => ({
      startMs: new Date(t + "Z").getTime(), // timezone=UTC → เวลาไม่มี Z ต่อท้าย
      mmPer15: precs[i] ?? 0,
    }));

    const vals = minutely15ToPerMinute(steps, Date.now());
    if (!vals.length) return noRainForecast("open_meteo");

    const { willRain, startMinute, durationMinutes, maxIntensity } = detectRainWindow(vals);
    if (!willRain) return noRainForecast("open_meteo");

    const [level, levelTh, emoji] = classifyIntensity(maxIntensity);

    return {
      willRain: true,
      minutesToRain: startMinute,
      intensity: level,
      intensityTh: levelTh,
      precipitationMm: Math.round(maxIntensity * 100) / 100,
      source: "open_meteo",
      description: `ข้อมูลจาก Open-Meteo — ความเข้มฝนสูงสุด ${maxIntensity.toFixed(1)} mm/hr`,
      emoji,
      rainDurationMin: durationMinutes,
    };
  } catch (e) {
    console.error(`Open-Meteo error: ${e}`);
    return null;
  }
}

/** เช็คฝนที่ชั่วโมงเป้าหมาย (UTC ms) จาก Open-Meteo hourly — ใช้เมื่อไม่มี Tomorrow.io key */
export async function getOpenMeteoForecastAtTime(
  lat: number,
  lon: number,
  targetUtcMs: number
): Promise<RainForecast> {
  const params = new URLSearchParams({
    latitude: String(lat),
    longitude: String(lon),
    hourly: "precipitation",
    forecast_days: "2",
    timezone: "UTC",
  });

  try {
    const resp = await fetch(`${OPEN_METEO_URL}?${params.toString()}`);
    if (!resp.ok) {
      console.error(`Open-Meteo hourly HTTP error: ${resp.status}`);
      return noRainForecast("none");
    }
    const data: any = await resp.json();
    const times: string[] = data?.hourly?.time ?? [];
    const precs: number[] = data?.hourly?.precipitation ?? [];
    if (!times.length) return noRainForecast("open_meteo");

    // เอาชั่วโมงที่คาบเกี่ยวช่วง target ±30 นาที (สูงสุด 2 ชั่วโมง)
    let maxMm = 0;
    for (let i = 0; i < times.length; i++) {
      const hourStart = new Date(times[i] + "Z").getTime();
      const hourEnd = hourStart + 3600_000;
      if (hourEnd > targetUtcMs - 30 * 60_000 && hourStart < targetUtcMs + 30 * 60_000) {
        maxMm = Math.max(maxMm, precs[i] ?? 0);
      }
    }

    if (maxMm < 0.1) return noRainForecast("open_meteo");

    const [level, levelTh, emoji] = classifyIntensity(maxMm);
    return {
      willRain: true,
      minutesToRain: null,
      intensity: level,
      intensityTh: levelTh,
      precipitationMm: Math.round(maxMm * 100) / 100,
      source: "open_meteo",
      description: `Open-Meteo — ${maxMm.toFixed(1)} mm/hr`,
      emoji,
      rainDurationMin: null,
    };
  } catch (e) {
    console.error(`Open-Meteo hourly error: ${e}`);
    return noRainForecast("none");
  }
}

// ─────────────────────────────────────────────
//  TMD (กรมอุตุนิยมวิทยา)
// ─────────────────────────────────────────────

export async function getTmdForecast(
  lat: number,
  lon: number,
  apiKey: string
): Promise<RainForecast | null> {
  if (!apiKey) {
    console.warn("TMD_API_KEY ไม่ได้ตั้งค่า — ข้ามการดึงข้อมูล TMD");
    return null;
  }

  const params = new URLSearchParams({
    lat: String(lat),
    lon: String(lon),
    fields: "rain,rh,tc",
    // เอกสาร TMD ใช้ชื่อพารามิเตอร์เต็ม "duration" ไม่ใช่ "dur" — ของเดิมส่งผิดชื่อ
    // ทำให้ TMD เมินพารามิเตอร์นี้ไปเงียบ ๆ แล้ว fallback เป็น default 1 ชม.
    duration: "3",
  });

  try {
    const resp = await fetch(`${TMD_FORECAST_URL}?${params.toString()}`, {
      headers: {
        accept: "application/json",
        authorization: `Bearer ${apiKey}`,
      },
    });
    if (!resp.ok) {
      console.error(`TMD HTTP error: ${resp.status}`);
      return null;
    }
    const data: any = await resp.json();
    const forecasts: any[] = data?.WeatherForecastLocation?.forecasts ?? [];
    if (!forecasts.length) return noRainForecast("tmd");

    let nextRainMm = 0;
    for (const f of forecasts.slice(0, 2)) {
      const rain = Number(f?.data?.rain?.value ?? 0) || 0;
      nextRainMm = Math.max(nextRainMm, rain);
    }

    if (nextRainMm < RAIN_THRESHOLD_MM) return noRainForecast("tmd");

    const [level, levelTh, emoji] = classifyIntensity(nextRainMm);

    return {
      willRain: true,
      minutesToRain: null, // TMD ให้ข้อมูล hourly ไม่มี minutely
      intensity: level,
      intensityTh: levelTh,
      precipitationMm: Math.round(nextRainMm * 100) / 100,
      source: "tmd",
      description: `ข้อมูลจากกรมอุตุนิยมวิทยา — คาดการณ์ฝน ${nextRainMm.toFixed(1)} mm ใน 1-2 ชั่วโมงข้างหน้า`,
      emoji,
      rainDurationMin: null,
    };
  } catch (e) {
    console.error(`TMD error: ${e}`);
    return null;
  }
}

// ─────────────────────────────────────────────
//  Combined: Tomorrow.io (primary) + TMD (fallback)
// ─────────────────────────────────────────────

// Cache ผลพยากรณ์ต่อพิกัด (ปัดเป็น ~100m) — ลด API calls ให้อยู่ในโควตา
// Tomorrow.io free tier: 25 calls/hour. Per-isolate best-effort cache — not
// global correctness, just quota conservation (same caveat as the Python version).
const CACHE_TTL_MS = 240_000; // 4 นาที (สั้นกว่ารอบ scheduler 5 นาที)
const forecastCache = new Map<string, { at: number; forecast: RainForecast }>();

/** ลำดับความรุนแรง — ใช้เทียบว่าแหล่งไหนประเมินฝนแรงกว่ากัน (export ให้ index.ts ใช้ร่วม) */
export const SEVERITY: Record<string, number> = { none: 0, light: 1, moderate: 2, heavy: 3, violent: 4 };

/** Tomorrow.io (ถ้ามี key — ละเอียด 1 นาที) → Open-Meteo (ไม่ต้องมี key, ละเอียด 15 นาที) */
async function getPrimaryForecast(
  lat: number,
  lon: number,
  tomorrowApiKey: string
): Promise<RainForecast | null> {
  if (tomorrowApiKey) {
    const forecast = await getTomorrowForecast(lat, lon, tomorrowApiKey);
    if (forecast !== null) return forecast;
  }
  return getOpenMeteoForecast(lat, lon);
}

/**
 * ผสานผลจากแหล่ง minutely (primary — ละเอียดเรื่องเวลา) กับ TMD (ทางการไทย —
 * ดักฝนพาความร้อนในพื้นที่ได้ดีกว่าโมเดล global แต่ความละเอียดแค่ราย ชม.)
 * ไม่ใช้ TMD เป็นแค่ fallback ตอน error อีกต่อไป — ดึงพร้อมกันแล้วเทียบกันทุกครั้ง
 */
function mergeForecasts(primary: RainForecast | null, tmd: RainForecast | null): RainForecast | null {
  if (!primary) return tmd;
  if (!tmd) return primary;

  if (!primary.willRain && tmd.willRain) {
    // primary ไม่เห็นฝน แต่ TMD เห็น — เชื่อ TMD (มักเป็นฝนพาความร้อนที่โมเดล global พลาด)
    return tmd;
  }

  if (primary.willRain && tmd.willRain && SEVERITY[tmd.intensity] > SEVERITY[primary.intensity]) {
    // ทั้งคู่เห็นฝน แต่ TMD ประเมินแรงกว่า — ใช้ความรุนแรง/ปริมาณของ TMD
    // แต่คงเวลา/ระยะเวลาจาก primary ไว้ (ละเอียดกว่าในระยะสั้น)
    return {
      ...primary,
      intensity: tmd.intensity,
      intensityTh: tmd.intensityTh,
      precipitationMm: tmd.precipitationMm,
      emoji: tmd.emoji,
      description: `${primary.description} | เทียบ TMD: ${tmd.description}`,
    };
  }

  return primary;
}

export async function getRainForecast(
  lat: number,
  lon: number,
  keys: { tomorrowApiKey: string; tmdApiKey: string }
): Promise<RainForecast> {
  const key = `${lat.toFixed(3)},${lon.toFixed(3)}`;
  const cached = forecastCache.get(key);
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) {
    return cached.forecast;
  }

  const [primary, tmd] = await Promise.all([
    getPrimaryForecast(lat, lon, keys.tomorrowApiKey),
    keys.tmdApiKey ? getTmdForecast(lat, lon, keys.tmdApiKey) : Promise.resolve(null),
  ]);

  const forecast = mergeForecasts(primary, tmd);

  if (forecast === null) {
    // ไม่มีข้อมูลจาก source ใดเลย — ไม่ cache เพื่อให้ลองใหม่รอบหน้า
    return noRainForecast("none");
  }

  forecastCache.set(key, { at: Date.now(), forecast });
  return forecast;
}

// ─────────────────────────────────────────────
//  Forecast at a specific time (เวลาไทย) — ล่วงหน้าสูงสุด 6 ชม.
// ─────────────────────────────────────────────

const THAI_OFFSET_MS = 7 * 3600_000;

/** ชั่วโมง:นาที ปัจจุบันตามเวลาไทย (ไม่มี DST) */
export function thaiNow(): Date {
  return new Date(Date.now() + THAI_OFFSET_MS);
}

export async function getRainForecastAtTime(
  lat: number,
  lon: number,
  targetHour: number,
  targetMinute: number,
  apiKey: string
): Promise<RainForecast> {
  const nowThaiMs = Date.now() + THAI_OFFSET_MS;
  const nowThai = new Date(nowThaiMs);

  let target = new Date(nowThaiMs);
  target.setUTCHours(targetHour, targetMinute, 0, 0);
  if (target.getTime() <= nowThaiMs) {
    target = new Date(target.getTime() + 24 * 3600_000);
  }

  const hoursAhead = (target.getTime() - nowThaiMs) / 3600_000;
  if (hoursAhead > 6) {
    return {
      willRain: false,
      minutesToRain: null,
      intensity: "out_of_range",
      intensityTh: `ไม่สามารถดูล่วงหน้า ${Math.floor(hoursAhead)} ชม.`,
      precipitationMm: 0,
      source: "none",
      description: "Tomorrow.io ฟรีดูได้ถึง 6 ชม. ล่วงหน้าเท่านั้น",
      emoji: "❓",
      rainDurationMin: null,
    };
  }

  const targetUtcMs = target.getTime() - THAI_OFFSET_MS;

  // ไม่มี Tomorrow.io key → ใช้ Open-Meteo hourly แทน
  if (!apiKey) return getOpenMeteoForecastAtTime(lat, lon, targetUtcMs);

  const startIso = new Date(targetUtcMs - 30 * 60_000).toISOString().replace(/\.\d{3}Z$/, "Z");
  const endIso = new Date(targetUtcMs + 30 * 60_000).toISOString().replace(/\.\d{3}Z$/, "Z");

  const params = new URLSearchParams({
    location: `${lat},${lon}`,
    units: "metric",
    startTime: startIso,
    endTime: endIso,
    apikey: apiKey,
  });
  params.append("fields", "precipitationIntensity");
  params.append("timesteps", "1h");

  try {
    const resp = await fetch(`${TOMORROW_URL}?${params.toString()}`);
    if (!resp.ok) {
      console.error(`Tomorrow.io at_time HTTP error: ${resp.status}`);
      return noRainForecast("none");
    }
    const data: any = await resp.json();
    const intervals = data?.data?.timelines?.[0]?.intervals ?? [];
    if (!intervals.length) return noRainForecast("tomorrow_io");

    const maxMm = Math.max(
      ...intervals.map((iv: any) => iv?.values?.precipitationIntensity ?? 0)
    );
    if (maxMm < 0.1) return noRainForecast("tomorrow_io");

    const [level, levelTh, emoji] = classifyIntensity(maxMm);
    return {
      willRain: true,
      minutesToRain: null,
      intensity: level,
      intensityTh: levelTh,
      precipitationMm: Math.round(maxMm * 100) / 100,
      source: "tomorrow_io",
      description: `Tomorrow.io — ${maxMm.toFixed(1)} mm/hr`,
      emoji,
      rainDurationMin: null,
    };
  } catch (e) {
    console.error(`Tomorrow.io at_time error: ${e}`);
    return noRainForecast("none");
  }
}

// ─────────────────────────────────────────────
//  Utilities
// ─────────────────────────────────────────────

function noRainForecast(source: RainForecast["source"]): RainForecast {
  return {
    willRain: false,
    minutesToRain: null,
    intensity: "none",
    intensityTh: "ไม่มีฝน",
    precipitationMm: 0.0,
    source,
    description: "ท้องฟ้าแจ่มใส ไม่มีฝนในอีก 1 ชั่วโมงข้างหน้า",
    emoji: "☀️",
    rainDurationMin: null,
  };
}

/** จำแนกความรุนแรงฝน (ตามมาตรฐาน WMO + กรมอุตุฯ ไทย) — คืน [level_en, level_th, emoji] */
export function classifyIntensity(
  mmPerHr: number
): [RainForecast["intensity"], string, string] {
  if (mmPerHr < 0.1) return ["none", "ไม่มีฝน", "☀️"];
  if (mmPerHr < 2.5) return ["light", "ฝนเล็กน้อย", "🌦️"];
  if (mmPerHr < 10.0) return ["moderate", "ฝนปานกลาง", "🌧️"];
  if (mmPerHr < 50.0) return ["heavy", "ฝนหนัก", "⛈️"];
  return ["violent", "ฝนหนักมาก", "🌩️"];
}

/** สร้างคำแนะนำตามสถานการณ์ */
export function buildAlertRecommendation(forecast: RainForecast): string {
  if (!forecast.willRain) return "ไม่ต้องกังวล ท้องฟ้าแจ่มใส 😊";

  const mins = forecast.minutesToRain;

  if (mins === null || mins === 0) return "ฝนกำลังตกอยู่ — หาที่หลบฝนด่วน! 🏃";
  if (mins <= 10) return `ฝนจะมาใน ${mins} นาที — รีบเดินทางหรือหาที่หลบฝนเลย! ⚡`;
  if (mins <= 20) {
    if (["moderate", "heavy", "violent"].includes(forecast.intensity)) {
      return `ฝนจะมาใน ${mins} นาที — ควรออกก่อนหรือรอจนฝนหยุด 🌂`;
    }
    return `ฝนเล็กน้อยจะมาใน ${mins} นาที — เตรียมร่มได้เลย ☂️`;
  }
  return `ฝนจะมาในอีก ${mins} นาที — ยังมีเวลา แต่เตรียมร่มไว้ด้วยนะ 🌂`;
}
