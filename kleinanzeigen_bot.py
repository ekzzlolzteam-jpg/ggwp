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
    "requests_per_second": 8,        # общий лимит на все потоки
    "workers": 6,                    # потоков при переборе
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
    "stop_on_exact_match": True,
    "auto_watch_on_match": True,
    # --- Мониторинг ---
    "monitor_minutes_min": 10,
    "monitor_minutes_max": 30,
    # --- Прочее ---
    "tesseract_cmd": "",             # пусто = автоопределение
    "data_dir": "data",
}


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        unknown = {k for k in user_cfg if not k.startswith("_")} - set(DEFAULT_CONFIG)
        if unknown:
            print(f"⚠️  Неизвестные ключи в {path.name}: {', '.join(sorted(unknown))}")
        cfg.update(user_cfg)
    cfg["telegram_token"] = os.environ.get("KA_TELEGRAM_TOKEN", cfg["telegram_token"])
    cfg["proxy_url"] = os.environ.get("KA_PROXY_URL", cfg["proxy_url"])
    cfg["allowed_chat_ids"] = [str(x) for x in cfg["allowed_chat_ids"]]
    if cfg["monitor_minutes_min"] > cfg["monitor_minutes_max"]:
        raise ValueError("monitor_minutes_min больше monitor_minutes_max")
    if cfg["requests_per_second"] <= 0 or cfg["workers"] <= 0:
        raise ValueError("requests_per_second и workers должны быть > 0")
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

    @property
    def url(self) -> str:
        return PROFILE_URL.format(uid=self.user_id)


AD_ID_RE = re.compile(r"/s-anzeige/(?:[^/?#\"']+/)?(\d{5,})")
BLOCK_MARKERS = ("captcha", "access denied", "zugriff verweigert", "too many requests", "ungewöhnlich")


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(s or "")).strip()


def extract_name(soup: BeautifulSoup, raw_html: str) -> Optional[str]:
    el = soup.select_one(".userprofile--name, [class*='userprofile--name'], [class*='userprofile-name']")
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


def _int_after(pattern: str, text: str) -> Optional[int]:
    m = re.search(pattern, text, re.I)
    return int(m.group(1)) if m else None


def parse_profile_html(raw_html: str, user_id: int) -> Profile:
    soup = BeautifulSoup(raw_html, "html.parser")
    text = soup.get_text(" ", strip=True)
    prof = Profile(user_id=user_id)
    m = AKTIV_RE.search(text)
    prof.aktiv_seit = parse_date(m.group(1)) if m else None
    prof.name = extract_name(soup, raw_html)
    region = _profile_region_text(soup) or ""
    prof.badges = detect_badges(region) if region else detect_badges(text)
    src = region or text
    prof.online_ads = _int_after(r"(\d+)\s+Anzeigen?\s+online", src)
    prof.total_ads = _int_after(r"(\d+)\s+gesamt", src)
    prof.followers = _int_after(r"(\d+)\s+Follower", src)
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


class Fetcher:
    def __init__(self, cfg: dict, notify: Callable[[str], None] = lambda _m: None):
        self.cfg = cfg
        self.notify = notify
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._pause_until = 0.0
        self._soft_until = 0.0
        self._blocked_streak = 0
        self._block_level = 0
        self._local = threading.local()
        self.stats = {"requests": 0, "ok": 0, "missing": 0, "blocked": 0, "errors": 0}

    # --- сессии по потокам ---
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
            })
            if self.cfg.get("proxy_url"):
                s.proxies.update({"http": self.cfg["proxy_url"], "https": self.cfg["proxy_url"]})
            self._local.session = s
        return s

    # --- лимит скорости (общий на все потоки) ---
    def _wait_slot(self, stop: Optional[threading.Event]) -> bool:
        with self._lock:
            now = time.monotonic()
            rps = float(self.cfg["requests_per_second"])
            if now < self._soft_until:
                rps = max(1.0, rps / 3)
            start = max(now, self._next_slot, self._pause_until)
            self._next_slot = start + (1.0 / rps) * random.uniform(0.85, 1.15)
        delay = start - time.monotonic()
        if delay > 0:
            if stop is not None:
                return not stop.wait(delay)
            time.sleep(delay)
        return True

    def paused_for(self) -> int:
        return max(0, int(self._pause_until - time.monotonic()))

    def _on_success(self) -> None:
        with self._lock:
            self._blocked_streak = 0

    def _on_blocked(self) -> None:
        with self._lock:
            self.stats["blocked"] += 1
            self._blocked_streak += 1
            if self._blocked_streak < self.cfg["block_threshold"]:
                return
            minutes = self.cfg["block_pause_minutes"] * (2 ** min(self._block_level, 3))
            now = time.monotonic()
            self._pause_until = now + minutes * 60
            self._soft_until = self._pause_until + 180
            self._block_level += 1
            self._blocked_streak = 0
        log.warning("Похоже на блокировку, пауза %s мин", minutes)
        self.notify(f"⛔ Сайт режет запросы (403/429). Автопауза {minutes} мин, потом продолжу "
                    f"на пониженной скорости. Ничего делать не нужно.")

    def get(self, user_id: int, stop: Optional[threading.Event] = None) -> FetchResult:
        url = PROFILE_URL.format(uid=user_id)
        last = FetchResult("error", error="no attempts")
        for attempt in range(self.cfg["max_retries"] + 1):
            if stop is not None and stop.is_set():
                return FetchResult("error", error="stopped")
            if not self._wait_slot(stop):
                return FetchResult("error", error="stopped")
            blocked = False
            try:
                resp = self._session().get(url, timeout=self.cfg["request_timeout"])
                with self._lock:
                    self.stats["requests"] += 1
                code = resp.status_code
                if code in (404, 410):
                    self._on_success()
                    with self._lock:
                        self.stats["missing"] += 1
                    return FetchResult("missing", http_code=code)
                if code == 200:
                    body = resp.text
                    if looks_like_profile(body):
                        prof = parse_profile_html(body, user_id)
                        if prof.aktiv_seit or prof.name:
                            self._on_success()
                            with self._lock:
                                self.stats["ok"] += 1
                            return FetchResult("ok", profile=prof, http_code=code)
                    if looks_blocked(body):
                        blocked = True
                        last = FetchResult("error", http_code=code, error="block page")
                    else:
                        self._on_success()
                        with self._lock:
                            self.stats["missing"] += 1
                        return FetchResult("missing", http_code=code)
                elif code in (403, 429):
                    blocked = True
                    last = FetchResult("error", http_code=code, error=f"HTTP {code}")
                else:
                    last = FetchResult("error", http_code=code, error=f"HTTP {code}")
            except requests.RequestException as exc:
                last = FetchResult("error", error=type(exc).__name__)
            if blocked:
                self._on_blocked()
            backoff = min(30.0, 1.5 * (2 ** attempt)) * random.uniform(0.7, 1.3)
            if stop is not None:
                if stop.wait(backoff):
                    return FetchResult("error", error="stopped")
            else:
                time.sleep(backoff)
        with self._lock:
            self.stats["errors"] += 1
        return last


# =====================================================================
# БАЗА ЯКОРЕЙ (user_id -> дата регистрации), сама пополняется
# =====================================================================
class AnchorStore:
    def __init__(self, path: Path, seeds: Iterable = ()):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[int, date] = {}
        self._pending: list[str] = []
        for uid, d in seeds:
            pd = parse_date(str(d))
            if pd:
                self._data[int(uid)] = pd
        for p in (path, BASE_DIR / "known_accounts.txt"):  # совместимость со старым Osnova.py
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    if ";" in line:
                        uid, d = line.split(";", 1)
                        pd = parse_date(d)
                        if uid.strip().isdigit() and pd:
                            self._data[int(uid.strip())] = pd

    def __len__(self) -> int:
        return len(self._data)

    def add(self, uid: int, d: date) -> None:
        with self._lock:
            if self._data.get(uid) == d:
                return
            self._data[uid] = d
            self._pending.append(f"{uid};{fmt_date(d)}")
            if len(self._pending) >= 20:
                self._flush_locked()

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
            return sorted(self._data.items())


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
            self.anchors.add(uid, res.profile.aktiv_seit)
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

    def to_dict(self) -> dict:
        d = asdict(self)
        d["aktiv_seit"] = fmt_date(self.aktiv_seit)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Target":
        return cls(name=d["name"], aktiv_seit=parse_date(d["aktiv_seit"]), badges=d.get("badges") or {},
                   followers=d.get("followers"), online_ads=d.get("online_ads"), total_ads=d.get("total_ads"))

    def summary(self) -> str:
        lines = [f"Имя: {self.name}", f"Дата регистрации: {fmt_date(self.aktiv_seit)}",
                 f"Плашки: {badges_str(self.badges)}"]
        if self.followers is not None:
            lines.append(f"Подписчики: {self.followers}")
        if self.online_ads is not None:
            lines.append(f"Объявления онлайн: {self.online_ads}")
        return "\n".join(lines)


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
    started: float = field(default_factory=time.time)
    message: str = ""

    @property
    def total(self) -> int:
        return (self.last - self.first + 1) if self.first is not None and self.last is not None else 0

    def has_exact(self) -> bool:
        return any(m["level"] == "exact" for m in self.matches)


class Scanner:
    def __init__(self, fetcher, cfg: dict, stop: threading.Event, pause: threading.Event,
                 on_match: Callable[[Match], None], save: Callable[[], None]):
        self.fetcher = fetcher
        self.cfg = cfg
        self.stop = stop
        self.pause = pause
        self.on_match = on_match
        self.save = save

    def _wait_if_paused(self) -> None:
        while self.pause.is_set() and not self.stop.is_set():
            time.sleep(0.5)

    def _process(self, job: Job, target: Target, ids: list[int], pool: ThreadPoolExecutor) -> bool:
        """Проверяет пачку ID. Возвращает False, если поиск остановили посреди пачки."""
        results = list(pool.map(lambda uid: (uid, self.fetcher.get(uid, self.stop)), ids))
        if any(r.error == "stopped" for _, r in results):
            return False
        for uid, res in results:
            if res.status == "ok" and res.profile:
                job.profiles += 1
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

    def run(self, job: Job) -> None:
        target = Target.from_dict(job.target)
        size = max(1, int(self.cfg["chunk_size"]))
        if job.cursor is None:
            job.cursor = job.first
        with ThreadPoolExecutor(max_workers=self.cfg["workers"]) as pool:
            while job.cursor <= job.last:
                self._wait_if_paused()
                if self.stop.is_set():
                    return
                ids = list(range(job.cursor, min(job.cursor + size - 1, job.last) + 1))
                if not self._process(job, target, ids, pool):
                    return
                job.cursor = ids[-1] + 1
                self.save()
                if self.cfg["stop_on_exact_match"] and job.has_exact():
                    return
            # автоматическая перепроверка ID с ошибками (до 3 кругов, пока их становится меньше)
            for _ in range(3):
                before = len(job.error_ids)
                if not before or self.stop.is_set():
                    break
                self.recheck(job, pool)
                if len(job.error_ids) >= before:
                    break

    def recheck(self, job: Job, pool: Optional[ThreadPoolExecutor] = None) -> int:
        ids = list(job.error_ids)
        if not ids:
            return 0
        own = pool is None
        pool = pool or ThreadPoolExecutor(max_workers=self.cfg["workers"])
        try:
            size = max(1, int(self.cfg["chunk_size"]))
            for i in range(0, len(ids), size):
                self._wait_if_paused()
                if self.stop.is_set() or not self._process(job, Target.from_dict(job.target), ids[i:i + size], pool):
                    break
                self.save()
        finally:
            if own:
                pool.shutdown(wait=True)
        return len(ids)


# =====================================================================
# ВВОД ЦЕЛИ: текст и OCR
# =====================================================================
FIELD_KEYS = {
    "name": ("имя", "name", "ник", "nick"),
    "date": ("дата регистрации", "дата", "aktiv seit", "date", "регистрация"),
    "badges": ("плашки", "плашка", "badges", "бейджи", "значки"),
    "followers": ("подписчики", "follower", "followers"),
    "online_ads": ("объявления онлайн", "объявления", "anzeigen online", "anzeigen"),
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
                      followers=_opt_int(fields.get("followers")), online_ads=_opt_int(fields.get("online_ads")))
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
                     "folge", "antwortet", "verkaufsübersicht", "sicher bezahlen", "kleinanzeigen",
                     "zufriedenheit", "freundlich", "zuverl", "profil", "bestandsliste", "nachricht")


def parse_ocr_text(text: str) -> tuple[Optional[str], Optional[date], dict, Optional[int], Optional[int]]:
    m = AKTIV_RE.search(text)
    d = parse_date(m.group(1)) if m else parse_date(text)
    badges = detect_badges(text)
    followers = _int_after(r"(\d+)\s+Follower", text)
    online = _int_after(r"(\d+)\s+Anzeigen?\s+online", text)
    name = None
    for raw in text.splitlines():
        line = re.sub(r"[©®™>|«»]", " ", raw)
        line = re.sub(r"\s+", " ", line).strip(" -_.,:;")
        low = line.lower()
        if len(line) < 2 or any(w in low for w in IGNORE_NAME_WORDS) or re.search(r"\d", line):
            continue
        if not re.search(r"[A-Za-zÄÖÜäöüß]", line) or len(line) > 40:
            continue
        name = line
        break
    return name, d, badges, followers, online


def ocr_image(image_bytes: bytes, tesseract_cmd: str = "") -> str:
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("OCR не установлен: pip install pytesseract pillow и установи Tesseract-OCR "
                           "(или отправь данные текстом)") from exc
    win_default = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    elif win_default.exists():
        pytesseract.pytesseract.tesseract_cmd = str(win_default)
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
                log.warning("Telegram %s: %s", method, data.get("description"))
                return None
            except (requests.RequestException, ValueError) as exc:
                log.warning("Telegram %s: %s (попытка %d)", method, exc, attempt + 1)
                time.sleep(2 * (attempt + 1))
        return None

    def send(self, chat_id: str, text: str, buttons: Optional[list] = None) -> None:
        for i in range(0, max(1, len(text)), 4000):
            params = {"chat_id": chat_id, "text": text[i:i + 4000], "disable_web_page_preview": True}
            if buttons and i + 4000 >= len(text):
                params["reply_markup"] = {"inline_keyboard": buttons}
            self.call("sendMessage", **params)

    def download(self, file_id: str) -> bytes:
        meta = self.call("getFile", file_id=file_id)
        if not meta:
            raise RuntimeError("не удалось получить файл из Telegram")
        r = self.http.get(self.file_base + meta["file_path"], timeout=60)
        r.raise_for_status()
        return r.content


HELP = """🔎 Поиск пользователя Kleinanzeigen

Пришли СКРИНШОТ профиля или ТЕКСТ, например:
Имя: Manuela
Дата регистрации: 08.02.2017
Плашки: TOP Zufriedenheit, Besonders freundlich

или построчно (имя / дата / плашки / подписчики / объявления),
или одной строкой: Manuela 08.02.2017 top, sehr freundlich

Бот покажет распознанное → жми «🔍 Искать» (или исправь текстом).

Команды:
/status — прогресс поиска и мониторинга
/pause, /resume, /stop — управление поиском
/recheck — перепроверить ID с ошибками
/watch <id|ссылка> — следить за новыми объявлениями
/unwatch <id> — перестать следить
/watchlist — список отслеживаемых
/check — проверить отслеживаемых прямо сейчас
/anchor <id> <дд.мм.гггг> — добавить известную пару ID→дата
/cancel — сбросить ввод"""


class Bot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.data_dir = (BASE_DIR / cfg["data_dir"]).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "state.json"
        self.tg = TelegramAPI(cfg["telegram_token"])
        self.fetcher = Fetcher(cfg, notify=self.broadcast)
        self.anchors = AnchorStore(self.data_dir / "known_accounts.txt", cfg["seed_anchors"])
        self.lock = threading.RLock()
        self.state = self._load_state()
        self.job: Optional[Job] = Job(**self.state["job"]) if self.state.get("job") else None
        self.job_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.phase = ""
        self.monitor_wakeup = threading.Event()

    # --- состояние ---
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                st = json.loads(self.state_path.read_text(encoding="utf-8"))
                st.setdefault("watch", {})
                st.setdefault("pending", {})
                return st
            except (OSError, json.JSONDecodeError) as exc:
                log.error("state.json повреждён (%s), начинаю с чистого", exc)
        return {"offset": None, "watch": {}, "pending": {}, "job": None}

    def save(self) -> None:
        with self.lock:
            self.state["job"] = asdict(self.job) if self.job else None
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)

    # --- сообщения ---
    def broadcast(self, text: str, buttons: Optional[list] = None) -> None:
        log.info("→ TG: %s", text.replace("\n", " | ")[:200])
        for chat_id in self.cfg["allowed_chat_ids"]:
            self.tg.send(chat_id, text, buttons)

    # --- поиск ---
    def job_running(self) -> bool:
        return self.job_thread is not None and self.job_thread.is_alive()

    def start_job(self, job: Job) -> None:
        self.job = job
        self.stop_event.clear()
        self.pause_event.clear()
        self.save()
        self.job_thread = threading.Thread(target=self._run_job, name="search", daemon=True)
        self.job_thread.start()

    def _on_match(self, m: Match) -> None:
        if m.level == "conflict":
            log.info("Совпало имя, но не плашки: %s %s", m.user_id, m.notes)
            return   # покажем в итоговом отчёте
        head = "🎯 НАЙДЕН ПОЛЬЗОВАТЕЛЬ!" if m.level == "exact" else "🤔 Возможное совпадение"
        notes = ("\n" + "\n".join("• " + n for n in m.notes)) if m.notes else ""
        self.broadcast(f"{head}\n{m.name} (ID {m.user_id})\n{m.url}{notes}",
                       [[{"text": "👁 Следить за объявлениями", "callback_data": f"watch:{m.user_id}"}]])

    def _run_job(self) -> None:
        job = self.job
        target = Target.from_dict(job.target)
        try:
            if job.first is None:
                job.status = "locating"
                self.phase = "ищу диапазон ID"
                locator = RangeLocator(self.fetcher, self.anchors, self.cfg, self.stop_event,
                                       progress=lambda s: setattr(self, "phase", "диапазон: " + s))
                job.first, job.last = locator.locate(target.aktiv_seit)
                self.save()
                eta = job.total / self.cfg["requests_per_second"] / 60
                self.broadcast(f"📍 Диапазон для {fmt_date(target.aktiv_seit)}: ID {job.first:,}–{job.last:,} "
                               f"({job.total:,} ID, ~{eta:.0f} мин). Запросов на поиск диапазона: "
                               f"{locator.requests}. Начинаю перебор…")
            job.status = "scanning"
            self.phase = "перебор"
            scanner = Scanner(self.fetcher, self.cfg, self.stop_event, self.pause_event, self._on_match, self.save)
            scanner.run(job)
            job.status = "stopped" if self.stop_event.is_set() else "done"
        except LocateError as exc:
            if self.stop_event.is_set():
                job.status = "stopped"
            else:
                job.status, job.message = "failed", str(exc)
                self.broadcast(f"❌ Не удалось найти диапазон: {exc}")
        except Exception as exc:   # не роняем бота
            log.exception("Ошибка поиска")
            job.status, job.message = "failed", f"{type(exc).__name__}: {exc}"
            self.broadcast(f"💥 Ошибка поиска: {job.message}\nПрогресс сохранён, можно /resume")
        finally:
            self.anchors.flush()
            self.phase = ""
            self.save()
        if job.status in ("done", "stopped"):
            self.broadcast(self.report(job))
            if job.status == "done" and self.cfg["auto_watch_on_match"]:
                for m in job.matches:
                    if m["level"] == "exact":
                        self.add_watch(m["user_id"], m["name"])

    def report(self, job: Job) -> str:
        t = Target.from_dict(job.target)
        mins = (time.time() - job.started) / 60
        lines = [("✅ Поиск завершён" if job.status == "done" else "⏹ Поиск остановлен") + f" за {mins:.0f} мин",
                 f"Цель: {t.name}, {fmt_date(t.aktiv_seit)}, плашки: {badges_str(t.badges)}"]
        if job.first is not None:
            lines.append(f"Диапазон: {job.first:,}–{job.last:,}; проверено {job.checked:,}/{job.total:,} "
                         f"(профилей {job.profiles:,}, пустых {job.missing:,}, ошибок {len(job.error_ids)})")
        groups = (("exact", "🎯 Точные совпадения"), ("partial", "🤔 Возможные"),
                  ("conflict", "↔️ То же имя, другие плашки"))
        for level, title in groups:
            items = [m for m in job.matches if m["level"] == level]
            if items:
                lines.append(f"\n{title}:")
                lines += [f"• {m['name']} — {PROFILE_URL.format(uid=m['user_id'])}"
                          + (f" ({'; '.join(m['notes'])})" if m["notes"] else "") for m in items]
        if not job.matches:
            lines.append("\n❌ Совпадений не найдено.")
        if job.error_ids and not job.has_exact():
            lines.append(f"\n⚠️ {len(job.error_ids)} ID не удалось проверить — /recheck")
        return "\n".join(lines)

    def status_text(self) -> str:
        parts = []
        job = self.job
        if job:
            t = Target.from_dict(job.target)
            st = {"locating": "ищу диапазон", "scanning": "перебор", "done": "завершён", "stopped": "остановлен",
                  "failed": "ошибка"}.get(job.status, job.status)
            if self.pause_event.is_set() and self.job_running():
                st = "⏸ пауза"
            parts.append(f"Поиск: {t.name} / {fmt_date(t.aktiv_seit)} — {st}")
            if self.phase:
                parts.append(f"Этап: {self.phase}")
            if job.total:
                pct = job.checked / job.total * 100
                parts.append(f"Проверено {job.checked:,}/{job.total:,} ({pct:.1f}%), профилей {job.profiles:,}, "
                             f"ошибок {len(job.error_ids)}, совпадений {len(job.matches)}")
            if job.message:
                parts.append(job.message)
        else:
            parts.append("Поиск не запускался.")
        paused = getattr(self.fetcher, "paused_for", lambda: 0)()
        if paused:
            parts.append(f"⛔ Автопауза из-за блокировки: ещё {paused // 60 + 1} мин")
        s = getattr(self.fetcher, "stats", None)
        if s:
            parts.append(f"Запросов: {s['requests']:,} (блокировок {s['blocked']}, ошибок {s['errors']})")
        parts.append(f"Мониторинг: {len(self.state['watch'])} аккаунт(ов). Якорей в базе: {len(self.anchors):,}")
        return "\n".join(parts)

    # --- мониторинг ---
    def add_watch(self, uid: int, name: str = "") -> bool:
        with self.lock:
            key = str(uid)
            if key in self.state["watch"]:
                return False
            self.state["watch"][key] = {"name": name, "seen": None, "next": 0, "fails": 0}
            self.save()
        self.monitor_wakeup.set()
        self.broadcast(f"👁 Слежу за {name or uid}: {PROFILE_URL.format(uid=uid)}\n"
                       f"Проверка каждые {self.cfg['monitor_minutes_min']}–{self.cfg['monitor_minutes_max']} мин.")
        return True

    def check_watch(self, key: str) -> list[str]:
        """Проверяет один аккаунт. Возвращает сообщения для отправки."""
        if key not in self.state["watch"]:
            return []
        res = self.fetcher.get(int(key))
        now = time.time()
        out: list[str] = []
        with self.lock:
            w = self.state["watch"].get(key)
            if w is None:          # успели сделать /unwatch
                return []
            if res.status != "ok" or not res.profile:
                # ошибка/пустая страница: список «виденных» НЕ трогаем, иначе всё придёт как новое
                w["fails"] = w.get("fails", 0) + 1
                if res.status == "missing" and w["fails"] == 3:
                    out.append(f"⚠️ Профиль {w.get('name') or key} не открывается (удалён?): "
                               f"{PROFILE_URL.format(uid=key)}")
                w["next"] = now + 5 * 60
                return out
            prof = res.profile
            w["fails"] = 0
            w["name"] = w.get("name") or prof.name or ""
            current = prof.listings
            if w["seen"] is None:
                w["seen"] = sorted(current)
                out.append(f"📋 {w['name'] or key}: сейчас {len(current)} объявл. — запомнил, "
                           f"буду присылать только новые.")
            else:
                seen = set(w["seen"])
                for ad_id, ad in current.items():
                    if ad_id in seen:
                        continue
                    out.append(f"🆕 Новое объявление у {w['name'] or key}\n{ad['title'] or 'без названия'}"
                               + (f"\n💶 {ad['price']}" if ad.get("price") else "")
                               + f"\n{ad['url']}\nПрофиль: {PROFILE_URL.format(uid=key)}")
                # объединяем, а не заменяем: временно скрытое объявление не придёт повторно
                w["seen"] = sorted(seen | set(current), key=int)[-3000:]
            lo, hi = self.cfg["monitor_minutes_min"], self.cfg["monitor_minutes_max"]
            w["next"] = now + random.uniform(lo, hi) * 60
        return out

    def monitor_loop(self) -> None:
        while True:
            try:
                due = [k for k, w in list(self.state["watch"].items()) if w.get("next", 0) <= time.time()]
                for key in due:
                    messages = self.check_watch(key)
                    self.save()
                    for text in messages:
                        self.broadcast(text)
            except Exception:
                log.exception("Ошибка мониторинга")
            self.monitor_wakeup.wait(15)
            self.monitor_wakeup.clear()

    # --- обработка входящих ---
    def ask_confirm(self, chat_id: str, target: Target, source: str) -> None:
        with self.lock:
            self.state["pending"][chat_id] = target.to_dict()
            self.save()
        self.tg.send(chat_id, f"{source}:\n{target.summary()}\n\nВсё верно? Если нет — пришли исправленный текст.",
                     [[{"text": "🔍 Искать", "callback_data": "go"}, {"text": "❌ Отмена", "callback_data": "cancel"}]])

    def confirm(self, chat_id: str) -> None:
        pend = self.state["pending"].pop(chat_id, None)
        if not pend:
            self.tg.send(chat_id, "Нечего подтверждать — пришли данные пользователя.")
            return
        if self.job_running():
            self.state["pending"][chat_id] = pend
            self.tg.send(chat_id, "Уже идёт поиск. /stop — остановить текущий, потом нажми «Искать» ещё раз.")
            return
        target = Target.from_dict(pend)
        self.tg.send(chat_id, f"🚀 Запускаю поиск: {target.name}, {fmt_date(target.aktiv_seit)}")
        self.start_job(Job(target=target.to_dict()))

    def handle_photo(self, chat_id: str, file_id: str) -> None:
        self.tg.send(chat_id, "📸 Распознаю скриншот…")
        try:
            text = ocr_image(self.tg.download(file_id), self.cfg["tesseract_cmd"])
        except Exception as exc:
            self.tg.send(chat_id, f"Ошибка OCR: {exc}")
            return
        log.info("OCR:\n%s", text)
        name, d, badges, followers, online = parse_ocr_text(text)
        if not name or not d:
            self.tg.send(chat_id, "Не смог распознать имя или дату. Пришли данные текстом:\n"
                                  f"Имя: {name or '?'}\nДата регистрации: {fmt_date(d) if d else '?'}\n"
                                  f"Плашки: {badges_str(badges)}")
            return
        self.ask_confirm(chat_id, Target(name, d, badges, followers, online), "Распознано со скриншота")

    def handle_text(self, chat_id: str, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd in ("/start", "/help", "/find"):
            self.tg.send(chat_id, HELP)
        elif cmd in ("/go", "/search"):
            self.confirm(chat_id)
        elif cmd in ("/cancel", "/clear"):
            self.state["pending"].pop(chat_id, None)
            self.save()
            self.tg.send(chat_id, "Ввод сброшен.")
        elif cmd == "/status":
            self.tg.send(chat_id, self.status_text())
        elif cmd == "/pause":
            self.pause_event.set()
            self.tg.send(chat_id, "⏸ Пауза. /resume — продолжить.")
        elif cmd == "/resume":
            if self.job_running():
                self.pause_event.clear()
                self.tg.send(chat_id, "▶️ Продолжаю.")
            elif self.job and self.job.status in ("stopped", "failed", "locating", "scanning") \
                    and (self.job.cursor is None or self.job.cursor <= (self.job.last or 0)):
                self.tg.send(chat_id, "▶️ Продолжаю сохранённый поиск с места остановки.")
                self.job.message = ""
                self.start_job(self.job)
            else:
                self.tg.send(chat_id, "Нечего продолжать.")
        elif cmd == "/stop":
            if self.job_running():
                self.stop_event.set()
                self.pause_event.clear()
                self.tg.send(chat_id, "⏹ Останавливаю… (прогресс сохранится, /resume продолжит)")
            else:
                self.tg.send(chat_id, "Поиск не идёт.")
        elif cmd == "/recheck":
            self.recheck(chat_id)
        elif cmd == "/watch":
            m = re.search(r"userId=(\d+)|^(\d+)", arg)
            if not m:
                self.tg.send(chat_id, "Формат: /watch <user_id или ссылка на профиль>")
            elif not self.add_watch(int(m.group(1) or m.group(2))):
                self.tg.send(chat_id, "Уже в списке.")
        elif cmd == "/unwatch":
            with self.lock:
                removed = self.state["watch"].pop(arg, None)
                self.save()
            self.tg.send(chat_id, "Удалён из мониторинга." if removed else "Такого ID нет в списке.")
        elif cmd == "/watchlist":
            w = self.state["watch"]
            self.tg.send(chat_id, "\n".join(f"• {k} {v.get('name', '')} — {len(v.get('seen') or [])} объявл."
                                            for k, v in w.items()) or "Список пуст.")
        elif cmd == "/check":
            for w in self.state["watch"].values():
                w["next"] = 0
            self.monitor_wakeup.set()
            self.tg.send(chat_id, "Проверяю отслеживаемые аккаунты…")
        elif cmd in ("/anchor", "/add"):
            parts = arg.split()
            d = parse_date(parts[1]) if len(parts) == 2 else None
            if not d or not parts[0].isdigit():
                self.tg.send(chat_id, "Формат: /anchor <user_id> <дд.мм.гггг>")
            else:
                self.anchors.add(int(parts[0]), d)
                self.anchors.flush()
                self.tg.send(chat_id, f"Добавлено: {parts[0]} → {fmt_date(d)} (всего {len(self.anchors):,})")
        elif cmd.startswith("/"):
            self.tg.send(chat_id, "Не знаю такую команду. /help")
        else:
            target = parse_target_text(text)
            if target:
                self.ask_confirm(chat_id, target, "Понял так")
            else:
                self.tg.send(chat_id, "Не понял. Нужны минимум имя и дата регистрации. Пример:\n"
                                      "Имя: Manuela\nДата регистрации: 08.02.2017\nПлашки: TOP Zufriedenheit")

    def recheck(self, chat_id: str) -> None:
        if self.job_running():
            self.tg.send(chat_id, "Сначала дождись окончания или /stop.")
            return
        if not self.job or not self.job.error_ids:
            self.tg.send(chat_id, "ID с ошибками нет.")
            return
        job = self.job
        self.tg.send(chat_id, f"🔁 Перепроверяю {len(job.error_ids)} ID…")
        self.stop_event.clear()

        def _run():
            Scanner(self.fetcher, self.cfg, self.stop_event, self.pause_event, self._on_match, self.save).recheck(job)
            self.save()
            self.broadcast(self.report(job))

        self.job_thread = threading.Thread(target=_run, name="recheck", daemon=True)
        self.job_thread.start()

    def handle_update(self, upd: dict) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            chat_id = str(cq["message"]["chat"]["id"])
            self.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
            if chat_id not in self.cfg["allowed_chat_ids"]:
                return
            data = cq.get("data", "")
            if data == "go":
                self.confirm(chat_id)
            elif data == "cancel":
                self.state["pending"].pop(chat_id, None)
                self.tg.send(chat_id, "Отменено.")
            elif data.startswith("watch:"):
                if not self.add_watch(int(data.split(":", 1)[1])):
                    self.tg.send(chat_id, "Уже в списке.")
            self.save()
            return
        msg = upd.get("message")
        if not msg:
            return
        chat_id = str(msg["chat"]["id"])
        if chat_id not in self.cfg["allowed_chat_ids"]:
            log.warning("Сообщение от чужого chat_id %s — игнор", chat_id)
            if not self.cfg["allowed_chat_ids"]:
                self.tg.send(chat_id, f"Твой chat_id: {chat_id}\nДобавь его в allowed_chat_ids в config.json "
                                      f"и перезапусти бота.")
            return
        if msg.get("photo"):
            threading.Thread(target=self.handle_photo, args=(chat_id, msg["photo"][-1]["file_id"]),
                             daemon=True).start()
        elif msg.get("document", {}).get("mime_type", "").startswith("image/"):
            threading.Thread(target=self.handle_photo, args=(chat_id, msg["document"]["file_id"]),
                             daemon=True).start()
        elif "text" in msg:
            self.handle_text(chat_id, msg["text"].strip())

    # --- главный цикл ---
    def run(self) -> None:
        me = self.tg.call("getMe")
        if not me:
            sys.exit("❌ Telegram не отвечает или неверный telegram_token")
        log.info("Бот @%s запущен. Разрешённые чаты: %s", me.get("username"), self.cfg["allowed_chat_ids"] or "—")
        if self.cfg.get("proxy_url"):
            log.info("Прокси для сайта: включён")
        threading.Thread(target=self.monitor_loop, name="monitor", daemon=True).start()
        if self.job and self.job.status in ("locating", "scanning"):
            self.broadcast("♻️ Бот перезапущен — продолжаю незаконченный поиск.")
            self.start_job(self.job)
        else:
            self.broadcast("🤖 Бот запущен. /help — инструкция.")
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
                self.stop_event.set()
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


def main() -> None:
    if "--selftest" in sys.argv:
        import unittest
        sys.path.insert(0, str(BASE_DIR / "tests"))
        suite = unittest.defaultTestLoader.discover(str(BASE_DIR / "tests"))
        sys.exit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
    cfg_path = BASE_DIR / "config.json"
    if not cfg_path.exists():
        sys.exit("❌ Нет config.json. Скопируй config.example.json → config.json и впиши токен бота.")
    cfg = load_config(cfg_path)
    if not cfg["telegram_token"]:
        sys.exit("❌ Не задан telegram_token (config.json или переменная KA_TELEGRAM_TOKEN)")
    setup_logging((BASE_DIR / cfg["data_dir"]).resolve())
    Bot(cfg).run()


if __name__ == "__main__":
    main()
