#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kleinanzeigen: СТРОГИЙ поиск аккаунта (находит и после смены имени).

Отдельный бот. Сеть, прокси, поиск диапазона ID, перебор, мониторинг и меню
берутся из kleinanzeigen_bot.py — здесь только то, что отличается: правила сравнения.

Правила:
  • дата регистрации и плашки — жёсткий фильтр: должны совпасть точно;
  • подписчики — ± followers_tolerance (по умолчанию 2);
  • объявления онлайн — ± погрешность, которая задаётся ДЛЯ КАЖДОГО поиска (можно 0);
  • время ответа — на странице не больше, чем на скрине (может только уменьшиться);
  • имя — признак, а не фильтр: если всё остальное сошлось, а имя другое,
    профиль показывается как «возможно, сменил имя».

Запуск:  python strict_bot.py            (первый раз сам спросит токен и создаст strict_config.json)
         python strict_bot.py --setup    (повторить настройку)
Настройки — в strict_config.json, данные — в папке data_strict (со старым ботом не пересекаются).
Нужен СВОЙ токен бота: два бота с одним токеном одновременно работать не могут.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from typing import Optional

import kleinanzeigen_bot as base
from kleinanzeigen_bot import Match, Profile, btn, fmt_date, log, parse_date

STRICT_DEFAULTS: dict = dict(
    base.DEFAULT_CONFIG,
    data_dir="data_strict",
    followers_tolerance=2,       # подписчики: ± столько
    online_ads_tolerance=2,      # объявления онлайн: погрешность по умолчанию (меняется в каждом поиске)
    max_live_matches=15,         # столько совпадений присылаем сразу, остальные — только в итоговом отчёте
)
CONFIG_NAME = "strict_config.json"
ADS_PAGE_SIZE = 25               # столько объявлений сайт показывает на первой странице профиля


# =====================================================================
# ВРЕМЯ ОТВЕТА
# =====================================================================
_UNIT_MINUTES = (("мин", 1), ("min", 1), ("м", 1), ("m", 1),
                 ("ч", 60), ("h", 60), ("std", 60), ("stunde", 60),
                 ("д", 1440), ("сут", 1440), ("tag", 1440), ("d", 1440))
_RESPONSE_INPUT_RE = re.compile(r"(\d+)\s*([a-zа-яё]+)", re.I)


def parse_response_input(text: str) -> Optional[int]:
    """Время ответа из OCR или от пользователя: «…innerhalb von 3 Stunden», «3 ч», «10 мин», «1 день»."""
    minutes = base.parse_response_minutes(text)
    if minutes is not None:
        return minutes
    m = _RESPONSE_INPUT_RE.search(text or "")
    if not m:
        return None
    unit = m.group(2).lower()
    for prefix, mult in _UNIT_MINUTES:
        if unit.startswith(prefix):
            return int(m.group(1)) * mult
    return None


def fmt_response(minutes: Optional[int]) -> str:
    if minutes is None:
        return "—"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} дн"
    if minutes % 60 == 0:
        return f"{minutes // 60} ч"
    return f"{minutes} мин"


# =====================================================================
# ЦЕЛЬ
# =====================================================================
@dataclass
class StrictTarget(base.Target):
    response_minutes: Optional[int] = None    # время ответа на скрине; None — не проверять
    ads_tolerance: int = 2                    # погрешность «объявлений онлайн» для этого поиска

    @classmethod
    def from_dict(cls, d: dict) -> "StrictTarget":
        return cls(name=d["name"], aktiv_seit=parse_date(d["aktiv_seit"]), badges=d.get("badges") or {},
                   followers=d.get("followers"), online_ads=d.get("online_ads"), total_ads=d.get("total_ads"),
                   response_minutes=d.get("response_minutes"),
                   ads_tolerance=int(d.get("ads_tolerance") or 0))

    def features(self) -> int:
        """Сколько у цели примет помимо имени и даты. Мало примет — без имени искать нельзя:
        «пустых» профилей за день сотни, и все они подошли бы."""
        return (len(self.badges) + (self.response_minutes is not None)
                + (self.followers is not None and self.followers >= 3)
                + (self.online_ads is not None and self.online_ads >= 1))

    def name_optional(self) -> bool:
        return self.features() >= 2


def page_ads(prof: Profile) -> tuple[int, bool]:
    """(сколько объявлений онлайн видно на странице, упёрлись ли в размер страницы).
    Счётчика «N Anzeigen online» в HTML обычно нет — считаем сами объявления."""
    if prof.online_ads is not None:
        return prof.online_ads, False
    n = len(prof.listings)
    return n, n >= ADS_PAGE_SIZE


# =====================================================================
# СРАВНЕНИЕ
# =====================================================================
def strict_match(target: StrictTarget, prof: Profile, cfg: dict) -> Optional[Match]:
    # --- жёсткие фильтры: дата регистрации и плашки ---
    if prof.aktiv_seit != target.aktiv_seit:
        return None
    if dict(prof.badges) != dict(target.badges):
        return None
    notes: list[str] = []
    unknown = False
    # --- подписчики: ± followers_tolerance ---
    if target.followers is not None:
        have = prof.followers or 0            # строки «N Follower» нет, когда подписчиков 0
        if abs(have - target.followers) > int(cfg.get("followers_tolerance", 2)):
            return None
        if have != target.followers:
            notes.append(f"подписчики: {target.followers} → {have}")
    # --- объявления онлайн: ± погрешность этого поиска ---
    if target.online_ads is not None:
        have, capped = page_ads(prof)
        tol = max(0, int(target.ads_tolerance))
        if capped:
            if target.online_ads + tol < have:
                return None
            notes.append(f"объявлений на странице {have}+ (точное число не видно)")
        else:
            if abs(have - target.online_ads) > tol:
                return None
            if have != target.online_ads:
                notes.append(f"объявления онлайн: {target.online_ads} → {have}")
    # --- время ответа: только такое же или меньше ---
    if target.response_minutes is not None:
        if prof.response_minutes is None:
            unknown = True
            notes.append("время ответа на странице не показано")
        elif prof.response_minutes > target.response_minutes:
            return None
        elif prof.response_minutes < target.response_minutes:
            notes.append(f"время ответа: {fmt_response(target.response_minutes)} → "
                         f"{fmt_response(prof.response_minutes)}")
    # --- имя: признак ---
    name = prof.name or ""
    if name and base.names_equal(target.name, name):
        name_ok = True
    else:
        name_ok = False
        sim = base.name_similarity(target.name, name) if name else 0.0
        if sim >= float(cfg.get("name_fuzzy_threshold", 0.85)):
            notes.insert(0, f"имя похоже ({sim:.0%}): «{name}»")
        elif not target.name_optional():
            return None                       # примет мало — без совпадения имени не берём
        else:
            notes.insert(0, f"имя другое: «{name or '—'}» (ждали «{target.name}») — возможно, сменил")
    level = "exact" if name_ok and not unknown else "partial"
    return Match(level, prof.user_id, name or "(без имени)", notes)


class StrictScanner(base.Scanner):
    def load_target(self, d: dict) -> StrictTarget:
        return StrictTarget.from_dict(d)

    def match(self, target, prof: Profile) -> Optional[Match]:
        return strict_match(target, prof, self.cfg)


# =====================================================================
# ВВОД ЦЕЛИ: текст и OCR
# =====================================================================
EXTRA_KEYS = {
    "response_minutes": ("время ответа", "ответ", "antwortet", "response"),
    "ads_tolerance": ("погрешность объявлений", "погрешность", "допуск"),
}


def parse_target_text(text: str, default_tolerance: int = 2) -> Optional[StrictTarget]:
    """Форматы старого бота + строки «Время ответа: 3 ч» и «Погрешность объявлений: 5»."""
    t = base.parse_target_text(text)
    if t is None:
        return None
    response, tolerance = None, default_tolerance
    for ln in text.splitlines():
        if ":" not in ln:
            continue
        key, value = (p.strip() for p in ln.split(":", 1))
        key = re.sub(r"^[^\w]+", "", key).lower()
        if key in EXTRA_KEYS["response_minutes"]:
            response = parse_response_input(value)
        elif key in EXTRA_KEYS["ads_tolerance"]:
            m = re.search(r"\d+", value)
            tolerance = int(m.group(0)) if m else tolerance
    return StrictTarget(name=t.name, aktiv_seit=t.aktiv_seit, badges=t.badges, followers=t.followers,
                        online_ads=t.online_ads, response_minutes=response, ads_tolerance=tolerance)


def parse_ocr(text: str) -> dict:
    """Черновик цели из текста скриншота."""
    name, d, badges, followers, online = base.parse_ocr_text(text)
    return {"name": name, "aktiv_seit": fmt_date(d) if d else None, "badges": badges, "followers": followers,
            "online_ads": online, "response_minutes": base.parse_response_minutes(text)}


# =====================================================================
# БОТ
# =====================================================================
HELP = f"""🔎 СТРОГИЙ поиск пользователя Kleinanzeigen (находит и после смены имени)

Правила сравнения:
• дата регистрации и плашки — должны совпасть точно;
• подписчики — ±2;
• объявления онлайн — ± погрешность, задаётся на карточке поиска (можно 0);
• время ответа — такое же или меньше, чем на скрине;
• имя — признак: если остальное сошлось, а имя другое — пришлю «🕵️ возможно, сменил имя».

Пришли СКРИНШОТ профиля или нажми «{base.M_SEARCH}».
Текстом:
Имя: Manuela
Дата регистрации: 08.02.2017
Плашки: top, sehr freundlich
Подписчики: 8
Объявления онлайн: 3
Время ответа: 3 ч
Погрешность объявлений: 5"""

MENU_TEXTS = (base.M_SEARCH, base.M_TASKS, base.M_STATUS, base.M_RESULTS, base.M_MONITOR, base.M_PROXY)
RESPONSE_CHOICES = (10, 30, 60, 120, 180, 360, 720, 1440, 2880)
TOLERANCE_CHOICES = (0, 1, 2, 3, 5, 10, 20, 50)
STEP_PROMPTS = {
    "x_fol": "👥 Сколько подписчиков на скрине? Напиши число (или «-», чтобы не проверять).",
    "x_ads": "📦 Сколько объявлений онлайн на скрине? Напиши число (или «-», чтобы не проверять).",
}


class StrictBot(base.Bot):
    scanner_cls = StrictScanner

    # ------------------------------------------------------------------
    # карточка поиска
    # ------------------------------------------------------------------
    def _tolerance(self, d: dict) -> int:
        tol = d.get("ads_tolerance")
        return int(self.cfg["online_ads_tolerance"] if tol is None else tol)

    def _target_from_draft(self, d: dict) -> Optional[StrictTarget]:
        date_ = parse_date(d.get("aktiv_seit") or "")
        if not d.get("name") or not date_:
            return None
        return StrictTarget(d["name"], date_, dict(d.get("badges") or {}), d.get("followers"), d.get("online_ads"),
                            response_minutes=d.get("response_minutes"), ads_tolerance=self._tolerance(d))

    def draft_text(self, d: dict) -> str:
        ftol = int(self.cfg["followers_tolerance"])
        fol, ads, resp = d.get("followers"), d.get("online_ads"), d.get("response_minutes")
        lines = [f"👤 Имя: {d.get('name') or '❓ не задано'}",
                 f"📅 Дата регистрации: {d.get('aktiv_seit') or '❓ не задана'}",
                 f"🏷 Плашки: {base.badges_str(d.get('badges') or {})}",
                 f"👥 Подписчики: {fol if fol is not None else 'не проверять'}" + (f" (±{ftol})" if fol is not None else ""),
                 f"📦 Объявления онлайн: {ads if ads is not None else 'не проверять'}",
                 f"± Погрешность объявлений: {self._tolerance(d)}",
                 f"⏱ Время ответа: {fmt_response(resp) if resp is not None else 'не проверять'}"
                 + (" (или меньше)" if resp is not None else "")]
        return "\n".join(lines)

    def screen_card(self, chat_id: str, msg_id: Optional[int] = None, title: str = "Проверь данные") -> None:
        u = self.ui(chat_id)
        u["step"], u["then"] = None, "card"
        d = u["draft"]
        target = self._target_from_draft(d)
        rows = [[btn("🔍 Искать", "c:go")]] if target else []
        rows += [[btn("✏️ Имя", "c:name"), btn("📅 Дата", "c:date"), btn("🏷 Плашки", "c:badges")],
                 [btn("👥 Подписчики", "x:fol"), btn("📦 Объявления", "x:ads"), btn("⏱ Ответ", "x:resp")],
                 [btn(f"± Погрешность объявлений: {self._tolerance(d)}", "x:tol")],
                 [btn("❌ Отмена", "c:cancel")]]
        if not target:
            hint = "\n\nНе хватает имени или даты — задай кнопками ниже."
        elif target.name_optional():
            hint = "\n\nДата и плашки — строго. Имя — признак: найду и после смены имени.\nЖми «🔍 Искать» или исправь кнопками."
        else:
            hint = ("\n\n⚠️ Мало примет (нужно хотя бы две: плашки, время ответа, подписчики от 3, объявления) — "
                    "без них профиль после смены имени не отличить от сотен других, поэтому имя тоже должно совпасть.")
        self.show(chat_id, f"{title}:\n\n{self.draft_text(d)}{hint}", rows, msg_id)

    def confirm(self, chat_id: str, msg_id: Optional[int] = None) -> None:
        target = self._target_from_draft(self.ui(chat_id)["draft"])
        if target is None:
            self.screen_card(chat_id, msg_id)
            return
        self.state["ui"][chat_id] = {"step": None, "draft": {}, "then": "flow"}
        if msg_id:
            self.tg.edit(chat_id, msg_id, f"✅ Принято:\n\n{self.draft_text(target.to_dict())}")
        self.tg.send(chat_id, self.submit_search(target), [[btn("📋 Задачи", "t:refresh")]])

    # ------------------------------------------------------------------
    # ввод новых полей
    # ------------------------------------------------------------------
    def response_picker(self, chat_id: str, msg_id: Optional[int]) -> None:
        self.ui(chat_id)["step"] = "x_resp"
        rows = base.chunked([btn(fmt_response(m), f"x:resp:{m}") for m in RESPONSE_CHOICES], 3)
        rows.append([btn("не проверять", "x:resp:none")])
        self.show(chat_id, "⏱ Время ответа на скрине («Antwortet in der Regel innerhalb von …»)?\n"
                           "Выбери или напиши: 3 ч, 20 мин, 2 дня.\n"
                           "Подойдут профили с таким же или МЕНЬШИМ временем.", rows, msg_id)

    def tolerance_picker(self, chat_id: str, msg_id: Optional[int]) -> None:
        self.ui(chat_id)["step"] = "x_tol"
        rows = base.chunked([btn(f"±{n}" if n else "0 (точно)", f"x:tol:{n}") for n in TOLERANCE_CHOICES], 4)
        self.show(chat_id, "± На сколько может отличаться число объявлений онлайн в этом поиске?\n"
                           "Выбери или напиши своё число.", rows, msg_id)

    def handle_callback(self, chat_id: str, data: str, msg_id: int) -> None:
        parts = data.split(":")
        if parts[0] != "x":
            super().handle_callback(chat_id, data, msg_id)
            return
        u = self.ui(chat_id)
        d = u["draft"]
        act, value = parts[1], (parts[2] if len(parts) > 2 else None)
        if act in ("fol", "ads"):
            u["step"] = "x_" + act
            self.show(chat_id, STEP_PROMPTS[u["step"]], [[btn("⬅️ Назад", "x:back")]], msg_id)
        elif act == "resp" and value is None:
            self.response_picker(chat_id, msg_id)
        elif act == "tol" and value is None:
            self.tolerance_picker(chat_id, msg_id)
        else:
            if act == "resp":
                d["response_minutes"] = None if value == "none" else int(value)
            elif act == "tol":
                d["ads_tolerance"] = int(value)
            self.screen_card(chat_id, msg_id)

    def handle_text(self, chat_id: str, text: str) -> None:
        u = self.ui(chat_id)
        step = u["step"]
        if text in MENU_TEXTS or text.startswith("/"):
            if step and step.startswith("x_"):
                u["step"] = None
            super().handle_text(chat_id, text)
            return
        d = u["draft"]
        if step in ("x_fol", "x_ads", "x_tol"):
            m = re.search(r"\d+", text)
            if not m and step == "x_tol":
                self.tg.send(chat_id, "Напиши число, например 5 (0 — без погрешности).")
                return
            key = {"x_fol": "followers", "x_ads": "online_ads", "x_tol": "ads_tolerance"}[step]
            d[key] = int(m.group(0)) if m else None
            self.screen_card(chat_id)
        elif step == "x_resp":
            minutes = parse_response_input(text)
            if minutes is None and text.strip().lower() not in base.EMPTY_VALUES:
                self.tg.send(chat_id, "Не понял. Примеры: 3 ч, 20 мин, 2 дня — или «-», чтобы не проверять.")
                return
            d["response_minutes"] = minutes
            self.screen_card(chat_id)
        elif step is None:
            target = parse_target_text(text, int(self.cfg["online_ads_tolerance"]))
            if target:
                self.set_draft_from_target(chat_id, target, "Понял так")
            else:
                self.tg.send(chat_id, "Не понял 🤔 Пришли скриншот профиля или нажми «" + base.M_SEARCH + "».\n"
                                      "Формат текста — в /help.", keyboard=base.MAIN_KEYBOARD)
        else:
            super().handle_text(chat_id, text)

    def handle_command(self, chat_id: str, text: str) -> None:
        if text.split()[0].split("@")[0].lower() in ("/start", "/help", "/menu"):
            self.ui(chat_id)["step"] = None
            self.tg.send(chat_id, HELP, keyboard=base.MAIN_KEYBOARD)
        else:
            super().handle_command(chat_id, text)

    def handle_photo(self, chat_id: str, file_id: str) -> None:
        self.tg.send(chat_id, "📸 Распознаю скриншот…")
        try:
            text = base.ocr_image(self.tg.download(file_id), self.cfg["tesseract_cmd"])
        except Exception as exc:
            self.tg.send(chat_id, f"Ошибка распознавания: {exc}\nМожно ввести данные через «{base.M_SEARCH}».")
            return
        log.info("OCR:\n%s", text)
        with self.lock:
            self.ui(chat_id)["draft"] = parse_ocr(text)
        self.screen_card(chat_id, title="📸 Распознано со скриншота")

    # ------------------------------------------------------------------
    # совпадения
    # ------------------------------------------------------------------
    def _on_match(self, run, m: Match) -> None:
        self.anchors.add(m.user_id, run.target.aktiv_seit)
        self.anchors.flush()
        job = run.job
        limit = int(self.cfg["max_live_matches"])
        if len(job.matches) > limit:
            if len(job.matches) == limit + 1:
                self.broadcast(f"⚠️ Поиск #{run.num}: совпадений уже больше {limit} — примет слишком мало. "
                               f"Остальные покажу только в итоговом отчёте.")
            return
        head = "🎯 НАЙДЕН ПОЛЬЗОВАТЕЛЬ!" if m.level == "exact" else "🕵️ Все приметы сошлись — возможно, сменил имя"
        notes = ("\n" + "\n".join("• " + n for n in m.notes)) if m.notes else ""
        prog = f"\nПоиск #{run.num} продолжается: проверено {job.checked / job.total * 100:.0f}% " \
               f"— если это он, останови кнопкой ниже." if job.total else ""
        self.broadcast(f"{head} (поиск #{run.num})\n{m.name} (ID {m.user_id})\n{m.url}{notes}{prog}",
                       [[btn(f"✅ Это он — остановить поиск #{run.num}", f"j:ok:{run.num}:{m.user_id}")],
                        [btn("👁 Следить за объявлениями", f"w:add:{m.user_id}")]])

    @staticmethod
    def _matches_lines(matches: list) -> list[str]:
        lines = []
        for level, title in (("exact", "🎯 Совпало всё, включая имя"),
                             ("partial", "🕵️ Приметы совпали, имя другое или не всё удалось проверить")):
            items = [m for m in matches if m["level"] == level]
            if items:
                lines.append(f"\n{title}:")
                lines += [f"• {m['name']} — {base.PROFILE_URL.format(uid=m['user_id'])}"
                          + (f" ({'; '.join(m['notes'])})" if m["notes"] else "") for m in items[:40]]
                if len(items) > 40:
                    lines.append(f"… и ещё {len(items) - 40}")
        if not matches:
            lines.append("\n❌ Совпадений не найдено.")
        return lines


# =====================================================================
# ЗАПУСК
# =====================================================================
def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    cfg_path = base.BASE_DIR / CONFIG_NAME
    if not cfg_path.exists() or "--setup" in sys.argv:
        if not (sys.stdin and sys.stdin.isatty()):
            base.fail(f"❌ Нет {CONFIG_NAME}. Запусти в консоли: python strict_bot.py --setup")
        print(f"Настройка СТРОГОГО бота → {CONFIG_NAME}. Нужен отдельный токен (новый бот у @BotFather).")
        base.setup_wizard(cfg_path)
    try:
        cfg = base.load_config(cfg_path, STRICT_DEFAULTS)
    except (ValueError, json.JSONDecodeError) as exc:
        base.fail(f"❌ Ошибка в {CONFIG_NAME}: {exc}")
    if cfg["data_dir"] == base.DEFAULT_CONFIG["data_dir"]:
        cfg["data_dir"] = STRICT_DEFAULTS["data_dir"]     # не делим state.json со старым ботом
    if not base.TOKEN_RE.match(cfg["telegram_token"] or ""):
        base.fail(f"❌ В {CONFIG_NAME} нет правильного telegram_token. Запусти: python strict_bot.py --setup")
    base.setup_logging((base.BASE_DIR / cfg["data_dir"]).resolve())
    StrictBot(cfg, cfg_path).run()


if __name__ == "__main__":
    main()
