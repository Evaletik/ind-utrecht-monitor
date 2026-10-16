# IND Utrecht Oekraïne – Flexloket → Telegram (Render / PythonAnywhere)

**Призначення:** перевіряти офіційну сторінку IND для **однієї особи** з інтервалом не частіше 60 секунд; повідомити в Telegram, якщо з'явиться зелена дата **до 19 листопада 2026 року**. Запис на 19.11.2026 залишається недоторканим. Ніякого бронювання, скасування чи відправлення анкет.

**Поточний статус:** вихідні селектори Playwright та парсер кольорів перевірені **на локальній імітації**. Живий сайт IND з середовища розробки повертає лише `Loading...`, тому **ще не доведено, що автоматизація працює на IND**. Спершу проведи один тест `--once --debug --network-debug` на сервері та переглянь журнал. Не вважай розгортання успішним, доки не буде рядка `Calendar 2026-11` з реальним календарем. Перша зелена дата означає лише доступність дня; конкретний час треба перевіряти вручну.

## Файли

- `monitor.py` — головний процес. Telegram, дедуплікація, проксі, пауза при помилках.
- `calendar_probe.js` — лише читає видимі дні та ставить локальні маркери на стрілки календаря. Дані не надсилає.
- `.env.example` — приклад конфігурації (без секретів).
- `Dockerfile`, `render.yaml` — для Render Background Worker.
- `tests/test_local.py` — тест на імітації календаря.

## Telegram (один раз)

1. У Telegram відкрий [@BotFather](https://t.me/BotFather), команда `/newbot`, створи бота. Токен зберігай приватно.
2. Відкрий чат із ботом, натисни Start та надішли `/start`.
3. Скопіюй `.env.example` у `.env`, введи лише `TELEGRAM_BOT_TOKEN`.
4. Запусти `python monitor.py --chat-id` і внеси `TELEGRAM_CHAT_ID` у `.env`.
5. Перевір `python monitor.py --test-telegram`.

Якщо Telegram уже був налаштований у боті ДП Документ, можна повторно використати свій токен/chat ID, але не публікуй їх у GitHub та не надсилай у чат.

## PythonAnywhere (платний аккаунт, рекомендований якщо Always-on вільний)

PythonAnywhere **офіційно підтримує Playwright** у Always-on tasks та має Chromium за `/usr/bin/chromium`. Потрібний платний тариф і незайняте місце Always-on task: [документація](https://help.pythonanywhere.com/pages/Playwright), [Always-on tasks](https://help.pythonanywhere.com/pages/AlwaysOnTasks/).

1. На вкладці **Files** створи папку `ind-monitor` і завантаж ZIP, або файли. У Bash console розпакуй, якщо завантажив ZIP:

   ```bash
   cd ~
   unzip IND_Utrecht_Server_v2.zip -d ind-monitor
   cd ~/ind-monitor
   python3.11 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

   Якщо у твоєму акаунті немає Python 3.11 — заміни на доступний Python 3.10+.

2. Зроби файл `.env` на основі `.env.example` і внеси Telegram-дані. Обов'язково встанови `CHROMIUM_PATH=/usr/bin/chromium` та `HEADLESS=true`. За потреби додай `PROXY_URL=...`. Налаштовуй `.env` у приватній файловій області, **не публікуй файл**.
3. Проведи разовий тест:

   ```bash
   cd ~/ind-monitor
   .venv/bin/python monitor.py --test-telegram
   .venv/bin/python monitor.py --once --debug --network-debug
   ```

   Перевір лог і `debug.png`, `debug_calendar.json`, `debug_network.json` (мережевий файл містить лише методи, статуси й шляхи XHR/fetch без секретів запитів).
4. Якщо тест успішний, відкрий **Tasks → Always-on tasks → Add a new always-on task**:

   ```bash
   /home/YOUR_USERNAME/ind-monitor/.venv/bin/python -u /home/YOUR_USERNAME/ind-monitor/monitor.py
   ```

   Заміни `YOUR_USERNAME` на ім'я акаунта. Не використовуй Scheduled task: її погодинний графік не забезпечить хвилинний цикл.

*Примітка:* назва папки у ZIP може залежати від розпаковки. У цих командах ZIP містить файли без зайвого верхнього каталогу, тому вони з'являться прямо в `~/ind-monitor`.

## Render (альтернатива з Docker, якщо PythonAnywhere Always-on зайнятий)

1. Створи приватний репозиторій GitHub з файлами цієї папки, **крім `.env`**. У Render створи **New → Background Worker**, під'єднай репозиторій, `Runtime: Docker`, `Dockerfile` з проєкту. Або скористайся **Blueprint** із `render.yaml`.
2. У Environment Variables створи:
   - `TELEGRAM_BOT_TOKEN` і `TELEGRAM_CHAT_ID` (секрети);
   - `CHECK_INTERVAL_SECONDS=60`, `HEADLESS=true`;
   - за потреби `PROXY_URL`.
3. Щоб уникнути повторних сповіщень після перевстановлення, **прикріпи persistent disk** і встанови `STATE_DIR=/var/data` (disk mount = `/var/data`). Без диска повідомлення про вже побачений слот можуть повторитися після restart/deploy.
4. Спершу протестуй одноразовий запуск у локальному Docker або в Render Shell (якщо доступний). `docker build -t ind-monitor .` → `docker run --rm --env-file .env ind-monitor python monitor.py --once --debug --network-debug`. Потім запуск фоновий із `python -u monitor.py`.

**Чому не Render Cron кожну хвилину?** Кожен запуск може зайняти до хвилини, а Cron не зберігає локальний диск; для довготривалого циклу простіший Background Worker. Один інстанс, без горизонтального масштабування.

## Проксі (необов'язково)

```dotenv
PROXY_URL=http://username:password@proxy-host:port
```

Підтримуються `http`, `https`, `socks5`. Якщо пароль містить символи `@` або `:`, закодуй їх URL encoding. Проксі застосовується **лише до браузера**, Telegram надсилає повідомлення напряму. Не запускай кілька інстансів з різними IP та не обходь CAPTCHA/403/429 — при помилках монітор застосує збільшені паузи.

## Перевірка та безпека

```bash
python monitor.py --chat-id
python monitor.py --test-telegram
python monitor.py --once --debug --network-debug
python monitor.py
python tests/test_local.py
```

- Кожний успішний цикл зберігає актуальні дати у `state.json`. Повторне сповіщення надходить лише після того, як дата зникла з календаря і з'явилася знову.
- За **трьох помилок поспіль** приходить попередження; цикл збільшує інтервали аж до години, щоб не навантажувати сайт і не обходити обмеження.
- Якщо запущено два однакові монітори, можуть бути дублікати. Тримай увімкненим один сервер.
- Скрипт завершує перевірки після 19.11.2026.
- Якщо сайт змінить верстку, `--once --debug` може показати, які селектори треба змінити. Зберігай скріншоти та логи **без особистих даних**.

Офіційна сторінка: https://oap.ind.nl/nl/oap/en/#/OEK
