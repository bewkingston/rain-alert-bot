# Rich Menu Setup Guide

## วิธีสร้าง Rich Menu ใน LINE Developers Console

### ขั้นตอน:
1. ไปที่ [LINE Developers Console](https://developers.line.biz/console/)
2. เลือก Channel ของ Rain Alert Bot
3. ไปที่ **Messaging API** → **Rich Menu**
4. กด **Create**

### ตั้งค่า Rich Menu:

#### 1. Upload Image (2500 x 810 px)
- ใช้ภาพ `rich-menu.png` (ด้านล่าง)
- 4 ปุ่ม:
  - **🌧️ เช็คฝน** (ซ้าย)
  - **📍 แชร์ Location** (กลาง-ซ้าย)
  - **⏰ เวลา** (กลาง-ขวา)
  - **🗺️ Rain Route** (ขวา)

#### 2. Areas (Line Coordinates)
LINE ใช้ coordinate system:
- Width: 2500 px (ต่อเมื่อ display width = 2500)
- Height: 810 px

#### 3. Actions

| ปุ่ม | X | Y | Width | Height | Action |
|-----|---|---|-------|--------|--------|
| 🌧️ เช็คฝน | 0 | 0 | 625 | 810 | Message: `ฝน` |
| 📍 Location | 625 | 0 | 625 | 810 | URI: `line://nv/location` |
| ⏰ เวลา | 1250 | 0 | 625 | 810 | Message: `แจ้งเตือน` |
| 🗺️ Rain Route | 1875 | 0 | 625 | 810 | URI: `https://liff.line.me/YOUR_LIFF_ID` |

#### 4. Advanced Settings
- **Consistent with bot's display name**: Enable
- **Selected by default**: Enable

---

## ค่าที่ใช้ใน Code

### Alert Time Settings
ผู้ใช้สามารถตั้งเวลาแจ้งเตือน โดยพิมพ์:
```
แจ้งเตือน 6:00-22:00
```

### Default Settings
- **Cooldown**: 20 นาที (ลดจาก 30)
- **Alert Start Hour**: 6:00 AM
- **Alert End Hour**: 22:00 (10:00 PM)
- **Rain Threshold**: 0.5 mm/hr

---

## Image Design (rich-menu.png)

ขนาด: 2500 x 810 px

```
┌─────────┬─────────┬─────────┬─────────┐
│  625px  │  625px  │  625px  │  625px  │
│         │         │         │         │
│ 🌧️ ฝน │ 📍 Loc  │ ⏰ เวลา  │ 🗺️ Route│
│         │         │         │         │
│  เช็ค   │ แชร์   │ ตั้งเวลา │ Rain   │
│ ฝนตอนนี้│location │ แจ้งเตือน│ Route  │
│         │         │         │         │
└─────────┴─────────┴─────────┴─────────┘
```

---

## JSON Template

File: `rich-menu.json` มีค่าพร้อมใช้ — แค่แทนที่ `LIFF_ID_HERE` ด้วย LIFF ID จริงของคุณ

