/**
 * web.ts — กุชชี่ dashboard (Phase 1: ฝนอย่างเดียว, ไม่ผูก LINE)
 * หน้าเว็บ static ที่ fetch ข้อมูลจาก /api/status ฝั่ง client
 */
export function renderStatusPage(): string {
  return /* html */ `<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>กุชชี่ว่า...</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f4f6f8; color: #1a1a1a;
  }
  @media (prefers-color-scheme: dark) { body { background: #14171a; color: #f0f0f0; } }
  .card {
    width: min(420px, 92vw); padding: 32px 28px; border-radius: 20px;
    background: #ffffff; box-shadow: 0 8px 30px rgba(0,0,0,0.08); text-align: center;
  }
  @media (prefers-color-scheme: dark) { .card { background: #1f2429; box-shadow: 0 8px 30px rgba(0,0,0,0.35); } }
  .dog { font-size: 48px; }
  h1 { font-size: 18px; font-weight: 600; margin: 8px 0 20px; opacity: 0.85; }
  .emoji { font-size: 56px; margin: 8px 0; }
  .message { font-size: 20px; font-weight: 600; line-height: 1.5; margin: 12px 0 20px; }
  .conditions {
    display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px;
    margin: 4px 0 20px; text-align: left;
  }
  .cond {
    display: flex; align-items: center; gap: 10px; padding: 10px 12px;
    border-radius: 12px; background: #f4f6f8; font-size: 13px;
  }
  @media (prefers-color-scheme: dark) { .cond { background: #262b31; } }
  .cond .cond-emoji { font-size: 20px; }
  .cond .cond-label { opacity: 0.6; font-size: 11px; }
  .cond .cond-value { font-weight: 600; }
  .meta { font-size: 13px; opacity: 0.6; line-height: 1.6; }
  .refresh { margin-top: 20px; font-size: 13px; opacity: 0.5; }
  .error { color: #d33; }
</style>
</head>
<body>
  <div class="card" id="card">
    <div class="dog">🐶</div>
    <h1>กุชชี่ว่า...</h1>
    <div class="emoji" id="emoji">⏳</div>
    <div class="message" id="message">กำลังเช็คให้อยู่นะ...</div>
    <div class="conditions" id="conditions"></div>
    <div class="meta" id="meta"></div>
    <div class="refresh" id="refresh"></div>
  </div>

<script>
async function loadStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'โหลดไม่สำเร็จ');

    document.getElementById('emoji').textContent = data.forecast.emoji;
    document.getElementById('message').textContent = data.message;
    document.getElementById('meta').textContent =
      \`📍 \${data.label} · อัปเดตล่าสุด \${new Date(data.updatedAt).toLocaleTimeString('th-TH')}\`;

    const c = data.conditions;
    const cards = [
      ['PM2.5', c.pm25 !== null ? \`\${c.pm25} · \${c.pm25LevelTh}\` : 'ไม่มีข้อมูล', c.pm25Emoji],
      ['UV', c.uvIndex !== null ? \`\${c.uvIndex} · \${c.uvLevelTh}\` : 'ไม่มีข้อมูล', c.uvEmoji],
      ['รู้สึกเหมือน', c.heatIndexC !== null ? \`\${c.heatIndexC}°C · \${c.heatIndexLevelTh}\` : 'ไม่มีข้อมูล', c.heatIndexEmoji],
      ['ลม', c.windSpeedKmh !== null ? \`\${c.windSpeedKmh} km/h · \${c.windLevelTh}\` : 'ไม่มีข้อมูล', c.windEmoji],
    ];
    document.getElementById('conditions').innerHTML = cards.map(([label, value, emoji]) => \`
      <div class="cond">
        <span class="cond-emoji">\${emoji}</span>
        <span>
          <div class="cond-label">\${label}</div>
          <div class="cond-value">\${value}</div>
        </span>
      </div>
    \`).join('');
  } catch (e) {
    document.getElementById('emoji').textContent = '⚠️';
    document.getElementById('message').textContent = 'เช็คไม่ได้ตอนนี้ ลองใหม่อีกครั้งนะ';
    document.getElementById('message').classList.add('error');
  }
}
loadStatus();
setInterval(loadStatus, 5 * 60 * 1000);
</script>
</body>
</html>`;
}
