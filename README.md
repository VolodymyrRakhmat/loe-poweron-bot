# LOE Telegram bot (без браузера)

Бот бере графік з `https://api.loe.lviv.ua/api/menus?page=1&type=photo-grafic&jsonld=1`
через `aiohttp` — Playwright/Chromium більше не потрібні.

## Запуск локально
    pip install -r requirements.txt
    cp .env.example .env   # впиши BOT_TOKEN
    python bot.py

## Безкоштовний хостинг

Важливо: бот використовує long polling і SQLite-файл (підписки, закріплені
повідомлення, нагадування). На хостингах без постійного диска файл
стирається при кожному перезапуску/деплої — тоді доведеться підписуватись знову.

### Варіант 1 (найнадійніший): Oracle Cloud Always Free VM
Постійний диск, працює 24/7, без засинання.
1. Створи VM (Ubuntu, Always Free shape), підключись по SSH.
2. `sudo apt update && sudo apt install -y python3-venv git fonts-dejavu-core`
3. Скопіюй файли, далі:
       python3 -m venv venv && . venv/bin/activate
       pip install -r requirements.txt
       cp .env.example .env && nano .env
4. systemd-сервіс `/etc/systemd/system/loebot.service`:
       [Unit]
       Description=LOE bot
       After=network-online.target
       [Service]
       WorkingDirectory=/home/ubuntu/loebot
       ExecStart=/home/ubuntu/loebot/venv/bin/python bot.py
       Restart=always
       Environment=TZ=Europe/Kyiv
       [Install]
       WantedBy=multi-user.target
   `sudo systemctl enable --now loebot`

### Варіант 2: Render (Web Service, Free) + UptimeRobot
Free-сервіс засинає без вхідного трафіку, тому потрібен зовнішній пінг.
1. Залий код у GitHub-репозиторій (без `.env`).
2. Render → New → Web Service → репозиторій, Runtime: Docker.
3. Env: `BOT_TOKEN`, `TZ=Europe/Kyiv`. `PORT` Render виставляє сам —
   бот підніме `/health`.
4. UptimeRobot → HTTP monitor на `https://<твій-сервіс>.onrender.com/health`
   кожні 5 хв.
5. Мінус: диск тимчасовий (SQLite скидається при рестарті).

### Варіант 3: Koyeb / інші Docker-хостинги
Той самий Dockerfile; задай `BOT_TOKEN`, `TZ`. Умови безкоштовного плану
перевір на сайті хостингу — вони змінюються.

## Корисні змінні
`CHECK_INTERVAL` (с), `REMINDER_MINUTES_BEFORE`, `USE_IMAGES`, `PIN_SCHEDULE`,
`DIGESTS`, `LOE_API_URL`, `DB_PATH`, `TZ`.
