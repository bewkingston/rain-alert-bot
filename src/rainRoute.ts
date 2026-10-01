/**
 * rainRoute.ts — Rain Route: route-weather analysis + Google Places proxies
 * Ported from github.com/bewkingston/rain-alert-bot @ c7e89ae
 * (route_weather.py + main.py's places-autocomplete/geocode/reverse-geocode).
 *
 * Flow: Google Directions (fallback: Nominatim geocode + OSRM route) →
 * rain forecast at each step's ETA via weather.ts's getRainForecastAtTime →
 * summarize risk + departure-time recommendation.
 */

import { getRainForecastAtTime, classifyIntensity } from "./weather";

const THAI_OFFSET_MS = 7 * 3600_000;
const INTENSITY_ORDER = ["none", "light", "moderate", "heavy", "violent"] as const;

/** วิเคราะห์เส้นทางไม่สำเร็จเพราะ input ไม่ถูกต้อง (422) */
export class RouteValidationError extends Error {}
/** ไม่พบเส้นทาง/สถานที่ (404) */
export class RouteNotFoundError extends Error {}

interface RouteStep {
  name: string;
  endLat: number;
  endLon: number;
  durationSec: number;
}

export interface WaypointWeather {
  name: string;
  lat: number;
  lon: number;
  etaStr: string;
  intensity: string;
  intensityTh: string;
  emoji: string;
  mm: number;
}

export interface RouteWeatherResult {
  origin: string;
  destination: string;
  departureStr: string;
  durationMin: number;
  waypoints: WaypointWeather[];
  rainWaypoints: WaypointWeather[];
  riskPct: number;
  maxIntensity: string;
  maxEmoji: string;
  recommendation: string;
  bestAltDeparture: string | null;
}

function thaiParts(date: Date): { hour: number; minute: number; hhmm: string } {
  const thai = new Date(date.getTime() + THAI_OFFSET_MS);
  const hour = thai.getUTCHours();
  const minute = thai.getUTCMinutes();
  return { hour, minute, hhmm: `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}` };
}

function mmFromIntensity(intensity: string): number {
  return { none: 0, light: 1, moderate: 5, heavy: 20, violent: 60 }[intensity] ?? 0;
}

export async function analyzeRoute(
  gmapsKey: string,
  tomorrowApiKey: string,
  origin: string,
  destination: string,
  departureIso: string,
  originLat?: number | null,
  originLon?: number | null,
  destLat?: number | null,
  destLon?: number | null
): Promise<RouteWeatherResult> {
  if (!gmapsKey) throw new RouteValidationError("GOOGLE_MAPS_API_KEY not set");

  const depUtc = new Date(departureIso);
  if (isNaN(depUtc.getTime())) throw new RouteValidationError("departure_iso format ไม่ถูกต้อง");

  const steps = await getRouteSteps(gmapsKey, origin, destination, depUtc, originLat, originLon, destLat, destLon);
  if (!steps.length) throw new RouteNotFoundError("ไม่พบเส้นทาง กรุณาตรวจสอบชื่อสถานที่");

  let elapsedSec = 0;
  const waypoints: WaypointWeather[] = [];
  for (const step of steps) {
    elapsedSec += step.durationSec;
    const eta = new Date(depUtc.getTime() + elapsedSec * 1000);
    const { hour, minute, hhmm } = thaiParts(eta);
    const forecast = await getRainForecastAtTime(step.endLat, step.endLon, hour, minute, tomorrowApiKey);
    waypoints.push({
      name: step.name,
      lat: step.endLat,
      lon: step.endLon,
      etaStr: hhmm,
      intensity: forecast.intensity,
      intensityTh: forecast.intensityTh,
      emoji: forecast.emoji,
      mm: forecast.precipitationMm,
    });
  }

  const rainWaypoints = waypoints.filter((w) => w.intensity !== "none" && w.intensity !== "out_of_range");
  const riskPct = Math.round((rainWaypoints.length / Math.max(waypoints.length, 1)) * 100);

  const seenIntensities = waypoints.map((w) => w.intensity).filter((i) => (INTENSITY_ORDER as readonly string[]).includes(i));
  const maxIntensity = seenIntensities.reduce<string>(
    (max, cur) => (INTENSITY_ORDER.indexOf(cur as any) > INTENSITY_ORDER.indexOf(max as any) ? cur : max),
    "none"
  );
  const [, , maxEmoji] = classifyIntensity(mmFromIntensity(maxIntensity));

  const durationMin = Math.floor(steps.reduce((s, st) => s + st.durationSec, 0) / 60);
  const { hhmm: departureStr } = thaiParts(depUtc);

  const { recommendation, bestAltDeparture } = buildRecommendation(riskPct, maxIntensity, rainWaypoints, depUtc);

  return {
    origin,
    destination,
    departureStr,
    durationMin,
    waypoints,
    rainWaypoints,
    riskPct,
    maxIntensity,
    maxEmoji,
    recommendation,
    bestAltDeparture,
  };
}

function buildRecommendation(
  riskPct: number,
  maxIntensity: string,
  rainWaypoints: WaypointWeather[],
  depUtc: Date
): { recommendation: string; bestAltDeparture: string | null } {
  if (riskPct === 0) return { recommendation: "เส้นทางแจ่มใส ออกเดินทางได้เลย ☀️", bestAltDeparture: null };

  let recommendation: string;
  if (maxIntensity === "light") recommendation = "มีฝนเล็กน้อยบางช่วง พกร่มไว้ด้วย 🌂";
  else if (maxIntensity === "moderate") recommendation = "ฝนปานกลาง แนะนำพกเสื้อกันฝน หรือรอฝนซาก่อน 🧥";
  else recommendation = "ฝนหนัก ควรเลื่อนเวลาออกเดินทาง ⛈️";

  let bestAltDeparture: string | null = null;
  if (riskPct >= 50) {
    const alt30 = thaiParts(new Date(depUtc.getTime() + 30 * 60_000)).hhmm;
    const alt60 = thaiParts(new Date(depUtc.getTime() + 60 * 60_000)).hhmm;
    bestAltDeparture = `${alt30} หรือ ${alt60}`;
  }

  return { recommendation, bestAltDeparture };
}

// ─────────────────────────────────────────────
//  Routing: Google Directions → Nominatim + OSRM fallback
// ─────────────────────────────────────────────

async function getRouteSteps(
  gmapsKey: string,
  origin: string,
  destination: string,
  depUtc: Date,
  originLat?: number | null,
  originLon?: number | null,
  destLat?: number | null,
  destLon?: number | null
): Promise<RouteStep[]> {
  const steps = await googleDirections(gmapsKey, origin, destination, depUtc);
  if (steps.length) return steps;
  return osrmRoute(origin, destination, originLat, originLon, destLat, destLon);
}

async function googleDirections(gmapsKey: string, origin: string, destination: string, depUtc: Date): Promise<RouteStep[]> {
  const params = new URLSearchParams({
    origin,
    destination,
    departure_time: String(Math.floor(depUtc.getTime() / 1000)),
    mode: "driving",
    language: "th",
    key: gmapsKey,
  });
  try {
    const resp = await fetch(`https://maps.googleapis.com/maps/api/directions/json?${params.toString()}`);
    const data: any = await resp.json();
    if (data.status !== "OK") {
      console.warn(`Directions API: ${data.status} — ${data.error_message ?? ""}`);
      return [];
    }
    const steps: RouteStep[] = [];
    for (const leg of data.routes[0].legs) {
      for (const step of leg.steps) {
        const name = String(step.html_instructions ?? "")
          .replace(/<[^>]+>/g, "")
          .trim()
          .slice(0, 30);
        steps.push({
          name: name || "จุดระหว่างทาง",
          endLat: step.end_location.lat,
          endLon: step.end_location.lng,
          durationSec: step.duration.value,
        });
      }
    }
    return steps;
  } catch (e) {
    console.warn(`Google Directions error: ${e}`);
    return [];
  }
}

async function nominatimGeocode(query: string): Promise<[number, number]> {
  const params = new URLSearchParams({
    q: query,
    format: "json",
    limit: "1",
    countrycodes: "th",
    "accept-language": "th",
  });
  const res = await fetch(`https://nominatim.openstreetmap.org/search?${params.toString()}`, {
    headers: { "User-Agent": "RainAlertWorker/1.0" },
  });
  const data: any = await res.json();
  if (!data.length) throw new RouteNotFoundError(`ไม่พบสถานที่: ${query}`);
  return [parseFloat(data[0].lat), parseFloat(data[0].lon)];
}

async function osrmRoute(
  origin: string,
  destination: string,
  origLat?: number | null,
  origLon?: number | null,
  destLat?: number | null,
  destLon?: number | null
): Promise<RouteStep[]> {
  try {
    let oLat = origLat, oLon = origLon, dLat = destLat, dLon = destLon;
    if (oLat == null || oLon == null) [oLat, oLon] = await nominatimGeocode(origin);
    if (dLat == null || dLon == null) [dLat, dLon] = await nominatimGeocode(destination);

    if (Math.abs(oLat! - dLat!) < 0.001 && Math.abs(oLon! - dLon!) < 0.001) {
      throw new RouteNotFoundError("ต้นทางและปลายทางอยู่ใกล้กันมาก กรุณาตรวจสอบชื่อสถานที่");
    }

    const url = `https://router.project-osrm.org/route/v1/driving/${oLon},${oLat};${dLon},${dLat}?overview=false&steps=true`;
    const resp = await fetch(url, { headers: { "User-Agent": "RainAlertWorker/1.0" } });
    if (!resp.ok) {
      console.error(`OSRM HTTP error: ${resp.status}`);
      return [];
    }
    const data: any = await resp.json();
    if (data.code !== "Ok" || !data.routes?.length) {
      console.error(`OSRM error: ${data.code}`);
      return [];
    }

    const steps: RouteStep[] = [];
    for (const leg of data.routes[0].legs) {
      for (const step of leg.steps ?? []) {
        const dur = Math.floor(step.duration ?? 0);
        if (dur < 5) continue;
        const [lon, lat] = step.maneuver.location;
        steps.push({ name: step.name || "จุดระหว่างทาง", endLat: lat, endLon: lon, durationSec: dur });
      }
    }
    return steps;
  } catch (e) {
    if (e instanceof RouteNotFoundError) throw e;
    console.error(`OSRM route error: ${e}`);
    return [];
  }
}

// ─────────────────────────────────────────────
//  Google Places proxies (avoid deprecated client-side JS SDK)
// ─────────────────────────────────────────────

export async function placesAutocomplete(gmapsKey: string, query: string): Promise<{ description: string; place_id: string }[]> {
  if (!query || query.length < 2) return [];
  const params = new URLSearchParams({ input: query, key: gmapsKey, language: "th", components: "country:th" });
  try {
    const res = await fetch(`https://maps.googleapis.com/maps/api/place/autocomplete/json?${params.toString()}`);
    const data: any = await res.json();
    if (data.status !== "OK" && data.status !== "ZERO_RESULTS") return [];
    return (data.predictions ?? []).slice(0, 5).map((p: any) => ({ description: p.description, place_id: p.place_id }));
  } catch (e) {
    console.warn(`Places autocomplete error: ${e}`);
    return [];
  }
}

export async function geocodePlaceId(gmapsKey: string, placeId: string): Promise<{ lat: number; lon: number } | null> {
  const params = new URLSearchParams({ place_id: placeId, key: gmapsKey, fields: "geometry", language: "th" });
  try {
    const res = await fetch(`https://maps.googleapis.com/maps/api/place/details/json?${params.toString()}`);
    const data: any = await res.json();
    if (data.status !== "OK") return null;
    const loc = data.result.geometry.location;
    return { lat: loc.lat, lon: loc.lng };
  } catch (e) {
    console.warn(`Geocode error: ${e}`);
    return null;
  }
}

export async function reverseGeocode(gmapsKey: string, lat: number, lon: number): Promise<string | null> {
  const params = new URLSearchParams({ latlng: `${lat},${lon}`, key: gmapsKey, language: "th" });
  try {
    const res = await fetch(`https://maps.googleapis.com/maps/api/geocode/json?${params.toString()}`);
    const data: any = await res.json();
    if (data.status !== "OK" || !data.results?.length) return null;
    return data.results[0].formatted_address;
  } catch (e) {
    console.warn(`Reverse geocode error: ${e}`);
    return null;
  }
}
