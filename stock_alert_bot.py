"""
Telegram-бот для отслеживания акций.

Режимы запуска:
  python stock_alert_bot.py --mode check    -> обработка команд из Telegram + проверка резких просадок
  python stock_alert_bot.py --mode digest   -> еженедельный дайджест новостей

Управление списком тикеров прямо из чата с ботом (обрабатывается при запуске check):
  /list             - показать текущие тикеры и порог падения
  /add TSLA         - добавить тикер
  /remove AAPL      - убрать тикер
  /threshold 7      - изменить порог падения в %
  /digest           - прислать дайджест новостей сейчас
  /help             - список команд

Все ключи и токены берутся из переменных окружения (см. README.md):
  FINNHUB_API_KEY       - обязателен (бесплатный ключ на finnhub.io)
  TELEGRAM_BOT_TOKEN    - обязателен (токен бота от @BotFather)
  TELEGRAM_CHAT_ID      - обязателен (твой chat_id)
  ANTHROPIC_API_KEY     - опционально, для AI-объяснения причины падения

Список тикеров и порог падения настраиваются в tickers.json.
"""

import os
import re
import html
import json
import sys
import argparse
from datetime import datetime, timedelta, timezone

import requests

CONFIG_PATH = "tickers.json"
STATE_PATH = "state.json"

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # опционально

TELEGRAM_MAX_LEN = 4000  # запас от лимита Telegram в 4096 символов

HELP_TEXT = (
    "🤖 <b>Команды бота</b>\n\n"
    "/list — показать текущие тикеры и порог падения\n"
    "/add TSLA — добавить тикер\n"
    "/remove AAPL — убрать тикер\n"
    "/threshold 7 — изменить порог падения (в %)\n"
    "/digest — прислать дайджест новостей сейчас\n"
    "/help — эта справка\n\n"
    "Команды применяются при ближайшем запуске бота по расписанию "
    "(каждые 30 минут в будни в торговые часы)."
)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def get_updates(offset=None):
    """Забирает новые сообщения, присланные боту в Telegram (для команд управления)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("result", [])


def send_telegram(text):
    """Отправляет сообщение, разбивая на части при превышении лимита Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for i in range(0, len(text), TELEGRAM_MAX_LEN):
        chunk = text[i:i + TELEGRAM_MAX_LEN]
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        resp.raise_for_status()


def get_quote(symbol):
    """Возвращает словарь с ценой. Ключевые поля: c (текущая цена), pc (закрытие пред. дня), dp (% изменения за день)."""
    url = "https://finnhub.io/api/v1/quote"
    resp = requests.get(url, params={"symbol": symbol, "token": FINNHUB_API_KEY}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_company_news(symbol, days_back=3):
    today = datetime.now(timezone.utc).date()
    from_date = today - timedelta(days=days_back)
    url = "https://finnhub.io/api/v1/company-news"
    resp = requests.get(url, params={
        "symbol": symbol,
        "from": from_date.isoformat(),
        "to": today.isoformat(),
        "token": FINNHUB_API_KEY,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_telegram_channel(channel, hours_back=48):
    """Скачивает публичное веб-превью Telegram-канала (t.me/s/<channel>) и возвращает
    свежие посты за последние hours_back часов: список {text, dt, channel}.

    Работает только для ПУБЛИЧНЫХ каналов с включённым веб-превью. Если канал приватный,
    превью отключено или страница недоступна — возвращает пустой список и не роняет бота."""
    url = f"https://t.me/s/{channel}"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"Канал @{channel}: не удалось загрузить ({e})")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    posts = []
    # Разбиваем страницу на блоки-сообщения и в каждом ищем текст и время.
    blocks = re.split(r'<div class="tgme_widget_message ', resp.text)
    for block in blocks[1:]:
        text_m = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        time_m = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if not text_m:
            continue  # пост без текста (только медиа) — пропускаем
        text = re.sub(r'<br\s*/?>', '\n', text_m.group(1))
        text = re.sub(r'<[^>]+>', '', text)
        text = html.unescape(text).strip()
        if not text:
            continue
        dt = None
        if time_m:
            try:
                dt = datetime.fromisoformat(time_m.group(1))
            except ValueError:
                dt = None
        if dt and dt < cutoff:
            continue
        posts.append({"text": text, "dt": dt, "channel": channel})
    return posts


def load_channel_posts(config, hours_back=48):
    """Собирает посты со всех каналов из config['telegram_channels'] за период."""
    posts = []
    for ch in config.get("telegram_channels", []):
        posts.extend(fetch_telegram_channel(ch, hours_back=hours_back))
    return posts


def summarize_reason_with_ai(symbol, change_pct, headlines, channel_posts=None):
    """Опционально спрашивает у Claude (Haiku) вероятную причину падения по заголовкам новостей.
    Если ANTHROPIC_API_KEY не задан или новостей нет - возвращает None, и бот просто
    присылает сами заголовки без AI-комментария."""
    if not ANTHROPIC_API_KEY or (not headlines and not channel_posts):
        return None

    titles = (
        "\n".join(f"- {h.get('headline', '')}" for h in headlines[:8])
        if headlines else "(нет новостей от Finnhub по этой компании)"
    )

    # Контекст из Telegram-каналов: сначала посты, где упомянут тикер (специфичные),
    # иначе — несколько самых свежих постов как макро-фон (ФРС, геополитика и т.п.).
    tg_block = ""
    if channel_posts:
        sym = symbol.upper()
        specific = [p for p in channel_posts if sym in p["text"].upper()]
        chosen = specific[:6] if specific else channel_posts[-8:]
        tg_lines = "\n".join(f"- [@{p['channel']}] {p['text'][:200]}" for p in chosen)
        tg_block = (
            "\n\nСвежие посты из Telegram-каналов (могут объяснять как новость по компании, "
            f"так и общий фон рынка):\n{tg_lines}"
        )

    prompt = (
        f"Акция {symbol} упала на {abs(change_pct):.1f}% за день. "
        f"Вот свежие заголовки новостей по этой компании:\n{titles}"
        f"{tg_block}\n\n"
        f"В 1-2 коротких предложениях на русском: похоже ли падение связано "
        f"с конкретной новостью/событием, и с каким именно? Учитывай и новости по компании, "
        f"и общий фон рынка из постов каналов. Если явной причины не видно "
        f"(например, просадка вместе со всем рынком без своего триггера), честно скажи об этом."
    )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 250,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()
    except Exception as e:
        return f"(не удалось получить AI-объяснение: {e})"


def process_commands(config, state):
    """Читает команды из чата с ботом (/add, /remove, /list, /threshold, /digest, /help)
    и применяет их. Возвращает (config, state, want_digest).

    Команды принимаются только из чата с TELEGRAM_CHAT_ID — чужие сообщения игнорируются.
    Чтобы не обрабатывать одни и те же сообщения дважды, храним last_update_id в state.json."""
    last_id = state.get("last_update_id", 0)
    offset = last_id + 1 if last_id else None
    want_digest = False

    try:
        updates = get_updates(offset=offset)
    except Exception as e:
        print(f"Не удалось получить команды из Telegram: {e}")
        return config, state, want_digest

    config_changed = False

    for upd in updates:
        state["last_update_id"] = upd["update_id"]
        msg = upd.get("message")
        if not msg:
            continue

        # реагируем только на сообщения из «своего» чата
        if str(msg.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
            continue

        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue

        parts = text.split()
        cmd = parts[0].lower().split("@")[0]  # убираем @botname, если добавлен
        arg = parts[1] if len(parts) > 1 else None

        if cmd == "/list":
            tickers = config.get("tickers", [])
            send_telegram(
                "📋 <b>Текущие тикеры:</b>\n" + (", ".join(tickers) or "(список пуст)")
                + f"\n\nПорог падения: {config.get('drop_threshold_percent', 5.0)}%"
            )
        elif cmd == "/add" and arg:
            sym = arg.upper()
            if sym in config["tickers"]:
                send_telegram(f"{sym} уже в списке.")
            else:
                config["tickers"].append(sym)
                config_changed = True
                send_telegram(f"✅ Добавлен {sym}.\nСейчас отслеживаю: {', '.join(config['tickers'])}")
        elif cmd == "/remove" and arg:
            sym = arg.upper()
            if sym in config["tickers"]:
                config["tickers"].remove(sym)
                config_changed = True
                send_telegram(f"🗑 Убран {sym}.\nСейчас отслеживаю: {', '.join(config['tickers']) or '(список пуст)'}")
            else:
                send_telegram(f"{sym} нет в списке.")
        elif cmd == "/threshold" and arg:
            try:
                val = float(arg.replace(",", "."))
                config["drop_threshold_percent"] = val
                config_changed = True
                send_telegram(f"✅ Порог падения теперь {val}%.")
            except ValueError:
                send_telegram("Укажи число, например: /threshold 5")
        elif cmd == "/digest":
            want_digest = True
            send_telegram("🗞 Готовлю дайджест новостей…")
        elif cmd in ("/help", "/start"):
            send_telegram(HELP_TEXT)
        else:
            send_telegram("Не понял команду. /help — список команд.")

    if config_changed:
        save_config(config)

    return config, state, want_digest


def mode_check(config, state):
    """Проверяет все тикеры на резкое падение и шлёт алерт не чаще раза в день на тикер."""
    threshold = config.get("drop_threshold_percent", 5.0)
    today_str = datetime.now(timezone.utc).date().isoformat()
    alerts_sent = 0

    # Посты каналов грузим один раз на весь прогон (общий макро-фон для всех тикеров).
    channel_posts = load_channel_posts(config, hours_back=48) if config.get("telegram_channels") else []

    for symbol in config["tickers"]:
        try:
            quote = get_quote(symbol)
            change_pct = quote.get("dp")
            current = quote.get("c")
            prev_close = quote.get("pc")

            if change_pct is None or current is None:
                print(f"{symbol}: нет данных от Finnhub, пропускаю")
                continue

            already_alerted_today = state.get(symbol, {}).get("last_alert_date") == today_str

            if change_pct <= -threshold and not already_alerted_today:
                news = get_company_news(symbol, days_back=2)
                reason = summarize_reason_with_ai(symbol, change_pct, news, channel_posts)

                text = (
                    f"📉 <b>{symbol}</b>: {change_pct:.1f}% за день\n"
                    f"Цена: {current:.2f} (закрытие пред. дня: {prev_close:.2f})\n"
                )
                if reason:
                    text += f"\n<b>Возможная причина:</b> {reason}\n"
                if news:
                    text += "\nСвежие заголовки:\n"
                    for h in news[:3]:
                        text += f"• {h.get('headline', '')} ({h.get('source', '')})\n"

                send_telegram(text)
                state.setdefault(symbol, {})["last_alert_date"] = today_str
                alerts_sent += 1
                print(f"{symbol}: алерт отправлен ({change_pct:.1f}%)")
            else:
                print(f"{symbol}: {change_pct:.1f}% - без алерта")

        except Exception as e:
            print(f"{symbol}: ошибка при обработке - {e}")
            continue

    print(f"Готово. Проверено {len(config['tickers'])} тикеров, отправлено алертов: {alerts_sent}")
    return state


def mode_digest(config, state):
    """Раз в неделю собирает свежие новости по всем тикерам и шлёт одним дайджестом."""
    today_str = datetime.now(timezone.utc).date().isoformat()
    lines = [f"🗞 <b>Еженедельный дайджест</b> ({today_str})"]

    for symbol in config["tickers"]:
        lines.append(f"\n<b>{symbol}</b>")
        try:
            news = get_company_news(symbol, days_back=7)
            if not news:
                lines.append("— заметных новостей не нашлось")
                continue
            for h in news[:3]:
                lines.append(f"• {h.get('headline', '')} ({h.get('source', '')})")
        except Exception as e:
            lines.append(f"— ошибка получения новостей: {e}")

    # Блок с постами из Telegram-каналов за неделю (по нескольку свежих на канал).
    channels = config.get("telegram_channels", [])
    if channels:
        lines.append("\n🌐 <b>Из Telegram-каналов</b>")
        for ch in channels:
            posts = fetch_telegram_channel(ch, hours_back=24 * 7)
            lines.append(f"\n<b>@{ch}</b>")
            if not posts:
                lines.append("— не удалось прочитать (приватный канал или превью выключено)")
                continue
            for p in posts[-5:]:  # 5 самых свежих
                first_line = p["text"].splitlines()[0][:150]
                lines.append(f"• {first_line}")

    try:
        send_telegram("\n".join(lines))
        print("Дайджест отправлен.")
    except Exception as e:
        print(f"Не удалось отправить дайджест в Telegram: {e}")
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["check", "digest"], required=True)
    args = parser.parse_args()

    missing = [name for name, val in [
        ("FINNHUB_API_KEY", FINNHUB_API_KEY),
        ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
    ] if not val]
    if missing:
        print(f"Ошибка: не заданы переменные окружения: {', '.join(missing)}")
        sys.exit(1)

    config = load_config()
    state = load_state()

    try:
        if args.mode == "check":
            # сначала обрабатываем команды из чата (/add, /remove, /list, /threshold, /digest)
            config, state, want_digest = process_commands(config, state)
            state = mode_check(config, state)
            if want_digest:
                state = mode_digest(config, state)
        else:
            state = mode_digest(config, state)
    except Exception as e:
        save_state(state)  # сохраняем то, что успели накопить, перед выходом
        print(f"Ошибка во время выполнения: {e}")
        sys.exit(1)

    save_state(state)


if __name__ == "__main__":
    main()
