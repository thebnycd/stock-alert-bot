"""
Telegram-бот для отслеживания акций.

Режимы запуска:
  python stock_alert_bot.py --mode check    -> проверка резких просадок
  python stock_alert_bot.py --mode digest   -> еженедельный дайджест новостей

Все ключи и токены берутся из переменных окружения (см. README.md):
  FINNHUB_API_KEY       - обязателен (бесплатный ключ на finnhub.io)
  TELEGRAM_BOT_TOKEN    - обязателен (токен бота от @BotFather)
  TELEGRAM_CHAT_ID      - обязателен (твой chat_id)
  ANTHROPIC_API_KEY     - опционально, для AI-объяснения причины падения

Список тикеров и порог падения настраиваются в tickers.json.
"""

import os
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


def summarize_reason_with_ai(symbol, change_pct, headlines):
    """Опционально спрашивает у Claude (Haiku) вероятную причину падения по заголовкам новостей.
    Если ANTHROPIC_API_KEY не задан или новостей нет - возвращает None, и бот просто
    присылает сами заголовки без AI-комментария."""
    if not ANTHROPIC_API_KEY or not headlines:
        return None

    titles = "\n".join(f"- {h.get('headline', '')}" for h in headlines[:8])
    prompt = (
        f"Акция {symbol} упала на {abs(change_pct):.1f}% за день. "
        f"Вот свежие заголовки новостей по этой компании:\n{titles}\n\n"
        f"В 1-2 коротких предложениях на русском: похоже ли падение связано "
        f"с одной из этих новостей и какой конкретно? Если явной причины не видно "
        f"(например, просадка вместе со всем рынком), честно скажи об этом."
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
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()
    except Exception as e:
        return f"(не удалось получить AI-объяснение: {e})"


def mode_check(config, state):
    """Проверяет все тикеры на резкое падение и шлёт алерт не чаще раза в день на тикер."""
    threshold = config.get("drop_threshold_percent", 5.0)
    today_str = datetime.now(timezone.utc).date().isoformat()
    alerts_sent = 0

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
                reason = summarize_reason_with_ai(symbol, change_pct, news)

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
            state = mode_check(config, state)
        else:
            state = mode_digest(config, state)
    except Exception as e:
        save_state(state)  # сохраняем то, что успели накопить, перед выходом
        print(f"Ошибка во время выполнения: {e}")
        sys.exit(1)

    save_state(state)


if __name__ == "__main__":
    main()
