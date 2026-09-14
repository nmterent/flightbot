#!/usr/bin/env python3
"""
Flight Price Bot — интерактивный Telegram-бот для мониторинга авиабилетов
через Aviasales / Travelpayouts.
 
Возможности:
  • Несколько маршрутов сразу (Москва, Нижний Новгород и любые другие).
  • Отслеживание всей ближайшей недели — минимум по всем дням.
  • Билеты в одну сторону И туда-обратно (для round-trip задаётся длительность поездки).
  • История цен в CSV + ежедневный график динамики в Telegram.
  • Мгновенное уведомление при падении ниже порога.
  • Управление кнопками прямо в чате:
        /start                — показать меню
        «📊 График сейчас»    — прислать графики немедленно
        «💰 Текущие цены»     — показать актуальный минимум по всем маршрутам
        «🎚 Изменить порог»   — поменять порог цены, не редактируя код
        «ℹ️ Статус»           — что и как отслеживается
 
Запуск 24/7: см. README.md, Dockerfile и flightbot.service в этой папке.
 
Зависимости:  pip install requests matplotlib
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

# Маршруты. Для round-trip укажите trip_days (длительность поездки в днях);
# для перелёта в одну сторону оставьте trip_days = None.
ROUTES = [
    {"origin": "MOW", "destination": "AYT", "threshold": 15000, "trip_days": None},
    {"origin": "GOJ", "destination": "AYT", "threshold": 20000, "trip_days": None},
    {"origin": "MOW", "destination": "AYT", "threshold": 30000, "trip_days": 7},
    # Газипаша-Аланья (GZP) — ближе к Аланье. Рейсов меньше, чем в AYT,
    # поэтому иногда бот будет показывать «нет данных» — это нормально.
    {"origin": "MOW", "destination": "GZP", "threshold": 18000, "trip_days": None},
    {"origin": "GOJ", "destination": "GZP", "threshold": 22000, "trip_days": None},
]
 
WEEK_AHEAD_DAYS  = 7          # горизонт «ближайшей недели» для даты вылета
CHECK_INTERVAL   = 300        # период проверки цен, сек (300 = 5 мин)
DAILY_CHART_HOUR = 10         # час ежедневной отправки графиков (0-23)
CURRENCY         = "rub"
 
HISTORY_CSV   = os.getenv("HISTORY_CSV", "price_history.csv")
SETTINGS_JSON = os.getenv("SETTINGS_JSON", "settings.json")   # для порогов из чата
# ----------------------------------------------------------------------------
 
# порог может меняться из чата — храним отдельно и подгружаем поверх ROUTES
_settings_lock = threading.Lock()
 
 
def route_key(route):
    base = f"{route['origin']}-{route['destination']}"
    return base + (f"-rt{route['trip_days']}" if route.get("trip_days") else "-ow")
 
 
def route_label(route):
    kind = f"туда-обратно, {route['trip_days']} дн." if route.get("trip_days") else "в одну сторону"
    return f"{route['origin']}→{route['destination']} ({kind})"
 
 
# ----------------------- НАСТРОЙКИ (пороги) из чата --------------------------
def load_settings():
    if os.path.exists(SETTINGS_JSON):
        try:
            with open(SETTINGS_JSON, encoding="utf-8") as f:
                data = json.load(f)
            for route in ROUTES:
                k = route_key(route)
                if k in data.get("thresholds", {}):
                    route["threshold"] = data["thresholds"][k]
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
    for route in ROUTES:
        if route_key(route) == route_k:
            route["threshold"] = value
 
 
# ----------------------------- ЗАПРОС ЦЕН -----------------------------------
def get_week_min_price(route):
    """Мин. цена по маршруту в ближайшую неделю. Возвращает (price, details) или (None, None)."""
    today = dt.date.today()
    params = {
        "origin": route["origin"],
        "destination": route["destination"],
        "departure_at": today.strftime("%Y-%m"),
        "currency": CURRENCY,
        "token": API_TOKEN,
        "sorting": "price",
        "limit": 100,
    }
    if route.get("trip_days"):
        params["one_way"] = "false"
    else:
        params["one_way"] = "true"
 
    resp = requests.get(
        "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
        params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success") or not data.get("data"):
        return None, None
 
    horizon = today + dt.timedelta(days=WEEK_AHEAD_DAYS)
    best = None
    for item in data["data"]:
        dep_str = item.get("departure_at", "")[:10]
        try:
            dep_date = dt.date.fromisoformat(dep_str)
        except ValueError:
            continue
        if not (today <= dep_date <= horizon):
            continue
        # для round-trip при желании можно проверять длительность поездки,
        # но Travelpayouts сам подбирает return_at близко к запросу
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
 
 
def send_text(text, reply_markup=None):
    data = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    tg("sendMessage", data=data)
 
 
def send_photo(path, caption=""):
    with open(path, "rb") as img:
        tg("sendPhoto",
           data={"chat_id": TG_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
           files={"photo": img})
 
 
def main_menu():
    return {"keyboard": [["🔥 Лучшие цены недели", "🏖 Куда дешевле"],
                         ["💰 Текущие цены", "📊 График сейчас"],
                         ["🎚 Изменить порог", "ℹ️ Статус"]],
            "resize_keyboard": True}
 
 
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
                linewidth=1, label=f"Порог {route['threshold']} ₽")
    plt.title(f"Динамика цены  {route_label(route)}")
    plt.xlabel("Проверка"); plt.ylabel("Мин. цена, ₽")
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
    plt.gcf().autofmt_xdate()
    plt.legend(); plt.grid(True, alpha=0.3); plt.tight_layout()
    fname = f"chart_{route_key(route)}.png"
    plt.savefig(fname, dpi=120); plt.close()
    return fname, prices
 
 
def send_all_charts():
    any_sent = False
    for route in ROUTES:
        res = build_chart(route)
        if res:
            fname, prices = res
            cap = (f"📊 <b>{route_label(route)}</b>\n"
                   f"Сейчас: {prices[-1]:.0f} ₽ • мин: {min(prices):.0f} ₽ • "
                   f"макс: {max(prices):.0f} ₽")
            send_photo(fname, cap)
            any_sent = True
    if not any_sent:
        send_text("Пока недостаточно данных для графика — "
                  "подождите пару проверок.")
 
 
# ------------------------ ФОНОВЫЙ МОНИТОРИНГ --------------------------------
def monitor_loop():
    notified = {route_key(r): False for r in ROUTES}
    last_chart_date = None
    while True:
        now = dt.datetime.now()
        for route in ROUTES:
            key = route_key(route)
            try:
                price, details = get_week_min_price(route)
                if price is None:
                    print(f"[{now:%H:%M}] {key}: нет данных")
                    continue
                append_history(route, price)
                print(f"[{now:%H:%M}] {key}: {price} ₽")
                if price < route["threshold"] and not notified[key]:
                    dep = details.get("departure_at", "")[:10]
                    link = (f"https://www.aviasales.ru/search/"
                            f"{route['origin']}{dep[8:10]}{dep[5:7]}"
                            f"{route['destination']}1")
                    send_text(f"✈️ <b>Цена упала!</b>\n\n"
                              f"<b>{route_label(route)}</b>\n"
                              f"Лучшая дата: {dep}\n"
                              f"Цена: <b>{price} ₽</b> (порог {route['threshold']} ₽)\n\n"
                              f"🔗 {link}")
                    notified[key] = True
                elif price >= route["threshold"]:
                    notified[key] = False
            except Exception as e:
                print(f"   ! {key}: {e}")
 
        if now.hour == DAILY_CHART_HOUR and last_chart_date != now.date():
            send_all_charts()
            last_chart_date = now.date()
 
        time.sleep(CHECK_INTERVAL)
 
 
# ------------------------ ОБРАБОТКА КНОПОК ----------------------------------
# простое состояние диалога «изменить порог»
_await_threshold = {"active": False, "route_k": None}
 
 
def current_prices_text():
    lines = ["💰 <b>Текущий минимум за неделю:</b>"]
    for route in ROUTES:
        try:
            price, _ = get_week_min_price(route)
            p = f"{price} ₽" if price else "нет данных"
        except Exception:
            p = "ошибка запроса"
        lines.append(f"• {route_label(route)}: {p} (порог {route['threshold']} ₽)")
    return "\n".join(lines)
 
 
def status_text():
    lines = ["ℹ️ <b>Отслеживаю маршруты:</b>"]
    for route in ROUTES:
        lines.append(f"• {route_label(route)} — порог {route['threshold']} ₽")
    lines.append(f"\nПроверка каждые {CHECK_INTERVAL//60} мин. "
                 f"График ежедневно в {DAILY_CHART_HOUR}:00.")
    return "\n".join(lines)
 
 
# понятные названия аэропортов-курортов для вывода
CITY_NAMES = {
    "MOW": "Москва", "GOJ": "Нижний Новгород",
    "AYT": "Анталья", "GZP": "Аланья (Газипаша)",
    "DLM": "Даламан", "BJV": "Бодрум", "AER": "Сочи",
}
 
 
def city_name(code):
    return CITY_NAMES.get(code, code)
 
 
# Направления для кнопки «🔥 Лучшие цены недели».
# Каждый элемент: (город вылета, курорт). Все — билеты в одну сторону.
BEST_DEALS_ROUTES = [
    ("MOW", "AYT"),   # Москва — Анталья
    ("MOW", "GZP"),   # Москва — Аланья
    ("GOJ", "AYT"),   # Нижний Новгород — Анталья
]
 
 
def aviasales_link(origin, destination, dep_iso):
    """Ссылка на поиск Aviasales по дате вылета (формат MOW + ддмм + AYT + 1)."""
    d = dep_iso[8:10]      # день
    m = dep_iso[5:7]       # месяц
    return f"https://www.aviasales.ru/search/{origin}{d}{m}{destination}1"
 
 
def best_deals_text():
    """
    Дайджест самых дешёвых поездок на ближайшую неделю по заданным
    направлениям: цена, дата вылета и ссылка на покупку.
    Отсортировано от самого дешёвого к дорогому.
    """
    found = []
    for origin, dest in BEST_DEALS_ROUTES:
        route = {"origin": origin, "destination": dest, "trip_days": None}
        try:
            price, details = get_week_min_price(route)
        except Exception:
            price, details = None, None
        if price is not None:
            dep = details.get("departure_at", "")[:10]
            found.append((price, origin, dest, dep))
 
    if not found:
        return ("🔥 <b>Лучшие цены недели</b>\n\nПока нет данных по этим "
                "направлениям. Попробуйте чуть позже.")
 
    found.sort(key=lambda x: x[0])   # от дешёвого к дорогому
 
    lines = ["🔥 <b>Самые дешёвые поездки на ближайшую неделю</b>", ""]
    for i, (price, origin, dest, dep) in enumerate(found, 1):
        medal = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else "•"))
        link = aviasales_link(origin, dest, dep)
        lines.append(
            f"{medal} <b>{city_name(origin)} → {city_name(dest)}</b>\n"
            f"    {price} ₽ • вылет {dep}\n"
            f"    🔗 <a href=\"{link}\">купить</a>")
        lines.append("")
    return "\n".join(lines).strip()
 
 
def compare_destinations_text():
    """
    Сравнивает курорты по цене и говорит, куда дешевле лететь.
    Сравнение честное: только билеты «в одну сторону» (trip_days = None),
    сгруппированные по городу вылета.
    """
    # собираем: origin -> список (destination, price)
    by_origin = {}
    for route in ROUTES:
        if route.get("trip_days"):      # round-trip в сравнении не участвует
            continue
        origin = route["origin"]
        try:
            price, details = get_week_min_price(route)
        except Exception:
            price, details = None, None
        if price is not None:
            dep = details.get("departure_at", "")[:10]
            by_origin.setdefault(origin, []).append(
                (route["destination"], price, dep))
 
    if not by_origin:
        return ("🏖 <b>Куда дешевле</b>\n\nПока нет данных по направлениям. "
                "Попробуйте чуть позже — возможно, по этим курортам сейчас "
                "нет предложений.")
 
    lines = ["🏖 <b>Куда сейчас дешевле лететь</b>", ""]
    for origin, items in by_origin.items():
        # сортируем курорты по цене
        items.sort(key=lambda x: x[1])
        lines.append(f"<b>Из {city_name(origin)}:</b>")
        for dest, price, dep in items:
            lines.append(f"   • {city_name(dest)}: {price} ₽ (вылет {dep})")
        best_dest, best_price, best_dep = items[0]
        # экономия относительно самого дорогого варианта, если он есть
        if len(items) > 1:
            worst_price = items[-1][1]
            save = worst_price - best_price
            lines.append(f"   👉 Дешевле всего: <b>{city_name(best_dest)}</b> "
                         f"— экономия {save} ₽")
        else:
            lines.append(f"   👉 Доступно одно направление: "
                         f"<b>{city_name(best_dest)}</b>")
        lines.append("")
 
    return "\n".join(lines).strip()
 
 
def threshold_keyboard():
    kb = [[{"text": route_label(r), "callback_data": f"setth:{route_key(r)}"}]
          for r in ROUTES]
    return {"inline_keyboard": kb}
 
 
def handle_message(text):
    global _await_threshold
 
    if _await_threshold["active"]:
        # ждём число — новый порог
        try:
            value = int("".join(ch for ch in text if ch.isdigit()))
            save_threshold(_await_threshold["route_k"], value)
            send_text(f"✅ Новый порог: {value} ₽", main_menu())
        except Exception:
            send_text("Не понял число. Попробуйте ещё раз, напр. 15000.")
            return
        _await_threshold = {"active": False, "route_k": None}
        return
 
    if text in ("/start", "/menu"):
        send_text("Привет! Я слежу за ценами на авиабилеты и пришлю сигнал, "
                  "когда цена упадёт. Выберите действие:", main_menu())
    elif text.startswith("🔥"):
        send_text("Собираю лучшие цены недели…")
        send_text(best_deals_text(), main_menu())
    elif text.startswith("💰"):
        send_text(current_prices_text(), main_menu())
    elif text.startswith("🏖"):
        send_text("Сравниваю курорты…")
        send_text(compare_destinations_text(), main_menu())
    elif text.startswith("📊"):
        send_text("Готовлю графики…")
        send_all_charts()
    elif text.startswith("ℹ️"):
        send_text(status_text(), main_menu())
    elif text.startswith("🎚"):
        send_text("Для какого маршрута изменить порог?", threshold_keyboard())
    else:
        send_text("Не понял команду. Нажмите /start для меню.", main_menu())
 
 
def handle_callback(data):
    global _await_threshold
    if data.startswith("setth:"):
        _await_threshold = {"active": True, "route_k": data.split(":", 1)[1]}
        send_text("Введите новый порог в рублях (просто число, напр. 15000):")
 
 
def telegram_loop():
    """Читает апдейты Telegram (long polling) и реагирует на кнопки."""
    offset = None
    # приветствие при старте
    try:
        send_text("🚀 Бот запущен и следит за ценами.", main_menu())
    except Exception as e:
        print("! Не смог отправить стартовое сообщение:", e)
 
    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset
            r = tg("getUpdates", params=params).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd and "text" in upd["message"]:
                    if str(upd["message"]["chat"]["id"]) == str(TG_CHAT_ID):
                        handle_message(upd["message"]["text"])
                elif "callback_query" in upd:
                    cq = upd["callback_query"]
                    tg("answerCallbackQuery",
                       data={"callback_query_id": cq["id"]})
                    if str(cq["message"]["chat"]["id"]) == str(TG_CHAT_ID):
                        handle_callback(cq["data"])
        except Exception as e:
            print("! Ошибка Telegram-цикла:", e)
            time.sleep(5)
 
 
def main():
    load_settings()
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] Старт бота. Маршруты: "
          f"{', '.join(route_key(r) for r in ROUTES)}")
    # мониторинг — в фоне, Telegram-цикл — в основном потоке
    threading.Thread(target=monitor_loop, daemon=True).start()
    telegram_loop()
 
 
if __name__ == "__main__":
    main()
 


if __name__ == "__main__":
    main()
