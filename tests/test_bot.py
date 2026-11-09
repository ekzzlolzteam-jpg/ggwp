"""Офлайн-тесты: парсеры, поиск диапазона и перебор на «фейковом сайте», мониторинг.

Запуск:  python kleinanzeigen_bot.py --selftest    или    python -m unittest discover tests
"""
import random
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kleinanzeigen_bot as kb  # noqa: E402

CFG = dict(kb.DEFAULT_CONFIG, requests_per_second=1000, workers=4, chunk_size=50, max_retries=2,
           locate_max_requests=800, boundary_precision=30, seed_anchors=[])

PROFILE_HTML = """<html><head><title>Bestandsliste von Manuela | kleinanzeigen</title></head><body>
<section class="userprofile">
  <div class="userprofile-vip">
    <h2 class="userprofile--name"><span class="sr-only">Profil von</span> Manuela </h2>
    <span>Privater Nutzer</span> <span>Aktiv seit 08.02.2017</span>
    <div class="userbadges"><span>TOP</span> <span>Zufriedenheit</span>
      <span>Besonders freundlich</span></div>
    <span>12 Anzeigen online / 40 gesamt</span> <span>3 Follower</span>
  </div>
</section>
<ul id="srchrslt-adtable">
  <li><article class="aditem" data-adid="2712345678" data-href="/s-anzeige/fahrrad/2712345678-217-1234">
    <h2><a class="ellipsis" href="/s-anzeige/fahrrad/2712345678-217-1234">Fahrrad sehr zuverlässig</a></h2>
    <p class="aditem-main--middle--price-shipping--price">50 € VB</p></article></li>
  <li><article class="aditem" data-adid="2799999999">
    <a href="/s-anzeige/lampe/2799999999-80-5555">Lampe</a></article></li>
</ul></body></html>"""


class FakeSite:
    """ID растут вместе с датой регистрации; ~40% ID пустые; часть запросов падает с ошибкой."""

    def __init__(self, start_id=40_000_000, per_day=3000, days=400, base=date(2016, 6, 1), seed=1,
                 error_rate=0.05):
        self.start_id, self.per_day, self.days, self.base = start_id, per_day, days, base
        self.rnd = random.Random(seed)
        self.error_rate = error_rate
        self.special = {}
        self.calls = 0
        self.lock = threading.Lock()

    def date_of(self, uid):
        if uid < self.start_id or uid >= self.start_id + self.per_day * self.days:
            return None
        return self.base + timedelta(days=(uid - self.start_id) // self.per_day)

    def exists(self, uid):
        return uid in self.special or (self.date_of(uid) is not None and (uid * 2654435761) % 10 >= 4)

    def ids_on(self, d):
        first = self.start_id + (d - self.base).days * self.per_day
        return [u for u in range(first, first + self.per_day) if self.exists(u)]

    def get(self, uid, stop=None):
        with self.lock:
            self.calls += 1
            fail = self.rnd.random() < self.error_rate
        if stop is not None and stop.is_set():
            return kb.FetchResult("error", error="stopped")
        if fail:
            return kb.FetchResult("error", http_code=403, error="HTTP 403")
        if not self.exists(uid):
            return kb.FetchResult("missing", http_code=404)
        prof = self.special.get(uid) or kb.Profile(uid, name=f"user{uid}", aktiv_seit=self.date_of(uid),
                                                   badges={"satisfaction": "OK Zufriedenheit"})
        return kb.FetchResult("ok", profile=prof, http_code=200)


def anchors(tmp, pairs):
    store = kb.AnchorStore(Path(tmp) / "a.txt")
    for uid, d in pairs:
        store.add(uid, d)
    return store


class TestParsing(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(kb.parse_date("08.02.2017"), date(2017, 2, 8))
        self.assertEqual(kb.parse_date("Aktiv seit 8/2/17"), date(2017, 2, 8))
        self.assertIsNone(kb.parse_date("31.02.2017"))

    def test_badges(self):
        self.assertEqual(kb.detect_badges("TOP Zufriedenheit Besonders freundlich Sehr zuverlässig"),
                         {"satisfaction": "TOP Zufriedenheit", "friendliness": "Besonders freundlich",
                          "reliability": "Sehr zuverlässig"})
        self.assertEqual(kb.detect_badges("NAJA\nZufriedenheit  zuverlassig"),
                         {"satisfaction": "Naja Zufriedenheit", "reliability": "Zuverlässig"})
        self.assertEqual(kb.detect_badges("top, sehr freundlich", shorthand=True),
                         {"satisfaction": "TOP Zufriedenheit", "friendliness": "Sehr freundlich"})
        self.assertEqual(kb.detect_badges("freundliche Grüße"), {})

    def test_names(self):
        self.assertTrue(kb.names_equal("Jürgen ", "juergen"))
        self.assertTrue(kb.names_equal("Jürgen", "Jurgen"))
        self.assertFalse(kb.names_equal("Manuela", "Manuel"))

    def test_profile_html(self):
        p = kb.parse_profile_html(PROFILE_HTML, 123)
        self.assertEqual(p.name, "Manuela")
        self.assertEqual(p.aktiv_seit, date(2017, 2, 8))
        # «sehr zuverlässig» из названия объявления НЕ должно стать плашкой
        self.assertEqual(p.badges, {"satisfaction": "TOP Zufriedenheit", "friendliness": "Besonders freundlich"})
        self.assertEqual((p.online_ads, p.total_ads, p.followers), (12, 40, 3))
        self.assertEqual(set(p.listings), {"2712345678", "2799999999"})
        self.assertEqual(p.listings["2712345678"]["price"], "50 € VB")
        self.assertTrue(p.listings["2799999999"]["url"].startswith("https://www.kleinanzeigen.de/s-anzeige/"))

    def test_target_text_formats(self):
        t = kb.parse_target_text("Имя: Manuela\nДата регистрации: 08.02.2017\nПлашки: TOP Zufriedenheit, "
                                 "Besonders freundlich\nПодписчики: 3\nОбъявления онлайн: -")
        self.assertEqual((t.name, t.aktiv_seit, t.followers, t.online_ads), ("Manuela", date(2017, 2, 8), 3, None))
        self.assertEqual(t.badges["friendliness"], "Besonders freundlich")
        t = kb.parse_target_text("vondeliasa\n08.02.2017\nnaja\n-\n5")
        self.assertEqual((t.name, t.badges, t.online_ads), ("vondeliasa", {"satisfaction": "Naja Zufriedenheit"}, 5))
        t = kb.parse_target_text("Manuela 08.02.2017 top, sehr zuverlässig")
        self.assertEqual(t.badges, {"satisfaction": "TOP Zufriedenheit", "reliability": "Sehr zuverlässig"})
        self.assertIsNone(kb.parse_target_text("привет"))
        # повторно отправленный ответ бота тоже понимается
        same = kb.parse_target_text("Понял так:\n" + t.summary())
        self.assertEqual(same.badges, t.badges)

    def test_ocr_text(self):
        text = "kleinanzeigen\n\nManuela\nPrivater Nutzer · Aktiv seit 08.02.2017\nTOP\nZufriedenheit\n" \
               "Besonders\nfreundlich\n12 Anzeigen online / 40 gesamt\n3 Follower"
        name, d, badges, followers, online = kb.parse_ocr_text(text)
        self.assertEqual((name, d, followers, online), ("Manuela", date(2017, 2, 8), 3, 12))
        self.assertEqual(badges, {"satisfaction": "TOP Zufriedenheit", "friendliness": "Besonders freundlich"})


class TestLocator(unittest.TestCase):
    def check(self, site, store, target):
        loc = kb.RangeLocator(site, store, CFG)
        first, last = loc.locate(target)
        real = site.ids_on(target)
        self.assertLessEqual(first, real[0], "потерян начальный ID дня")
        self.assertGreaterEqual(last, real[-1], "потерян конечный ID дня")
        extra = (last - first + 1) - (real[-1] - real[0] + 1)
        self.assertLess(extra, 400, f"слишком широкий диапазон (+{extra})")
        self.assertLess(loc.requests, CFG["locate_max_requests"])
        return loc

    def test_anchor_far_above(self):
        site = FakeSite()
        with tempfile.TemporaryDirectory() as tmp:
            self.check(site, anchors(tmp, [(site.start_id + 300 * 3000 + 17, site.date_of(site.start_id + 300 * 3000 + 17))]),
                       date(2016, 8, 20))

    def test_anchor_far_below_with_wrong_rate(self):
        site = FakeSite(per_day=9000, days=200)   # реальная скорость ~3x ниже users_per_day из конфига
        cfg_rate = dict(CFG, users_per_day=25000)
        with tempfile.TemporaryDirectory() as tmp:
            store = anchors(tmp, [(site.start_id + 5, site.base)])
            loc = kb.RangeLocator(site, store, cfg_rate)
            target = date(2016, 11, 3)
            first, last = loc.locate(target)
            real = site.ids_on(target)
            self.assertLessEqual(first, real[0])
            self.assertGreaterEqual(last, real[-1])

    def test_bracketing_anchors_and_learning(self):
        site = FakeSite()
        with tempfile.TemporaryDirectory() as tmp:
            store = anchors(tmp, [(site.start_id + 10 * 3000 + 1, site.date_of(site.start_id + 10 * 3000 + 1)),
                                  (site.start_id + 390 * 3000 + 1, site.date_of(site.start_id + 390 * 3000 + 1))])
            before = len(store)
            self.check(site, store, date(2017, 3, 1))
            self.assertGreater(len(store), before, "база якорей должна пополняться")

    def test_empty_anchors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(kb.LocateError):
                kb.RangeLocator(FakeSite(), kb.AnchorStore(Path(tmp) / "x.txt"), CFG).locate(date(2017, 1, 1))


class TestScanner(unittest.TestCase):
    def run_scan(self, site, target, cfg=CFG, first=None, last=None):
        d = target.aktiv_seit
        ids = site.ids_on(d)
        job = kb.Job(target=target.to_dict(), first=first or ids[0] - 10, last=last or ids[-1] + 10)
        found = []
        sc = kb.Scanner(site, cfg, threading.Event(), threading.Event(), found.append, lambda: None)
        sc.run(job)
        return job, found

    def test_finds_exact_and_conflict(self):
        site = FakeSite(error_rate=0.1)
        d = date(2016, 9, 9)
        ids = site.ids_on(d)
        target = kb.Target("Manuela", d, {"satisfaction": "TOP Zufriedenheit", "friendliness": "Besonders freundlich"})
        site.special[ids[100]] = kb.Profile(ids[100], "Manuela", d, {"satisfaction": "OK Zufriedenheit"})
        site.special[ids[1500]] = kb.Profile(ids[1500], "manuela", d, dict(target.badges), followers=3)
        job, found = self.run_scan(site, target, dict(CFG, stop_on_exact_match=False))
        levels = {m["user_id"]: m["level"] for m in job.matches}
        self.assertEqual(levels, {ids[100]: "conflict", ids[1500]: "exact"})
        # 10% запросов падает, а у фейка нет встроенных повторов — после 3 кругов перепроверки
        # должно остаться ~0 ошибок, и ни один ID не должен «потеряться»
        self.assertLessEqual(len(job.error_ids), 3)
        self.assertEqual(job.checked + len(job.error_ids), job.total)

    def test_stop_on_exact(self):
        site = FakeSite(error_rate=0)
        d = date(2016, 9, 9)
        ids = site.ids_on(d)
        target = kb.Target("Manuela", d, {})
        site.special[ids[10]] = kb.Profile(ids[10], "Manuela", d, {})
        job, _ = self.run_scan(site, target)
        self.assertTrue(job.has_exact())
        self.assertLess(job.cursor, job.last, "после точного совпадения перебор должен остановиться")

    def test_partial_when_badge_unknown_or_name_fuzzy(self):
        d = date(2017, 2, 8)
        t = kb.Target("vondeliasa", d, {"satisfaction": "Naja Zufriedenheit"})
        self.assertEqual(kb.match_profile(t, kb.Profile(1, "vondeliasa", d, {})).level, "partial")
        self.assertEqual(kb.match_profile(t, kb.Profile(1, "vondelisa", d, {"satisfaction": "Naja Zufriedenheit"})).level,
                         "partial")
        self.assertIsNone(kb.match_profile(t, kb.Profile(1, "vondeliasa", date(2017, 2, 9), {})))
        self.assertIsNone(kb.match_profile(t, kb.Profile(1, "peter", d, {})))


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, buttons=None):
        self.sent.append(text)

    def call(self, *a, **k):
        return {}


class TestMonitor(unittest.TestCase):
    def test_baseline_new_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = kb.Bot(dict(CFG, data_dir=tmp, allowed_chat_ids=["1"], telegram_token="x"))
            bot.tg = FakeTG()
            pages = []

            class F:
                def get(self, uid, stop=None):
                    return pages.pop(0)

            bot.fetcher = F()

            def page(ad_ids):
                listings = {a: {"title": f"Ad {a}", "url": f"u/{a}", "price": ""} for a in ad_ids}
                return kb.FetchResult("ok", profile=kb.Profile(5, "Manuela", listings=listings))

            bot.state["watch"]["5"] = {"name": "", "seen": None, "next": 0, "fails": 0}
            pages += [page(["1", "2"]), kb.FetchResult("error", error="HTTP 403"), page(["1", "3"]), page(["1", "2", "3"])]
            msgs = [bot.check_watch("5") for _ in range(4)]
            self.assertIn("сейчас 2 объявл.", msgs[0][0])            # первый раз — без спама
            self.assertEqual(msgs[1], [])                              # ошибка не сбрасывает «виденные»
            self.assertEqual(len(msgs[2]), 1)
            self.assertIn("Ad 3", msgs[2][0])
            self.assertEqual(msgs[3], [])                              # «2» вернулось — не новое


if __name__ == "__main__":
    unittest.main()


# =====================================================================
# Реальные страницы и скриншоты (tests/samples)
# =====================================================================
SAMPLES = Path(__file__).resolve().parent / "samples"

REAL_PAGES = {
    # user_id: (имя, дата, плашки, подписчики)
    64785484: ("Sally & Patrick", date(2019, 6, 16),
               {"satisfaction": "TOP Zufriedenheit", "friendliness": "Besonders freundlich",
                "reliability": "Besonders zuverlässig"}, 6),
    43061549: ("Alex", date(2016, 10, 25),
               {"satisfaction": "TOP Zufriedenheit", "friendliness": "Freundlich", "reliability": "Zuverlässig"}, None),
    43538220: ("Christian", date(2016, 11, 16),
               {"satisfaction": "TOP Zufriedenheit", "friendliness": "Freundlich",
                "reliability": "Sehr zuverlässig"}, 7),
    53898451: ("Linda Kglhd", date(2018, 3, 3),
               {"satisfaction": "TOP Zufriedenheit", "friendliness": "Sehr freundlich",
                "reliability": "Sehr zuverlässig"}, None),
    26402834: ("IBV Verwaltung", date(2014, 12, 1), {}, 1151),
    164857427: ("Noah", date(2026, 7, 23), {}, None),
}

# сырой вывод Tesseract 5 (deu+eng) по присланным скриншотам — чтобы тест работал и без Tesseract
OCR_RAW = {
    "screen_willi.png": "Willi\n\n0 Anzeigen online f=] Verkaufsübersicht >\nDeine Einnahmen über unser "
                        "Bezahlsystem\n\nA Privater Nutzer N Aktiv seit 01.11.2024 ©",
    "screen_arne.png": "Arne Schwarzmann\nAS 2 Anzeigen online / 138 gesamt Verkaufsübersicht\n\nDeine Einnahmen "
                       "über unser Bezahlsystem\n© TOP Zufriedenheit db Zuverlässig\n\n[FE] Zum Immobilien-Profil |NEU\n"
                       "= Prasentiere dich optimal für dein neues Zuhause\n\nA Privater Nutzer N Aktiv seit 05.03.2016 "
                       "[0) 7 6 Folge ich, 4 Follower",
    "screen_privat.png": "Privat\n\n39 Anzeigen online / 231 gesamt f=] Verkaufsubersicht >\nDeine Einnahmen über "
                         "unser Bezahlsystem\n\n@ TOP Zufriedenheit ‚& Freundlich\n\n& Sehr zuverlässig\n\nA Privater "
                         "Nutzer N Aktiv seit 17.08.2010 © Antwortet in der Regel innerhalb von 3 Stunden % 1 Folge "
                         "ich, 5 Follower",
}
OCR_EXPECTED = {
    "screen_willi.png": ("Willi", date(2024, 11, 1), {}, None, 0),
    "screen_arne.png": ("Arne Schwarzmann", date(2016, 3, 5),
                        {"satisfaction": "TOP Zufriedenheit", "reliability": "Zuverlässig"}, 4, 2),
    "screen_privat.png": ("Privat", date(2010, 8, 17),
                          {"satisfaction": "TOP Zufriedenheit", "friendliness": "Freundlich",
                           "reliability": "Sehr zuverlässig"}, 5, 39),
}


class TestRealPages(unittest.TestCase):
    def test_profiles(self):
        for uid, (name, d, badges, followers) in REAL_PAGES.items():
            with self.subTest(uid=uid):
                p = kb.parse_profile_html((SAMPLES / f"profile_{uid}.html").read_text(encoding="utf-8"), uid)
                self.assertEqual((p.name, p.aktiv_seit, p.badges, p.followers, p.page_user_id),
                                 (name, d, badges, followers, uid))
                self.assertTrue(p.listings, "объявления должны находиться")
                for ad_id, ad in p.listings.items():
                    self.assertTrue(ad["title"] and ad["url"].startswith("https://www.kleinanzeigen.de/s-anzeige/"))
                    self.assertIn(ad_id, ad["url"])
                self.assertTrue(kb.looks_like_profile(p and (SAMPLES / f"profile_{uid}.html").read_text(encoding="utf-8")))

    def test_level_fallback_when_label_missing(self):
        html = ('<div class="profile-userbadges"><button data-badge-name="friendliness" data-badge-level="1"></button>'
                '<button data-badge-name="reliability" data-badge-level="2"></button></div>Aktiv seit 01.01.2020')
        self.assertEqual(kb.parse_profile_html(html, 1).badges,
                         {"friendliness": "Sehr freundlich", "reliability": "Besonders zuverlässig"})

    def test_target_from_screenshot_matches_page(self):
        """Скрин → цель → сравнение с HTML той же «формы» даёт точное совпадение."""
        page = kb.parse_profile_html((SAMPLES / "profile_43538220.html").read_text(encoding="utf-8"), 43538220)
        target = kb.parse_target_text("Christian\n16.11.2016\nTOP, Freundlich, Sehr zuverlässig")
        self.assertEqual(kb.match_profile(target, page).level, "exact")
        wrong = kb.parse_target_text("Christian\n16.11.2016\nTOP, Sehr freundlich")
        self.assertEqual(kb.match_profile(wrong, page).level, "conflict")

    def test_anchor_file_from_repo(self):
        store = kb.AnchorStore(Path(tempfile.mkdtemp()) / "x.txt", extra_files=kb.LEGACY_ANCHOR_FILES)
        items = store.items()
        self.assertGreaterEqual(len(items), 160)
        dates = [d for _, d in items]
        self.assertEqual(dates, sorted(dates), "в known_accounts ID и даты должны расти вместе")


class TestRealOCR(unittest.TestCase):
    def test_recorded_ocr_text(self):
        for fname, expected in OCR_EXPECTED.items():
            with self.subTest(fname=fname):
                self.assertEqual(kb.parse_ocr_text(OCR_RAW[fname]), expected)

    def test_initials_never_become_name(self):
        self.assertEqual(kb.parse_ocr_text("AS\nArne Schwarzmann\nAS\n2 Anzeigen online\nAktiv seit 05.03.2016")[0],
                         "Arne Schwarzmann")
        self.assertEqual(kb.parse_ocr_text("W\nWilli\n0 Anzeigen online\nAktiv seit 01.11.2024")[0], "Willi")

    def test_real_tesseract(self):
        try:
            import pytesseract
            pytesseract.get_tesseract_version()
        except Exception:
            self.skipTest("Tesseract не установлен")
        for fname, expected in OCR_EXPECTED.items():
            with self.subTest(fname=fname):
                text = kb.ocr_image((SAMPLES / fname).read_bytes())
                self.assertEqual(kb.parse_ocr_text(text)[:3], expected[:3])
