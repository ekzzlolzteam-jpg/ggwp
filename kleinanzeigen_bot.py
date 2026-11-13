#!/usr/bin/env python3
"""
Kleinanzeigen: поиск конкретного пользователя + мониторинг новых объявлений.
Один скрипт, один Telegram-бот.

Как это работает
----------------
1. В бота отправляется скриншот профиля (OCR) или текст с данными пользователя:
   имя, дата регистрации ("Aktiv seit"), плашки (TOP/OK/Naja Zufriedenheit,
   freundlich, zuverlässig), опционально подписчики/объявления.
2. Бот показывает распознанные данные и ждёт подтверждения (кнопка «Искать»).
3. ПОИСК ДИАПАЗОНА (бывшие 1.py + 2v2.py): по базе известных пар
   «user_id → дата регистрации» и интерполяционному/бинарному поиску находим
   границы ID, зарегистрированных в нужный день. Ошибки сети/403 НЕ считаются
   «пустыми» ID — запрос повторяется, поэтому границы не «уезжают».
4. ПЕРЕБОР (бывший 3neu.py): все ID в диапазоне проверяются параллельно с
   ограничением скорости; сравнивается имя, дата и плашки. Прогресс
   сохраняется — после перезапуска поиск продолжается с того же места.
5. МОНИТОРИНГ (бывший 10min.py): найденный аккаунт (или любой ID через /watch)
   проверяется каждые N–M минут, о НОВЫХ объявлениях приходит сообщение.

Запуск:  python kleinanzeigen_bot.py           (бот)
         python kleinanzeigen_bot.py --selftest (проверка парсеров без сети)
Настройки — в config.json рядом со скриптом (см. config.example.json).
"""
from __future__ import annotations

import difflib
import html as html_lib
import io
import json
import logging
import os
import random
import re
import sys
import threading
import time
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
SITE = "https://www.kleinanzeigen.de"
PROFILE_URL = SITE + "/s-bestandsliste.html?userId={uid}"

log = logging.getLogger("ka")

# =====================================================================
# КОНФИГ
# =====================================================================
DEFAULT_CONFIG: dict = {
    # --- Telegram ---
    "telegram_token": "",            # или переменная окружения KA_TELEGRAM_TOKEN
    "allowed_chat_ids": [],          # кто может пользоваться ботом; им же идут уведомления
    # --- Сеть ---
    "proxy_url": "",                 # "socks5://user:pass@host:port" или KA_PROXY_URL
    "proxy_urls": [],                # несколько прокси — скорость складывается
    "requests_per_second": 8,        # лимит НА ОДИН прокси (IP); потоки подбираются автоматически
    "request_timeout": 15,
    "max_retries": 4,                # повторов на один ID при 403/5xx/таймауте
    "block_threshold": 20,           # столько блокировок подряд -> пауза
    "block_pause_minutes": 5,        # первая пауза, дальше удваивается (до x8)
    # --- Поиск диапазона ---
    "users_per_day": 25000,          # грубая оценка, уточняется по базе якорей
    "locate_max_requests": 600,
    "boundary_precision": 30,        # до скольких ID сужаем границы (остаток просто переберём)
    "range_margin": 0,               # доп. запас ID с каждой стороны
    "max_scan_ids": 400000,
    "seed_anchors": [[46211501, "24.03.2017"], [48075804, "13.06.2017"]],
    # --- Перебор ---
    "chunk_size": 200,
    "name_fuzzy_threshold": 0.85,
    "auto_watch_on_match": True,     # «✅ Это он» или единственное точное совпадение → мониторинг
    "parallel_searches": 3,          # сколько поисков идут одновременно (делят общую скорость)
    "max_queue": 15,                 # сколько поисков может ждать в «корзине»
    "recheck_delay_seconds": 3,      # пауза между запросами при «медленной» перепроверке ошибок
    # --- Мониторинг ---
    "monitor_every_check": False,    # True — сообщение о КАЖДОЙ проверке (с заметкой), даже без новых
    "monitor_minutes_min": 10,
    "monitor_minutes_max": 30,
    # --- Прочее ---
    "tesseract_cmd": "",             # пусто = автоопределение
    "data_dir": "data",
}


OBSOLETE_KEYS = ("stop_on_exact_match", "save_found_to_config", "day_cache_days", "found_accounts",
                 "workers", "max_parallel_searches")


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        for old in OBSOLETE_KEYS:          # настройки прошлых версий — молча убираем
            user_cfg.pop(old, None)
        unknown = {k for k in user_cfg if not k.startswith("_")} - set(DEFAULT_CONFIG)
        if unknown:
            print(f"⚠️  Неизвестные ключи в {path.name}: {', '.join(sorted(unknown))}")
        cfg.update(user_cfg)
    cfg["telegram_token"] = os.environ.get("KA_TELEGRAM_TOKEN", cfg["telegram_token"])
    cfg["proxy_url"] = os.environ.get("KA_PROXY_URL", cfg["proxy_url"])
    cfg["allowed_chat_ids"] = [str(x) for x in cfg["allowed_chat_ids"]]
    if cfg["monitor_minutes_min"] > cfg["monitor_minutes_max"]:
        raise ValueError("monitor_minutes_min больше monitor_minutes_max")
    if cfg["requests_per_second"] <= 0:
        raise ValueError("requests_per_second должен быть > 0")
    return cfg


# =====================================================================
# ДАТЫ, ИМЕНА, ПЛАШКИ
# =====================================================================
DATE_RE = re.compile(r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{2,4})")
AKTIV_RE = re.compile(r"Aktiv\s*seit\s*:?\s*(\d{1,2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{2,4})", re.I)


def parse_date(text: Optional[str]) -> Optional[date]:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    if y < 100:
        y += 2000
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def fmt_date(d: Optional[date]) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


_UMLAUT_A = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "ss"})
_UMLAUT_AE = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def _base_fold(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"\s+", " ", s).strip()


def name_variants(name: str) -> set[str]:
    """Варианты имени для сравнения: 'Jürgen' == 'Juergen' == 'Jurgen'."""
    base = _base_fold(name).strip(" .,-_|")
    ae = "".join(_UMLAUT_AE.get(ch, ch) for ch in base)
    return {base, ae, base.translate(_UMLAUT_A), ae.replace("ae", "a").replace("oe", "o").replace("ue", "u")}


def names_equal(a: str, b: str) -> bool:
    return bool(name_variants(a) & name_variants(b))


def name_similarity(a: str, b: str) -> float:
    va, vb = name_variants(a), name_variants(b)
    return max(difflib.SequenceMatcher(None, x, y).ratio() for x in va for y in vb)


BADGE_LABELS = {
    "satisfaction": {"top": "TOP Zufriedenheit", "ok": "OK Zufriedenheit", "naja": "Naja Zufriedenheit"},
    "friendliness": {"besonders": "Besonders freundlich", "sehr": "Sehr freundlich", "": "Freundlich"},
    "reliability": {"besonders": "Besonders zuverlässig", "sehr": "Sehr zuverlässig", "": "Zuverlässig"},
}
CATEGORY_RU = {"satisfaction": "довольство", "friendliness": "дружелюбие", "reliability": "надёжность"}


def _badge_fold(text: str) -> str:
    s = _base_fold(text).translate(_UMLAUT_A)
    return s.replace("ae", "a").replace("oe", "o").replace("ue", "u")


def detect_badges(text: str, shorthand: bool = False) -> dict[str, str]:
    """Ищет плашки в тексте страницы / OCR / вводе пользователя.

    Возвращает {категория: каноническое название}. При нескольких уровнях
    одной категории берётся самый высокий (Besonders > Sehr > просто).
    shorthand=True разрешает короткую запись пользователя: «top», «naja», «ok».
    """
    f = _badge_fold(text)
    res: dict[str, str] = {}
    m = re.search(r"\b(top|ok|naja)\s*-?\s*zufriedenheit", f)
    if not m and shorthand:
        m = re.search(r"\b(top|naja|ok)\b", f)
    if m:
        res["satisfaction"] = BADGE_LABELS["satisfaction"][m.group(1)]
    for cat, word in (("friendliness", "freundlich"), ("reliability", "zuverlassig")):
        for level in ("besonders", "sehr", ""):
            pattern = (rf"\b{level}\s+{word}\b" if level else rf"\b{word}\b")
            if re.search(pattern, f):
                res[cat] = BADGE_LABELS[cat][level]
                break
    return res


def badges_str(badges: dict[str, str]) -> str:
    order = ("satisfaction", "friendliness", "reliability")
    vals = [badges[c] for c in order if c in badges]
    return ", ".join(vals) if vals else "—"


# =====================================================================
# ПАРСИНГ СТРАНИЦЫ ПРОФИЛЯ
# =====================================================================
@dataclass
class Profile:
    user_id: int
    name: Optional[str] = None
    aktiv_seit: Optional[date] = None
    badges: dict = field(default_factory=dict)
    online_ads: Optional[int] = None
    total_ads: Optional[int] = None
    followers: Optional[int] = None
    listings: dict = field(default_factory=dict)  # ad_id -> {"title","url","price"}
    page_user_id: Optional[int] = None             # userId, который реально отдала страница

    @property
    def url(self) -> str:
        return PROFILE_URL.format(uid=self.user_id)


AD_ID_RE = re.compile(r"/s-anzeige/(?:[^/?#\"']+/)?(\d{5,})")
BLOCK_MARKERS = ("captcha", "access denied", "zugriff verweigert", "too many requests", "ungewöhnlich")


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(s or "")).strip()


_NAME_CLASS_RE = re.compile(r"userprofile-+name")


def extract_name(soup: BeautifulSoup, raw_html: str) -> Optional[str]:
    # прямой поиск тега быстрее CSS-селектора (это выполняется для каждого из ~25 тыс. ID)
    el = soup.find("h2", class_="userprofile--name") or soup.find(class_=_NAME_CLASS_RE)
    if el:
        t = re.sub(r"^\s*Profil\s+von\s*:?\s*", "", el.get_text(" ", strip=True), flags=re.I)
        if t:
            return _clean_text(t)
    m = re.search(r"Profil\s+von\s*</span>\s*([^<]+?)\s*<", raw_html, re.I)
    if m and _clean_text(m.group(1)):
        return _clean_text(m.group(1))
    if soup.title and soup.title.string:
        m = re.search(r"(?:Anzeigen|Bestandsliste|Profil)\s+von\s+(.+?)\s*(?:[|–]|\s-\s|$)", soup.title.string, re.I)
        if m:
            return _clean_text(m.group(1))
    return None


def _profile_region_text(soup: BeautifulSoup) -> Optional[str]:
    """Текст «шапки» профиля без списка объявлений (чтобы «zuverlässig»
    из названия объявления не считалось плашкой)."""
    node = soup.find(string=re.compile(r"Aktiv\s*seit", re.I))
    if node is None:
        return None
    best = node.parent
    el = best
    for _ in range(10):
        parent = el.parent
        if parent is None or parent.name in ("body", "html", "[document]"):
            break
        if parent.find(attrs={"data-adid": True}) or parent.find("a", href=re.compile(r"/s-anzeige/")):
            break
        if len(parent.get_text(" ", strip=True)) > 4000:
            break
        el = best = parent
    return best.get_text(" ", strip=True)


def extract_listings(soup: BeautifulSoup) -> dict[str, dict]:
    ads: dict[str, dict] = {}
    for el in soup.select("[data-adid]"):
        ad_id = str(el.get("data-adid") or "").strip()
        if not ad_id.isdigit():
            continue
        href = el.get("data-href") or ""
        if not href:
            a = el.find("a", href=re.compile(r"/s-anzeige/"))
            href = a["href"] if a else ""
        title_el = el.select_one("h2, h3, .ellipsis, a.ellipsis")
        price_el = el.select_one("[class*='price']")
        ads[ad_id] = {
            "title": _clean_text(title_el.get_text(" ", strip=True)) if title_el else "",
            "url": (SITE + href) if href.startswith("/") else (href or f"{SITE}/s-anzeige/{ad_id}"),
            "price": _clean_text(price_el.get_text(" ", strip=True)) if price_el else "",
        }
    for a in soup.find_all("a", href=True):
        m = AD_ID_RE.search(a["href"])
        if not m:
            continue
        ad = ads.setdefault(m.group(1), {"title": "", "url": "", "price": ""})
        if not ad["url"]:
            href = a["href"]
            ad["url"] = SITE + href if href.startswith("/") else href
        if not ad["title"]:
            ad["title"] = _clean_text(a.get_text(" ", strip=True))
    return ads


BADGE_ATTR_CATEGORY = {"rating": "satisfaction", "friendliness": "friendliness", "reliability": "reliability"}
# data-badge-level на сайте: friendliness/reliability 0=обычная, 1=Sehr, 2=Besonders; rating 2=TOP
BADGE_LEVEL_KEY = {"satisfaction": {2: "top", 1: "ok", 0: "naja"},
                   "friendliness": {2: "besonders", 1: "sehr", 0: ""},
                   "reliability": {2: "besonders", 1: "sehr", 0: ""}}


def extract_badges(soup: BeautifulSoup) -> Optional[dict]:
    """Плашки из разметки <button class="userbadge" data-badge-name=... data-badge-level=...>.
    None — если таких элементов нет (тогда ищем по тексту)."""
    elements = soup.find_all(attrs={"data-badge-name": True})
    if not elements:
        return None
    res: dict[str, str] = {}
    for el in elements:
        cat = BADGE_ATTR_CATEGORY.get(str(el.get("data-badge-name", "")).lower())
        if not cat:
            continue
        label = detect_badges(el.get_text(" ", strip=True)).get(cat)
        if not label:
            level = str(el.get("data-badge-level", ""))
            key = BADGE_LEVEL_KEY[cat].get(int(level)) if level.isdigit() else None
            label = BADGE_LABELS[cat][key] if key is not None else None
        if label:
            res[cat] = label
    return res


def _int_after(pattern: str, text: str) -> Optional[int]:
    m = re.search(pattern, text, re.I)
    return int(m.group(1)) if m else None


_ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0xAD}
_CHARREF_RE = re.compile(r"&#(?:([0-9]+)|[xX](200[bBcCdD]|2060|[fF][eE][fF][fF]|[aA][dD]"
                         r"|[0-9a-fA-F]{1,6}(?=;|[^0-9a-fA-F]|$)));?")
_SPECIAL = {0x26: "&amp;", 0x3C: "&lt;", 0x3E: "&gt;", 0x22: "&quot;", 0x27: "&apos;"}  # не ломаем разметку


def normalize_html(raw_html: str) -> str:
    """Заменяет числовые ссылки &#...; на символы до разбора.

    Сайт пишет «&#8203» (невидимый пробел) БЕЗ точки с запятой прямо перед
    названием города: «&#8203Elbe». Старые beautifulsoup4 (<4.13) на этом
    падали: ValueError: invalid literal for int() with base 10: '8203Elbe…'."""
    def fix(m: re.Match) -> str:
        cp = int(m.group(1)) if m.group(1) else int(m.group(2), 16)
        if cp in _ZERO_WIDTH or cp == 0 or cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
            return ""
        return _SPECIAL.get(cp) or chr(cp)
    return _CHARREF_RE.sub(fix, raw_html).replace("&#", "&amp;#")


def parse_profile_html(raw_html: str, user_id: int, listings: bool = True) -> Profile:
    """listings=False — не собирать список объявлений (при переборе он не нужен, нужен мониторингу)."""
    raw_html = normalize_html(raw_html)
    soup = BeautifulSoup(raw_html, "html.parser")
    text = soup.get_text(" ", strip=True)
    prof = Profile(user_id=user_id)
    m = AKTIV_RE.search(text)
    prof.aktiv_seit = parse_date(m.group(1)) if m else None
    prof.name = extract_name(soup, raw_html)
    region = _profile_region_text(soup) or ""
    structured = extract_badges(soup)
    if structured is not None:
        prof.badges = structured
    else:
        prof.badges = detect_badges(region) if region else detect_badges(text)
    m = re.search(r'name="userId"\s+value="(\d+)"', raw_html) or re.search(r'data-followed-user="(\d+)"', raw_html)
    prof.page_user_id = int(m.group(1)) if m else None
    src = region or text
    prof.online_ads = _int_after(r"(\d+)\s+Anzeigen?\s+online", src)
    prof.total_ads = _int_after(r"(\d+)\s+gesamt", src)
    prof.followers = _int_after(r"(\d+)\s+Follower", src)
    if listings:
        prof.listings = extract_listings(soup)
    return prof


def looks_like_profile(raw_html: str) -> bool:
    return bool(re.search(r"Aktiv\s*seit|userprofile", raw_html, re.I))


def looks_blocked(raw_html: str) -> bool:
    low = raw_html[:20000].lower()
    return any(marker in low for marker in BLOCK_MARKERS)


# =====================================================================
# HTTP: лимит скорости, повторы, авто-пауза при блокировке
# =====================================================================
@dataclass
class FetchResult:
    status: str                      # "ok" | "missing" | "error"
    profile: Optional[Profile] = None
    http_code: Optional[int] = None
    error: str = ""


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]


def proxy_list(cfg: dict) -> list[str]:
    """Все прокси из настроек (proxy_urls + старый proxy_url). Пустой список = без прокси."""
    urls = [u for u in (cfg.get("proxy_urls") or []) if u]
    if cfg.get("proxy_url") and cfg["proxy_url"] not in urls:
        urls.insert(0, cfg["proxy_url"])
    return urls


class Lane:
    """Один «выход в интернет»: прокси или прямое соединение. У каждого свой лимит скорости
    и своя авто-пауза: сайт ограничивает каждый IP отдельно."""

    def __init__(self, url: str):
        self.url = url
        self.next_slot = 0.0
        self.pause_until = 0.0
        self.soft_until = 0.0
        self.streak = 0
        self.level = 0
        self.requests = 0
        self.blocked = 0
        self.errors = 0
        self.latency = 1.0          # среднее время ответа, сек

    @property
    def label(self) -> str:
        return mask_proxy(self.url) if self.url else "без прокси"


class Fetcher:
    def __init__(self, cfg: dict, notify: Callable[[str], None] = lambda _m: None):
        self.cfg = cfg
        self.notify = notify
        self._lock = threading.Lock()
        self._local = threading.local()
        self._gen = 0                   # меняется при смене прокси -> сессии пересоздаются
        self.lanes = [Lane(u) for u in proxy_list(cfg) or [""]]
        self.stats = {"requests": 0, "ok": 0, "missing": 0, "blocked": 0, "errors": 0}
        self.error_kinds: dict[str, int] = {}   # почему ID не открылся (после всех повторов)
        self._done = deque(maxlen=20000)        # время каждого ответа — для реальной скорости

    def current_rate(self, window: float = 60.0) -> float:
        """Сколько запросов в секунду реально делается сейчас (за последнюю минуту)."""
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._done if now - t <= window]
        if len(recent) < 2:
            return 0.0
        return len(recent) / max(1.0, min(window, now - min(recent)))

    def _fail(self, kind: str, lane: Optional[Lane]) -> None:
        with self._lock:
            self.stats["errors"] += 1
            self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1
            if lane is not None:
                lane.errors += 1

    def errors_summary(self, top: int = 4) -> str:
        with self._lock:
            items = sorted(self.error_kinds.items(), key=lambda x: -x[1])[:top]
        return ", ".join(f"{k} — {v}" for k, v in items)

    def set_proxies(self, urls: list[str]) -> None:
        with self._lock:
            self.lanes = [Lane(u) for u in urls or [""]]
            self._gen += 1

    def set_proxy(self, url: str) -> None:   # совместимость
        self.set_proxies([url] if url else [])

    @property
    def total_rps(self) -> float:
        return float(self.cfg["requests_per_second"]) * len(self.lanes)

    def worker_count(self) -> int:
        """Сколько потоков нужно, чтобы реально выйти на лимит скорости.
        Поток ждёт ответа ~latency сек, поэтому нужно rps × latency потоков (+ запас)."""
        latency = max(0.5, max(lane.latency for lane in self.lanes))
        return int(min(64, max(4, self.total_rps * latency * 2)))

    # --- сессии: своя на каждый поток и прокси ---
    def _session(self, lane: Lane) -> requests.Session:
        sessions = getattr(self._local, "sessions", None)
        if sessions is None or getattr(self._local, "gen", -1) != self._gen:
            sessions = self._local.sessions = {}
            self._local.gen = self._gen
        s = sessions.get(lane.url)
        if s is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
            })
            if lane.url:
                s.proxies.update({"http": lane.url, "https": lane.url})
            sessions[lane.url] = s
        return s

    # --- лимит скорости: выбираем прокси, который освободится раньше всех ---
    def _acquire(self, stop: Optional[threading.Event]) -> Optional[Lane]:
        with self._lock:
            now = time.monotonic()
            rps = float(self.cfg["requests_per_second"])
            best, best_start = None, None
            for lane in self.lanes:
                start = max(now, lane.next_slot, lane.pause_until)
                if best is None or start < best_start:
                    best, best_start = lane, start
            lane_rps = max(1.0, rps / 3) if now < best.soft_until else rps
            best.next_slot = best_start + (1.0 / lane_rps) * random.uniform(0.85, 1.15)
        delay = best_start - time.monotonic()
        if delay > 0:
            if stop is not None:
                return None if stop.wait(delay) else best
            time.sleep(delay)
        return best

    def paused_for(self) -> int:
        """Сколько секунд ещё ждать, если на паузе ВСЕ прокси (иначе 0)."""
        now = time.monotonic()
        return max(0, int(min(lane.pause_until for lane in self.lanes) - now))

    def lanes_info(self) -> list[str]:
        now = time.monotonic()
        out = []
        for lane in self.lanes:
            st = f"⛔ пауза {int(lane.pause_until - now) // 60 + 1} мин" if lane.pause_until > now else "✅"
            out.append(f"{lane.label}: {st}, запросов {lane.requests:,}, блок. {lane.blocked}, "
                       f"ошибок {lane.errors}, ответ ~{lane.latency:.1f} с")
        return out

    def _on_success(self, lane: Lane, latency: float) -> None:
        with self._lock:
            lane.streak = 0
            lane.latency = lane.latency * 0.9 + latency * 0.1

    def _on_blocked(self, lane: Lane) -> None:
        with self._lock:
            self.stats["blocked"] += 1
            lane.blocked += 1
            lane.streak += 1
            if lane.streak < self.cfg["block_threshold"]:
                return
            minutes = self.cfg["block_pause_minutes"] * (2 ** min(lane.level, 3))
            lane.pause_until = time.monotonic() + minutes * 60
            lane.soft_until = lane.pause_until + 180
            lane.level += 1
            lane.streak = 0
            others = sum(1 for x in self.lanes if x is not lane and x.pause_until <= time.monotonic())
        log.warning("Блокировка на %s, пауза %s мин", lane.label, minutes)
        tail = f" Остальные прокси ({others}) продолжают работать." if others else \
            " Потом продолжу на пониженной скорости, ничего делать не нужно."
        self.notify(f"⛔ Сайт режет запросы через {lane.label} (403/429). Пауза для него {minutes} мин.{tail}")

    supports_light = True     # умеет get(..., listings=False) — для перебора

    def get(self, user_id: int, stop: Optional[threading.Event] = None, retries: Optional[int] = None,
            listings: bool = True) -> FetchResult:
        url = PROFILE_URL.format(uid=user_id)
        last = FetchResult("error", error="no attempts")
        for attempt in range((self.cfg["max_retries"] if retries is None else retries) + 1):
            if stop is not None and stop.is_set():
                return FetchResult("error", error="stopped")
            lane = self._acquire(stop)
            if lane is None:
                return FetchResult("error", error="stopped")
            blocked = False
            t0 = time.monotonic()
            try:
                resp = self._session(lane).get(url, timeout=self.cfg["request_timeout"])
                elapsed = time.monotonic() - t0
                with self._lock:
                    self.stats["requests"] += 1
                    lane.requests += 1
                    self._done.append(time.monotonic())
                code = resp.status_code
                if code in (404, 410):
                    self._on_success(lane, elapsed)
                    with self._lock:
                        self.stats["missing"] += 1
                    return FetchResult("missing", http_code=code)
                if code == 200:
                    body = resp.text
                    if looks_like_profile(body):
                        try:
                            prof = parse_profile_html(body, user_id, listings=listings)
                        except Exception as exc:   # кривая страница — ошибка одного ID, а не всего поиска
                            log.warning("Не удалось разобрать страницу %s: %s: %s", user_id,
                                        type(exc).__name__, str(exc)[:100])
                            self._fail("страница не разобралась", lane)
                            return FetchResult("error", http_code=code, error=f"parse {type(exc).__name__}")
                        if prof.page_user_id not in (None, user_id):
                            prof.name = prof.aktiv_seit = None   # редирект на чужой профиль
                        if prof.aktiv_seit or prof.name:
                            self._on_success(lane, elapsed)
                            with self._lock:
                                self.stats["ok"] += 1
                            return FetchResult("ok", profile=prof, http_code=code)
                    if looks_blocked(body):
                        blocked = True
                        last = FetchResult("error", http_code=code, error="block page")
                    else:
                        self._on_success(lane, elapsed)
                        with self._lock:
                            self.stats["missing"] += 1
                        return FetchResult("missing", http_code=code)
                elif code in (403, 429):
                    blocked = True
                    last = FetchResult("error", http_code=code, error=f"HTTP {code}")
                else:
                    last = FetchResult("error", http_code=code, error=f"HTTP {code}")
            except Exception as exc:   # requests / socks / ssl — всё считаем сетевой ошибкой
                with self._lock:
                    self._done.append(time.monotonic())
                last = FetchResult("error", error=type(exc).__name__)
            if blocked:
                self._on_blocked(lane)
            backoff = min(30.0, 1.5 * (2 ** attempt)) * random.uniform(0.7, 1.3)
            if stop is not None:
                if stop.wait(backoff):
                    return FetchResult("error", error="stopped")
            else:
                time.sleep(backoff)
        self._fail(ERROR_RU.get(last.error, last.error or "неизвестно"), lane)
        return last


ERROR_RU = {   # понятные названия частых ошибок
    "ReadTimeout": "прокси/сайт не ответил вовремя", "ConnectTimeout": "не подключиться к прокси",
    "ProxyError": "ошибка прокси", "ConnectionError": "обрыв связи", "SSLError": "ошибка SSL",
    "ChunkedEncodingError": "оборванный ответ", "HTTP 403": "блокировка 403", "HTTP 429": "блокировка 429",
    "block page": "страница-капча", "HTTP 500": "сайт: 500", "HTTP 502": "сайт: 502", "HTTP 503": "сайт: 503",
    "HTTP 504": "сайт: 504",
}


# =====================================================================
# БАЗА ЯКОРЕЙ (user_id -> дата регистрации), сама пополняется
# =====================================================================
LEGACY_ANCHOR_FILES = (BASE_DIR / "known_accounts", BASE_DIR / "known_accounts.txt")


class AnchorStore:
    """База дат «ID → дата регистрации». Хранится ОДНА запись на дату: для прыжков этого хватает,
    лишние записи рядом ничего не дают. Новые записи дописываются в path, база из репозитория
    (extra_files) только читается."""

    def __init__(self, path: Path, seeds: Iterable = (), extra_files: Iterable[Path] = ()):
        self.path = path
        self._lock = threading.Lock()
        self._by_day: dict[int, int] = {}      # ordinal даты -> user_id
        self._pending: list[str] = []
        base: list[tuple[int, date]] = []
        for uid, d in seeds:
            pd = parse_date(str(d))
            if pd:
                base.append((int(uid), pd))
        for p in extra_files:
            base += self._read(p)
        for uid, d in base:
            self._by_day.setdefault(d.toordinal(), uid)
        own = self._read(path)
        kept = []
        for uid, d in own:
            if d.toordinal() not in self._by_day:
                self._by_day[d.toordinal()] = uid
                kept.append((uid, d))
        if len(kept) != len(own):              # сжать файл: по одной записи на дату
            self._rewrite(kept)
            log.info("База дат сжата: %d → %d записей", len(own), len(kept))

    @staticmethod
    def _read(p: Path) -> list[tuple[int, date]]:
        out = []
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                if ";" in line:
                    uid, d = line.split(";", 1)
                    pd = parse_date(d)
                    if uid.strip().isdigit() and pd:
                        out.append((int(uid.strip()), pd))
        return out

    def _rewrite(self, rows: list[tuple[int, date]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(f"{u};{fmt_date(d)}\n" for u, d in sorted(rows)), encoding="utf-8")
        tmp.replace(self.path)

    def __len__(self) -> int:
        return len(self._by_day)

    def has_day(self, d: date) -> bool:
        return d.toordinal() in self._by_day

    def add(self, uid: int, d: date) -> bool:
        """Запоминает дату, если для неё ещё нет записи. True — если добавлено."""
        with self._lock:
            if d.toordinal() in self._by_day:
                return False
            self._by_day[d.toordinal()] = uid
            self._pending.append(f"{uid};{fmt_date(d)}")
            self._flush_locked()
            return True

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(self._pending) + "\n")
        self._pending.clear()

    def items(self) -> list[tuple[int, date]]:
        with self._lock:
            return sorted((uid, date.fromordinal(day)) for day, uid in self._by_day.items())


# =====================================================================
# ЭТАП 1+2: ПОИСК ДИАПАЗОНА ID ДЛЯ ДАТЫ
# =====================================================================
class LocateError(Exception):
    pass


@dataclass
class Point:
    uid: int
    day: Optional[int]   # date.toordinal(); None = «за краем» (ID ещё не существует / <1)


class RangeLocator:
    _ERROR = object()

    def __init__(self, fetcher, anchors: AnchorStore, cfg: dict,
                 stop: Optional[threading.Event] = None, progress: Callable[[str], None] = lambda _m: None):
        self.fetcher = fetcher
        self.anchors = anchors
        self.cfg = cfg
        self.stop = stop or threading.Event()
        self.progress = progress
        self.cache: dict[int, Optional[int]] = {}
        self.requests = 0

    # --- запросы ---
    def _budget_left(self) -> bool:
        return self.requests < self.cfg["locate_max_requests"]

    def probe(self, uid: int):
        if uid in self.cache:
            return self.cache[uid]
        if self.stop.is_set():
            raise LocateError("остановлено")
        self.requests += 1
        res = self.fetcher.get(uid, self.stop)
        if res.status == "ok" and res.profile and res.profile.aktiv_seit:
            day = res.profile.aktiv_seit.toordinal()
            self.cache[uid] = day
            return day
        if res.status in ("ok", "missing"):
            self.cache[uid] = None
            return None
        if res.error == "stopped":
            raise LocateError("остановлено")
        return self._ERROR

    def nearest(self, center: int, lo: int, hi: int, max_probes: int = 24) -> Optional[Point]:
        """Ближайший существующий профиль к center строго внутри (lo, hi)."""
        offsets, step = [0], 1
        while len(offsets) < 4 * max_probes:
            offsets += [step, -step]
            step = step + 1 if step < 4 else int(step * 1.5) + 1
        probes = 0
        for off in offsets:
            if probes >= max_probes or not self._budget_left():
                break
            if center - abs(off) <= lo and center + abs(off) >= hi:
                break   # весь интервал уже просмотрен
            uid = center + off
            if not (lo < uid < hi) or uid < 1:
                continue
            if uid not in self.cache:
                probes += 1
            day = self.probe(uid)
            if isinstance(day, int):
                return Point(uid, day)
        return None

    # --- оценка «ID в день» по якорям ---
    def _rate(self, pts: list[tuple[int, int]]) -> float:
        rates = []
        for (u1, d1), (u2, d2) in zip(pts, pts[1:]):
            if d2 - d1 >= 3 and u2 > u1:
                rates.append((u2 - u1) / (d2 - d1))
        if not rates:
            return float(self.cfg["users_per_day"])
        rates.sort()
        return max(100.0, rates[len(rates) // 2])

    def _gallop(self, ref: Point, t: int, direction: int, rate: float) -> Point:
        """Прыгаем от ref в сторону direction, пока не перепрыгнем дату t.

        Если прыжок попал в «пустоту» (ID ещё не выданы / все удалены) —
        возвращаемся делением пополам к последней известной точке.
        Скорость «ID в день» уточняется по реально увиденным точкам."""
        mult = 1.2
        cur = ref
        for _ in range(60):
            if not self._budget_left():
                raise LocateError("закончился лимит запросов на поиск диапазона")
            days = abs(t - cur.day)
            jump = int(max(days, 1) * rate * mult) + 1
            cand = max(1, cur.uid + direction * jump)
            lo, hi = (cur.uid, cand + jump) if direction > 0 else (max(0, cand - jump), cur.uid)
            p = self.nearest(cand, lo, hi)
            a = cand
            while p is None and abs(a - cur.uid) > 50 and self._budget_left():
                a = (a + cur.uid) // 2
                d = abs(a - cur.uid)
                p = self.nearest(a, cur.uid, a + d) if direction > 0 else self.nearest(a, a - d, cur.uid)
            self.progress(f"прыжок → ID {cand}: {fmt_date(date.fromordinal(p.day)) if p else 'пусто'}")
            if p is None:
                return Point(cand, None) if direction > 0 else Point(0, None)
            if abs(p.day - cur.day) >= 3:
                rate = max(50.0, abs(p.uid - cur.uid) / abs(p.day - cur.day))
                mult = 1.2
            else:
                mult *= 2
            if (direction < 0 and p.day < t) or (direction > 0 and p.day > t):
                return p
            if p.day == t:
                self._eq.append(p)
            cur = p
        raise LocateError("не удалось перепрыгнуть дату за 60 прыжков")

    def _narrow(self, left: Point, right: Point, crossing: int, is_right: Callable[[int], bool]) -> tuple[Point, Point]:
        """Сужает (left, right): left — «до» границы, right — «после».
        Точка с day=None — край базы (ID ещё/уже не существуют)."""
        precision = max(1, int(self.cfg["boundary_precision"]))
        while right.uid - left.uid > precision and self._budget_left():
            span = right.uid - left.uid
            mid = left.uid + span // 2
            guess = mid
            if left.day is not None and right.day is not None and right.day - left.day > 1:
                frac = (crossing - left.day - 0.5) / (right.day - left.day)
                guess = left.uid + int(span * frac)
            margin = span // 8 if span >= 16 else 1
            guess = min(max(guess, left.uid + margin), right.uid - margin)
            p = self.nearest(guess, left.uid, right.uid)
            if p is None and guess != mid:
                p = self.nearest(mid, left.uid, right.uid)
            if p is None:
                if right.day is None:
                    right = Point(mid, None)      # пустота у верхнего края — это и есть край
                    continue
                if left.day is None:
                    left = Point(mid, None)
                    continue
                break   # внутри только пустые ID — просто переберём остаток
            if is_right(p.day):
                right = p
            else:
                left = p
            self.progress(f"границы {left.uid}..{right.uid}")
        return left, right

    def _known_eq(self, t: int) -> list[Point]:
        pts = {p.uid: p for p in self._eq}
        pts.update({u: Point(u, d) for u, d in self.cache.items() if d == t})
        return list(pts.values())

    def locate(self, target: date) -> tuple[int, int]:
        t = target.toordinal()
        self._eq: list[Point] = []
        pts = [(u, d.toordinal()) for u, d in self.anchors.items()]
        if not pts:
            raise LocateError("база якорей пуста — добавь /anchor <user_id> <дд.мм.гггг>")
        below = [Point(u, d) for u, d in pts if d < t]
        above = [Point(u, d) for u, d in pts if d > t]
        self._eq = [Point(u, d) for u, d in pts if d == t]
        rate = self._rate(sorted(pts))
        lo = max(below, key=lambda p: (p.day, p.uid)) if below else None
        hi = min(above, key=lambda p: (p.day, p.uid)) if above else None
        if lo and hi and lo.uid >= hi.uid:   # якоря противоречат друг другу — не доверяем
            lo = hi = None
        ref = (self._eq[0] if self._eq else None) or lo or hi or Point(*min(pts, key=lambda x: abs(x[1] - t)))
        # проверяем живым запросом опорную точку (на сайте дата могла «уехать»)
        day = self.probe(ref.uid)
        if isinstance(day, int):
            ref = Point(ref.uid, day)
        if lo is None:
            lo = ref if ref.day is not None and ref.day < t else self._gallop(ref, t, -1, rate)
        if hi is None:
            hi = ref if ref.day is not None and ref.day > t else self._gallop(ref if ref.uid > lo.uid else lo, t, +1, rate)
        if lo.uid >= hi.uid:
            raise LocateError(f"нарушена монотонность ID/дат около {lo.uid}..{hi.uid}")

        # FIRST: lo.day < t <= right.day
        eq_inside = [p for p in self._known_eq(t) if lo.uid < p.uid < hi.uid]
        right = min(eq_inside, key=lambda p: p.uid) if eq_inside else hi
        l1, r1 = self._narrow(lo, right, t, lambda d: d >= t)
        # LAST: left.day <= t < right.day
        right2 = r1 if (r1.day is not None and r1.day > t) else hi
        eq_inside = [p for p in self._known_eq(t) if r1.uid <= p.uid < right2.uid]
        left = max(eq_inside, key=lambda p: p.uid) if eq_inside else l1
        l2, r2 = self._narrow(left, right2, t + 1, lambda d: d > t)
        self.anchors.flush()

        first = max(1, l1.uid + 1 - int(self.cfg["range_margin"]))
        last = r2.uid - 1 + int(self.cfg["range_margin"])
        if last < first:
            raise LocateError("в эту дату не найдено ни одного ID")
        if last - first + 1 > self.cfg["max_scan_ids"]:
            raise LocateError(f"диапазон слишком большой ({last - first + 1:,} ID) — "
                              f"добавь якорь ближе к дате через /anchor")
        return first, last


# =====================================================================
# ЭТАП 3: СРАВНЕНИЕ ПРОФИЛЯ С ЦЕЛЬЮ
# =====================================================================
@dataclass
class Target:
    name: str
    aktiv_seit: date
    badges: dict = field(default_factory=dict)
    followers: Optional[int] = None
    online_ads: Optional[int] = None
    total_ads: Optional[int] = None
    note: str = ""                       # заметка: чей это аккаунт

    def to_dict(self) -> dict:
        d = asdict(self)
        d["aktiv_seit"] = fmt_date(self.aktiv_seit)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Target":
        return cls(name=d["name"], aktiv_seit=parse_date(d["aktiv_seit"]), badges=d.get("badges") or {},
                   followers=d.get("followers"), online_ads=d.get("online_ads"), total_ads=d.get("total_ads"),
                   note=d.get("note") or "")

    def summary(self) -> str:
        lines = [f"Имя: {self.name}", f"Дата регистрации: {fmt_date(self.aktiv_seit)}",
                 f"Плашки: {badges_str(self.badges)}"]
        if self.followers is not None:
            lines.append(f"Подписчики: {self.followers}")
        if self.online_ads is not None:
            lines.append(f"Объявления онлайн: {self.online_ads}")
        if self.note:
            lines.append(f"Заметка: {self.note}")
        return "\n".join(lines)


def _norm_title(title: Optional[str]) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", title or "")).strip().casefold()


def note_suffix(note: Optional[str]) -> str:
    return f"\n📝 {note}" if note else ""


@dataclass
class Match:
    level: str           # "exact" | "partial" | "conflict"
    user_id: int
    name: str
    notes: list = field(default_factory=list)

    @property
    def url(self) -> str:
        return PROFILE_URL.format(uid=self.user_id)


def match_profile(target: Target, prof: Profile, fuzzy_threshold: float = 0.85) -> Optional[Match]:
    if not prof.name:
        return None
    notes: list[str] = []
    if names_equal(target.name, prof.name):
        name_exact = True
    else:
        sim = name_similarity(target.name, prof.name)
        if sim < fuzzy_threshold:
            return None
        name_exact = False
        notes.append(f"имя похоже ({sim:.0%}): «{prof.name}»")
    if prof.aktiv_seit and prof.aktiv_seit != target.aktiv_seit:
        return None
    conflict = unknown = False
    for cat, want in target.badges.items():
        have = prof.badges.get(cat)
        if have is None:
            unknown = True
            notes.append(f"{CATEGORY_RU[cat]}: на странице нет плашки (ждали {want})")
        elif have != want:
            conflict = True
            notes.append(f"{CATEGORY_RU[cat]}: на странице {have}, ждали {want}")
    for attr, label in (("followers", "подписчики"), ("online_ads", "объявл. онлайн")):
        want, have = getattr(target, attr), getattr(prof, attr)
        if want is not None and have is not None and want != have:
            notes.append(f"{label}: на странице {have}, ждали {want} (могло измениться)")
    if conflict:
        level = "conflict"
    elif name_exact and not unknown:
        level = "exact"
    else:
        level = "partial"
    return Match(level, prof.user_id, prof.name, notes)


# =====================================================================
# ЗАДАЧА ПОИСКА (состояние сохраняется -> можно продолжить после рестарта)
# =====================================================================
@dataclass
class Job:
    target: dict
    status: str = "locating"     # locating | scanning | paused | done | stopped | failed
    first: Optional[int] = None
    last: Optional[int] = None
    cursor: Optional[int] = None
    checked: int = 0
    profiles: int = 0
    missing: int = 0
    error_ids: list = field(default_factory=list)
    matches: list = field(default_factory=list)   # dict(Match)
    sample: Optional[int] = None                   # любой профиль этой даты — для базы дат
    started: float = field(default_factory=time.time)
    message: str = ""

    @property
    def total(self) -> int:
        return (self.last - self.first + 1) if self.first is not None and self.last is not None else 0

    def has_exact(self) -> bool:
        return any(m["level"] == "exact" for m in self.matches)


class Scanner:
    def __init__(self, fetcher, cfg: dict, stop: threading.Event, pause: threading.Event,
                 on_match: Callable[[Match], None], save: Callable[[], None],
                 share: Callable[[], int] = lambda: 1):
        self.fetcher = fetcher
        self.cfg = cfg
        self.stop = stop
        self.pause = pause
        self.on_match = on_match
        self.save = save
        self.share = share           # сколько поисков сейчас делят потоки

    def _workers(self) -> int:
        wc = getattr(self.fetcher, "worker_count", None)
        return wc() if wc else max(1, int(self.cfg.get("workers") or 6))

    def _wait_if_paused(self) -> None:
        while self.pause.is_set() and not self.stop.is_set():
            time.sleep(0.5)

    def _submit(self, ids: list[int], pool: ThreadPoolExecutor) -> list:
        if getattr(self.fetcher, "supports_light", False):
            return [(uid, pool.submit(self.fetcher.get, uid, self.stop, None, False)) for uid in ids]
        return [(uid, pool.submit(self.fetcher.get, uid, self.stop)) for uid in ids]

    def _collect(self, job: Job, target: Target, batch: list) -> bool:
        """Обрабатывает пачку. Возвращает False, если поиск остановили посреди пачки."""
        results = []
        for uid, item in batch:
            results.append((uid, item.result()))
        if any(r.error == "stopped" for _, r in results):
            return False
        for uid, res in results:
            if res.status == "ok" and res.profile:
                job.profiles += 1
                if job.sample is None and res.profile.aktiv_seit == target.aktiv_seit:
                    job.sample = uid
                m = match_profile(target, res.profile, self.cfg["name_fuzzy_threshold"])
                if m and not any(x["user_id"] == uid for x in job.matches):
                    job.matches.append(asdict(m))
                    self.on_match(m)
            elif res.status == "missing":
                job.missing += 1
            else:
                if uid not in job.error_ids:
                    job.error_ids.append(uid)
                continue
            if uid in job.error_ids:
                job.error_ids.remove(uid)
            job.checked += 1
        return True

    def _run_chunks(self, job: Job, target: Target, chunks: Iterable[list[int]], pool: ThreadPoolExecutor,
                    advance_cursor: bool) -> bool:
        """Конвейер: следующая пачка уже качается, пока обрабатывается текущая —
        один медленный запрос не останавливает остальные потоки."""
        pending: list = []
        chunks = iter(chunks)
        # сколько ID держать «в работе»: с запасом на зависшие запросы (таймаут прокси, повтор с паузой)
        depth = max(2 * int(self.cfg["chunk_size"]), self._workers() * 6 // max(1, self.share()))
        try:
            while True:
                while (len(pending) < 2 or sum(len(i) for i, _ in pending) < depth) and not self.stop.is_set():
                    ids = next(chunks, None)
                    if ids is None:
                        break
                    pending.append((ids, self._submit(ids, pool)))
                if not pending:
                    return not self.stop.is_set()
                ids, batch = pending.pop(0)
                if not self._collect(job, target, batch):
                    return False
                if advance_cursor:
                    job.cursor = ids[-1] + 1
                self.save()
                self._wait_if_paused()
                if self.stop.is_set():
                    return False
        finally:
            for _, batch in pending:             # недоделанные пачки отменяем
                for _, item in batch:
                    item.cancel()

    def run(self, job: Job, pool: Optional[ThreadPoolExecutor] = None) -> None:
        """Перебирает ВЕСЬ диапазон (не останавливается на первом совпадении).
        pool — общий пул потоков, если поисков несколько."""
        if pool is None:
            with ThreadPoolExecutor(max_workers=self._workers()) as own:
                return self.run(job, own)
        target = Target.from_dict(job.target)
        size = max(1, int(self.cfg["chunk_size"]))
        if job.cursor is None:
            job.cursor = job.first
        chunks = (list(range(c, min(c + size - 1, job.last) + 1)) for c in range(job.cursor, job.last + 1, size))
        self._run_chunks(job, target, chunks, pool, advance_cursor=True)
        # ID, которые не открылись, остаются в job.error_ids — их перепроверяют потом
        # медленно и отдельно (кнопка «🔁 Перепроверить»), чтобы не нарываться на блокировки


# =====================================================================
# ВВОД ЦЕЛИ: текст и OCR
# =====================================================================
FIELD_KEYS = {
    "name": ("имя", "name", "ник", "nick"),
    "date": ("дата регистрации", "дата", "aktiv seit", "date", "регистрация"),
    "badges": ("плашки", "плашка", "badges", "бейджи", "значки"),
    "followers": ("подписчики", "follower", "followers"),
    "online_ads": ("объявления онлайн", "объявления", "anzeigen online", "anzeigen"),
    "note": ("заметка", "note", "комментарий", "чей", "подпись"),
}
EMPTY_VALUES = {"", "-", "—", "нет", "не найдено", "не найдены", "none"}


def _opt_int(value: Optional[str]) -> Optional[int]:
    if value is None or value.strip().lower() in EMPTY_VALUES:
        return None
    m = re.search(r"\d+", value)
    return int(m.group(0)) if m else None


def _opt_badges(value: Optional[str]) -> dict:
    if value is None or value.strip().lower() in EMPTY_VALUES:
        return {}
    return detect_badges(value, shorthand=True)


def parse_target_text(text: str) -> Optional[Target]:
    """Понимает три формата:
    1) «Имя: X / Дата регистрации: dd.mm.yyyy / Плашки: ...» (как в ответе бота);
    2) построчно: имя, дата, плашки, подписчики, объявления онлайн;
    3) одной строкой: «Manuela 08.02.2017 TOP, besonders freundlich».
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    fields: dict[str, str] = {}
    for ln in lines:
        if ":" not in ln:
            continue
        key, value = (p.strip() for p in ln.split(":", 1))
        k = key.lower()
        for fname, keys in FIELD_KEYS.items():
            if k in keys and fname not in fields:
                fields[fname] = value
                break
    if "name" in fields and "date" in fields:
        d = parse_date(fields["date"])
        if not d or fields["name"].lower() in EMPTY_VALUES:
            return None
        return Target(name=fields["name"], aktiv_seit=d, badges=_opt_badges(fields.get("badges")),
                      followers=_opt_int(fields.get("followers")), online_ads=_opt_int(fields.get("online_ads")),
                      note=(fields.get("note") or "").strip()[:200])
    if len(lines) >= 2 and parse_date(lines[1]) and not parse_date(lines[0]):
        rest = lines[2:] + [None] * 3
        return Target(name=lines[0], aktiv_seit=parse_date(lines[1]), badges=_opt_badges(rest[0]),
                      followers=_opt_int(rest[1]), online_ads=_opt_int(rest[2]))
    if len(lines) == 1:
        m = DATE_RE.search(lines[0])
        if m and m.start() > 0:
            name = lines[0][:m.start()].strip(" ,;-")
            d = parse_date(m.group(0))
            if name and d:
                return Target(name=name, aktiv_seit=d, badges=_opt_badges(lines[0][m.end():]))
    return None


IGNORE_NAME_WORDS = ("privater nutzer", "gewerblicher", "aktiv seit", "anzeigen", "gesamt", "follower",
                     "folge", "antwortet", "verkaufs", "sicher bezahlen", "kleinanzeigen", "einnahmen",
                     "bezahlsystem", "immobilien", "zufriedenheit", "freundlich", "zuverl", "profil",
                     "bestandsliste", "nachricht", "präsentiere", "prasentiere")
BADGE_WORDS = {"top", "ok", "naja", "zufriedenheit", "besonders", "sehr", "freundlich", "zuverlassig"}
INITIALS_RE = re.compile(r"^[A-ZÄÖÜ]{1,3}$")


def _name_candidate(raw: str) -> Optional[str]:
    line = re.sub(r"[©®™>|«»\[\]{}]", " ", raw)
    line = re.sub(r"\s+", " ", line).strip(" -_.,:;=")
    line = re.sub(r"^[A-ZÄÖÜ]{1,3}\s+(?=[A-ZÄÖÜ][a-zäöüß])", "", line)   # «AS Arne …» — инициалы аватарки
    low = line.lower()
    if len(line) < 2 or len(line) > 40 or INITIALS_RE.match(line):
        return None
    if any(w in low for w in IGNORE_NAME_WORDS) or DATE_RE.search(line):
        return None
    if not re.search(r"[A-Za-zÄÖÜäöüß]", line):
        return None
    words = re.findall(r"[^\W\d_]+", _badge_fold(line))
    if words and all(w in BADGE_WORDS for w in words):
        return None   # «Besonders», «TOP», «Sehr» — обрывки плашек
    return line


def parse_ocr_text(text: str) -> tuple[Optional[str], Optional[date], dict, Optional[int], Optional[int]]:
    m = AKTIV_RE.search(text)
    d = parse_date(m.group(1)) if m else parse_date(text)
    badges = detect_badges(text)
    followers = _int_after(r"(\d+)\s+Follower", text)
    online = _int_after(r"(\d+)\s+Anzeigen?\s+online", text)
    lines = text.splitlines()
    # имя — последняя осмысленная строка перед «N Anzeigen online» (так на всех скринах профиля)
    idx = next((i for i, ln in enumerate(lines) if re.search(r"Anzeigen?\s+online", ln, re.I)), None)
    name = None
    if idx is not None:
        for raw in reversed(lines[:idx]):
            name = _name_candidate(raw)
            if name:
                break
    if not name:
        for raw in lines:
            name = _name_candidate(raw)
            if name and not re.search(r"\d", name):
                break
            name = None
    return name, d, badges, followers, online


def find_tesseract(tesseract_cmd: str = "") -> Optional[str]:
    """Путь к tesseract: из настроек, из PATH или из стандартных папок Windows."""
    import shutil
    if tesseract_cmd:
        return tesseract_cmd
    found = shutil.which("tesseract")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        candidate = Path(base) / "Tesseract-OCR" / "tesseract.exe"
        if base and candidate.exists():
            return str(candidate)
    return None


def ocr_image(image_bytes: bytes, tesseract_cmd: str = "") -> str:
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("OCR не установлен: pip install pytesseract pillow и установи Tesseract-OCR "
                           "(или отправь данные текстом)") from exc
    cmd = find_tesseract(tesseract_cmd)
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    img = ImageOps.grayscale(Image.open(io.BytesIO(image_bytes)))
    if img.width < 1600:
        k = 1600 / img.width
        img = img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)
    try:
        return pytesseract.image_to_string(img, lang="deu+eng")
    except pytesseract.TesseractError:
        return pytesseract.image_to_string(img)   # нет немецкого словаря
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError("Tesseract не найден. Укажи tesseract_cmd в config.json "
                           "или отправь данные текстом") from exc


# =====================================================================
# TELEGRAM
# =====================================================================
class TelegramAPI:
    def __init__(self, token: str):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"
        self.http = requests.Session()   # без прокси сайта: Telegram ходит напрямую

    def call(self, method: str, http_timeout: int = 30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(4):
            try:
                r = self.http.post(self.base + method, json=params, timeout=http_timeout)
                data = r.json()
                if data.get("ok"):
                    return data["result"]
                retry = (data.get("parameters") or {}).get("retry_after")
                if retry:
                    time.sleep(retry + 1)
                    continue
                desc = str(data.get("description", ""))
                if "message is not modified" not in desc:
                    log.warning("Telegram %s: %s", method, desc)
                return None
            except (requests.RequestException, ValueError) as exc:
                log.warning("Telegram %s: %s (попытка %d)", method,
                            str(exc).replace(self.token, "<TOKEN>"), attempt + 1)   # токен не пишем в лог
                time.sleep(2 * (attempt + 1))
        return None

    MAX_BUTTONS = 100     # лимит Telegram на одно сообщение

    @classmethod
    def _fit(cls, buttons: Optional[list]) -> Optional[list]:
        """Обрезает кнопки до лимита Telegram (иначе сообщение отклоняется целиком)."""
        if not buttons:
            return buttons
        out, total = [], 0
        for row in buttons:
            if total + len(row) > cls.MAX_BUTTONS:
                log.warning("Слишком много кнопок (%d) — лишние обрезаны", sum(len(r) for r in buttons))
                break
            out.append(row)
            total += len(row)
        return out

    def send(self, chat_id: str, text: str, buttons: Optional[list] = None,
             keyboard: Optional[dict] = None) -> Optional[int]:
        """Отправляет сообщение (длинное — частями). buttons — inline-кнопки под последней частью,
        keyboard — нижнее меню. Возвращает message_id последней части."""
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
        msg_id = None
        for n, chunk in enumerate(chunks):
            params = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
            if n == len(chunks) - 1:
                if buttons:
                    params["reply_markup"] = {"inline_keyboard": self._fit(buttons)}
                elif keyboard:
                    params["reply_markup"] = keyboard
            res = self.call("sendMessage", **params)
            if res is None and "reply_markup" in params:      # отклонили из-за кнопок — шлём хотя бы текст
                params.pop("reply_markup")
                res = self.call("sendMessage", **params)
            if res:
                msg_id = res.get("message_id")
        return msg_id

    def edit(self, chat_id: str, message_id: int, text: str, buttons: Optional[list] = None) -> None:
        self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4000],
                  disable_web_page_preview=True, reply_markup={"inline_keyboard": self._fit(buttons) or []})

    def delete(self, chat_id: str, message_id: int) -> None:
        self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    def download(self, file_id: str) -> bytes:
        meta = self.call("getFile", file_id=file_id)
        if not meta:
            raise RuntimeError("не удалось получить файл из Telegram")
        r = self.http.get(self.file_base + meta["file_path"], timeout=60)
        r.raise_for_status()
        return r.content


# --- главное меню (нижняя клавиатура) ---
M_SEARCH, M_TASKS, M_STATUS = "🔍 Поиск по имени", "📋 Задачи", "📊 Статус"
M_RESULTS, M_MONITOR, M_PROXY = "🏆 Результаты", "👁 Мониторинг", "🌐 Прокси"
MAIN_KEYBOARD = {
    "keyboard": [[{"text": M_SEARCH}, {"text": M_TASKS}],
                 [{"text": M_STATUS}, {"text": M_RESULTS}],
                 [{"text": M_MONITOR}, {"text": M_PROXY}]],
    "resize_keyboard": True,
    "is_persistent": True,
}

HELP = f"""🔎 Поиск пользователя Kleinanzeigen

Всё управляется кнопками внизу:
{M_SEARCH} — ввести имя, выбрать дату и плашки кнопками
{M_TASKS} — поиски в работе (⏸/⏹ для каждого), 🧺 корзина, перепроверка ошибок
{M_STATUS} — прогресс и скорость
{M_RESULTS} — что найдено в идущих поисках
{M_MONITOR} — слежка за новыми объявлениями, 📝 заметки «чей аккаунт»
{M_PROXY} — прокси и скорость запросов

📸 В любой момент можно прислать СКРИНШОТ профиля (можно сразу несколько) —
каждый станет отдельным поиском: 3 идут одновременно, остальные ждут в корзине (до 15)
и запускаются сами, как только освобождается место.
Поиск перебирает день до конца и сообщает о КАЖДОМ совпадении.
Уверен, что нашёлся нужный? Жми «✅ Это он» под сообщением — поиск остановится.
📝 На карточке перед поиском можно добавить заметку (чей аккаунт) — она будет
в каждом уведомлении поиска и мониторинга.
Текстом тоже можно: Manuela 08.02.2017 top, sehr freundlich"""

MONTHS_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
BADGE_PICKER = {   # код категории -> (категория, [(ключ уровня, подпись кнопки)])
    "s": ("satisfaction", [("top", "TOP"), ("ok", "OK"), ("naja", "Naja")]),
    "f": ("friendliness", [("", "Freundlich"), ("sehr", "Sehr"), ("besonders", "Besonders")]),
    "r": ("reliability", [("", "Zuverlässig"), ("sehr", "Sehr"), ("besonders", "Besonders")]),
}
CATEGORY_TITLE = {"satisfaction": "😊 Оценка", "friendliness": "🙂 Дружелюбие", "reliability": "👍 Надёжность"}
RPS_CHOICES = (4, 8, 12, 15, 20)
INTERVAL_CHOICES = ((5, 15), (10, 30), (30, 60), (60, 120))
JOB_STATUS_RU = {"locating": "ищу диапазон", "scanning": "перебор", "done": "завершён",
                 "stopped": "остановлен", "failed": "ошибка", "queued": "в очереди"}


def parse_watch_input(text: str) -> tuple[Optional[int], str]:
    """«12345 Петя, Audi» / «https://…userId=12345 Петя» → (12345, "Петя, Audi")."""
    text = text.strip()
    m = re.match(r"^\S*?userId=(\d+)\S*\s*(.*)$", text, re.S) or re.match(r"^(\d{3,})\b\s*(.*)$", text, re.S)
    if not m:
        return None, ""
    return int(m.group(1)), re.sub(r"\s+", " ", m.group(2)).strip(" -—:,")[:200]


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data[:64]}


def chunked(items: list, size: int) -> list:
    return [items[i:i + size] for i in range(0, len(items), size)]


def mask_proxy(url: str) -> str:
    return re.sub(r"//([^:@/]+):([^@/]+)@", r"//\1:***@", url) if url else "не используется"


def normalize_proxy(text: str) -> Optional[str]:
    """«user:pass@ip:port» / «socks5://…» / «ip:port» → URL прокси или None, если не похоже."""
    p = text.strip().strip('"').strip()
    if not p:
        return None
    if "://" not in p:
        parts = p.split(":")
        if "@" not in p and len(parts) == 4 and parts[1].isdigit():   # ip:port:user:pass
            p = f"{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        p = "socks5://" + p
    m = re.match(r"^(socks5h?|socks4|https?)://(?:[^:@/\s]+:[^@/\s]+@)?[\w.-]+:\d{2,5}/?$", p)
    return p.rstrip("/") if m else None


class SearchRun:
    """Один поиск: живёт только пока идёт (на диск не сохраняется)."""

    def __init__(self, num: int, target: Target):
        self.num = num
        self.target = target
        self.job = Job(target=target.to_dict())
        self.stop = threading.Event()
        self.pause = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.phase = "ждёт очереди"
        self.confirmed: Optional[int] = None    # ID, который пользователь подтвердил кнопкой «✅ Это он»


class Bot:
    def __init__(self, cfg: dict, cfg_path: Optional[Path] = None):
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.data_dir = (BASE_DIR / cfg["data_dir"]).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self.tg = TelegramAPI(cfg["telegram_token"])
        self.fetcher = Fetcher(cfg, notify=self.broadcast)
        self.anchors = AnchorStore(self.data_dir / "known_accounts.txt", cfg["seed_anchors"], LEGACY_ANCHOR_FILES)
        self.lock = threading.RLock()
        self.state = self._load_state()
        self.runs: dict[int, SearchRun] = {}    # идущие и ждущие поиски (только в памяти)
        self.notes_by_uid: dict[int, str] = {}  # заметка найденного аккаунта — для кнопки «👁 Следить»
        self.leftovers: dict[int, dict] = {}    # № поиска -> ID, которые не открылись (до перепроверки)
        self.recheck: Optional[dict] = None     # идущая медленная перепроверка
        self.next_num = 1
        # общий пул потоков для всех поисков: лимит скорости всё равно общий
        self.pool = ThreadPoolExecutor(max_workers=64, thread_name_prefix="fetch")
        self.monitor_wakeup = threading.Event()

    # ------------------------------------------------------------------
    # состояние и настройки
    # ------------------------------------------------------------------
    def _load_state(self) -> dict:
        st: dict = {}
        if self.state_path.exists():
            try:
                st = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.error("state.json повреждён (%s), начинаю с чистого", exc)
        for key, default in (("offset", None), ("watch", {}), ("ui", {})):
            st.setdefault(key, default)
        for old in ("pending", "job", "queue", "history"):   # данные прошлых версий не храним
            st.pop(old, None)
        return st

    def save(self) -> None:
        with self.lock:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)

    def save_config(self, **changes) -> None:
        """Меняет настройки на лету и записывает их в config.json."""
        self.cfg.update(changes)
        if not self.cfg_path:
            return
        try:
            data = json.loads(self.cfg_path.read_text(encoding="utf-8")) if self.cfg_path.exists() else {}
            data.update(changes)
            self.cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Не удалось сохранить config.json: %s", exc)

    def ui(self, chat_id: str) -> dict:
        return self.state["ui"].setdefault(chat_id, {"step": None, "draft": {}, "then": "flow"})

    # ------------------------------------------------------------------
    # сообщения
    # ------------------------------------------------------------------
    def broadcast(self, text: str, buttons: Optional[list] = None) -> None:
        log.info("→ TG: %s", text.replace("\n", " | ")[:200])
        for chat_id in self.cfg["allowed_chat_ids"]:
            self.tg.send(chat_id, text, buttons)

    def show(self, chat_id: str, text: str, buttons: Optional[list] = None, msg_id: Optional[int] = None) -> None:
        """Обновляет сообщение с кнопками (если нажали кнопку) или присылает новое."""
        if msg_id:
            self.tg.edit(chat_id, msg_id, text, buttons)
        else:
            self.tg.send(chat_id, text, buttons)

    # ------------------------------------------------------------------
    # поиски: параллельно, у каждого своя пауза/стоп; после окончания — забываем
    # ------------------------------------------------------------------
    def active_runs(self) -> list[SearchRun]:
        return [r for r in self.runs.values() if r.thread is not None]

    def queued_runs(self) -> list[SearchRun]:
        return [r for r in self.runs.values() if r.thread is None]

    def job_running(self) -> bool:
        return bool(self.active_runs())

    @property
    def parallel_limit(self) -> int:
        return max(1, int(self.cfg["parallel_searches"]))

    def find_duplicate(self, name: Optional[str], aktiv_seit: Optional[date]) -> Optional[SearchRun]:
        """Тот же аккаунт (имя + дата регистрации) уже ищется или ждёт в корзине?"""
        if not name or not aktiv_seit:
            return None
        for run in self.runs.values():
            if run.target.aktiv_seit == aktiv_seit and names_equal(run.target.name, name):
                return run
        return None

    def duplicate_warning(self, run: SearchRun) -> str:
        where = "уже ищется" if run.thread is not None else "уже ждёт в корзине"
        return f"⚠️ Дубль! {run.target.name} / {fmt_date(run.target.aktiv_seit)} {where} — поиск #{run.num}." \
               f"{note_suffix(run.target.note)}\nВторой такой же поиск не запускаю."

    def submit_search(self, target: Target) -> str:
        """Запускает поиск, если есть свободный слот (parallel_searches), иначе кладёт в корзину."""
        with self.lock:
            dup = self.find_duplicate(target.name, target.aktiv_seit)
            if dup is not None:
                return self.duplicate_warning(dup)
            if len(self.active_runs()) >= self.parallel_limit and \
                    len(self.queued_runs()) >= int(self.cfg["max_queue"]):
                return f"🧺 Корзина полна ({self.cfg['max_queue']}/{self.cfg['max_queue']}). Дождись, пока " \
                       f"освободится место, или убери лишнее в «{M_TASKS}»."
            run = SearchRun(self.next_num, target)
            self.next_num += 1
            self.runs[run.num] = run
            if len(self.active_runs()) < self.parallel_limit:
                self._start(run)
                n = len(self.active_runs())
                tail = f"\nСейчас идёт поисков: {n} из {self.parallel_limit} — скорость делится." if n > 1 else ""
                return f"🚀 Поиск #{run.num} запущен: {target.name}, {fmt_date(target.aktiv_seit)}{tail}"
            return f"🧺 Поиск #{run.num} в корзине ({len(self.queued_runs())}/{self.cfg['max_queue']}): " \
                   f"{target.name}, {fmt_date(target.aktiv_seit)}.\nВсе {self.parallel_limit} слота заняты — " \
                   f"начнётся сам, как только один освободится."

    def _start(self, run: SearchRun) -> None:
        run.phase = "ищу диапазон ID"
        run.thread = threading.Thread(target=self._run_job, args=(run,), name=f"search-{run.num}", daemon=True)
        run.thread.start()

    def _start_queued(self) -> None:
        with self.lock:
            limit = self.parallel_limit
            started = []
            for run in self.queued_runs():
                if len(self.active_runs()) >= limit:
                    break
                self._start(run)
                started.append(run)
        for run in started:
            self.broadcast(f"▶️ Из корзины в работу — поиск #{run.num}: {run.target.name}, "
                           f"{fmt_date(run.target.aktiv_seit)}{note_suffix(run.target.note)}")

    def _on_match(self, run: SearchRun, m: Match) -> None:
        if m.level == "conflict":
            log.info("#%s: совпало имя, но не плашки: %s %s", run.num, m.user_id, m.notes)
            return   # покажем в итоговом отчёте
        self.anchors.add(m.user_id, run.target.aktiv_seit)   # дата найденного — в базу дат
        self.anchors.flush()
        head = "🎯 НАЙДЕН ПОЛЬЗОВАТЕЛЬ!" if m.level == "exact" else "🤔 Возможное совпадение"
        notes = ("\n" + "\n".join("• " + n for n in m.notes)) if m.notes else ""
        job = run.job
        prog = f"\nПоиск #{run.num} продолжается: проверено {job.checked / job.total * 100:.0f}% " \
               f"— если это он, останови кнопкой ниже." if job.total else ""
        if run.target.note:
            self.notes_by_uid[m.user_id] = run.target.note
        self.broadcast(f"{head} (поиск #{run.num}){note_suffix(run.target.note)}\n"
                       f"{m.name} (ID {m.user_id})\n{m.url}{notes}{prog}",
                       [[btn(f"✅ Это он — остановить поиск #{run.num}", f"j:ok:{run.num}:{m.user_id}")],
                        [btn("👁 Следить за объявлениями", f"w:add:{m.user_id}")]])

    def _run_job(self, run: SearchRun) -> None:
        job, target = run.job, run.target
        try:
            job.status = "locating"
            locator = RangeLocator(self.fetcher, self.anchors, self.cfg, run.stop,
                                   progress=lambda s: setattr(run, "phase", "диапазон: " + s))
            job.first, job.last = locator.locate(target.aktiv_seit)
            share = max(1, len(self.active_runs()))
            eta = job.total / max(1.0, getattr(self.fetcher, "total_rps", self.cfg["requests_per_second"])) * share / 60
            self.broadcast(f"📍 Поиск #{run.num} ({target.name}): {fmt_date(target.aktiv_seit)} — ID "
                           f"{job.first:,}–{job.last:,} ({job.total:,} ID, ~{eta:.0f} мин). Перебираю до конца, "
                           f"о каждом совпадении сообщу сразу.")
            job.status, run.phase = "scanning", "перебор"
            Scanner(self.fetcher, self.cfg, run.stop, run.pause, lambda m: self._on_match(run, m), lambda: None,
                    share=lambda: max(1, len(self.active_runs()))).run(job, self.pool)
            job.status = "stopped" if run.stop.is_set() else "done"
        except LocateError as exc:
            if run.stop.is_set():
                job.status = "stopped"
            else:
                job.status, job.message = "failed", str(exc)
        except Exception as exc:   # не роняем бота
            log.exception("Ошибка поиска #%s", run.num)
            job.status, job.message = "failed", f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            # в базу дат — одна запись на искомую дату: подтверждённый/найденный аккаунт или любой этого дня
            exact = [m["user_id"] for m in job.matches if m["level"] == "exact"]
            keep = run.confirmed or (exact[0] if exact else None) or job.sample
            if keep:
                self.anchors.add(keep, target.aktiv_seit)
            with self.lock:
                self.runs.pop(run.num, None)     # поиск закончен — забываем
                if job.error_ids:
                    self.leftovers[run.num] = {"target": target, "ids": list(job.error_ids)}
        buttons = self._watch_buttons(job.matches) or []
        if job.error_ids:
            buttons = [[btn(f"🔁 Перепроверить {len(job.error_ids)} ID (медленно)", f"j:re:{run.num}")]] + buttons
        self.broadcast(self.report(run), buttons or None)
        if self.cfg["auto_watch_on_match"]:
            exact = [m for m in job.matches if m["level"] == "exact"]
            if run.confirmed:
                name = next((m["name"] for m in job.matches if m["user_id"] == run.confirmed), "")
                self.add_watch(run.confirmed, name, note=run.target.note)
            elif job.status == "done" and len(exact) == 1:
                self.add_watch(exact[0]["user_id"], exact[0]["name"], note=run.target.note)
        self._start_queued()

    def report(self, run: SearchRun) -> str:
        job, t = run.job, run.target
        mins = (time.time() - job.started) / 60
        if run.confirmed:
            head = f"✅ Поиск #{run.num} остановлен: ты подтвердил ID {run.confirmed}"
        elif job.status == "done":
            head = f"✅ Поиск #{run.num} завершён"
        elif job.status == "stopped":
            head = f"⏹ Поиск #{run.num} остановлен"
        else:
            head = f"❌ Поиск #{run.num}: ошибка — {job.message}"
        lines = [f"{head} ({mins:.0f} мин){note_suffix(t.note)}",
                 f"Цель: {t.name}, {fmt_date(t.aktiv_seit)}, плашки: {badges_str(t.badges)}"]
        if job.first is not None:
            lines.append(f"Диапазон: {job.first:,}–{job.last:,}; проверено {job.checked:,}/{job.total:,} "
                         f"(профилей {job.profiles:,}, пустых {job.missing:,}, не открылись {len(job.error_ids)})")
        lines += self._matches_lines(job.matches)
        if job.error_ids:
            lines.append(f"\n⚠️ {len(job.error_ids)} ID не открылись (ошибки сети/блокировки) — среди них может "
                         f"быть нужный. Кнопка «🔁 Перепроверить» проверит их медленно и осторожно, "
                         f"по одному раз в ~{self.cfg['recheck_delay_seconds']} с.")
        return "\n".join(lines)

    @staticmethod
    def _matches_lines(matches: list) -> list[str]:
        lines = []
        groups = (("exact", "🎯 Точные совпадения"), ("partial", "🤔 Возможные"),
                  ("conflict", "↔️ То же имя, другие плашки"))
        for level, title in groups:
            items = [m for m in matches if m["level"] == level]
            if items:
                lines.append(f"\n{title}:")
                lines += [f"• {m['name']} — {PROFILE_URL.format(uid=m['user_id'])}"
                          + (f" ({'; '.join(m['notes'])})" if m["notes"] else "") for m in items]
        if not matches:
            lines.append("\n❌ Совпадений не найдено.")
        return lines

    def _watch_buttons(self, matches: list) -> Optional[list]:
        rows = [[btn(f"👁 Следить: {m['name'][:24]}", f"w:add:{m['user_id']}")]
                for m in matches if m["level"] in ("exact", "partial") and str(m["user_id"]) not in self.state["watch"]]
        return rows[:8] or None

    def run_action(self, num: int, action: str, uid: Optional[int] = None) -> str:
        if action == "re":
            return self.start_recheck(num)
        if action == "rmre":
            self.leftovers.pop(num, None)
            return f"🗑 Ошибки поиска #{num} забыты."
        if action == "restop" and self.recheck:
            self.recheck["stop"].set()
            return "⏹ Останавливаю перепроверку…"
        if action == "par":
            self.save_config(parallel_searches=int(uid or 3))
            self._start_queued()
            return f"Одновременно будет идти поисков: {self.parallel_limit}."
        run = self.runs.get(num)
        if run is None:
            if action == "ok" and uid:               # поиск уже закончен — просто ставим в мониторинг
                if not self.add_watch(uid, note=self.notes_by_uid.get(uid, "")):
                    return "Этот аккаунт уже в мониторинге."
                return ""
            return f"Поиск #{num} уже закончен."
        if action in ("stop", "ok"):
            if action == "ok" and uid:
                run.confirmed = uid
            run.stop.set()
            run.pause.clear()
            if run.thread is None:              # ещё не начинался — просто убираем
                with self.lock:
                    self.runs.pop(num, None)
                return f"🗑 Поиск #{num} убран из ожидания."
            return f"⏹ Останавливаю поиск #{num}…" if action == "stop" else \
                f"✅ Принято: ID {uid}. Останавливаю поиск #{num}…"
        if action == "pause":
            run.pause.set()
            return f"⏸ Поиск #{num} на паузе."
        if action == "resume":
            run.pause.clear()
            return f"▶️ Поиск #{num} продолжается."
        return ""

    def set_run_note(self, num: int, note: str) -> bool:
        """Заметка к идущему (или ждущему в корзине) поиску. Переносится и на уже найденные аккаунты."""
        with self.lock:
            run = self.runs.get(num)
            if run is None:
                return False
            run.target.note = note
            run.job.target["note"] = note
            for m in run.job.matches:
                if m["level"] in ("exact", "partial"):
                    if note:
                        self.notes_by_uid[m["user_id"]] = note
                    else:
                        self.notes_by_uid.pop(m["user_id"], None)
                    key = str(m["user_id"])
                    if key in self.state["watch"] and not self.state["watch"][key].get("note"):
                        self.set_watch_note(key, note)      # уже в мониторинге без подписи — подпишем
        return True

    def start_recheck(self, num: int) -> str:
        item = self.leftovers.get(num)
        if not item:
            return f"У поиска #{num} нет ID для перепроверки (или они уже перепроверены)."
        if self.recheck is not None:
            return "Уже идёт перепроверка — дождись её конца (видно в «📋 Задачи»)."
        task = {"num": num, "target": item["target"], "ids": item["ids"], "done": 0, "fixed": 0,
                "stop": threading.Event()}
        self.recheck = task
        threading.Thread(target=self._recheck_slowly, args=(task,), name=f"recheck-{num}", daemon=True).start()
        mins = len(item["ids"]) * (self.cfg["recheck_delay_seconds"] + 1) / 60
        return f"🔁 Перепроверяю {len(item['ids'])} ID поиска #{num} медленно (~{max(1, round(mins))} мин). " \
               f"О совпадениях сообщу, итог пришлю."

    def _recheck_slowly(self, task: dict) -> None:
        """Перепроверка ID с ошибками: по одному, с паузой, с дополнительными повторами —
        чтобы не нарваться на блокировку заново."""
        num, target, stop = task["num"], task["target"], task["stop"]
        fake_run = SearchRun(num, target)
        retries = max(6, int(self.cfg["max_retries"]))     # больше повторов, чем при обычном переборе
        is_real = isinstance(self.fetcher, Fetcher)
        left = []
        matches = []
        try:
            for uid in task["ids"]:
                if stop.is_set():
                    left.append(uid)
                    continue
                res = self.fetcher.get(uid, stop, retries=retries) if is_real else self.fetcher.get(uid, stop)
                task["done"] += 1
                if res.status == "ok" and res.profile:
                    task["fixed"] += 1
                    m = match_profile(target, res.profile, self.cfg["name_fuzzy_threshold"])
                    if m:
                        matches.append(asdict(m))
                        self._on_match(fake_run, m)
                elif res.status == "missing":
                    task["fixed"] += 1
                else:
                    left.append(uid)
                stop.wait(float(self.cfg["recheck_delay_seconds"]) * random.uniform(0.7, 1.3))
        except Exception:
            log.exception("Ошибка перепроверки #%s", num)
        finally:
            with self.lock:
                self.recheck = None
                if left:
                    self.leftovers[num] = {"target": target, "ids": left}
                else:
                    self.leftovers.pop(num, None)
        lines = [f"🔁 Перепроверка поиска #{num} ({target.name}, {fmt_date(target.aktiv_seit)})"
                 f"{note_suffix(target.note)}",
                 f"Открылись: {task['fixed']} из {len(task['ids'])}" + (f", снова не открылись: {len(left)}" if left
                                                                       else " — все проверены ✅")]
        if matches:
            lines += self._matches_lines(matches)
        buttons = [[btn(f"🔁 Ещё раз ({len(left)} ID)", f"j:re:{num}"), btn("🗑 Забыть", f"j:rmre:{num}")]] \
            if left else None
        self.broadcast("\n".join(lines), buttons)

    def all_runs_action(self, action: str) -> None:
        for num in list(self.runs):
            self.run_action(num, action)

    # ------------------------------------------------------------------
    # экраны: статус, задачи, результаты
    # ------------------------------------------------------------------
    def _run_line(self, run: SearchRun) -> str:
        job, t = run.job, run.target
        if run.thread is None:
            st = "⏳ ждёт очереди"
        elif run.pause.is_set():
            st = "⏸ пауза"
        elif job.total:
            st = f"{job.checked / job.total * 100:.0f}% ({job.checked:,}/{job.total:,})"
        else:
            st = run.phase
        found = sum(1 for m in job.matches if m["level"] in ("exact", "partial"))
        return f"#{run.num} {t.name} / {fmt_date(t.aktiv_seit)} — {st}" + (f", найдено {found}" if found else "") \
            + (f"\n     📝 {t.note}" if t.note else "")

    def status_text(self) -> str:
        parts = ["📊 Статус"]
        active, queued = self.active_runs(), self.queued_runs()
        if active or queued:
            parts.append(f"Поисков идёт: {len(active)} из {self.parallel_limit}, в корзине: {len(queued)} "
                         f"из {self.cfg['max_queue']}")
            parts += ["  " + self._run_line(r) for r in active + queued]
            left = sum(max(0, (r.job.last or 0) - (r.job.cursor or r.job.first or 0) + 1)
                       for r in active if r.job.total and not r.pause.is_set())
            real = getattr(self.fetcher, "current_rate", lambda: 0.0)()
            rps = real or getattr(self.fetcher, "total_rps", self.cfg["requests_per_second"])
            if left:
                parts.append(f"Осталось перебрать {left:,} ID ≈ {left / max(0.5, rps) / 60:.0f} мин на всё")
        else:
            parts.append("Сейчас поисков нет.")
        paused = getattr(self.fetcher, "paused_for", lambda: 0)()
        if paused:
            parts.append(f"⛔ Автопауза из-за блокировки: ещё {paused // 60 + 1} мин")
        s = getattr(self.fetcher, "stats", None)
        if s:
            parts.append(f"Запросов: {s['requests']:,}; блокировок (403/429, с повтором): {s['blocked']}; "
                         f"не открылись даже после повторов: {s['errors']}")
            kinds = getattr(self.fetcher, "errors_summary", lambda: "")()
            if kinds:
                parts.append(f"  причины: {kinds}")
        lanes = getattr(self.fetcher, "lanes", None) or []
        n_proxy = len(proxy_list(self.cfg))
        total = self.cfg["requests_per_second"] * max(1, n_proxy)
        workers = getattr(self.fetcher, "worker_count", lambda: 6)()
        real = getattr(self.fetcher, "current_rate", lambda: 0.0)()
        parts.append(f"Скорость: сейчас {real:.1f} запр/с, лимит {total} ({self.cfg['requests_per_second']}/с × "
                     f"{n_proxy or 1} {'прокси' if n_proxy else 'прямое соединение'}), потоков {workers}")
        if len(lanes) > 1 or (lanes and any(lane.pause_until > time.monotonic() for lane in lanes)):
            parts += ["  " + x for x in self.fetcher.lanes_info()]
        if self.leftovers:
            parts.append(f"⚠️ Ждут перепроверки: {sum(len(i['ids']) for i in self.leftovers.values())} ID "
                         f"— «{M_TASKS}»")
        parts.append(f"Мониторинг: {len(self.state['watch'])} акк. · дат в базе: {len(self.anchors):,}")
        return "\n".join(parts)

    def screen_status(self, chat_id: str, msg_id: Optional[int] = None) -> None:
        self.show(chat_id, self.status_text(), [[btn("🔄 Обновить", "s:refresh"), btn("📋 Задачи", "t:refresh")]],
                  msg_id)

    def screen_tasks(self, chat_id: str, msg_id: Optional[int] = None) -> None:
        active, queued = self.active_runs(), self.queued_runs()
        lines = ["📋 Задачи", f"\n▶️ В работе: {len(active)} из {self.parallel_limit}"]
        rows: list = []
        if not active:
            lines.append("  сейчас ничего — пришли скриншот или нажми «Поиск по имени»")
        else:
            lines.append("  ⏸ пауза · 📝 заметка (чей аккаунт) · ⏹ стоп")
        for run in active:
            lines.append("  " + self._run_line(run))
            rows.append([btn(f"▶️ #{run.num}", f"j:resume:{run.num}") if run.pause.is_set()
                         else btn(f"⏸ #{run.num}", f"j:pause:{run.num}"),
                         btn(f"📝 #{run.num}", f"j:note:{run.num}"),
                         btn(f"⏹ Стоп #{run.num}", f"j:stop:{run.num}")])
        lines.append(f"\n🧺 Корзина: {len(queued)} из {self.cfg['max_queue']}"
                     + (" — берутся по очереди, как только освобождается слот" if queued else ""))
        for run in queued:
            lines.append("  " + self._run_line(run))
            rows.append([btn(f"📝 #{run.num}", f"j:note:{run.num}"),
                         btn(f"🗑 Убрать из корзины #{run.num}", f"j:stop:{run.num}")])
        if self.recheck:
            r = self.recheck
            lines.append(f"\n🔁 Перепроверка #{r['num']}: {r['done']}/{len(r['ids'])}, открылись {r['fixed']}")
            rows.append([btn(f"⏹ Стоп перепроверки #{r['num']}", f"j:restop:{r['num']}")])
        waiting = [(n, it) for n, it in self.leftovers.items() if not self.recheck or self.recheck["num"] != n]
        if waiting:
            lines.append("\n⚠️ Не открылись (ошибки/блокировки), ждут перепроверки:")
            for n, it in waiting:
                lines.append(f"  #{n} {it['target'].name} / {fmt_date(it['target'].aktiv_seit)} — {len(it['ids'])} ID")
                rows.append([btn(f"🔁 Перепроверить #{n} ({len(it['ids'])})", f"j:re:{n}"),
                             btn("🗑", f"j:rmre:{n}")])
        rows.append([btn(("✅" if k == self.parallel_limit else "") + f"{k} парал.", f"j:par:0:{k}")
                     for k in (1, 2, 3, 4, 5)])
        rows.append([btn("🔄 Обновить", "t:refresh"), btn(M_SEARCH, "c:new")])
        self.show(chat_id, "\n".join(lines), rows, msg_id)

    def screen_results(self, chat_id: str, msg_id: Optional[int] = None) -> None:
        runs = self.active_runs()
        if not runs:
            self.show(chat_id, "🏆 Сейчас поисков нет. Результаты закончившихся поисков — в их отчётах в чате.",
                      [[btn(M_SEARCH, "c:new")]], msg_id)
            return
        lines = ["🏆 Найдено в идущих поисках"]
        rows: list = []
        for run in runs:
            lines.append("\n" + self._run_line(run))
            for m in run.job.matches:
                icon = {"exact": "🎯", "partial": "🤔", "conflict": "↔️"}[m["level"]]
                lines.append(f"  {icon} {m['name']} — {PROFILE_URL.format(uid=m['user_id'])}")
                if m["level"] in ("exact", "partial"):
                    rows.append([btn(f"✅ #{run.num}: это {m['name'][:18]} (ID {m['user_id']})",
                                     f"j:ok:{run.num}:{m['user_id']}")])
            if not run.job.matches:
                lines.append("  — пока совпадений нет")
        self.show(chat_id, "\n".join(lines), rows[:10] + [[btn("🔄 Обновить", "r:refresh")]], msg_id)

    # ------------------------------------------------------------------
    # мастер поиска: имя → дата → плашки → карточка
    # ------------------------------------------------------------------
    @staticmethod
    def draft_text(d: dict) -> str:
        lines = [f"👤 Имя: {d.get('name') or '❓ не задано'}",
                 f"📅 Дата регистрации: {d.get('aktiv_seit') or '❓ не задана'}",
                 f"🏷 Плашки: {badges_str(d.get('badges') or {})}"]
        if d.get("followers") is not None:
            lines.append(f"Подписчики: {d['followers']}")
        if d.get("online_ads") is not None:
            lines.append(f"Объявления онлайн: {d['online_ads']}")
        lines.append(f"📝 Заметка: {d.get('note') or '— (нажми «📝 Заметка», чтобы подписать, чей аккаунт)'}")
        return "\n".join(lines)

    def screen_card(self, chat_id: str, msg_id: Optional[int] = None, title: str = "Проверь данные") -> None:
        u = self.ui(chat_id)
        u["step"], u["then"] = None, "card"
        d = u["draft"]
        ready = d.get("name") and d.get("aktiv_seit")
        rows = [[btn("🔍 Искать", "c:go")]] if ready else []
        rows += [[btn("✏️ Имя", "c:name"), btn("📅 Дата", "c:date"), btn("🏷 Плашки", "c:badges")],
                 [btn("📝 Заметка", "c:note"), btn("❌ Отмена", "c:cancel")]]
        hint = "\n\nВсё верно? Жми «🔍 Искать» или исправь кнопками." if ready else \
            "\n\nНе хватает имени или даты — задай кнопками ниже."
        dup = self.find_duplicate(d.get("name"), parse_date(d.get("aktiv_seit") or "")) if ready else None
        if dup is not None:
            hint = "\n\n" + self.duplicate_warning(dup).replace("Второй такой же поиск не запускаю.",
                                                               "Это дубль скриншота — искать второй раз не нужно.")
            rows = rows[1:]                        # без кнопки «Искать»
        self.show(chat_id, f"{title}:\n\n{self.draft_text(d)}{hint}", rows, msg_id)

    def ask_note(self, chat_id: str, msg_id: Optional[int] = None) -> None:
        u = self.ui(chat_id)
        u["step"] = "note"
        cur = u["draft"].get("note")
        rows = [[btn("🗑 Без заметки", "c:nonote")]] if cur else []
        self.show(chat_id, "📝 Напиши заметку к аккаунту — чей он, зачем ищем (например: «Петя, Audi A4»).\n"
                           "Она будет в каждом уведомлении поиска и мониторинга."
                  + (f"\n\nСейчас: {cur}" if cur else ""), rows + [[btn("⬅️ Назад", "c:back")]], msg_id)

    def ask_name(self, chat_id: str, msg_id: Optional[int] = None) -> None:
        u = self.ui(chat_id)
        u["step"] = "name"
        self.show(chat_id, "👤 Напиши имя пользователя точно как на профиле\n(например: Arne Schwarzmann)",
                  [[btn("❌ Отмена", "c:cancel")]], msg_id)

    def year_picker(self, chat_id: str, msg_id: Optional[int] = None) -> None:
        self.ui(chat_id)["step"] = "date"
        years = [btn(str(y), f"d:y:{y}") for y in range(2009, date.today().year + 1)]
        rows = chunked(years, 4) + [[btn("❌ Отмена", "c:cancel")]]
        self.show(chat_id, "📅 Год регистрации («Aktiv seit»)?\nИли напиши дату текстом: 08.02.2017", rows, msg_id)

    def month_picker(self, chat_id: str, year: int, msg_id: Optional[int]) -> None:
        months = [btn(MONTHS_RU[m - 1], f"d:m:{year}:{m}") for m in range(1, 13)
                  if date(year, m, 1) <= date.today()]
        self.show(chat_id, f"📅 {year} — месяц?", chunked(months, 4) + [[btn("⬅️ Год", "d:y")]], msg_id)

    def day_picker(self, chat_id: str, year: int, month: int, msg_id: Optional[int]) -> None:
        import calendar
        days = [btn(str(d), f"d:d:{year}:{month}:{d}") for d in range(1, calendar.monthrange(year, month)[1] + 1)
                if date(year, month, d) <= date.today()]
        self.show(chat_id, f"📅 {MONTHS_RU[month - 1]} {year} — число?",
                  chunked(days, 7) + [[btn("⬅️ Месяц", f"d:y:{year}")]], msg_id)

    def badge_picker(self, chat_id: str, msg_id: Optional[int] = None) -> None:
        u = self.ui(chat_id)
        u["step"] = "badges"
        badges = u["draft"].setdefault("badges", {})
        rows = []
        for code, (cat, options) in BADGE_PICKER.items():
            rows.append([btn(f"— {CATEGORY_TITLE[cat]} —", "noop")])
            row = [btn(("✅ " if badges.get(cat) == BADGE_LABELS[cat][key] else "") + label, f"b:{code}:{key or '-'}")
                   for key, label in options]
            row.append(btn(("✅ " if cat not in badges else "") + "нет", f"b:{code}:x"))
            rows.append(row)
        rows.append([btn("✅ Готово", "b:done")])
        self.show(chat_id, f"🏷 Отметь плашки как на профиле (если плашки нет — «нет»).\n"
                           f"Сейчас: {badges_str(badges)}", rows, msg_id)

    def after_name(self, chat_id: str) -> None:
        d = self.ui(chat_id)["draft"]
        if self.ui(chat_id)["then"] == "card" or d.get("aktiv_seit"):
            self.screen_card(chat_id)
        else:
            self.year_picker(chat_id)

    def after_date(self, chat_id: str, msg_id: Optional[int]) -> None:
        if self.ui(chat_id)["then"] == "card":
            self.screen_card(chat_id, msg_id)
        else:
            self.badge_picker(chat_id, msg_id)

    def confirm(self, chat_id: str, msg_id: Optional[int] = None) -> None:
        u = self.ui(chat_id)
        d = u["draft"]
        date_ = parse_date(d.get("aktiv_seit") or "")
        if not d.get("name") or not date_:
            self.screen_card(chat_id, msg_id)
            return
        target = Target(d["name"], date_, dict(d.get("badges") or {}), d.get("followers"), d.get("online_ads"),
                        note=(d.get("note") or "").strip())
        dup = self.find_duplicate(target.name, target.aktiv_seit)
        if dup is not None:
            self.state["ui"][chat_id] = {"step": None, "draft": {}, "then": "flow"}
            if msg_id:
                self.tg.edit(chat_id, msg_id, f"🚫 Не запущено:\n\n{self.draft_text(target.to_dict())}")
            self.tg.send(chat_id, self.duplicate_warning(dup), [[btn("📋 Задачи", "t:refresh")]])
            return
        self.state["ui"][chat_id] = {"step": None, "draft": {}, "then": "flow"}
        if msg_id:
            self.tg.edit(chat_id, msg_id, f"✅ Принято:\n\n{self.draft_text(target.to_dict())}")
        self.tg.send(chat_id, self.submit_search(target), [[btn("📋 Задачи", "t:refresh")]])

    def set_draft_from_target(self, chat_id: str, target: Target, title: str) -> None:
        u = self.ui(chat_id)
        u["draft"] = target.to_dict()
        self.screen_card(chat_id, title=title)

    # ------------------------------------------------------------------
    # мониторинг
    # ------------------------------------------------------------------
    def add_watch(self, uid: int, name: str = "", note: str = "") -> bool:
        with self.lock:
            key = str(uid)
            if key in self.state["watch"]:
                if note and not self.state["watch"][key].get("note"):
                    self.set_watch_note(key, note)
                return False
            self.state["watch"][key] = {"name": name, "note": note, "seen": None, "next": 0, "fails": 0}
            self.save()
        self.monitor_wakeup.set()
        self.broadcast(f"👁 Слежу за {name or uid}{note_suffix(note)}\n{PROFILE_URL.format(uid=uid)}\n"
                       f"Проверка каждые {self.cfg['monitor_minutes_min']}–{self.cfg['monitor_minutes_max']} мин."
                       + ("" if note else "\nЧтобы подписать, чей это аккаунт: «👁 Мониторинг» → 📝."))
        return True

    def set_watch_note(self, key: str, note: str) -> None:
        with self.lock:
            if key in self.state["watch"]:
                self.state["watch"][key]["note"] = note
                self.save()

    @staticmethod
    def watch_label(key: str, w: dict) -> str:
        """«Arne Schwarzmann (ID 123) — 📝 Петя, Audi A4» — заметка видна в каждом сообщении."""
        return f"{w.get('name') or 'ID ' + key}" + (f" (ID {key})" if w.get("name") else "") + \
            (f" — 📝 {w['note']}" if w.get("note") else "")

    def check_watch(self, key: str) -> list[str]:
        """Проверяет один аккаунт. Возвращает сообщения для отправки."""
        if key not in self.state["watch"]:
            return []
        res = self.fetcher.get(int(key))
        now = time.time()
        out: list[str] = []
        with self.lock:
            w = self.state["watch"].get(key)
            if w is None:          # успели удалить
                return []
            manual = w.pop("manual", False)
            label = self.watch_label(key, w)
            if res.status != "ok" or not res.profile:
                # ошибка/пустая страница: список «виденных» НЕ трогаем, иначе всё придёт как новое
                w["fails"] = w.get("fails", 0) + 1
                if res.status == "missing" and w["fails"] == 3:
                    out.append(f"⚠️ Профиль {label} не открывается (удалён?): {PROFILE_URL.format(uid=key)}")
                elif manual:
                    out.append(f"⚠️ {label}: не удалось открыть профиль ({res.error or res.status}), повторю позже."
                               f"\n{PROFILE_URL.format(uid=key)}")
                w["next"] = now + 5 * 60
                return out
            prof = res.profile
            w["fails"] = 0
            w["name"] = w.get("name") or prof.name or ""
            label = self.watch_label(key, w)
            current = prof.listings
            titles = w.setdefault("titles", {})          # ID объявления -> название (видели последним)
            if w["seen"] is None:
                w["seen"] = sorted(current)
                titles.update({ad_id: ad.get("title", "") for ad_id, ad in current.items()})
                out.append(f"📋 {label}: сейчас {len(current)} объявл. — запомнил, буду присылать только новые."
                           f"\n{PROFILE_URL.format(uid=key)}")
            else:
                seen = set(w["seen"])
                fresh = []
                for ad_id, ad in current.items():
                    title = ad.get("title", "")
                    if ad_id not in seen:
                        fresh.append((ad_id, ad, ""))                    # новое объявление (новый ID)
                    elif ad_id in titles and _norm_title(title) and \
                            _norm_title(title) != _norm_title(titles[ad_id]):
                        fresh.append((ad_id, ad, titles[ad_id]))         # то же ID, но другое название
                    titles.setdefault(ad_id, title)
                for ad_id, ad, old_title in fresh:
                    titles[ad_id] = ad.get("title", "")
                    w.setdefault("new_ads", []).append({
                        "id": ad_id, "title": ad.get("title", ""), "price": ad.get("price", ""),
                        "url": ad.get("url", ""), "at": now, "was": old_title})
                    head = f"🆕 Новое объявление у {label}" if not old_title else \
                        f"✏️ Объявление заменено у {label}\nбыло: {old_title}"
                    out.append(f"{head}\n\n{ad.get('title') or 'без названия'}"
                               + (f"\n💶 {ad['price']}" if ad.get("price") else "")
                               + f"\n{ad.get('url', '')}\nПрофиль: {PROFILE_URL.format(uid=key)}")
                w["new_ads"] = w.get("new_ads", [])[-300:]
                # объединяем, а не заменяем: временно скрытое объявление не придёт повторно
                w["seen"] = sorted(seen | set(current), key=lambda x: int(x) if x.isdigit() else 0)[-3000:]
                # названия нужны только для объявлений, которые сейчас на странице (ловим замену названия)
                w["titles"] = {ad_id: titles.get(ad_id, ad.get("title", "")) for ad_id, ad in current.items()}
                if (manual or self.cfg.get("monitor_every_check")) and not out:
                    out.append(f"✅ {label}: новых объявлений нет (на странице {len(current)})."
                               f"\n{PROFILE_URL.format(uid=key)}")
            w["last"] = now
            lo, hi = self.cfg["monitor_minutes_min"], self.cfg["monitor_minutes_max"]
            w["next"] = now + random.uniform(lo, hi) * 60
        return out

    def new_ads_text(self, keys: list[str]) -> str:
        """Список всех новых объявлений, замеченных после того, как аккаунт встал в мониторинг."""
        blocks = []
        for key in keys:
            w = self.state["watch"].get(key)
            if w is None:
                continue
            ads = w.get("new_ads") or []
            head = f"📜 {self.watch_label(key, w)} — новых объявлений: {len(ads)}"
            if not ads:
                blocks.append(head + "\n   пока ни одного нового с момента добавления в мониторинг")
                continue
            lines = [head]
            for i, ad in enumerate(ads, 1):
                when = time.strftime("%d.%m %H:%M", time.localtime(ad.get("at", 0)))
                was = f" (вместо «{ad['was']}»)" if ad.get("was") else ""
                lines.append(f"{i}. {when} — {ad.get('title') or 'без названия'}{was}"
                             + (f" — {ad['price']}" if ad.get("price") else "") + f"\n   {ad.get('url', '')}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) or "В мониторинге никого нет."

    def active_watch_keys(self) -> list[str]:
        """Аккаунты, которые после добавления в мониторинг хоть раз залили (или заменили) объявление.
        Сначала — те, кто заливал последним."""
        active = [k for k, w in list(self.state["watch"].items()) if w.get("new_ads")]
        return sorted(active, key=lambda k: max(a.get("at", 0) for a in self.state["watch"][k]["new_ads"]),
                      reverse=True)

    def active_accounts_text(self) -> tuple[str, Optional[list]]:
        """«🔥 Активные»: кто заливал товар с момента добавления в мониторинг + кнопки на их карточки."""
        keys = self.active_watch_keys()
        total = len(self.state["watch"])
        if not keys:
            return (f"🔥 Активных пока нет: ни один из {total} аккаунтов не заливал новых объявлений "
                    f"с момента добавления в мониторинг."), None
        lines = [f"🔥 Активные аккаунты — заливали товар после начала мониторинга: {len(keys)} из {total}"]
        buttons = []
        for i, key in enumerate(keys, 1):
            w = self.state["watch"][key]
            ads = w["new_ads"]
            n_new = sum(1 for a in ads if not a.get("was"))
            n_repl = len(ads) - n_new
            last_ad = max(ads, key=lambda a: a.get("at", 0))
            when = time.strftime("%d.%m %H:%M", time.localtime(last_ad.get("at", 0)))
            counts = f"🆕 {n_new}" + (f" · ✏️ {n_repl}" if n_repl else "")
            lines.append(f"\n{i}. {self.watch_label(key, w)}\n   {counts} · последнее {when}: "
                         f"{last_ad.get('title') or 'без названия'}\n   {PROFILE_URL.format(uid=key)}")
            buttons.append(btn(f"{i}. {(w.get('note') or w.get('name') or key)[:14]}", f"w:card:{key}"))
        lines.append("\nНажми на аккаунт — откроется его карточка (там «📜 Новые» — все его объявления).")
        return "\n".join(lines), chunked(buttons, 3)

    def monitor_loop(self) -> None:
        while True:
            try:
                due = [k for k, w in list(self.state["watch"].items()) if w.get("next", 0) <= time.time()]
                for key in due:
                    messages = self.check_watch(key)
                    self.save()
                    n = len(self.state["watch"].get(key, {}).get("new_ads") or [])
                    for text in messages:
                        new_ad = text.startswith(("🆕", "✏️"))
                        self.broadcast(text, [[btn(f"📜 Все новые у этого аккаунта ({n})", f"w:ads:{key}")]]
                                       if new_ad else None)
            except Exception:
                log.exception("Ошибка мониторинга")
            self.monitor_wakeup.wait(15)
            self.monitor_wakeup.clear()

    def request_check(self, keys: Iterable[str]) -> None:
        with self.lock:
            for k in keys:
                if k in self.state["watch"]:
                    self.state["watch"][k]["next"] = 0
                    self.state["watch"][k]["manual"] = True
        self.monitor_wakeup.set()

    MONITOR_PAGE = 15     # аккаунтов на странице: одна кнопка на аккаунт, 3 в ряд

    def screen_monitor(self, chat_id: str, msg_id: Optional[int] = None, page: Optional[int] = None) -> None:
        """Компактный список: одна строка и одна кнопка на аккаунт; всё остальное — в карточке."""
        w = self.state["watch"]
        lo, hi = self.cfg["monitor_minutes_min"], self.cfg["monitor_minutes_max"]
        keys = list(w)
        pages = max(1, (len(keys) + self.MONITOR_PAGE - 1) // self.MONITOR_PAGE)
        u = self.ui(chat_id)
        page = u.get("mon_page", 0) if page is None else page
        page = min(max(0, page), pages - 1)
        u["mon_page"] = page
        lines = [f"👁 Мониторинг (проверка каждые {lo}–{hi} мин) — аккаунтов: {len(keys)}"]
        rows = [[btn("➕ Добавить", "w:new")] + ([btn(f"🔄 Проверить всех ({len(keys)})", "w:all")] if w else [])]
        if w:
            n_active = len(self.active_watch_keys())
            rows.append([btn("📜 Все новые объявления", "w:ads:*"), btn(f"🔥 Активные ({n_active})", "w:active")])
        start = page * self.MONITOR_PAGE
        acc_buttons = []
        for i, key in enumerate(keys[start:start + self.MONITOR_PAGE], start + 1):
            item = w[key]
            last = time.strftime("%H:%M", time.localtime(item["last"])) if item.get("last") else "—"
            n_new = len(item.get("new_ads") or [])
            name = item.get("name") or f"ID {key}"
            lines.append(f"{i}. {name}" + (f" — 📝 {item['note']}" if item.get("note") else "")
                         + f" · {len(item.get('seen') or [])} объявл." + (f" · 🆕{n_new}" if n_new else "")
                         + f" · {last}")
            acc_buttons.append(btn(f"{i}. {(item.get('note') or name)[:14]}", f"w:card:{key}"))
        rows += chunked(acc_buttons, 3)
        if not w:
            lines.append("Пока никого. Найденные поиском добавляются сами, или нажми «➕ Добавить».")
        else:
            lines.append("\nНажми на аккаунт — откроется карточка: проверить, новые объявления, заметка, убрать.")
        if pages > 1:
            lines.append(f"Страница {page + 1} из {pages}")
            rows.append([btn("◀️", f"w:pg:{page - 1}") if page > 0 else btn("·", "noop"),
                         btn(f"{page + 1}/{pages}", "noop"),
                         btn("▶️", f"w:pg:{page + 1}") if page < pages - 1 else btn("·", "noop")])
        every = self.cfg.get("monitor_every_check")
        rows.append([btn(f"⏱ Интервал: {lo}–{hi} мин", "w:int"),
                     btn("🔔 О каждой проверке: " + ("вкл" if every else "выкл"), "w:every")])
        self.show(chat_id, "\n".join(lines), rows, msg_id)

    def screen_watch_card(self, chat_id: str, key: str, msg_id: Optional[int] = None, note: str = "") -> None:
        item = self.state["watch"].get(key)
        if item is None:
            self.screen_monitor(chat_id, msg_id)
            return
        last = time.strftime("%d.%m %H:%M", time.localtime(item["last"])) if item.get("last") else "ещё не было"
        n_new = len(item.get("new_ads") or [])
        lines = [f"👤 {item.get('name') or 'ID ' + key} (ID {key})",
                 f"📝 Заметка: {item.get('note') or '—'}",
                 f"Объявлений на странице: {len(item.get('seen') or [])}",
                 f"Новых замечено с начала мониторинга: {n_new}",
                 f"Последняя проверка: {last}",
                 PROFILE_URL.format(uid=key)]
        if note:
            lines.append("\n" + note)
        rows = [[btn("🔍 Проверить сейчас", f"w:chk:{key}"), btn(f"📜 Новые ({n_new})", f"w:ads:{key}")],
                [btn("📝 Заметка", f"w:note:{key}"), btn("🗑 Убрать", f"w:delask:{key}")],
                [btn("⬅️ К списку", "w:list")]]
        self.show(chat_id, "\n".join(lines), rows, msg_id)

    def screen_interval(self, chat_id: str, msg_id: Optional[int]) -> None:
        cur = (self.cfg["monitor_minutes_min"], self.cfg["monitor_minutes_max"])
        rows = [[btn(("✅ " if (lo, hi) == cur else "") + f"{lo}–{hi} мин", f"w:iv:{lo}:{hi}")
                 for lo, hi in INTERVAL_CHOICES], [btn("⬅️ Назад", "w:list")]]
        self.show(chat_id, "⏱ Как часто проверять новые объявления?\n(время каждый раз случайное в этих пределах)",
                  rows, msg_id)

    # ------------------------------------------------------------------
    # прокси и скорость
    # ------------------------------------------------------------------
    def screen_proxy(self, chat_id: str, msg_id: Optional[int] = None, note: str = "") -> None:
        proxies = proxy_list(self.cfg)
        rps = self.cfg["requests_per_second"]
        workers = getattr(self.fetcher, "worker_count", lambda: 6)()
        lines = ["🌐 Прокси и скорость"]
        if proxies:
            info = self.fetcher.lanes_info() if hasattr(self.fetcher, "lanes_info") else [mask_proxy(p) for p in proxies]
            lines += [f"{i + 1}. {x}" for i, x in enumerate(info)]
        else:
            lines.append("Прокси нет — запросы идут напрямую с этого компьютера.")
        lines.append(f"\nСкорость: {rps}/с на каждый прокси → всего до {rps * max(1, len(proxies))}/с, "
                     f"потоков {workers} (подбираются сами)")
        lines.append("Больше прокси = быстрее поиск: сайт ограничивает каждый IP отдельно.")
        if note:
            lines.append("\n" + note)
        rows = [[btn(f"🗑 {i + 1}. {mask_proxy(p)[:30]}", f"p:del:{i}")] for i, p in enumerate(proxies)]
        rows.append([btn("➕ Добавить", "p:set")] + ([btn("🧪 Проверить", "p:test")] if proxies else []))
        if proxies:
            rows.append([btn("🚫 Удалить все (без прокси)", "p:off")])
        rows.append([btn(("✅" if r == rps else "") + f"{r}/с", f"p:rps:{r}") for r in RPS_CHOICES])
        self.show(chat_id, "\n".join(lines), rows, msg_id)

    def set_proxies(self, urls: list[str]) -> None:
        if hasattr(self.fetcher, "set_proxies"):
            self.fetcher.set_proxies(urls)
        self.save_config(proxy_url="", proxy_urls=urls)

    def set_proxy(self, url: str) -> None:   # совместимость
        self.set_proxies([url] if url else [])

    @staticmethod
    def _test_one(proxy: str, uid: int) -> str:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENTS[0]
        if proxy:
            s.proxies.update({"http": proxy, "https": proxy})
        head = mask_proxy(proxy) if proxy else "без прокси"
        try:
            ip = s.get("https://api.ipify.org?format=json", timeout=20).json().get("ip")
        except Exception as exc:
            return f"❌ {head}: нет соединения ({type(exc).__name__})"
        try:
            t0 = time.time()
            r = s.get(PROFILE_URL.format(uid=uid), timeout=25)
            dt = time.time() - t0
            if r.status_code == 200 and looks_like_profile(r.text):
                return f"✅ {head}: IP {ip}, Kleinanzeigen открывается за {dt:.1f} с"
            why = " — похоже на блокировку" if r.status_code in (403, 429) or looks_blocked(r.text) else ""
            return f"⚠️ {head}: IP {ip}, Kleinanzeigen ответил {r.status_code}{why}"
        except Exception as exc:
            return f"❌ {head}: IP {ip}, Kleinanzeigen не открывается ({type(exc).__name__})"

    def proxy_test(self, chat_id: str) -> None:
        anchors = self.anchors.items()
        uid = anchors[-1][0] if anchors else 46211501
        proxies = proxy_list(self.cfg) or [""]
        with ThreadPoolExecutor(max_workers=min(8, len(proxies))) as pool:
            lines = list(pool.map(lambda p: self._test_one(p, uid), proxies))
        self.tg.send(chat_id, "🧪 Проверка прокси:\n" + "\n".join(lines), [[btn("🌐 Прокси", "p:menu")]])

    # ------------------------------------------------------------------
    # входящие: текст, фото, кнопки
    # ------------------------------------------------------------------
    def handle_photo(self, chat_id: str, file_id: str) -> None:
        self.tg.send(chat_id, "📸 Распознаю скриншот…")
        try:
            text = ocr_image(self.tg.download(file_id), self.cfg["tesseract_cmd"])
        except Exception as exc:
            self.tg.send(chat_id, f"Ошибка распознавания: {exc}\nМожно ввести данные через «{M_SEARCH}».")
            return
        log.info("OCR:\n%s", text)
        name, d, badges, followers, online = parse_ocr_text(text)
        with self.lock:
            u = self.ui(chat_id)
            u["draft"] = {"name": name, "aktiv_seit": fmt_date(d) if d else None, "badges": badges,
                          "followers": followers, "online_ads": online}
        self.screen_card(chat_id, title="📸 Распознано со скриншота")

    def handle_text(self, chat_id: str, text: str) -> None:
        u = self.ui(chat_id)
        menu = {M_SEARCH: lambda: self.start_wizard(chat_id), M_TASKS: lambda: self.screen_tasks(chat_id),
                M_STATUS: lambda: self.screen_status(chat_id), M_RESULTS: lambda: self.screen_results(chat_id),
                M_MONITOR: lambda: self.screen_monitor(chat_id), M_PROXY: lambda: self.screen_proxy(chat_id)}
        if text in menu:
            if u["step"] in ("proxy", "watch", "watch_note", "run_note"):
                u["step"] = None
            menu[text]()
            return
        if text.startswith("/"):
            self.handle_command(chat_id, text)
            return
        step = u["step"]
        if step == "name":
            u["draft"]["name"] = re.sub(r"\s+", " ", text).strip()[:60]
            u["step"] = None
            self.after_name(chat_id)
        elif step == "date":
            d = parse_date(text)
            if not d:
                self.tg.send(chat_id, "Не понял дату. Формат: 08.02.2017 — или выбери кнопками выше.")
                return
            u["draft"]["aktiv_seit"] = fmt_date(d)
            self.after_date(chat_id, None)
        elif step == "proxy":
            self.handle_proxy_input(chat_id, text)
        elif step == "note":
            u["draft"]["note"] = re.sub(r"\s+", " ", text).strip()[:200]
            self.screen_card(chat_id)
        elif step == "watch":
            uid, note = parse_watch_input(text)
            if not uid:
                self.tg.send(chat_id, "Пришли ID (цифры) или ссылку на профиль с userId=…, "
                                      "а после пробела — заметку, например:\n12345678 Петя, Audi A4")
                return
            u["step"] = None
            if not self.add_watch(uid, note=note):
                if note:
                    self.set_watch_note(str(uid), note)
                self.tg.send(chat_id, "Этот профиль уже в мониторинге" + (" — заметку обновил." if note else "."))
            self.screen_monitor(chat_id)
        elif step == "run_note":
            num = u.pop("run_num", None)
            u["step"] = None
            note = "" if text.strip() in ("-", "—") else re.sub(r"\s+", " ", text).strip()[:200]
            if num is not None and self.set_run_note(int(num), note):
                self.tg.send(chat_id, f"📝 Заметка к поиску #{num} " + (f"сохранена: {note}" if note else "убрана")
                             + ". Она будет во всех следующих сообщениях по этому поиску.")
            else:
                self.tg.send(chat_id, f"Поиск #{num} уже закончен — заметку можно поставить в «{M_MONITOR}» → 📝.")
            self.screen_tasks(chat_id)
        elif step == "watch_note":
            key = u.pop("watch_key", None)
            u["step"] = None
            if key:
                note = "" if text.strip() in ("-", "—") else re.sub(r"\s+", " ", text).strip()[:200]
                self.set_watch_note(key, note)
                self.screen_watch_card(chat_id, key, note="✅ Заметка сохранена." if note else "Заметка убрана.")
            else:
                self.screen_monitor(chat_id)
        else:
            target = parse_target_text(text)
            if target:
                self.set_draft_from_target(chat_id, target, "Понял так")
            else:
                self.tg.send(chat_id, "Не понял 🤔 Пользуйся кнопками меню или пришли скриншот профиля.\n"
                                      "Текстом: Manuela 08.02.2017 top, sehr freundlich", keyboard=MAIN_KEYBOARD)

    def start_wizard(self, chat_id: str) -> None:
        self.state["ui"][chat_id] = {"step": None, "draft": {"badges": {}}, "then": "flow"}
        self.ask_name(chat_id)

    def handle_proxy_input(self, chat_id: str, text: str, message_id: Optional[int] = None) -> None:
        """Один или несколько прокси (по одному в строке) добавляются к списку."""
        if message_id:
            self.tg.delete(chat_id, message_id)   # не оставляем пароль прокси в переписке
        lines = [ln for ln in re.split(r"[\s,;]+", text.strip()) if ln]
        urls = [normalize_proxy(ln) for ln in lines]
        if not urls or any(u is None for u in urls):
            bad = [ln for ln, u in zip(lines, urls) if u is None]
            self.tg.send(chat_id, f"Не похоже на прокси: {mask_proxy(bad[0]) if bad else '(пусто)'}\nПримеры:\n"
                                  "socks5://логин:пароль@1.2.3.4:5000\nлогин:пароль@1.2.3.4:5000\n"
                                  "1.2.3.4:5000:логин:пароль\nМожно несколько — каждый с новой строки.",
                         [[btn("❌ Отмена", "p:menu")]])
            return
        self.ui(chat_id)["step"] = None
        current = proxy_list(self.cfg)
        added = [u for u in dict.fromkeys(urls) if u not in current]
        self.set_proxies(current + added)
        self.screen_proxy(chat_id, note=f"✅ Добавлено прокси: {len(added)} (сообщение с паролем удалено). "
                                        f"Нажми «🧪 Проверить».")

    def handle_command(self, chat_id: str, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd in ("/start", "/help", "/menu"):
            self.ui(chat_id)["step"] = None
            self.tg.send(chat_id, HELP, keyboard=MAIN_KEYBOARD)
        elif cmd in ("/find", "/search"):
            self.start_wizard(chat_id)
        elif cmd == "/go":
            self.confirm(chat_id)
        elif cmd in ("/cancel", "/clear"):
            self.state["ui"][chat_id] = {"step": None, "draft": {}, "then": "flow"}
            self.tg.send(chat_id, "Ввод сброшен.", keyboard=MAIN_KEYBOARD)
        elif cmd == "/status":
            self.screen_status(chat_id)
        elif cmd in ("/tasks", "/pause", "/resume", "/stop"):
            action = {"/pause": "pause", "/resume": "resume", "/stop": "stop"}.get(cmd)
            if action:
                self.all_runs_action(action)     # команда — для всех поисков сразу
            self.screen_tasks(chat_id)
        elif cmd in ("/results",):
            self.screen_results(chat_id)
        elif cmd in ("/watch", "/watchlist", "/unwatch", "/check"):
            uid, note = parse_watch_input(arg)
            if cmd == "/watch" and uid:
                self.add_watch(uid, note=note)
            elif cmd == "/unwatch" and arg in self.state["watch"]:
                self.state["watch"].pop(arg)
            elif cmd == "/check":
                self.request_check(list(self.state["watch"]))
            self.screen_monitor(chat_id)
        elif cmd == "/proxy":
            self.screen_proxy(chat_id)
        elif cmd in ("/anchor", "/add"):
            parts = arg.split()
            d = parse_date(parts[1]) if len(parts) == 2 else None
            if not d or not parts[0].isdigit():
                self.tg.send(chat_id, "Формат: /anchor <user_id> <дд.мм.гггг>")
            else:
                self.anchors.add(int(parts[0]), d)
                self.anchors.flush()
                self.tg.send(chat_id, f"Добавлено: {parts[0]} → {fmt_date(d)} (всего {len(self.anchors):,})")
        else:
            self.tg.send(chat_id, "Не знаю такую команду — пользуйся кнопками меню.", keyboard=MAIN_KEYBOARD)

    def handle_callback(self, chat_id: str, data: str, msg_id: int) -> None:
        u = self.ui(chat_id)
        parts = data.split(":")
        kind = parts[0]
        if kind == "noop":
            return
        # --- карточка поиска ---
        if kind == "c":
            act = parts[1]
            if act == "new":
                self.start_wizard(chat_id)
            elif act == "go":
                self.confirm(chat_id, msg_id)
            elif act == "cancel":
                self.state["ui"][chat_id] = {"step": None, "draft": {}, "then": "flow"}
                self.tg.edit(chat_id, msg_id, "❌ Отменено.")
            elif act == "name":
                u["then"] = "card"
                self.ask_name(chat_id, msg_id)
            elif act == "date":
                u["then"] = "card"
                self.year_picker(chat_id, msg_id)
            elif act == "badges":
                u["then"] = "card"
                self.badge_picker(chat_id, msg_id)
            elif act == "note":
                self.ask_note(chat_id, msg_id)
            elif act == "nonote":
                u["draft"].pop("note", None)
                self.screen_card(chat_id, msg_id)
            elif act == "back":
                self.screen_card(chat_id, msg_id)
        # --- выбор даты ---
        elif kind == "d":
            if len(parts) == 2:
                self.year_picker(chat_id, msg_id)
            elif parts[1] == "y":
                self.month_picker(chat_id, int(parts[2]), msg_id)
            elif parts[1] == "m":
                self.day_picker(chat_id, int(parts[2]), int(parts[3]), msg_id)
            elif parts[1] == "d":
                u["draft"]["aktiv_seit"] = fmt_date(date(int(parts[2]), int(parts[3]), int(parts[4])))
                self.after_date(chat_id, msg_id)
        # --- плашки ---
        elif kind == "b":
            badges = u["draft"].setdefault("badges", {})
            if parts[1] == "done":
                self.screen_card(chat_id, msg_id)
                return
            cat, _ = BADGE_PICKER[parts[1]]
            key = parts[2]
            if key == "x":
                badges.pop(cat, None)
            else:
                label = BADGE_LABELS[cat]["" if key == "-" else key]
                if badges.get(cat) == label:
                    badges.pop(cat)        # повторное нажатие снимает отметку
                else:
                    badges[cat] = label
            self.badge_picker(chat_id, msg_id)
        # --- задачи / статус / результаты ---
        elif kind == "t":
            if u.get("step") == "run_note":
                u["step"] = None
            self.screen_tasks(chat_id, msg_id)
        elif kind == "j":       # действия с конкретным поиском: j:<действие>:<номер>[:<ID>]
            if parts[1] == "note":
                run = self.runs.get(int(parts[2]))
                if run is None:
                    self.tg.send(chat_id, f"Поиск #{parts[2]} уже закончен.")
                    self.screen_tasks(chat_id, msg_id)
                    return
                u["step"], u["run_num"] = "run_note", run.num
                cur = run.target.note
                self.show(chat_id, f"📝 Заметка к поиску #{run.num} ({run.target.name}, "
                                   f"{fmt_date(run.target.aktiv_seit)}) — чей это аккаунт?\n"
                                   + (f"Сейчас: {cur}\n" if cur else "")
                                   + "Напиши заметку («-» — убрать). Поиск при этом не останавливается.",
                          [[btn("⬅️ Назад", "t:refresh")]], msg_id)
                return
            uid = int(parts[3]) if len(parts) > 3 else None
            note = self.run_action(int(parts[2]), parts[1], uid)
            if note:
                self.tg.send(chat_id, note)
            if parts[1] in ("pause", "resume", "stop", "restop", "rmre", "par"):
                self.screen_tasks(chat_id, msg_id)
        elif kind == "s":
            self.screen_status(chat_id, msg_id)
        elif kind == "r":
            self.screen_results(chat_id, msg_id)
        # --- мониторинг ---
        elif kind == "w":
            act = parts[1]
            if act == "add":
                uid = int(parts[2])
                if not self.add_watch(uid, note=self.notes_by_uid.get(uid, "")):
                    self.tg.send(chat_id, "Этот профиль уже в мониторинге.")
                return
            if act == "new":
                u["step"] = "watch"
                self.show(chat_id, "➕ Пришли ID пользователя или ссылку на профиль (…userId=12345).\n"
                                   "Через пробел можно сразу заметку — чей аккаунт:\n12345678 Петя, Audi A4",
                          [[btn("⬅️ Назад", "w:list")]], msg_id)
                return
            if act == "pg":
                self.screen_monitor(chat_id, msg_id, page=int(parts[2]))
                return
            if act == "ads":
                keys = list(self.state["watch"]) if parts[2] == "*" else [parts[2]]
                self.tg.send(chat_id, self.new_ads_text(keys))
                return
            if act == "active":
                text, buttons = self.active_accounts_text()
                self.tg.send(chat_id, text, buttons)
                return
            if act == "note":
                key = parts[2]
                item = self.state["watch"].get(key)
                if item is None:
                    self.screen_monitor(chat_id, msg_id)
                    return
                u["step"], u["watch_key"] = "watch_note", key
                cur = item.get("note")
                self.show(chat_id, f"📝 Заметка к {item.get('name') or key} (ID {key}) — чей это аккаунт?\n"
                                   + (f"Сейчас: {cur}\n" if cur else "") + "Напиши новую («-» — убрать).",
                          [[btn("⬅️ Назад", f"w:card:{key}")]], msg_id)
                return
            if act == "every":
                self.save_config(monitor_every_check=not self.cfg.get("monitor_every_check"))
            if act == "card":
                u["step"] = None
                self.screen_watch_card(chat_id, parts[2], msg_id)
                return
            if act == "delask":
                item = self.state["watch"].get(parts[2])
                if item is None:
                    self.screen_monitor(chat_id, msg_id)
                    return
                self.show(chat_id, f"🗑 Убрать из мониторинга {self.watch_label(parts[2], item)}?\n"
                                   f"Новые объявления по нему больше не будут приходить.",
                          [[btn("✅ Да, убрать", f"w:del:{parts[2]}"), btn("❌ Нет", f"w:card:{parts[2]}")]], msg_id)
                return
            if act == "chk":
                self.request_check([parts[2]])
                self.screen_watch_card(chat_id, parts[2], msg_id,
                                       note="🔍 Проверяю… результат придёт отдельным сообщением.")
                return
            if act == "del":
                with self.lock:
                    self.state["watch"].pop(parts[2], None)
                    self.save()
            elif act == "all":
                self.request_check(list(self.state["watch"]))
                self.tg.send(chat_id, f"🔍 Проверяю все {len(self.state['watch'])} аккаунтов — по каждому придёт "
                                      f"результат: новые объявления или «новых нет».")
            elif act == "int":
                self.screen_interval(chat_id, msg_id)
                return
            elif act == "iv":
                self.save_config(monitor_minutes_min=int(parts[2]), monitor_minutes_max=int(parts[3]))
            if act in ("list", "del", "iv", "every"):
                u["step"] = None
            self.screen_monitor(chat_id, msg_id)
        # --- прокси ---
        elif kind == "p":
            act = parts[1]
            if act == "set":
                u["step"] = "proxy"
                self.show(chat_id, "➕ Пришли прокси одним сообщением, например:\n"
                                   "socks5://логин:пароль@1.2.3.4:5000\n"
                                   "Можно сразу несколько — каждый с новой строки.\n"
                                   "(сообщение с паролем я сразу удалю из чата)", [[btn("❌ Отмена", "p:menu")]], msg_id)
                return
            if act == "off":
                self.set_proxies([])
            elif act == "del":
                current = proxy_list(self.cfg)
                i = int(parts[2])
                if 0 <= i < len(current):
                    self.set_proxies(current[:i] + current[i + 1:])
            elif act == "rps":
                self.save_config(requests_per_second=int(parts[2]))
            elif act == "test":
                self.tg.send(chat_id, "🧪 Проверяю прокси…")
                threading.Thread(target=self.proxy_test, args=(chat_id,), daemon=True).start()
                return
            u["step"] = None
            self.screen_proxy(chat_id, msg_id)

    def handle_update(self, upd: dict) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            chat_id = str(cq["message"]["chat"]["id"])
            self.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
            if chat_id not in self.cfg["allowed_chat_ids"]:
                return
            with self.lock:
                self.handle_callback(chat_id, cq.get("data", ""), cq["message"]["message_id"])
                self.save()
            return
        msg = upd.get("message")
        if not msg:
            return
        chat_id = str(msg["chat"]["id"])
        if chat_id not in self.cfg["allowed_chat_ids"]:
            log.warning("Сообщение от чужого chat_id %s — игнор", chat_id)
            if not self.cfg["allowed_chat_ids"]:
                self.tg.send(chat_id, f"Твой chat_id: {chat_id}\nЗапусти на компьютере "
                                      f"«python kleinanzeigen_bot.py --setup» и впиши его.")
            return
        if msg.get("photo"):
            threading.Thread(target=self.handle_photo, args=(chat_id, msg["photo"][-1]["file_id"]),
                             daemon=True).start()
        elif msg.get("document", {}).get("mime_type", "").startswith("image/"):
            threading.Thread(target=self.handle_photo, args=(chat_id, msg["document"]["file_id"]),
                             daemon=True).start()
        elif "text" in msg:
            with self.lock:
                if self.ui(chat_id)["step"] == "proxy" and not msg["text"].startswith("/") \
                        and msg["text"] not in (M_SEARCH, M_TASKS, M_STATUS, M_RESULTS, M_MONITOR, M_PROXY):
                    self.handle_proxy_input(chat_id, msg["text"], msg.get("message_id"))
                else:
                    self.handle_text(chat_id, msg["text"].strip())
                self.save()

    # ------------------------------------------------------------------
    # главный цикл
    # ------------------------------------------------------------------
    def run(self) -> None:
        me = self.tg.call("getMe")
        if not me:
            sys.exit("❌ Telegram не отвечает или неверный telegram_token")
        log.info("Бот @%s запущен. Разрешённые чаты: %s", me.get("username"), self.cfg["allowed_chat_ids"] or "—")
        for proxy in proxy_list(self.cfg):
            log.info("Прокси для сайта: %s", mask_proxy(proxy))
        self.tg.call("setMyCommands", commands=[{"command": "start", "description": "Меню и помощь"},
                                                {"command": "status", "description": "Статус"},
                                                {"command": "cancel", "description": "Сбросить ввод"}])
        threading.Thread(target=self.monitor_loop, name="monitor", daemon=True).start()
        for chat_id in self.cfg["allowed_chat_ids"]:
            self.tg.send(chat_id, "🤖 Бот запущен. Меню — кнопками внизу, или пришли скриншот профиля.",
                         keyboard=MAIN_KEYBOARD)
        offset = self.state.get("offset")
        while True:
            try:
                updates = self.tg.call("getUpdates", http_timeout=65, timeout=50, offset=offset,
                                       allowed_updates=["message", "callback_query"]) or []
                for upd in updates:
                    offset = upd["update_id"] + 1
                    self.state["offset"] = offset
                    try:
                        self.handle_update(upd)
                    except Exception:
                        log.exception("Ошибка обработки сообщения")
                if updates:
                    self.save()
            except KeyboardInterrupt:
                log.info("Остановка…")
                self.all_runs_action("stop")
                self.anchors.flush()
                self.save()
                return
            except Exception:
                log.exception("Ошибка главного цикла")
                time.sleep(5)


# =====================================================================
# ЗАПУСК
# =====================================================================
def setup_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    fh = logging.FileHandler(data_dir / "bot.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [fh, sh]
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def run_check() -> int:
    """python kleinanzeigen_bot.py --check — проверка всего, что нужно для работы."""
    problems = 0

    def line(good: bool, text: str, fix: str = "", warn: bool = False) -> None:
        nonlocal problems
        mark = "✅" if good else ("⚠️ " if warn else "❌")
        print(f"{mark} {text}")
        if not good:
            if not warn:
                problems += 1
            if fix:
                print(f"     → {fix}")

    print("🔧 Проверка установки\n")
    line(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}", "нужен Python 3.9 или новее")
    import bs4
    bs_ver = tuple(int(x) for x in re.findall(r"\d+", bs4.__version__)[:2])
    line(bs_ver >= (4, 13), f"beautifulsoup4 {bs4.__version__}", "pip install -U beautifulsoup4", warn=True)
    line(True, f"requests {requests.__version__}")
    cfg_path = BASE_DIR / "config.json"
    cfg = load_config(cfg_path) if cfg_path.exists() else dict(DEFAULT_CONFIG)
    line(cfg_path.exists(), "config.json", "запусти: python kleinanzeigen_bot.py --setup")
    proxies = proxy_list(cfg)
    if any(p.startswith("socks") for p in proxies):
        import importlib.util
        line(importlib.util.find_spec("socks") is not None, "PySocks (для socks5-прокси)",
             "pip install PySocks")

    print("\n📸 OCR (распознавание скриншотов)")
    try:
        import pytesseract
        import PIL
        line(True, f"pytesseract и Pillow {PIL.__version__}")
        cmd = find_tesseract(cfg.get("tesseract_cmd", ""))
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        try:
            ver = pytesseract.get_tesseract_version()
            line(True, f"Tesseract {ver} — {cmd or 'из PATH'}")
            langs = pytesseract.get_languages(config="")
            line("deu" in langs, f"языки: {', '.join(sorted(langs))}",
                 "нет немецкого (deu): переустанови Tesseract с галочкой German или положи deu.traineddata "
                 "в папку tessdata (см. README)", warn=True)
            sample = BASE_DIR / "tests" / "samples" / "screen_arne.png"
            if sample.exists():
                name, d, badges, _, _ = parse_ocr_text(ocr_image(sample.read_bytes(), cfg.get("tesseract_cmd", "")))
                good = name == "Arne Schwarzmann" and d == date(2016, 3, 5) and len(badges) == 2
                line(good, f"тестовый скрин распознан: {name}, {fmt_date(d)}, {badges_str(badges)}",
                     "распознавание неточное — проверь язык deu", warn=True)
        except Exception as exc:
            line(False, f"Tesseract не найден ({type(exc).__name__})",
                 "установи Tesseract-OCR (см. README) или укажи путь в tesseract_cmd в config.json")
    except ImportError:
        line(False, "pytesseract/Pillow не установлены", "pip install -r requirements.txt")

    print("\n🌐 Сеть")
    token = cfg.get("telegram_token") or ""
    if TOKEN_RE.match(token):
        try:
            me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
            line(bool(me.get("ok")), f"Telegram-бот @{(me.get('result') or {}).get('username', '?')}",
                 f"Telegram ответил: {me.get('description')}")
        except requests.RequestException as exc:
            line(False, f"Telegram недоступен: {type(exc).__name__}")
    else:
        line(False, "telegram_token не задан", "python kleinanzeigen_bot.py --setup")
    anchors = AnchorStore(BASE_DIR / cfg["data_dir"] / "known_accounts.txt", cfg["seed_anchors"], LEGACY_ANCHOR_FILES)
    line(len(anchors) > 10, f"база якорей: {len(anchors)} записей")
    uid = anchors.items()[-1][0] if len(anchors) else 46211501
    for p in proxies or [""]:
        res = Bot._test_one(p, uid)
        line(res.startswith("✅"), res[2:].strip(),
             "проверь логин/пароль/адрес прокси" if p else "нет интернета или сайт не открывается с этого IP")
    print("\n" + ("Всё готово 👍" if not problems else f"Проблем: {problems} — исправь пункты с ❌"))
    return 1 if problems else 0


def main() -> None:
    # Windows: консоль/перенаправление в файл в cp866/cp1251 не умеет эмодзи — не падаем, а заменяем
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if "--check" in sys.argv:
        sys.exit(run_check())
    if "--selftest" in sys.argv:
        import unittest
        sys.path.insert(0, str(BASE_DIR / "tests"))
        suite = unittest.defaultTestLoader.discover(str(BASE_DIR / "tests"))
        sys.exit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
    cfg_path = BASE_DIR / "config.json"
    txt_path = BASE_DIR / "config.json.txt"      # Блокнот в Windows любит дописывать .txt
    if not cfg_path.exists() and txt_path.exists():
        txt_path.rename(cfg_path)
        print("ℹ️  Переименовал config.json.txt → config.json")
    if not cfg_path.exists() or "--setup" in sys.argv:
        setup_wizard(cfg_path)
    try:
        cfg = load_config(cfg_path)
    except (ValueError, json.JSONDecodeError) as exc:
        fail(f"❌ Ошибка в config.json: {exc}\nИсправь файл или удали его и запусти скрипт заново.")
    if not TOKEN_RE.match(cfg["telegram_token"] or ""):
        fail("❌ В config.json нет правильного telegram_token. Запусти: python kleinanzeigen_bot.py --setup")
    setup_logging((BASE_DIR / cfg["data_dir"]).resolve())
    Bot(cfg, cfg_path).run()


TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def fail(message: str) -> None:
    print(message)
    if os.name == "nt" and sys.stdin and sys.stdin.isatty():
        input("Нажми Enter, чтобы закрыть окно…")   # чтобы окно не закрылось сразу
    sys.exit(1)


def setup_wizard(cfg_path: Path) -> None:
    """Первый запуск: спрашивает токен/chat_id/прокси и создаёт config.json."""
    if not (sys.stdin and sys.stdin.isatty()):
        fail("❌ Нет config.json. Скопируй config.example.json → config.json и впиши токен бота.")
    print("⚙️  Первичная настройка (создам config.json). Ctrl+C — отмена.\n")
    example = BASE_DIR / "config.example.json"
    cfg = json.loads(example.read_text(encoding="utf-8")) if example.exists() else dict(DEFAULT_CONFIG)
    if cfg_path.exists():
        cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))
    while True:
        token = input("1) Токен бота от @BotFather (вида 123456789:AAH...): ").strip().strip('"')
        if TOKEN_RE.match(token):
            break
        print("   Не похоже на токен. Скопируй его целиком из сообщения @BotFather.")
    try:
        me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
        if me.get("ok"):
            print(f"   ✅ Бот найден: @{me['result'].get('username')}")
        else:
            print(f"   ⚠️ Telegram ответил: {me.get('description')} — проверь токен.")
    except requests.RequestException as exc:
        print(f"   ⚠️ Не удалось проверить токен (нет связи с Telegram?): {type(exc).__name__}")
    print("2) Твой chat_id (цифры). Если не знаешь — просто нажми Enter:\n"
          "   бот сам пришлёт его в ответ на любое сообщение, потом запусти --setup ещё раз.")
    chats = [c for c in re.split(r"[\s,;]+", input("   chat_id: ").strip()) if c]
    print("3) Прокси для сайта (Enter — без прокси). Формат: socks5://логин:пароль@ip:порт")
    proxy = input("   прокси: ").strip().strip('"')
    if proxy and "://" not in proxy:
        proxy = "socks5://" + proxy
    cfg.pop("_comment", None)
    cfg.update(telegram_token=token, allowed_chat_ids=chats, proxy_url=proxy)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ Сохранено в {cfg_path}. Остальные настройки можно поменять в этом файле.\n")


if __name__ == "__main__":
    main()
