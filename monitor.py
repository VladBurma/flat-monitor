#!/usr/bin/env python3
"""Моніторинг оренди квартир у ЖК Нова Англія з повідомленнями в Telegram.

Джерела: LUN, Flatfy, DOM.RIA, Rieltor, OLX.
Фільтри (config.json): ціна до max_price_uah, площа від min_area,
поверх до max_floor включно (якщо поверх не вказано — оголошення проходить).
"""
import html
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
STATE_PATH = os.path.join(ROOT, "seen.json")
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or str(CONFIG["chat_id"])
DRY_RUN = os.environ.get("DRY_RUN") == "1"
FAIL_ALERT_AFTER = 8          # ~2 години поспіль без даних із джерела
BOOTSTRAP_PAGES = 4           # скільки сторінок брати при першому запуску
REGULAR_PAGES = 2             # скільки сторінок брати при звичайному запуску
KYIV = timezone(timedelta(hours=3))

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


def log(*a):
    print(*a, flush=True)


def get(url, **kw):
    last = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=30, **kw)
            if r.status_code == 200:
                r.encoding = "utf-8"
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code in (403, 404, 410):
                break
        except requests.RequestException as e:
            last = str(e)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{url} -> {last}")


def with_page(url, page, param="page"):
    if page <= 1:
        return url
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if k != param] + [(param, str(page))]
    return urlunsplit(parts._replace(query=urlencode(q, safe=":%")))


# ---------------------------------------------------------------- курси валют
_RATES = {}


def rate(cur):
    cur = (cur or "UAH").upper()
    if cur in ("UAH", "ГРН"):
        return 1.0
    if cur not in _RATES:
        try:
            r = SESSION.get("https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange",
                            params={"valcode": cur, "json": ""}, timeout=20)
            _RATES[cur] = float(r.json()[0]["rate"])
        except Exception:
            _RATES[cur] = {"USD": 44.0, "EUR": 50.0}.get(cur, 44.0)
    return _RATES[cur]


def to_uah(price, cur):
    if price is None:
        return None
    try:
        return round(float(price) * rate(cur))
    except (TypeError, ValueError):
        return None


def num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"\d+(?:[.,]\d+)?", str(s).replace(" ", "").replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None


# ---------------------------------------------------------------- ключі для дедуплікації
def canon_key(url):
    u = url or ""
    m = re.search(r"rieltor\.ua/(?:ru/)?flats-rent/view/(\d+)", u)
    if m:
        return f"rieltor:{m.group(1)}"
    m = re.search(r"olx\.ua/.*?-(ID[0-9A-Za-z]+)\.html", u)
    if m:
        return f"olx:{m.group(1)}"
    m = re.search(r"dom\.ria\.com/.*?-(\d{6,})\.html", u)
    if m:
        return f"domria:{m.group(1)}"
    m = re.search(r"lun\.ua/(?:uk/|ru/)?realty/(\d+)", u)
    if m:
        return f"lun:{m.group(1)}"
    m = re.search(r"flatfy\.ua/.*?/realty/(\d+)", u)
    if m:
        return f"lun:{m.group(1)}"
    parts = urlsplit(u)
    return f"url:{parts.netloc}{parts.path}".rstrip("/")


def listing(source, url, price, currency, area=None, floor=None, floors=None, rooms=None,
            address=None, photo=None, extra_keys=(), via=None):
    price_uah = to_uah(price, currency)
    return {
        "source": source,
        "via": via,
        "url": url,
        "key": canon_key(url),
        "extra_keys": [k for k in extra_keys if k],
        "price": price,
        "currency": (currency or "UAH").upper(),
        "price_uah": price_uah,
        "area": num(area),
        "floor": int(num(floor)) if num(floor) is not None else None,
        "floors": int(num(floors)) if num(floors) is not None else None,
        "rooms": int(num(rooms)) if num(rooms) is not None else None,
        "address": (address or "").strip(),
        "photo": photo,
    }


# ---------------------------------------------------------------- джерела
def lun_image(img):
    if isinstance(img, dict) and img.get("image_id"):
        return f"https://market-images.lunstatic.net/lun-ua/720/720/images/{img['image_id']}.jpg"
    if isinstance(img, dict) and img.get("imageId"):
        return f"https://market-images.lunstatic.net/lun-ua/720/720/images/{img['imageId']}.jpg"
    if isinstance(img, str) and img.startswith("http"):
        return img
    return None


def lun_obj_to_listing(o, source):
    g = lambda *ks: next((o[k] for k in ks if o.get(k) is not None), None)
    url = g("urlRaw", "url_raw")
    site = (o.get("site") or {})
    site_name = site.get("displayName") or site.get("name") or ""
    if not url or site_name in ("lun.ua", "flatfy.ua", ""):
        url = f"https://lun.ua/uk/realty/{o['id']}"
    imgs = o.get("images") or []
    photo = lun_image(imgs[0]) if imgs else None
    return listing(
        source, url, g("price"), g("currency"),
        area=g("areaTotal", "area_total"), floor=g("floor"),
        floors=g("floorCount", "floor_count"), rooms=g("roomCount", "room_count"),
        address=g("header"), photo=photo,
        extra_keys=[f"lun:{o['id']}", f"lungroup:{g('groupId', 'group_id')}" if g("groupId", "group_id") else None],
        via=site_name if site_name not in ("lun.ua", "flatfy.ua") else None,
    )


def src_lun(pages):
    out = []
    for p in range(1, pages + 1):
        h = get(with_page(CONFIG["sources"]["lun"], p)).text
        chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', h, re.S)
        flight = "".join(json.loads('"' + c + '"') for c in chunks)
        dec = json.JSONDecoder()
        found = 0
        seen_ids = set()
        for m in re.finditer(r'\{"id":\d+,"groupId"', flight):
            try:
                o = dec.raw_decode(flight, m.start())[0]
            except ValueError:
                continue
            if o["id"] in seen_ids or "price" not in o:
                continue
            seen_ids.add(o["id"])
            out.append(lun_obj_to_listing(o, "LUN"))
            found += 1
        if not found:
            break
    return out


def src_flatfy(pages):
    out = []
    for p in range(1, pages + 1):
        h = get(with_page(CONFIG["sources"]["flatfy"], p)).text
        m = re.search(r"window\.INITIAL_STATE\s*=\s*", h)
        if not m:
            raise RuntimeError("flatfy: no INITIAL_STATE")
        s = h[m.end():]
        s = s[: s.find("</script>")].rstrip().rstrip(";").replace("undefined", "null")
        data = json.loads(s)
        lst = data["search"]["realties"]["list"]
        if not lst:
            break
        out += [lun_obj_to_listing(o, "Flatfy") for o in lst]
    return out


def src_domria(pages):
    out = []
    for p in range(1, pages + 1):
        h = get(with_page(CONFIG["sources"]["domria"], p)).text
        i = h.find("window.__INITIAL_STATE__=")
        if i < 0:
            raise RuntimeError("domria: no __INITIAL_STATE__")
        data, _ = json.JSONDecoder().raw_decode(h, i + len("window.__INITIAL_STATE__="))
        lst = data["catalog"]["realtyForCatalog"]
        if not lst:
            break
        for r in lst:
            pa = r.get("priceArr") or {}
            uah = num(pa.get("3"))
            url = "https://dom.ria.com/uk" + (r.get("beautifulUrl") or "/" + r.get("beautiful_url", ""))
            addr = " ".join(x for x in [r.get("street_name_uk"), r.get("building_number_str")] if x)
            out.append(listing(
                "DOM.RIA", url, uah if uah else r.get("price"),
                "UAH" if uah else ("USD" if r.get("currency_type") == "$" else "UAH"),
                area=r.get("total_square_meters"), floor=r.get("floor"),
                floors=r.get("floors_count"), rooms=r.get("rooms_count"),
                address=addr, photo=r.get("mainPhoto"),
            ))
    return out


def src_rieltor(pages):
    out = []
    for p in range(1, pages + 1):
        h = get(with_page(CONFIG["sources"]["rieltor"], p)).text
        cards = re.split(r'<div class="catalog-card[ "]', h)[1:]
        if not cards:
            break
        for c in cards:
            m = re.search(r'data-catalog-item-id="(\d+)"', c)
            if not m:
                continue
            rid = m.group(1)
            label = re.search(r'data-label="([^"]*)"', c)
            label = html.unescape(label.group(1)) if label else ""
            cur = "USD" if "$" in label else ("EUR" if "€" in label else "UAH")
            img = re.search(r'src="(https://[^"]+\.(?:jpg|jpeg|png|webp))"', c)
            text = re.sub(r"<script.*?</script>", " ", c, flags=re.S)
            text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)))
            area = re.search(r"(\d+(?:[.,]\d+)?)\s*/[^м]*м²", text) or re.search(r"(\d+(?:[.,]\d+)?)\s*м²", text)
            fl = re.search(r"поверх\s+(\d+)\s+з\s+(\d+)", text)
            rooms = re.search(r"(\d+)\s+кімнат", text)
            addr = re.search(r"/міс\s+(.*?)\s+Київ", text)
            out.append(listing(
                "Rieltor", f"https://rieltor.ua/flats-rent/view/{rid}/", num(label), cur,
                area=area.group(1) if area else None,
                floor=fl.group(1) if fl else None, floors=fl.group(2) if fl else None,
                rooms=rooms.group(1) if rooms else None,
                address=addr.group(1) if addr else None,
                photo=re.sub(r"/lun-ua/\d+/\d+/", "/lun-ua/720/720/", img.group(1)) if img else None,
            ))
    return out


def src_olx(pages):
    out = []
    for p in range(1, pages + 1):
        h = get(with_page(CONFIG["sources"]["olx"], p)).text
        m = re.search(r'window\.__PRERENDERED_STATE__\s*=\s*("(?:[^"\\]|\\.)*")', h)
        if not m:
            raise RuntimeError("olx: no __PRERENDERED_STATE__")
        data = json.loads(json.loads(m.group(1)))
        ads = data.get("listing", {}).get("listing", {}).get("ads", [])
        if not ads:
            break
        for a in ads:
            if a.get("isPromoted") and p > 1:
                pass
            params = {x.get("key"): x for x in a.get("params", [])}
            def pv(k):
                x = params.get(k) or {}
                return x.get("normalizedValue") or x.get("value")
            price = (a.get("price") or {}).get("regularPrice") or {}
            text = (a.get("title", "") + " " + a.get("description", "")).lower()
            if "англ" not in text:   # пошук OLX нечіткий — лишаємо тільки згадки ЖК
                continue
            photos = a.get("photos") or []
            photo = photos[0].replace("{width}", "800").replace("{height}", "600") if photos else None
            out.append(listing(
                "OLX", a.get("url"), price.get("value"), price.get("currencyCode") or "UAH",
                area=pv("total_area"), floor=pv("floor"), floors=pv("total_floors"),
                rooms=pv("number_of_rooms_string"), address=a.get("title"), photo=photo,
            ))
    return out


SOURCES = {
    "lun": src_lun,
    "flatfy": src_flatfy,
    "domria": src_domria,
    "rieltor": src_rieltor,
    "olx": src_olx,
}


# ---------------------------------------------------------------- фільтр
def matches(x):
    if x["price_uah"] is not None and x["price_uah"] > CONFIG["max_price_uah"]:
        return False
    if x["area"] is not None and x["area"] < CONFIG["min_area"]:
        return False
    if x["floor"] is not None and x["floor"] > CONFIG["max_floor"]:
        return False
    return True


# ---------------------------------------------------------------- Telegram
def tg(method, **payload):
    if DRY_RUN or not TOKEN:
        log(f"[dry] {method}: {json.dumps(payload, ensure_ascii=False)[:300]}")
        return {"ok": True}
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=payload, timeout=30)
    try:
        d = r.json()
    except ValueError:
        d = {"ok": False, "description": r.text[:200]}
    if not d.get("ok"):
        if d.get("parameters", {}).get("retry_after"):
            time.sleep(int(d["parameters"]["retry_after"]) + 1)
            return tg(method, **payload)
        log(f"telegram {method} error: {d}")
    return d


def fmt_price(x):
    if x["price"] is None:
        return "ціна не вказана"
    p = f"{int(x['price']):,}".replace(",", " ")
    if x["currency"] == "UAH":
        return f"{p} грн"
    sym = {"USD": "$", "EUR": "€"}.get(x["currency"], x["currency"])
    uah = f"{x['price_uah']:,}".replace(",", " ") if x["price_uah"] else "?"
    return f"{p} {sym} (≈{uah} грн)"


def caption(x):
    e = html.escape
    bits = []
    if x["rooms"]:
        bits.append(f"{x['rooms']}-кімн.")
    bits.append(f"{x['area']:g} м²" if x["area"] else "площа не вказана")
    if x["floor"] is not None:
        bits.append(f"поверх {x['floor']}" + (f" з {x['floors']}" if x["floors"] else ""))
    else:
        bits.append("поверх не вказано")
    src = x["source"] + (f" → {x['via']}" if x.get("via") else "")
    lines = [
        f"🏠 <b>{e(fmt_price(x))}</b>",
        e(" · ".join(bits)),
    ]
    if x["address"]:
        lines.append(f"📍 {e(x['address'][:120])}")
    lines.append(f'<a href="{e(x["url"])}">Відкрити оголошення</a> · {e(src)}')
    return "\n".join(lines)


def notify(x):
    cap = caption(x)
    if x.get("photo"):
        d = tg("sendPhoto", chat_id=CHAT_ID, photo=x["photo"], caption=cap, parse_mode="HTML")
        if d.get("ok"):
            return True
    return tg("sendMessage", chat_id=CHAT_ID, text=cap, parse_mode="HTML",
              disable_web_page_preview="false").get("ok", False)


# ---------------------------------------------------------------- стан
def load_state():
    try:
        return json.load(open(STATE_PATH, encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {"seen": {}, "fails": {}, "alerted": []}


def save_state(st):
    cutoff = time.time() - 120 * 86400
    st["seen"] = {k: v for k, v in st["seen"].items() if v > cutoff}
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=0, sort_keys=True)


def keys_of(x):
    return [x["key"]] + x["extra_keys"]


def main():
    st = load_state()
    bootstrap = not st["seen"]
    pages = BOOTSTRAP_PAGES if bootstrap else REGULAR_PAGES
    only = [s for s in os.environ.get("ONLY_SOURCES", "").split(",") if s]

    collected = []
    for name, fn in SOURCES.items():
        if only and name not in only:
            continue
        if not CONFIG["sources"].get(name):
            continue
        try:
            items = fn(pages)
            if not items:
                raise RuntimeError("0 оголошень (можливо змінилась верстка)")
            log(f"{name}: {len(items)} оголошень")
            collected += items
            if st["fails"].get(name, 0) >= FAIL_ALERT_AFTER and name in st["alerted"]:
                tg("sendMessage", chat_id=CHAT_ID, text=f"✅ Джерело {name} знову працює.")
                st["alerted"].remove(name)
            st["fails"][name] = 0
        except Exception as e:
            st["fails"][name] = st["fails"].get(name, 0) + 1
            log(f"{name}: ПОМИЛКА ({st['fails'][name]} поспіль): {e}")
            traceback.print_exc()
            if st["fails"][name] == FAIL_ALERT_AFTER and name not in st["alerted"]:
                tg("sendMessage", chat_id=CHAT_ID,
                   text=f"⚠️ Не вдається отримати оголошення з {name} вже ~2 години. "
                        f"Інші сайти перевіряються далі.\n{str(e)[:200]}")
                st["alerted"].append(name)

    now = time.time()
    new = []
    run_keys = set()
    for x in collected:
        ks = keys_of(x)
        if any(k in st["seen"] or k in run_keys for k in ks):
            for k in ks:
                st["seen"].setdefault(k, now)
            continue
        run_keys.update(ks)
        if matches(x):
            new.append(x)

    if bootstrap:
        if new:
            price = f"{CONFIG['max_price_uah']:,}".replace(",", " ")
            head = (f"🔎 Моніторинг ЖК Нова Англія запущено.\n"
                    f"Фільтр: до {price} грн, від {CONFIG['min_area']} м², "
                    f"поверх до {CONFIG['max_floor']} (або не вказаний).\n\n"
                    f"Зараз підходять такі оголошення ({len(new)}):")
            tg("sendMessage", chat_id=CHAT_ID, text=head)
            for x in new[:40]:
                notify(x)
                time.sleep(1.2)
        else:
            tg("sendMessage", chat_id=CHAT_ID,
               text="🔎 Моніторинг ЖК Нова Англія запущено. Зараз підходящих оголошень немає — "
                    "напишу, щойно з’явиться нове.")
    else:
        for x in new:
            notify(x)
            time.sleep(1.2)

    for x in collected:
        for k in keys_of(x):
            st["seen"].setdefault(k, now)
    st["last_run"] = datetime.now(KYIV).strftime("%Y-%m-%d %H:%M")
    save_state(st)
    log(f"Нових підходящих: {len(new)}; всього в базі: {len(st['seen'])}")


if __name__ == "__main__":
    main()
