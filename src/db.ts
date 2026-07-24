/**
 * db.ts — Rain Alert Worker
 * D1 helpers, ported from database.py. Plain prepared statements, no ORM.
 */

export interface User {
  lineUserId: string;
  displayName: string | null;
  isActive: boolean;
  alertEnabled: boolean;
  alertCooldown: number;
  alertStartHour: number;
  alertEndHour: number;
}

export interface UserLocation {
  id: number;
  lineUserId: string;
  label: string;
  latitude: number;
  longitude: number;
  isPrimary: boolean;
}

export interface ActiveUserLocation {
  lineUserId: string;
  alertCooldown: number;
  alertStartHour: number;
  alertEndHour: number;
  latitude: number;
  longitude: number;
  label: string;
}

export interface LastAlert {
  id: number;
  rainIntensity: string | null;
  sentAt: string;
  minutesAgo: number;
}

function nowIso(): string {
  return new Date().toISOString();
}

function rowToUser(row: any): User {
  return {
    lineUserId: row.line_user_id,
    displayName: row.display_name,
    isActive: row.is_active === 1,
    alertEnabled: row.alert_enabled === 1,
    alertCooldown: row.alert_cooldown,
    alertStartHour: row.alert_start_hour ?? 6,
    alertEndHour: row.alert_end_hour ?? 22,
  };
}

function rowToLocation(row: any): UserLocation {
  return {
    id: row.id,
    lineUserId: row.line_user_id,
    label: row.label,
    latitude: row.latitude,
    longitude: row.longitude,
    isPrimary: row.is_primary === 1,
  };
}

/** ดึง User จาก DB หรือสร้างใหม่ถ้ายังไม่มี */
export async function getOrCreateUser(
  db: D1Database,
  lineUserId: string,
  displayName?: string | null
): Promise<User> {
  const existing = await db
    .prepare("SELECT * FROM users WHERE line_user_id = ?")
    .bind(lineUserId)
    .first();

  if (!existing) {
    const ts = nowIso();
    await db
      .prepare(
        `INSERT INTO users (line_user_id, display_name, is_active, alert_enabled, alert_cooldown, alert_start_hour, alert_end_hour, created_at, updated_at)
         VALUES (?, ?, 1, 1, 5, 6, 22, ?, ?)`
      )
      .bind(lineUserId, displayName ?? null, ts, ts)
      .run();
    return {
      lineUserId,
      displayName: displayName ?? null,
      isActive: true,
      alertEnabled: true,
      alertCooldown: 5,
      alertStartHour: 6,
      alertEndHour: 22,
    };
  }

  if (displayName && existing.display_name !== displayName) {
    await db
      .prepare("UPDATE users SET display_name = ?, updated_at = ? WHERE line_user_id = ?")
      .bind(displayName, nowIso(), lineUserId)
      .run();
    return rowToUser({ ...existing, display_name: displayName });
  }

  return rowToUser(existing);
}

export async function getUser(db: D1Database, lineUserId: string): Promise<User | null> {
  const row = await db.prepare("SELECT * FROM users WHERE line_user_id = ?").bind(lineUserId).first();
  return row ? rowToUser(row) : null;
}

export async function setUserActive(db: D1Database, lineUserId: string, isActive: boolean): Promise<void> {
  await db
    .prepare("UPDATE users SET is_active = ?, updated_at = ? WHERE line_user_id = ?")
    .bind(isActive ? 1 : 0, nowIso(), lineUserId)
    .run();
}

export async function setAlertEnabled(db: D1Database, lineUserId: string, enabled: boolean): Promise<void> {
  await db
    .prepare("UPDATE users SET alert_enabled = ?, updated_at = ? WHERE line_user_id = ?")
    .bind(enabled ? 1 : 0, nowIso(), lineUserId)
    .run();
}

export async function setAlertHours(
  db: D1Database,
  lineUserId: string,
  startHour: number,
  endHour: number
): Promise<void> {
  await db
    .prepare("UPDATE users SET alert_start_hour = ?, alert_end_hour = ?, updated_at = ? WHERE line_user_id = ?")
    .bind(startHour, endHour, nowIso(), lineUserId)
    .run();
}

/** อัปเดตหรือสร้าง primary location ของ user */
export async function upsertLocation(
  db: D1Database,
  lineUserId: string,
  lat: number,
  lon: number,
  label: string = "ตำแหน่งปัจจุบัน"
): Promise<UserLocation> {
  const existing = await db
    .prepare("SELECT * FROM user_locations WHERE line_user_id = ? AND is_primary = 1")
    .bind(lineUserId)
    .first();

  const ts = nowIso();

  if (existing) {
    await db
      .prepare(
        "UPDATE user_locations SET latitude = ?, longitude = ?, label = ?, updated_at = ? WHERE id = ?"
      )
      .bind(lat, lon, label, ts, existing.id)
      .run();
    return rowToLocation({ ...existing, latitude: lat, longitude: lon, label });
  }

  const inserted = await db
    .prepare(
      `INSERT INTO user_locations (line_user_id, label, latitude, longitude, is_primary, updated_at)
       VALUES (?, ?, ?, ?, 1, ?)
       RETURNING *`
    )
    .bind(lineUserId, label, lat, lon, ts)
    .first();
  return rowToLocation(inserted);
}

export async function getPrimaryLocation(db: D1Database, lineUserId: string): Promise<UserLocation | null> {
  const row = await db
    .prepare("SELECT * FROM user_locations WHERE line_user_id = ? AND is_primary = 1")
    .bind(lineUserId)
    .first();
  return row ? rowToLocation(row) : null;
}

/** alert ล่าสุดของ user (null ถ้าไม่เคยส่ง) — ใช้ทั้งเช็ค cooldown และ no-repeat-same-rain-event */
export async function getLastAlert(db: D1Database, lineUserId: string): Promise<LastAlert | null> {
  const row = await db
    .prepare(
      "SELECT id, rain_intensity, sent_at FROM alert_logs WHERE line_user_id = ? ORDER BY sent_at DESC LIMIT 1"
    )
    .bind(lineUserId)
    .first<{ id: number; rain_intensity: string | null; sent_at: string }>();

  if (!row) return null;
  return {
    id: row.id,
    rainIntensity: row.rain_intensity,
    sentAt: row.sent_at,
    minutesAgo: Math.floor((Date.now() - new Date(row.sent_at).getTime()) / 60_000),
  };
}

/** บันทึก alert log แล้วคืน id (ใช้ผูกปุ่ม feedback ใน push) */
export async function insertAlertLog(
  db: D1Database,
  entry: {
    lineUserId: string;
    rainIntensity: string;
    minutesToRain: number | null;
    source: string;
    messageSent: string;
  }
): Promise<number> {
  const row = await db
    .prepare(
      `INSERT INTO alert_logs (line_user_id, rain_intensity, minutes_to_rain, source, message_sent, sent_at)
       VALUES (?, ?, ?, ?, ?, ?)
       RETURNING id`
    )
    .bind(entry.lineUserId, entry.rainIntensity, entry.minutesToRain, entry.source, entry.messageSent, nowIso())
    .first<{ id: number }>();
  return row!.id;
}

/**
 * บันทึก feedback ของ alert (✅ ฝนตกจริง / ❌ ไม่มีฝน)
 * คืนสถานะ: saved | already | not_found
 */
export async function recordAlertFeedback(
  db: D1Database,
  logId: number,
  result: "rain" | "no_rain"
): Promise<"saved" | "already" | "not_found"> {
  const log = await db
    .prepare("SELECT user_feedback FROM alert_logs WHERE id = ?")
    .bind(logId)
    .first<{ user_feedback: string | null }>();

  if (!log) return "not_found";
  if (log.user_feedback !== null) return "already";

  await db
    .prepare("UPDATE alert_logs SET user_feedback = ?, feedback_at = ? WHERE id = ?")
    .bind(result, nowIso(), logId)
    .run();
  return "saved";
}

export async function insertFeedback(db: D1Database, lineUserId: string, message: string): Promise<void> {
  await db
    .prepare("INSERT INTO feedbacks (line_user_id, message, created_at) VALUES (?, ?, ?)")
    .bind(lineUserId, message, nowIso())
    .run();
}

/**
 * ดึง user ที่ active + เปิด alert ไว้ พร้อม primary location ในคำสั่งเดียว
 * (single JOIN — เลี่ยง N+1 query เพื่อประหยัด subrequest budget บน Workers)
 */
export async function getActiveUsersWithPrimaryLocation(db: D1Database): Promise<ActiveUserLocation[]> {
  const { results } = await db
    .prepare(
      `SELECT u.line_user_id AS line_user_id, u.alert_cooldown AS alert_cooldown,
              u.alert_start_hour AS alert_start_hour, u.alert_end_hour AS alert_end_hour,
              l.latitude AS latitude, l.longitude AS longitude, l.label AS label
       FROM users u
       JOIN user_locations l ON l.line_user_id = u.line_user_id AND l.is_primary = 1
       WHERE u.is_active = 1 AND u.alert_enabled = 1`
    )
    .all();

  return results.map((row: any) => ({
    lineUserId: row.line_user_id,
    alertCooldown: row.alert_cooldown,
    alertStartHour: row.alert_start_hour ?? 6,
    alertEndHour: row.alert_end_hour ?? 22,
    latitude: row.latitude,
    longitude: row.longitude,
    label: row.label,
  }));
}
