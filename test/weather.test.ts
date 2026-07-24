import { describe, expect, it } from "vitest";
import {
  detectRainWindow,
  classifyIntensity,
  buildAlertRecommendation,
  minutely15ToPerMinute,
} from "../src/weather";

describe("detectRainWindow — regression tests for the jitter bug", () => {
  // These mirror the synthetic fixtures used to diagnose the original Python bug:
  // real minutely precipitation data jitters around the threshold rather than
  // holding a clean plateau, and the old "2 strictly consecutive minutes" rule
  // missed it entirely.

  it("steady clear sky — no rain", () => {
    const vals = new Array(60).fill(0.0);
    const r = detectRainWindow(vals);
    expect(r.willRain).toBe(false);
    expect(r.startMinute).toBeNull();
    expect(r.maxIntensity).toBe(0);
  });

  it("solid sustained moderate rain shortly after minute 10 (happy path sanity check)", () => {
    const vals = [...Array(10).fill(0.0), ...Array(20).fill(3.0), ...Array(30).fill(0.0)];
    const r = detectRainWindow(vals);
    expect(r.willRain).toBe(true);
    // The 3-minute look-ahead window can flag a sharp dry→wet transition one
    // minute early (2-of-3 in the window is enough even if vals[i] itself is
    // still dry) — always early or on-time, never late, which is the safe
    // direction for a "rain is coming" alert.
    expect(r.startMinute).toBe(9);
    expect(r.maxIntensity).toBe(3.0);
  });

  it("jittery light rain around the 0.1mm/hr threshold — must detect (was the bug)", () => {
    const jitterBlock = [0.6, 0.3, 0.6, 0.4, 0.6, 0.3, 0.7, 0.4, 0.6, 0.3];
    const vals = [...Array(10).fill(0.0), ...jitterBlock, ...jitterBlock, ...jitterBlock, ...Array(10).fill(0.0)];
    const r = detectRainWindow(vals);
    expect(r.willRain).toBe(true);
    // Look-ahead window flags the dry→wet transition 1 minute early (see the
    // "solid sustained moderate rain" test above) — every value in jitterBlock
    // clears the 0.1mm/hr threshold, so it's caught at minute 9, not 10.
    expect(r.startMinute).toBe(9);
  });

  it("alternating rain, never 2-in-a-row — must still detect (was the bug)", () => {
    const altBlock = [0.6, 0.2, 0.6, 0.2, 0.6, 0.2, 0.6, 0.2, 0.6, 0.2];
    const vals = [...Array(10).fill(0.0), ...altBlock, ...altBlock, ...altBlock, ...Array(10).fill(0.0)];
    const r = detectRainWindow(vals);
    expect(r.willRain).toBe(true);
    // Same 1-minute-early look-ahead as above.
    expect(r.startMinute).toBe(9);
  });

  it("single-minute noise blip is still correctly ignored", () => {
    const vals = [...Array(10).fill(0.0), 0.6, ...Array(49).fill(0.0)];
    const r = detectRainWindow(vals);
    expect(r.willRain).toBe(false);
  });

  it("rain starting at minute 0", () => {
    const vals = [0.6, 0.6, 0.4, ...Array(57).fill(0.0)];
    const r = detectRainWindow(vals);
    expect(r.willRain).toBe(true);
    expect(r.startMinute).toBe(0);
  });

  it("rain constant for the entire window — duration is null (continues past window)", () => {
    const vals = new Array(60).fill(3.0);
    const r = detectRainWindow(vals);
    expect(r.willRain).toBe(true);
    expect(r.startMinute).toBe(0);
    expect(r.durationMinutes).toBeNull();
  });

  it("rain starting within the last 2 minutes of the window is still detected", () => {
    const vals = [...Array(58).fill(0.0), 0.6, 0.6];
    const r = detectRainWindow(vals);
    expect(r.willRain).toBe(true);
    expect(r.startMinute).toBe(57); // look-ahead window catches it 1 minute early — see note above
  });

  it("rain that stops mid-window reports a finite duration", () => {
    const vals = [...Array(10).fill(0.0), ...Array(15).fill(3.0), ...Array(35).fill(0.0)];
    const r = detectRainWindow(vals);
    expect(r.willRain).toBe(true);
    expect(r.startMinute).toBe(9);
    expect(r.durationMinutes).toBe(15);
  });
});

describe("minutely15ToPerMinute (Open-Meteo)", () => {
  const T0 = 1_700_000_000_000; // arbitrary epoch anchor
  const step = (i: number, mm: number) => ({ startMs: T0 + i * 15 * 60_000, mmPer15: mm });

  it("converts mm-per-15min to mm/hr and expands each step to 15 minutes", () => {
    const vals = minutely15ToPerMinute([step(0, 0), step(1, 1.5), step(2, 0), step(3, 0), step(4, 0)], T0);
    expect(vals.length).toBe(60);
    expect(vals.slice(0, 15).every((v) => v === 0)).toBe(true);
    expect(vals.slice(15, 30).every((v) => v === 6.0)).toBe(true); // 1.5mm/15min = 6 mm/hr
    expect(vals.slice(30, 45).every((v) => v === 0)).toBe(true);
  });

  it("trims the elapsed part of the in-progress step so index 0 = now", () => {
    const now = T0 + 10 * 60_000; // 10 นาทีเข้าไปใน step แรก
    const vals = minutely15ToPerMinute([step(0, 3.0), step(1, 0), step(2, 0), step(3, 0), step(4, 0)], now);
    // เหลือ 5 นาทีจาก step แรก (12 mm/hr) แล้วต่อด้วย 0
    expect(vals.slice(0, 5).every((v) => v === 12.0)).toBe(true);
    expect(vals[5]).toBe(0);
    expect(vals.length).toBe(60);
  });

  it("skips steps entirely in the past", () => {
    const now = T0 + 20 * 60_000; // step 0 ผ่านไปแล้วทั้ง step
    const vals = minutely15ToPerMinute([step(0, 9.9), step(1, 0.6), step(2, 0), step(3, 0), step(4, 0), step(5, 0)], now);
    expect(vals[0]).toBe(2.4); // มาจาก step 1 (0.6*4) ไม่ใช่ step 0
  });

  it("feeds detectRainWindow correctly: threshold matches classifyIntensity's none/light boundary (0.1mm/hr)", () => {
    // 0.025mm/15min = 0.1mm/hr — ตรงขอบ "light" ใน classifyIntensity → ควรตรวจเจอ
    const rain = minutely15ToPerMinute([step(0, 0.025), step(1, 0.025), step(2, 0.025), step(3, 0.025), step(4, 0.025)], T0);
    expect(detectRainWindow(rain).willRain).toBe(true);
    // 0.01mm/15min = 0.04mm/hr — ต่ำกว่า 0.1 → ไม่เจอ
    const drizzle = minutely15ToPerMinute([step(0, 0.01), step(1, 0.01), step(2, 0.01), step(3, 0.01), step(4, 0.01)], T0);
    expect(detectRainWindow(drizzle).willRain).toBe(false);
  });
});

describe("classifyIntensity", () => {
  it("classifies bands per WMO/TMD thresholds", () => {
    expect(classifyIntensity(0.05)[0]).toBe("none");
    expect(classifyIntensity(1.0)[0]).toBe("light");
    expect(classifyIntensity(5.0)[0]).toBe("moderate");
    expect(classifyIntensity(20.0)[0]).toBe("heavy");
    expect(classifyIntensity(100.0)[0]).toBe("violent");
  });
});

describe("buildAlertRecommendation", () => {
  it("returns the clear-sky message when no rain", () => {
    const msg = buildAlertRecommendation({
      willRain: false,
      minutesToRain: null,
      intensity: "none",
      intensityTh: "ไม่มีฝน",
      precipitationMm: 0,
      source: "tomorrow_io",
      description: "",
      emoji: "☀️",
      rainDurationMin: null,
    });
    expect(msg).toContain("ไม่ต้องกังวล");
  });

  it("returns an urgent message when rain is imminent", () => {
    const msg = buildAlertRecommendation({
      willRain: true,
      minutesToRain: 5,
      intensity: "moderate",
      intensityTh: "ฝนปานกลาง",
      precipitationMm: 3,
      source: "tomorrow_io",
      description: "",
      emoji: "🌧️",
      rainDurationMin: 10,
    });
    expect(msg).toContain("5 นาที");
  });
});
