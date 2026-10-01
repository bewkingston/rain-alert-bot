/**
 * web.ts — กุชชี่ dashboard (Phase 1: ฝนอย่างเดียว, ไม่ผูก LINE)
 * หน้าเว็บ static ที่ fetch ข้อมูลจาก /api/status ฝั่ง client
 * UI แบบ chat bubble เลียนแบบ LINE — กุชชี่คุยกับผู้ใช้แทนที่จะโชว์เป็น dashboard นิ่ง ๆ
 */
export function renderStatusPage(): string {
  return /* html */ `<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>กุชชี่</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin: 0; height: 100%;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  body {
    display: flex; flex-direction: column;
    background: #9ea8b5;
    color: #1a1a1a;
  }
  @media (prefers-color-scheme: dark) { body { background: #0d1418; color: #f0f0f0; } }

  /* ── Header (LINE-style green bar) ── */
  .header {
    display: flex; align-items: center; gap: 10px;
    padding: max(10px, env(safe-area-inset-top)) 14px 10px;
    background: #06C755; color: #fff; flex: 0 0 auto;
  }
  .header .avatar {
    width: 34px; height: 34px; border-radius: 50%; background: #fff;
    display: flex; align-items: center; justify-content: center; font-size: 18px; flex: 0 0 auto;
  }
  .header .title { font-size: 16px; font-weight: 600; line-height: 1.2; }
  .header .subtitle { font-size: 11px; opacity: 0.85; }
  .header .spacer { flex: 1; }
  .header .icon { font-size: 18px; opacity: 0.9; }

  /* ── Chat area ── */
  .chat {
    flex: 1 1 auto; overflow-y: auto; padding: 14px 10px 10px;
    display: flex; flex-direction: column; gap: 2px;
  }
  .divider {
    text-align: center; font-size: 11px; color: #fff; opacity: 0.9;
    background: rgba(0,0,0,0.15); align-self: center;
    padding: 3px 12px; border-radius: 10px; margin: 6px 0 14px;
  }
  .row { display: flex; align-items: flex-end; gap: 6px; margin-top: 2px; max-width: 100%; }
  .row.bot { justify-content: flex-start; }
  .row.user { justify-content: flex-end; }
  .row .bot-avatar {
    width: 32px; height: 32px; border-radius: 50%; background: #fff;
    display: flex; align-items: center; justify-content: center; font-size: 16px;
    flex: 0 0 auto; box-shadow: 0 1px 3px rgba(0,0,0,0.15);
  }
  .row .bot-avatar.hidden { visibility: hidden; }
  .bubble-wrap { display: flex; flex-direction: column; max-width: 74%; }
  .row.user .bubble-wrap { align-items: flex-end; }
  .bubble {
    padding: 9px 13px; border-radius: 16px; font-size: 14.5px; line-height: 1.5;
    white-space: pre-line;
  }
  .row.bot .bubble { background: #fff; color: #1a1a1a; border-bottom-left-radius: 4px; }
  @media (prefers-color-scheme: dark) { .row.bot .bubble { background: #262b31; color: #f0f0f0; } }
  .row.user .bubble { background: #9ee87f; color: #08341a; border-bottom-right-radius: 4px; }
  @media (prefers-color-scheme: dark) { .row.user .bubble { background: #2f7d3f; color: #eafff0; } }
  .timestamp { font-size: 10.5px; opacity: 0.55; margin: 3px 4px 10px; flex: 0 0 auto; align-self: flex-end; }

  /* conditions card, rendered inside a bot bubble */
  .cond-card { background: transparent; }
  .cond-emoji-big { font-size: 40px; text-align: center; margin-bottom: 2px; }
  .cond-headline { font-size: 15px; font-weight: 600; text-align: center; margin-bottom: 10px; }
  .cond-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .cond {
    display: flex; align-items: center; gap: 8px; padding: 8px 10px;
    border-radius: 10px; background: #f4f6f8; font-size: 12px;
  }
  @media (prefers-color-scheme: dark) { .cond { background: #1c2126; } }
  .cond .cond-emoji { font-size: 18px; }
  .cond .cond-label { opacity: 0.6; font-size: 10px; }
  .cond .cond-value { font-weight: 600; }
  .loc-line { font-size: 12px; opacity: 0.6; margin-top: 8px; }
  .error .bubble { color: #c0392b; }

  /* ── Bottom input bar ── */
  .inputbar {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 10px max(8px, env(safe-area-inset-bottom));
    background: #fff; border-top: 1px solid rgba(0,0,0,0.08); flex: 0 0 auto;
  }
  @media (prefers-color-scheme: dark) { .inputbar { background: #14171a; border-top-color: rgba(255,255,255,0.08); } }
  .inputbar input {
    flex: 1; border: none; outline: none; background: #f0f1f3; border-radius: 20px;
    padding: 10px 16px; font-size: 14px; color: inherit;
  }
  @media (prefers-color-scheme: dark) { .inputbar input { background: #262b31; } }
  .inputbar button {
    flex: 0 0 auto; width: 38px; height: 38px; border-radius: 50%; border: none;
    background: #06C755; color: #fff; font-size: 16px; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
  }
  .inputbar button:active { opacity: 0.8; }
  .inputbar button[disabled] { opacity: 0.5; cursor: default; }
</style>
</head>
<body>
  <div class="header">
    <div class="avatar">🐶</div>
    <div>
      <div class="title">กุชชี่</div>
      <div class="subtitle" id="header-subtitle">กำลังเชื่อมต่อ...</div>
    </div>
    <div class="spacer"></div>
    <div class="icon">⋯</div>
  </div>

  <div class="chat" id="chat"></div>

  <div class="inputbar">
    <input id="text-input" type="text" placeholder="พิมพ์ข้อความ...">
    <button id="send-btn" title="ส่ง">➤</button>
  </div>

<script>
const chatEl = document.getElementById('chat');
const subtitleEl = document.getElementById('header-subtitle');
let lastDateLabel = null;

function nowTimeTh() {
  return new Date().toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit' });
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s;
  return div.innerHTML;
}

function ensureDateDivider() {
  const today = new Date().toLocaleDateString('th-TH', { day: 'numeric', month: 'long' });
  if (lastDateLabel !== today) {
    lastDateLabel = today;
    const div = document.createElement('div');
    div.className = 'divider';
    div.textContent = today;
    chatEl.appendChild(div);
  }
}

function addUserBubble(text) {
  ensureDateDivider();
  const row = document.createElement('div');
  row.className = 'row user';
  row.innerHTML = \`
    <div class="bubble-wrap">
      <div class="bubble">\${text}</div>
    </div>
    <div class="timestamp">\${nowTimeTh()}</div>
  \`;
  chatEl.appendChild(row);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function addBotBubble(innerHtml, opts = {}) {
  ensureDateDivider();
  const row = document.createElement('div');
  row.className = 'row bot' + (opts.error ? ' error' : '');
  row.innerHTML = \`
    <div class="bot-avatar\${opts.continued ? ' hidden' : ''}">🐶</div>
    <div class="bubble-wrap">
      <div class="bubble">\${innerHtml}</div>
    </div>
    <div class="timestamp">\${nowTimeTh()}</div>
  \`;
  chatEl.appendChild(row);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function condCard(data) {
  const c = data.conditions;
  const cards = [
    ['PM2.5', c.pm25 !== null ? \`\${c.pm25} · \${c.pm25LevelTh}\` : 'ไม่มีข้อมูล', c.pm25Emoji],
    ['UV', c.uvIndex !== null ? \`\${c.uvIndex} · \${c.uvLevelTh}\` : 'ไม่มีข้อมูล', c.uvEmoji],
    ['รู้สึกเหมือน', c.heatIndexC !== null ? \`\${c.heatIndexC}°C · \${c.heatIndexLevelTh}\` : 'ไม่มีข้อมูล', c.heatIndexEmoji],
    ['ลม', c.windSpeedKmh !== null ? \`\${c.windSpeedKmh} km/h · \${c.windLevelTh}\` : 'ไม่มีข้อมูล', c.windEmoji],
  ];
  return \`
    <div class="cond-card">
      <div class="cond-grid">
        \${cards.map(([label, value, emoji]) => \`
          <div class="cond">
            <span class="cond-emoji">\${emoji}</span>
            <span>
              <div class="cond-label">\${label}</div>
              <div class="cond-value">\${value}</div>
            </span>
          </div>
        \`).join('')}
      </div>
      <div class="loc-line">📍 \${data.label}</div>
    </div>
  \`;
}

async function askGucci() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'โหลดไม่สำเร็จ');

    subtitleEl.textContent = 'ออนไลน์ · อัปเดต ' + nowTimeTh();

    addBotBubble(\`
      <div class="cond-emoji-big">\${data.forecast.emoji}</div>
      <div class="cond-headline">\${data.message}</div>
    \`);
    addBotBubble(condCard(data), { continued: true });
  } catch (e) {
    subtitleEl.textContent = 'ออฟไลน์';
    addBotBubble('⚠️ เช็คไม่ได้ตอนนี้ ลองใหม่อีกครั้งนะ', { error: true });
  }
}

async function askGucciChat(text) {
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'ส่งไม่สำเร็จ');
    addBotBubble(escapeHtml(data.reply));
  } catch (e) {
    addBotBubble('⚠️ ส่งข้อความไม่สำเร็จ ลองใหม่อีกครั้งนะ', { error: true });
  }
}

const inputEl = document.getElementById('text-input');

function sendMessage() {
  const text = inputEl.value.trim();
  inputEl.value = '';
  if (!text) {
    addUserBubble('เช็คให้หน่อย 🙏');
    setTimeout(askGucci, 350);
    return;
  }
  addUserBubble(escapeHtml(text));
  setTimeout(() => askGucciChat(text), 350);
}

document.getElementById('send-btn').addEventListener('click', sendMessage);
inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') sendMessage();
});

// ทักทายรอบแรกตอนเปิดแอป
askGucci();
</script>
</body>
</html>`;
}
