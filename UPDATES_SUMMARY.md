# Rain Alert Bot Updates — 26 June 2026

## 🎯 3 New Features

### 1️⃣ Cooldown Time Optimization
**เปลี่ยน:** 30 นาที → **20 นาที**
- ผู้ใช้จะได้รับแจ้งเตือนบ่อยขึ้น แต่ไม่มากจนน่ารำคาญ
- ไฟล์: `database.py` line 31

```python
alert_cooldown = Column(Integer, default=20)  # นาที
```

---

### 2️⃣ Alert Time Preferences
**ผู้ใช้สามารถตั้งเวลารับแจ้งเตือน เช่น 6:00-22:00**

#### New Database Fields
ไฟล์: `database.py`
```python
alert_start_hour = Column(Integer, default=6)    # 06:00 AM
alert_end_hour   = Column(Integer, default=22)   # 10:00 PM
```

#### How to Use
ผู้ใช้พิมพ์:
```
แจ้งเตือน 6:00-22:00
```
หรือกดปุ่ม "⏰ ตั้งเวลาแจ้งเตือน" → เลือกจากตัวเลือก

**Time Presets:**
- 🌅 6:00 - 22:00 น. (Default)
- 🌄 7:00 - 21:00 น.
- 💼 8:00 - 20:00 น. (Office hours)
- 🌙 0:00 - 23:59 น. (24/7)

#### Files Changed
- `database.py`: เพิ่ม 2 columns
- `scheduler.py`: เพิ่มเช็ค alert_start_hour / alert_end_hour ก่อนส่ง
- `line_handler.py`: 
  - เพิ่ม `_alert_time_flex()` builder
  - เพิ่ม handler ใน `_on_text()`

---

### 3️⃣ Rich Menu (Line Footer Buttons)
**ปุ่มด้านล่าง LINE ที่ผู้ใช้เห็นตลอด**

4 ปุ่ม:
1. 🌧️ **เช็คฝนตอนนี้** → `ฝน`
2. 📍 **แชร์ Location** → เปิด Location Picker
3. ⏰ **ตั้งเวลาแจ้งเตือน** → `แจ้งเตือน`
4. 🗺️ **Rain Route** → เปิด LIFF

#### Setup
ดู: `RICH_MENU_SETUP.md`

**Quick steps:**
1. LINE Developers Console → Rich Menu → Create
2. Upload image (2500 x 810 px) — ใช้ `rich-menu.png` ของคุณ
3. Copy-paste areas จาก `rich-menu.json`
4. แทนที่ `LIFF_ID_HERE` ด้วย LIFF ID จริง
5. Publish

---

## 📋 Files Modified

| File | Changes |
|------|---------|
| `database.py` | + `alert_start_hour`, `alert_end_hour`, cooldown 30→20 |
| `scheduler.py` | Remove hardcoded quiet hours, check per-user settings |
| `line_handler.py` | + `_alert_time_flex()`, time preference handler in `_on_text()`, button in welcome |

## 📁 Files Added

| File | Purpose |
|------|---------|
| `rich-menu.json` | Rich Menu configuration (upload to LINE) |
| `RICH_MENU_SETUP.md` | Setup instructions |
| `UPDATES_SUMMARY.md` | This file |

---

## ✅ Ready to Deploy

1. **Push code:**
   ```bash
   git add .
   git commit -m "Add cooldown optimization, alert time preferences, and Rich Menu"
   git push origin main
   ```

2. **Setup Rich Menu:**
   - Follow `RICH_MENU_SETUP.md`
   - Get image from designer
   - Upload to LINE Developers Console

3. **Test:**
   - Try: `แจ้งเตือน 8:00-20:00`
   - Check: alerts only come between those hours
   - Tap: Rich Menu buttons work

---

## 🔍 Technical Details

### Alert Hour Check Logic
```python
# In scheduler.py
if not (user.alert_start_hour <= current_hour < user.alert_end_hour):
    skip_alert()
```

### Cooldown Logic
```python
# In scheduler.py
if mins_since < user.alert_cooldown:  # now 20 min
    skip_alert()
```

### User Preference Save
```python
# In line_handler.py
user.alert_start_hour = start_h
user.alert_end_hour = end_h
db.commit()
```

---

## 🎨 Rich Menu Coordinates

LINE uses 2500 x 810 px coordinate system:

```
┌──────────────────────────────────────┐
│  (0,0)    │  (625,0)  │  (1250,0) │  (1875,0)  │
│  625x810  │  625x810  │  625x810  │  625x810   │
│           │           │           │            │
│  🌧️ ฝน   │  📍 Loc   │  ⏰ เวลา   │  🗺️ Route │
│           │           │           │            │
└──────────────────────────────────────┘
  Area 0     Area 1      Area 2      Area 3
```

