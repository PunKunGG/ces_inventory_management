# CES Inventory Management System

เว็บภาษาไทยสำหรับทะเบียนคลัง รับเข้า เบิกอะไหล่ ยืมคืนเครื่องมือ และตรวจสอบประวัติในห้องช่าง CES เปิดจาก QR กลางด้วยกล้องมือถือและลงชื่อเข้าใช้ด้วยบัญชี CES

## เริ่มใช้งานบน Windows

เปิด PowerShell ในโฟลเดอร์โครงการ ใช้ Python 3.11 ขึ้นไป ขั้นตอนด้านล่างตรวจแล้วด้วย Python 3.14

```powershell
python -m venv .venv
$cesPython = '.\.venv\Scripts\python.exe'
& $cesPython -m pip install -r requirements.txt
$env:PYTHONUTF8 = '1'
$env:CES_SECRET_KEY = & $cesPython -c 'import secrets; print(secrets.token_urlsafe(48))'
& $cesPython -m flask --app ces init-db
& $cesPython -m flask --app ces seed-demo
.\.venv\Scripts\waitress-serve.exe --listen=127.0.0.1:8080 --call ces:create_app
```

เปิด `http://127.0.0.1:8080/` กำหนดรหัสผ่านตัวอย่างอย่างน้อย 12 ตัวอักษรเมื่อ `seed-demo` ถาม บัญชีด้านล่างใช้รหัสตัวอย่างที่ตั้งไว้ สามารถเปลี่ยนรหัสแยกบัญชีที่เมนูผู้ใช้ได้

| บัญชี | งานที่ทำได้ |
| --- | --- |
| `admin` | ผู้ใช้ หมวดหมู่ ตำแหน่ง ทะเบียน ปรับยอด เปลี่ยนสถานะเครื่องมือ ประวัติและรายงาน |
| `technician` | รับเข้า เบิก ยืม และคืน ค้นทะเบียนและดูธุรกรรมของตน |
| `supervisor` | ภาพรวม ทะเบียน ประวัติและรายงาน |

`seed-demo` ใช้กับฐานใหม่ หากตั้งฐานว่างสำหรับเตรียมข้อมูลภายใต้การอนุมัติ ให้ใช้ `create-admin` แทนคำสั่ง `seed-demo` ฐานข้อมูลเริ่มต้นอยู่ใน `instance/ces.sqlite3` และเปลี่ยนได้ผ่าน `CES_DATABASE` เก็บ `CES_SECRET_KEY` ไว้ในค่าตั้งของเครื่องที่รันระบบ หากสร้างค่าใหม่ เซสชันเดิมจะเข้าใช้ไม่ได้

## ทดลองสแกน QR จากมือถือ

1. ใช้เครื่อง Windows และมือถือบนเครือข่ายทดสอบที่เข้าถึงกันได้ เปิด `ipconfig` เพื่อหา IPv4 ของ Windows แล้วแทนค่า `192.168.1.50` ในตัวอย่าง
2. หยุด Waitress ที่ฟังเฉพาะเครื่องด้วย `Ctrl+C` สร้าง QR แล้วเริ่ม Waitress บน IP ของเครือข่ายทดสอบ

```powershell
$cesDemoIp = '192.168.1.50'
New-Item -ItemType Directory -Force artifacts | Out-Null
& $cesPython -m flask --app ces make-qr "http://${cesDemoIp}:8080/" 'artifacts/ces-demo.svg'
.\.venv\Scripts\waitress-serve.exe --listen="${cesDemoIp}:8080" --call ces:create_app
```

เปิด `artifacts/ces-demo.svg` แล้วพิมพ์หรือแสดงให้กล้องมือถือสแกน ไฟล์มี URL ใต้ QR ให้พิมพ์เข้าเว็บได้ ตรวจว่า Windows Firewall และเครือข่ายทดสอบอนุญาตให้มือถือเข้าพอร์ต 8080 ใช้ข้อมูลจำลองบน HTTP เฉพาะเครือข่ายทดสอบนี้

สำหรับการใช้งานจริง ให้ฝ่าย IT กำหนดชื่อโฮสต์และ reverse proxy HTTPS บนเครือข่ายที่อนุมัติ ตั้ง `CES_HTTPS=1` เพื่อใช้ Secure cookies และออก QR จาก URL จริง ขั้นตอนก่อนเปิดใช้รวมถึงยืนยันสิทธิ์และ workflow ตรวจนับยอดเปิดคลัง และกำหนดผู้รับผิดชอบการสำรองและกู้คืน

## สำรองและซ้อมกู้คืน

คำสั่งสำรองใช้ SQLite Backup API ตรวจ integrity และ foreign keys แล้วเผยแพร่ไฟล์เมื่อคัดลอกครบ ไฟล์สำรองเก็บข้อมูลคลังและบัญชีผู้ใช้ จัดเก็บในโฟลเดอร์ที่จำกัดสิทธิ์เข้าถึง คำสั่งใช้ชื่อปลายทางใหม่ทุกครั้ง และผู้ใช้ต้องล็อกอินใหม่หลังเปิดฐานที่กู้คืน

```powershell
$cesBackup = 'artifacts/ces-backup-2026-11-04.sqlite3'
& $cesPython -m flask --app ces backup-db $cesBackup
$cesOriginalDb = $env:CES_DATABASE
$env:CES_DATABASE = Join-Path (Get-Location) 'artifacts/ces-restore-drill.sqlite3'
& $cesPython -m flask --app ces restore-db $cesBackup
.\.venv\Scripts\waitress-serve.exe --listen=127.0.0.1:8081 --call ces:create_app
# เปิด http://127.0.0.1:8081/ ตรวจยอด ประวัติ และรายการยืมค้าง แล้วกด Ctrl+C
$env:CES_DATABASE = $cesOriginalDb
```

ทดสอบอัตโนมัติด้วย `& $cesPython -m unittest discover -s tests -v` ดูเกณฑ์ตรวจรับและขั้นตอนทดสอบมือถือใน [tests/ACCEPTANCE.md](tests/ACCEPTANCE.md)

## เอกสารระบบ

- [ขอบเขต MVP และสิทธิ์](docs/PRD.md)
- [สถาปัตยกรรมและโครงสร้างข้อมูล](docs/ARCHITECTURE.md)
- [แนวทางหน้าจอและการใช้งาน](docs/DESIGN_SYSTEM.md)
