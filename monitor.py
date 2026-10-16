"""IND Utrecht Ukrainian Flexloket availability notifier.

Open the official appointment site in an actual browser and read visible green
calendar days. Never submits forms, creates/cancels/changes an appointment,
or calls undocumented IND APIs.
"""

from __future__ import annotations

import argparse
import json
import signal
from urllib.parse import urlsplit
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

URL = os.getenv("IND_URL", "https://oap.ind.nl/nl/oap/en/#/OEK")
LOKET = "IND Utrecht Oekraïne - Flexloket"
CUTOFF = date.fromisoformat(os.getenv("BEFORE_DATE", "2026-11-19"))
TARGET_YEAR = int(os.getenv("TARGET_YEAR", "2026"))
TARGET_MONTH = int(os.getenv("TARGET_MONTH", "11"))
INTERVAL_SECONDS = max(60, int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
CHROMIUM_PATH = os.getenv("CHROMIUM_PATH", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
HEADLESS = os.getenv("HEADLESS", "true").lower() in ("1", "yes", "true")
STATE_DIR = Path(os.getenv("STATE_DIR", str(ROOT))).expanduser()
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = STATE_DIR / "state.json"
PROBE_SCRIPT = (ROOT / "calendar_probe.js").read_text(encoding="utf-8")
LOG = logging.getLogger("ind-monitor")


def clean(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def telegram_call(method: str, payload: dict | None = None) -> dict:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задано у .env")
    res = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload or {}, timeout=20)
    res.raise_for_status()
    data = res.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method}: {data.get('description', 'невідома помилка')}")
    return data


def notify(message: str) -> None:
    if not CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID не задано у .env")
    telegram_call("sendMessage", {"chat_id": CHAT_ID, "text": message,
                                  "disable_web_page_preview": True})


def find_chat_id() -> None:
    print("Надішли /start своєму Telegram-боту, а потім повтори цю команду.")
    updates = telegram_call("getUpdates", {"limit": 100}).get("result", [])
    seen = set()
    for u in updates:
        obj = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
        chat = obj.get("chat", {})
        chat_id = chat.get("id")
        if chat_id is not None and chat_id not in seen:
            print(f"CHAT_ID={chat_id}  ({chat.get('type', '')}: {chat.get('first_name', '')})")
            seen.add(chat_id)
    if not seen:
        print("Ще немає повідомлень. Відкрий бота, натисни Start і напиши /start.")


def load_state() -> set[str]:
    try:
        return set(json.loads(STATE_PATH.read_text(encoding="utf-8")).get("currently_available", []))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return set()


def save_state(dates: set[str]) -> None:
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps({"currently_available": sorted(dates)}, indent=2), encoding="utf-8")
    temp.replace(STATE_PATH)


def build_proxy(url: str) -> dict | None:
    """Playwright proxy settings; no proxy rotation or evasion."""
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https", "socks5") or not parsed.hostname or not parsed.port:
        raise ValueError("PROXY_URL must look like http://user:password@host:port")
    result = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        from urllib.parse import unquote
        result["username"] = unquote(parsed.username)
    if parsed.password:
        from urllib.parse import unquote
        result["password"] = unquote(parsed.password)
    return result


def attach_network_log(page: Page) -> None:
    """Diagnostics only: paths/statuses, no query, cookies or response bodies."""
    log = []
    def record(response):
        if response.request.resource_type not in ("xhr", "fetch"):
            return
        u = urlsplit(response.url)
        if u.hostname not in ("oap.ind.nl",):
            return
        item = {"method": response.request.method, "status": response.status,
                "path": u.path, "resource_type": response.request.resource_type}
        if item not in log:
            log.append(item)
        if len(log) > 200:
            del log[:-200]
    page.on("response", record)
    page._ind_network_log = log


def select_utrecht(page: Page) -> None:
    """Works with both HTML <select> and common custom drop-down widgets."""
    for element in page.locator("select").all():
        if not element.is_visible():
            continue
        options = element.locator("option").all()
        for opt in options:
            label = opt.inner_text().strip()
            simplified = clean(label)
            if "utrecht" in simplified and "flexloket" in simplified:
                element.select_option(value=opt.get_attribute("value"))
                LOG.info("Loket selected using native dropdown: %s", label)
                return

    pattern = re.compile(r"Utrecht.*(?:Oekra|Oekr|Ukr).*Flexloket", flags=re.I)
    matched = page.get_by_text(pattern)
    for ix in range(min(matched.count(), 5)):
        candidate = matched.nth(ix)
        if candidate.is_visible():
            LOG.info("Loket already displayed: %s", candidate.inner_text().strip()[:90])
            return

    # Try a custom combobox; never continue on a wrongly selected office.
    combos = page.get_by_role("combobox")
    for ix in range(min(combos.count(), 4)):
        combo = combos.nth(ix)
        if not combo.is_visible():
            continue
        combo.click(timeout=5000)
        chosen = page.get_by_text(pattern)
        for j in range(min(chosen.count(), 8)):
            option = chosen.nth(j)
            if option.is_visible():
                option.click(timeout=5000)
                LOG.info("Loket selected using custom combobox")
                return
    raise RuntimeError("Не вдалося вибрати Utrecht Flexloket. Збережено діагностичний скриншот.")


def ensure_one_person(page: Page) -> None:
    """Set 1 person using a native number field or verify/edit the +/- widget."""
    for locator in [page.locator('input[type="number"]'), page.locator('input[name*="person" i]')]:
        for i in range(min(locator.count(), 4)):
            box = locator.nth(i)
            if box.is_visible() and box.is_enabled():
                # IND renders this as <input type="number" readonly value="1">.
                # Do not try to fill a read-only field: verify its value first.
                current_value = box.input_value().strip()
                if current_value == "1":
                    LOG.info("Number of persons already set to 1")
                    return
                if not box.get_attribute("readonly") and box.is_editable():
                    box.fill("1")
                    box.dispatch_event("change")
                    LOG.info("Number of persons set to 1 via input")
                    return
                # A different read-only value will be handled by +/- below.

    for attempt in range(8):
        result = page.evaluate(r"""() => {
          const shown = e => e && e.getClientRects().length > 0;
          const all = Array.from(document.body.querySelectorAll('*'));
          const labels = all.filter(e => shown(e) &&
            /^(Aantal personen|Number of persons)$/i.test((e.textContent || '').trim()) &&
            !Array.from(e.children).some(c => (c.textContent || '').trim() === (e.textContent || '').trim()));
          for (const label of labels) {
            let scope = label;
            for (let k = 0; k < 4 && scope; k++, scope = scope.parentElement) {
              const next = scope.nextElementSibling;
              if (!next) continue;
              const buttons = Array.from(next.querySelectorAll('button, [role="button"], input[type="button"]'));
              const minus = buttons.find(b => (b.textContent || b.value || '').trim() === '-');
              const plus = buttons.find(b => (b.textContent || b.value || '').trim() === '+');
              const value = next.textContent.replace(/\s/g, '').match(/-([1-9][0-9]*)\+/);
              if (minus && plus && value) {
                minus.setAttribute('data-ind-monitor-minus', '1');
                return {ok: true, count: Number(value[1])};
              }
            }
          }
          return {ok: false};
        }""")
        if not result.get("ok"):
            raise RuntimeError("Не вдалося підтвердити 1 особу в календарі; перевір debug_error.png")
        if result["count"] == 1:
            LOG.info("Person counter confirmed: 1")
            return
        if result["count"] < 1:
            raise RuntimeError("Некоректне число людей")
        page.locator('[data-ind-monitor-minus="1"]').click(timeout=5000)
        page.wait_for_timeout(250)
    raise RuntimeError("Не вдалося встановити кількість осіб = 1")


def probe_calendar(page: Page) -> dict:
    return page.evaluate(PROBE_SCRIPT)


def read_november(page: Page) -> dict:
    current = probe_calendar(page)
    if not current.get("ok"):
        page.wait_for_timeout(1200)
        current = probe_calendar(page)
    if not current.get("ok"):
        raise RuntimeError(f"Календар не розпізнано: {current.get('reason')} | {current.get('snippet', '')[:180]}")

    target = TARGET_YEAR * 12 + TARGET_MONTH
    for _ in range(8):
        shown = int(current["year"]) * 12 + int(current["month"])
        if shown == target:
            if current["dayTiles"] < 28:
                raise RuntimeError("Неповний календар; результат відкинуто")
            return current
        direction = "next" if shown < target else "prev"
        if not current.get("navigation", {}).get(direction):
            raise RuntimeError(f"Не знайдено стрілку {direction} для переходу до листопада")
        LOG.info("Calendar %s-%02d -> click %s", current["year"], current["month"], direction)
        page.locator(f'[data-ind-monitor-nav="{direction}"]').click(timeout=5000)
        page.wait_for_timeout(800)
        updated = probe_calendar(page)
        if not updated.get("ok") or (updated.get("year"), updated.get("month")) == (current.get("year"), current.get("month")):
            raise RuntimeError("Місяць не змінився після натискання стрілки")
        current = updated
    raise RuntimeError("Не вдалося перейти до потрібного місяця за 8 кроків")


def check_once(page: Page, debug: bool = False) -> set[str]:
    response = page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    if response is not None and response.status in (403, 429):
        raise RuntimeError(f"IND відповів HTTP {response.status}; застосовується пауза, не обходимо блокування")
    page.wait_for_timeout(550)
    body = page.locator("body").inner_text(timeout=12000).lower()
    if "captcha" in body or "too many requests" in body:
        raise RuntimeError("Сайт показує CAPTCHA або обмеження запитів, перевірку зупинено/уповільнено")
    page.wait_for_function("document.body && (document.body.innerText || '').length > 80", timeout=30000)
    select_utrecht(page)
    page.wait_for_timeout(900)
    ensure_one_person(page)
    page.wait_for_timeout(700)
    calendar = read_november(page)
    LOG.info("Calendar %04d-%02d: %s green days out of %s tiles",
             calendar["year"], calendar["month"], calendar["availableDays"], calendar["dayTiles"])
    if debug:
        page.screenshot(path=str(STATE_DIR / "debug.png"), full_page=True)
        (STATE_DIR / "debug_calendar.json").write_text(json.dumps(calendar, indent=2, ensure_ascii=False), encoding="utf-8")
        LOG.info("Saved debug.png + debug_calendar.json")
    available_dates = set()
    for day in calendar["availableDays"]:
        try:
            d = date(TARGET_YEAR, TARGET_MONTH, day)
            if datetime.now(ZoneInfo("Europe/Amsterdam")).date() <= d < CUTOFF:
                available_dates.add(d.isoformat())
        except ValueError:
            pass
    return available_dates


def do_check(page: Page, debug: bool = False) -> None:
    dates = check_once(page, debug)
    old = load_state()
    fresh = dates - old
    if fresh:
        formatted = ", ".join(f"{int(s[8:10])} листопада" for s in sorted(fresh))
        msg = ("🟢 IND Utrecht: з'явилися раніші дати!\n\n"
               f"Вільні дні: {formatted} 2026 року.\n"
               "Кількість осіб: 1. Час потрібно уточнити у календарі.\n\n"
               f"Перевір і бронюй вручну: {URL}\n\n"
               "⚠️ Місця можуть швидко зникнути. НЕ скасовуй запис "
               "на 19 листопада, поки не впевнишся, що зможеш оформити новий.")
        notify(msg)
        LOG.info("Telegram alert sent: %s", sorted(fresh))
    else:
        LOG.info("No NEW earlier dates (currently available: %s)", sorted(dates))
    save_state(dates)


def main() -> None:
    parser = argparse.ArgumentParser(description="IND Ukrainian Utrecht -> Telegram notifier")
    parser.add_argument("--once", action="store_true", help="Check once, then stop")
    parser.add_argument("--debug", action="store_true", help="Save screenshot/inspection JSON")
    parser.add_argument("--test-telegram", action="store_true", help="Send Telegram test message")
    parser.add_argument("--chat-id", action="store_true", help="Print Telegram chat ID after /start")
    parser.add_argument("--network-debug", action="store_true", help="Save redacted XHR/fetch routes for future requests-mode development")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if args.chat_id:
        find_chat_id()
        return
    if args.test_telegram:
        notify("✅ Тест: Telegram-бот для моніторингу IND Utrecht працює.")
        print("Test message sent")
        return
    if not TOKEN or not CHAT_ID:
        print("Заповни TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID у файлі .env", file=sys.stderr)
        sys.exit(2)

    failures = 0
    error_reported = False
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    launch = {"headless": HEADLESS, "args": ["--disable-dev-shm-usage", "--no-sandbox"]}
    if CHROMIUM_PATH:
        launch["executable_path"] = CHROMIUM_PATH
    proxy = build_proxy(PROXY_URL)
    if proxy:
        launch["proxy"] = proxy
        LOG.info("Browser proxy configured (%s); credentials never logged", proxy['server'].split('@')[-1])
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        context = browser.new_context(locale="nl-NL", timezone_id="Europe/Amsterdam", viewport={"width": 1200, "height": 950})
        page = context.new_page()
        if args.network_debug:
            attach_network_log(page)
        try:
            while True:
                if datetime.now(ZoneInfo("Europe/Amsterdam")).date() >= CUTOFF:
                    LOG.info("Reached %s; stopping checks", CUTOFF)
                    break
                try:
                    do_check(page, debug=args.debug)
                    failures = 0
                    error_reported = False
                    if args.network_debug:
                        (STATE_DIR / "debug_network.json").write_text(
                            json.dumps(page._ind_network_log, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as e:
                    failures += 1
                    LOG.error("Check failed (%d): %s", failures, e)
                    if args.network_debug:
                        try:
                            (STATE_DIR / "debug_network.json").write_text(
                                json.dumps(page._ind_network_log, ensure_ascii=False, indent=2), encoding="utf-8")
                        except Exception:
                            LOG.exception("Could not save redacted network diagnostic")
                    try:
                        page.screenshot(path=str(STATE_DIR / "debug_error.png"), full_page=True, timeout=5000)
                    except Exception:
                        pass
                    if failures >= 3 and not error_reported:
                        try:
                            notify("⚠️ Моніторинг IND: три перевірки поспіль завершилися помилкою. "
                                   "Наявність місць невідома. Перевір debug_error.png і журнал програми.")
                            error_reported = True
                        except Exception:
                            LOG.exception("Could not report errors to Telegram")
                if args.once:
                    break
                # Back off on consecutive failures rather than hammering the public service.
                if failures:
                    seconds = min(3600, INTERVAL_SECONDS * (2 ** min(failures, 6)))
                else:
                    seconds = INTERVAL_SECONDS
                LOG.info("Next check in %d seconds (failure count=%d).", seconds, failures)
                time.sleep(seconds)
        except KeyboardInterrupt:
            LOG.info("Stopped by user")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
