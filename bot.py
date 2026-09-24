#!/usr/bin/env python3
"""
Flight Price Bot — Telegram-бот для мониторинга авиабилетов (Aviasales / Travelpayouts).
 
Текущая конфигурация:
  • Москва → Вьетнам (Нячанг, Дананг, Фукуок, Ханой), в одну сторону,
    поиск минимальной цены за ВЕСЬ ноябрь 2026.
  • Кнопка «🇹🇷 Стамбул» — Москва → Стамбул, поиск по октябрю 2026.
  • Цены только за одного взрослого (ограничение источника данных).
  • Порог по цене для каждого маршрута, меняется кнопкой в чате.
  • Уведомление при падении ниже порога, история в CSV, ежедневные графики.
  • Доступ у нескольких людей из списка ALLOWED_CHAT_IDS.
 
Зависимости:  pip install requests matplotlib
Запуск 24/7:  см. README.md, Dockerfile, flightbot.service
"""
 
import os
import csv
import json
import time
import threading
import datetime as dt
import requests
 
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
 
# ------------------------------- CONFIG -------------------------------------
API_TOKEN  = os.getenv("TP_TOKEN", "6b3cb2c3552940395c991540474872d6")
TG_TOKEN   = os.getenv("TG_TOKEN", "8693344775:AAFZXJ_bO_yvkIlQNuQxQQaUFAD2Ppw8bwc")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "497754887")
 

# Кому разрешён доступ и кому идут авто-уведомления (падение цены, графики).
# Впишите сюда chat_id всех участников. Узнать id: написать боту @userinfobot.
# Можно задать и через переменную окружения ALLOWED_CHAT_IDS="111,222,333".
ALLOWED_CHAT_IDS = [
    "497754887",         # участник 1 (вы)
    "477887785",         # участник 2
    "1209160227",        # участник 3
]
if os.getenv("ALLOWED_CHAT_IDS"):
    ALLOWED_CHAT_IDS = [x.strip() for x in os.getenv("ALLOWED_CHAT_IDS").split(",") if x.strip()]
ALLOWED_CHAT_IDS = list(dict.fromkeys(str(x) for x in ALLOWED_CHAT_IDS))
 
# Маршруты для постоянного мониторинга.
# Каждый: origin, destination, порог, период поиска (date_from / date_to, YYYY-MM-DD).
# Только билеты в одну сторону, цена за 1 взрослого.
ROUTES = [
    {"origin": "MOW", "destination": "CXR", "threshold": 45000,
     "date_from": "2026-11-01", "date_to": "2026-11-30"},   # Нячанг
    {"origin": "MOW", "destination": "DAD", "threshold": 45000,
     "date_from": "2026-11-01", "date_to": "2026-11-30"},   # Дананг
    {"origin": "MOW", "destination": "PQC", "threshold": 45000,
     "date_from": "2026-11-01", "date_to": "2026-11-30"},   # Фукуок
    {"origin": "MOW", "destination": "HAN", "threshold": 40000,
     "date_from": "2026-11-01", "date_to": "2026-11-30"},   # Ханой
]
 
# Отдельная кнопка «🇹🇷 Стамбул» — свой период (октябрь 2026).
ISTANBUL_ROUTE = {"origin": "MOW", "destination": "IST", "threshold": 20000,
                  "date_from": "2026-10-01", "date_to": "2026-10-31"}
 
CHECK_INTERVAL   = 300        # период проверки цен, сек (300 = 5 мин)
DAILY_CHART_HOUR = 10         # час ежедневной отправки графиков (0-23)
CURRENCY         = "rub"
 
HISTORY_CSV   = os.getenv("HISTORY_CSV", "price_history.csv")
SETTINGS_JSON = os.getenv("SETTINGS_JSON", "settings.json")
# ----------------------------------------------------------------------------
 
CITY_NAMES = {
    "MOW": "Москва", "CXR": "Нячанг", "DAD": "Дананг",
    "PQC": "Фукуок", "HAN": "Ханой", "IST": "Стамбул",
}
 
 
def city_name(code):
    return CITY_NAMES.get(code, code)
 
 
def route_key(route):
    return f"{route['origin']}-{route['destination']}"
 
 
def route_label(route):
    return f"{city_name(route['origin'])} → {city_name(route['destination'])}"
 
 
_settings_lock = threading.Lock()
 
 
# ----------------------- НАСТРОЙКИ (пороги) из чата --------------------------
def load_settings():
    if os.path.exists(SETTINGS_JSON):
        try:
            with open(SETTINGS_JSON, encoding="utf-8") as f:
                data = json.load(f)
            thr = data.get("thresholds", {})
            for route in ROUTES + [ISTANBUL_ROUTE]:
                k = route_key(route)
                if k in thr:
                    route["threshold"] = thr[k]
        except Exception as e:
            print("! Не удалось загрузить настройки:", e)
 
 
def save_threshold(route_k, value):
    with _settings_lock:
        data = {"thresholds": {}}
        if os.path.exists(SETTINGS_JSON):
            try:
                with open(SETTINGS_JSON, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
        data.setdefault("thresholds", {})[route_k] = value
        with open(SETTINGS_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    for route in ROUTES + [ISTANBUL_ROUTE]:
        if route_key(route) == route_k:
            route["threshold"] = value
 
 
# ----------------------------- ЗАПРОС ЦЕН -----------------------------------
def get_min_price(route):
    """
    Минимальная цена за 1 взрослого по маршруту в одну сторону
    в пределах периода route['date_from'] .. route['date_to'].
    Возвращает (price, details) или (None, None).
    """
    date_from = dt.date.fromisoformat(route["date_from"])
    date_to   = dt.date.fromisoformat(route["date_to"])
 
    months = []
    cur = date_from.replace(day=1)
    while cur <= date_to:
        months.append(cur.strftime("%Y-%m"))
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
 
    best = None
    for month in months:
        params = {
            "origin": route["origin"],
            "destination": route["destination"],
            "departure_at": month,
            "currency": CURRENCY,
            "token": API_TOKEN,
            "one_way": "true",
            "sorting": "price",
            "limit": 1000,
        }
        try:
            resp = requests.get(
                "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
                params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"   ! запрос {route_key(route)} {month}: {e}")
            continue
        if not data.get("success") or not data.get("data"):
            continue
        for item in data["data"]:
            dep_str = item.get("departure_at", "")[:10]
            try:
                dep_date = dt.date.fromisoformat(dep_str)
            except ValueError:
                continue
            if not (date_from <= dep_date <= date_to):
                continue
            if best is None or item["price"] < best["price"]:
                best = item
 
    if best is None:
        return None, None
    return best["price"], best
 
 
# ------------------------------ ИСТОРИЯ -------------------------------------
def append_history(route, price):
    new_file = not os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "route", "price"])
        w.writerow([dt.datetime.now().isoformat(timespec="minutes"),
                    route_key(route), price])
 
 
def read_history(route):
    key = route_key(route)
    pts = []
    if not os.path.exists(HISTORY_CSV):
        return pts
    with open(HISTORY_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["route"] == key:
                try:
                    pts.append((dt.datetime.fromisoformat(row["timestamp"]),
                                float(row["price"])))
                except (ValueError, KeyError):
                    continue
    return pts
 
 
# --------------------------- TELEGRAM API -----------------------------------
def tg(method, **kwargs):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    return requests.post(url, timeout=60, **kwargs)
 
 
def send_text(text, reply_markup=None, chat_id=None):
    """chat_id задан → одному; None → всем разрешённым."""
    targets = [chat_id] if chat_id else ALLOWED_CHAT_IDS
    for cid in targets:
        data = {"chat_id": cid, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        try:
            tg("sendMessage", data=data)
        except Exception as e:
            print(f"! Не смог отправить сообщение {cid}: {e}")
 
 
def send_photo(path, caption="", chat_id=None):
    targets = [chat_id] if chat_id else ALLOWED_CHAT_IDS
    for cid in targets:
        try:
            with open(path, "rb") as img:
                tg("sendPhoto",
                   data={"chat_id": cid, "caption": caption, "parse_mode": "HTML"},
                   files={"photo": img})
        except Exception as e:
            print(f"! Не смог отправить фото {cid}: {e}")
 
 
def main_menu():
    return {"keyboard": [["💰 Цены Вьетнам", "🇹🇷 Стамбул"],
                         ["📊 График сейчас", "🎚 Изменить порог"],
                         ["ℹ️ Статус"]],
            "resize_keyboard": True}
 
 
def aviasales_link(route, dep_iso):
    """Ссылка на поиск Aviasales, 1 взрослый, в одну сторону."""
    d = dep_iso[8:10]; m = dep_iso[5:7]
    return f"https://www.aviasales.ru/search/{route['origin']}{d}{m}{route['destination']}1"
 
 
def period_label(route):
    months_ru = ["", "январь", "февраль", "март", "апрель", "май", "июнь",
                 "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
    df = dt.date.fromisoformat(route["date_from"])
    dt_ = dt.date.fromisoformat(route["date_to"])
    if df.month == dt_.month and df.year == dt_.year:
        return f"{months_ru[df.month]} {df.year}"
    return f"{route['date_from']} .. {route['date_to']}"
 
 
# ------------------------------- ГРАФИК -------------------------------------
def build_chart(route):
    pts = read_history(route)
    if len(pts) < 2:
        return None
    pts.sort(key=lambda p: p[0])
    times = [p[0] for p in pts]
    prices = [p[1] for p in pts]
 
    plt.figure(figsize=(10, 5))
    plt.plot(times, prices, marker="o", markersize=3, linewidth=1.5)
    plt.axhline(route["threshold"], color="red", linestyle="--",
                linewidth=1, label=f"Порог {route['threshold']} \u20bd")
    plt.title(f"Динамика цены  {route_label(route)}  ({period_label(route)})")
    plt.xlabel("Проверка"); plt.ylabel("Мин. цена, \u20bd")
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
    plt.gcf().autofmt_xdate()
    plt.legend(); plt.grid(True, alpha=0.3); plt.tight_layout()
    fname = f"chart_{route_key(route)}.png"
    plt.savefig(fname, dpi=120); plt.close()
    return fname, prices
 
 
def send_all_charts(chat_id=None):
    any_sent = False
    for route in ROUTES + [ISTANBUL_ROUTE]:
        res = build_chart(route)
        if res:
            fname, prices = res
            cap = (f"📊 <b>{route_label(route)}</b> ({period_label(route)})\n"
                   f"Сейчас: {prices[-1]:.0f} \u20bd • мин: {min(prices):.0f} \u20bd • "
                   f"макс: {max(prices):.0f} \u20bd")
            send_photo(fname, cap, chat_id=chat_id)
            any_sent = True
    if not any_sent:
        send_text("Пока недостаточно данных для графика — подождите пару проверок.",
                  chat_id=chat_id)
 
 
# ------------------------ ФОНОВЫЙ МОНИТОРИНГ --------------------------------
def monitor_loop():
    all_routes = ROUTES + [ISTANBUL_ROUTE]
    notified = {route_key(r): False for r in all_routes}
    while True:
        now = dt.datetime.now()
        for route in all_routes:
            key = route_key(route)
            try:
                price, details = get_min_price(route)
                if price is None:
                    print(f"[{now:%H:%M}] {key}: нет данных")
                    continue
                append_history(route, price)
                print(f"[{now:%H:%M}] {key}: {price} \u20bd")
                if price < route["threshold"] and not notified[key]:
                    dep = details.get("departure_at", "")[:10]
                    link = aviasales_link(route, dep)
                    send_text(f"✈️ <b>Цена упала!</b>\n\n"
                              f"<b>{route_label(route)}</b> ({period_label(route)})\n"
                              f"Лучшая дата: {dep}\n"
                              f"Цена за взрослого: <b>{price} \u20bd</b> "
                              f"(порог {route['threshold']} \u20bd)\n\n"
                              f"🔗 {link}")
                    notified[key] = True
                elif price >= route["threshold"]:
                    notified[key] = False
            except Exception as e:
                print(f"   ! {key}: {e}")
 
        # ежедневная авто-рассылка графиков в 10:00 отключена намеренно.
        # Графики по-прежнему доступны вручную кнопкой «📊 График сейчас».
 
        time.sleep(CHECK_INTERVAL)
 
 
# ------------------------ ТЕКСТЫ КНОПОК -------------------------------------
def vietnam_prices_text():
    lines = ["💰 <b>Москва → Вьетнам, ноябрь 2026 (за взрослого, в одну сторону)</b>", ""]
    found = []
    for route in ROUTES:
        try:
            price, details = get_min_price(route)
        except Exception:
            price, details = None, None
        if price is None:
            lines.append(f"• {route_label(route)}: нет данных "
                         f"(порог {route['threshold']} \u20bd)")
        else:
            dep = details.get("departure_at", "")[:10]
            found.append((price, route, dep))
 
    found.sort(key=lambda x: x[0])
    for i, (price, route, dep) in enumerate(found, 1):
        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else "•"))
        link = aviasales_link(route, dep)
        lines.append(
            f"{medal} <b>{route_label(route)}</b>: {price} \u20bd\n"
            f"    вылет {dep} • порог {route['threshold']} \u20bd\n"
            f"    🔗 <a href=\"{link}\">купить</a>")
    lines.append("")
    lines.append("<i>Цена за 1 взрослого, ориентировочная (кэш Aviasales). "
                 "Точная — на странице покупки.</i>")
    return "\n".join(lines).strip()
 
 
def istanbul_text():
    route = ISTANBUL_ROUTE
    try:
        price, details = get_min_price(route)
    except Exception:
        price, details = None, None
    if price is None:
        return (f"🇹🇷 <b>{route_label(route)}, {period_label(route)}</b>\n\n"
                f"Нет данных. Попробуйте позже.")
    dep = details.get("departure_at", "")[:10]
    link = aviasales_link(route, dep)
    return (f"🇹🇷 <b>{route_label(route)}, {period_label(route)}</b>\n\n"
            f"Минимум за взрослого: <b>{price} \u20bd</b>\n"
            f"Лучшая дата: {dep} • порог {route['threshold']} \u20bd\n\n"
            f"🔗 <a href=\"{link}\">купить</a>\n\n"
            f"<i>Цена ориентировочная (кэш Aviasales). "
            f"Точная — на странице покупки.</i>")
 
 
def status_text():
    lines = ["ℹ️ <b>Отслеживаю маршруты:</b>", ""]
    for route in ROUTES:
        lines.append(f"• {route_label(route)} ({period_label(route)}) "
                     f"— порог {route['threshold']} \u20bd")
    r = ISTANBUL_ROUTE
    lines.append(f"• {route_label(r)} ({period_label(r)}) — порог {r['threshold']} \u20bd")
    lines.append(f"\nПроверка каждые {CHECK_INTERVAL//60} мин. "
                 f"График ежедневно в {DAILY_CHART_HOUR}:00.")
    lines.append(f"Участников с доступом: {len(ALLOWED_CHAT_IDS)}.")
    return "\n".join(lines)
 
 
def threshold_keyboard():
    kb = [[{"text": f"{route_label(r)} ({period_label(r)})",
            "callback_data": f"setth:{route_key(r)}"}]
          for r in ROUTES + [ISTANBUL_ROUTE]]
    return {"inline_keyboard": kb}
 
 
# ------------------------ ОБРАБОТКА КНОПОК ----------------------------------
_await_threshold = {"active": False, "route_k": None}
 
 
def handle_message(text, chat_id):
    global _await_threshold
 
    if _await_threshold["active"]:
        try:
            value = int("".join(ch for ch in text if ch.isdigit()))
            save_threshold(_await_threshold["route_k"], value)
            send_text(f"✅ Новый порог: {value} \u20bd", main_menu(), chat_id=chat_id)
        except Exception:
            send_text("Не понял число. Попробуйте ещё раз, напр. 45000.",
                      chat_id=chat_id)
            return
        _await_threshold = {"active": False, "route_k": None}
        return
 
    if text in ("/start", "/menu"):
        send_text("Привет! Слежу за ценами на авиабилеты и пришлю сигнал, "
                  "когда цена упадёт ниже порога. Выберите действие:",
                  main_menu(), chat_id=chat_id)
    elif text.startswith("💰"):
        send_text("Собираю цены по Вьетнаму…", chat_id=chat_id)
        send_text(vietnam_prices_text(), main_menu(), chat_id=chat_id)
    elif text.startswith("🇹🇷"):
        send_text("Смотрю Стамбул…", chat_id=chat_id)
        send_text(istanbul_text(), main_menu(), chat_id=chat_id)
    elif text.startswith("📊"):
        send_text("Готовлю графики…", chat_id=chat_id)
        send_all_charts(chat_id=chat_id)
    elif text.startswith("ℹ️"):
        send_text(status_text(), main_menu(), chat_id=chat_id)
    elif text.startswith("🎚"):
        send_text("Для какого маршрута изменить порог?",
                  threshold_keyboard(), chat_id=chat_id)
    else:
        send_text("Не понял команду. Нажмите /start для меню.",
                  main_menu(), chat_id=chat_id)
 
 
def handle_callback(data, chat_id):
    global _await_threshold
    if data.startswith("setth:"):
        _await_threshold = {"active": True, "route_k": data.split(":", 1)[1]}
        send_text("Введите новый порог в рублях (просто число, напр. 45000):",
                  chat_id=chat_id)
 
 
def telegram_loop():
    offset = None
    # стартовое сообщение всем участникам отключено намеренно
    # (чтобы не рассылать уведомление при каждом перезапуске/передеплое)
 
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            r = tg("getUpdates", params=params).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd and "text" in upd["message"]:
                    cid = str(upd["message"]["chat"]["id"])
                    if cid in ALLOWED_CHAT_IDS:
                        handle_message(upd["message"]["text"], cid)
                    else:
                        send_text("Извините, у вас нет доступа к этому боту.",
                                  chat_id=cid)
                elif "callback_query" in upd:
                    cq = upd["callback_query"]
                    tg("answerCallbackQuery", data={"callback_query_id": cq["id"]})
                    cid = str(cq["message"]["chat"]["id"])
                    if cid in ALLOWED_CHAT_IDS:
                        handle_callback(cq["data"], cid)
        except Exception as e:
            print("! Ошибка Telegram-цикла:", e)
            time.sleep(5)
 
 
def main():
    load_settings()
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] Старт бота. Маршруты: "
          f"{', '.join(route_key(r) for r in ROUTES + [ISTANBUL_ROUTE])}")
    threading.Thread(target=monitor_loop, daemon=True).start()
    telegram_loop()
 
 
if __name__ == "__main__":
    main()
    main()
 
