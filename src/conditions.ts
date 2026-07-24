/**
 * conditions.ts — PM2.5 / UV / Heat Index / ลมแรง (Phase 2 ของกุชชี่)
 * ทั้งหมดมาจาก Open-Meteo (keyless) — คนละ endpoint จาก weather.ts (ซึ่งโฟกัสแค่ฝน)
 */

const FORECAST_URL = "https://api.open-meteo.com/v1/forecast";
const AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality";

export interface Conditions {
  pm25: number | null;
  pm25LevelTh: string;
  pm25Emoji: string;
  uvIndex: number | null;
  uvLevelTh: string;
  uvEmoji: string;
  heatIndexC: number | null;
  heatIndexLevelTh: string;
  heatIndexEmoji: string;
  windSpeedKmh: number | null;
  windLevelTh: string;
  windEmoji: string;
}

/** Rothfusz regression — ใช้ได้แม่นตั้งแต่ ~27°C ขึ้นไป ต่ำกว่านั้นคืนอุณหภูมิจริง */
function heatIndexCelsius(tempC: number, rh: number): number {
  const tempF = (tempC * 9) / 5 + 32;
  if (tempF < 80) return tempC;
  const hiF =
    -42.379 +
    2.04901523 * tempF +
    10.14333127 * rh -
    0.22475541 * tempF * rh -
    0.00683783 * tempF * tempF -
    0.05481717 * rh * rh +
    0.00122874 * tempF * tempF * rh +
    0.00085282 * tempF * rh * rh -
    0.00000199 * tempF * tempF * rh * rh;
  return ((hiF - 32) * 5) / 9;
}

function classifyPm25(v: number): [string, string] {
  if (v <= 12) return ["ดีมาก", "🟢"];
  if (v <= 35.4) return ["ปานกลาง", "🟡"];
  if (v <= 55.4) return ["เริ่มมีผลต่อสุขภาพ", "🟠"];
  if (v <= 150.4) return ["มีผลต่อสุขภาพ", "🔴"];
  return ["อันตรายมาก", "🟣"];
}

function classifyUv(v: number): [string, string] {
  if (v <= 2) return ["ต่ำ", "🟢"];
  if (v <= 5) return ["ปานกลาง", "🟡"];
  if (v <= 7) return ["สูง", "🟠"];
  if (v <= 10) return ["สูงมาก", "🔴"];
  return ["อันตรายมาก", "🟣"];
}

function classifyHeatIndex(v: number): [string, string] {
  if (v < 27) return ["ปกติ", "🟢"];
  if (v < 32) return ["ระวัง", "🟡"];
  if (v < 41) return ["เพิ่มความระวัง", "🟠"];
  if (v < 54) return ["อันตราย", "🔴"];
  return ["อันตรายมาก", "🟣"];
}

function classifyWind(kmh: number): [string, string] {
  if (kmh < 20) return ["ปกติ", "🟢"];
  if (kmh < 40) return ["ลมแรงพอควร", "🟡"];
  if (kmh < 60) return ["ลมแรง ระวัง", "🟠"];
  return ["ลมแรงมาก อันตราย", "🔴"];
}

function noConditions(): Conditions {
  return {
    pm25: null,
    pm25LevelTh: "ไม่มีข้อมูล",
    pm25Emoji: "❓",
    uvIndex: null,
    uvLevelTh: "ไม่มีข้อมูล",
    uvEmoji: "❓",
    heatIndexC: null,
    heatIndexLevelTh: "ไม่มีข้อมูล",
    heatIndexEmoji: "❓",
    windSpeedKmh: null,
    windLevelTh: "ไม่มีข้อมูล",
    windEmoji: "❓",
  };
}

export async function getConditions(lat: number, lon: number): Promise<Conditions> {
  const weatherParams = new URLSearchParams({
    latitude: String(lat),
    longitude: String(lon),
    current: "temperature_2m,relative_humidity_2m,wind_speed_10m,uv_index",
    timezone: "UTC",
  });
  const airParams = new URLSearchParams({
    latitude: String(lat),
    longitude: String(lon),
    current: "pm2_5",
    timezone: "UTC",
  });

  const [weatherResp, airResp] = await Promise.allSettled([
    fetch(`${FORECAST_URL}?${weatherParams.toString()}`),
    fetch(`${AIR_QUALITY_URL}?${airParams.toString()}`),
  ]);

  const result = noConditions();

  if (weatherResp.status === "fulfilled" && weatherResp.value.ok) {
    const data: any = await weatherResp.value.json();
    const cur = data?.current;
    const tempC = cur?.temperature_2m;
    const rh = cur?.relative_humidity_2m;
    const windKmh = cur?.wind_speed_10m;
    const uv = cur?.uv_index;

    if (typeof tempC === "number" && typeof rh === "number") {
      const hi = Math.round(heatIndexCelsius(tempC, rh) * 10) / 10;
      const [levelTh, emoji] = classifyHeatIndex(hi);
      result.heatIndexC = hi;
      result.heatIndexLevelTh = levelTh;
      result.heatIndexEmoji = emoji;
    }
    if (typeof windKmh === "number") {
      const [levelTh, emoji] = classifyWind(windKmh);
      result.windSpeedKmh = Math.round(windKmh * 10) / 10;
      result.windLevelTh = levelTh;
      result.windEmoji = emoji;
    }
    if (typeof uv === "number") {
      const [levelTh, emoji] = classifyUv(uv);
      result.uvIndex = Math.round(uv * 10) / 10;
      result.uvLevelTh = levelTh;
      result.uvEmoji = emoji;
    }
  } else {
    console.error("Open-Meteo forecast (conditions) error");
  }

  if (airResp.status === "fulfilled" && airResp.value.ok) {
    const data: any = await airResp.value.json();
    const pm25 = data?.current?.pm2_5;
    if (typeof pm25 === "number") {
      const [levelTh, emoji] = classifyPm25(pm25);
      result.pm25 = Math.round(pm25 * 10) / 10;
      result.pm25LevelTh = levelTh;
      result.pm25Emoji = emoji;
    }
  } else {
    console.error("Open-Meteo air quality error");
  }

  return result;
}
