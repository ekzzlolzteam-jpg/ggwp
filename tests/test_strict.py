"""Офлайн-тесты строгого бота (strict_bot.py): правила сравнения, ввод, карточка, перебор."""
import sys
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kleinanzeigen_bot as kb  # noqa: E402
import strict_bot as sb  # noqa: E402
from test_bot import FakeTG  # noqa: E402

SAMPLES = Path(__file__).resolve().parent / "samples"
CFG = dict(sb.STRICT_DEFAULTS, requests_per_second=1000, workers=4, chunk_size=50, max_retries=2, seed_anchors=[])
BADGES = {"satisfaction": "TOP Zufriedenheit", "friendliness": "Freundlich", "reliability": "Sehr zuverlässig"}
DAY = date(2018, 3, 7)


def target(**kw):
    base = dict(name="Max Mustermann", aktiv_seit=DAY, badges=dict(BADGES), followers=8, online_ads=1,
                response_minutes=180, ads_tolerance=0)
    base.update(kw)
    return sb.StrictTarget(**base)


def profile(**kw):
    base = dict(user_id=1, name="Privat", aktiv_seit=DAY, badges=dict(BADGES), followers=8,
                listings={"1": {}}, response_minutes=180)
    base.update(kw)
    return kb.Profile(**base)


class TestResponseTime(unittest.TestCase):
    def test_page_phrases(self):
        f = kb.parse_response_minutes
        self.assertEqual(f("Antwortet in der Regel innerhalb von 3 Stunden"), 180)
        self.assertEqual(f("Antwortet in der Regel innerhalb von 1 Stunde"), 60)
        self.assertEqual(f("Antwortet in der Regel innerhalb von 10 Minuten"), 10)
        self.assertEqual(f("Antwortet in der Regel innerhalb von 2 Tagen"), 2880)
        self.assertEqual(f("Antwortet in der Regel innerhalb einer Stunde"), 60)
        self.assertIsNone(f("8 Follower"))

    def test_user_input(self):
        f = sb.parse_response_input
        self.assertEqual(f("3 ч"), 180)
        self.assertEqual(f("3 часа"), 180)
        self.assertEqual(f("20 мин"), 20)
        self.assertEqual(f("2 дня"), 2880)
        self.assertEqual(f("12h"), 720)
        self.assertIsNone(f("-"))
        self.assertEqual(sb.fmt_response(180), "3 ч")
        self.assertEqual(sb.fmt_response(20), "20 мин")
        self.assertEqual(sb.fmt_response(2880), "2 дн")


class TestRealChangedName(unittest.TestCase):
    """Страница профиля, у которого сменили имя (теперь «Privat»)."""

    def setUp(self):
        raw = (SAMPLES / "profile_53995642.html").read_text(encoding="utf-8")
        self.prof = kb.parse_profile_html(raw, 53995642)

    def test_parsed(self):
        p = self.prof
        self.assertEqual((p.name, p.aktiv_seit, p.followers, p.response_minutes), ("Privat", DAY, 8, 180))
        self.assertEqual(p.badges, BADGES)
        self.assertEqual(sb.page_ads(p), (1, False))

    def test_found_despite_new_name(self):
        m = sb.strict_match(target(response_minutes=720, followers=7), self.prof, CFG)
        self.assertEqual(m.level, "partial")
        self.assertIn("имя другое", m.notes[0])
        self.assertTrue(any("12 ч → 3 ч" in n for n in m.notes))

    def test_exact_when_name_same(self):
        self.assertEqual(sb.strict_match(target(name="Privat"), self.prof, CFG).level, "exact")


class TestStrictMatch(unittest.TestCase):
    def ok(self, t, p):
        return sb.strict_match(t, p, CFG) is not None

    def test_date_and_badges_are_hard(self):
        self.assertFalse(self.ok(target(), profile(aktiv_seit=date(2018, 3, 8))))
        self.assertFalse(self.ok(target(), profile(aktiv_seit=None)))
        self.assertFalse(self.ok(target(), profile(badges=dict(BADGES, reliability="Zuverlässig"))))
        fewer = {k: v for k, v in BADGES.items() if k != "reliability"}
        self.assertFalse(self.ok(target(), profile(badges=fewer)))              # плашки не хватает
        self.assertFalse(self.ok(target(badges=fewer), profile()))               # лишняя плашка
        self.assertFalse(self.ok(target(name="Privat", badges={}), profile()))   # ждали без плашек

    def test_followers_plus_minus_two(self):
        for have, expect in ((6, True), (7, True), (10, True), (5, False), (11, False)):
            self.assertEqual(self.ok(target(), profile(followers=have)), expect, have)
        self.assertTrue(self.ok(target(followers=2), profile(followers=None)))   # строки нет = 0
        self.assertFalse(self.ok(target(followers=3), profile(followers=None)))
        self.assertTrue(self.ok(target(followers=None), profile(followers=500)))  # не задано — не проверяем

    def test_ads_tolerance_per_search(self):
        five = {str(i): {} for i in range(5)}
        self.assertFalse(self.ok(target(online_ads=1, ads_tolerance=0), profile(listings=five)))
        self.assertFalse(self.ok(target(online_ads=1, ads_tolerance=3), profile(listings=five)))
        self.assertTrue(self.ok(target(online_ads=1, ads_tolerance=4), profile(listings=five)))
        self.assertTrue(self.ok(target(online_ads=10, ads_tolerance=5), profile(listings=five)))
        self.assertTrue(self.ok(target(online_ads=5, ads_tolerance=0), profile(listings=five)))
        self.assertTrue(self.ok(target(online_ads=None, ads_tolerance=0), profile(listings=five)))
        self.assertTrue(self.ok(target(online_ads=3, ads_tolerance=0), profile(listings=five, online_ads=3)))

    def test_ads_full_page_is_lower_bound(self):
        full = {str(i): {} for i in range(sb.ADS_PAGE_SIZE)}
        self.assertTrue(self.ok(target(online_ads=60, ads_tolerance=0), profile(listings=full)))
        self.assertTrue(self.ok(target(online_ads=23, ads_tolerance=2), profile(listings=full)))
        self.assertFalse(self.ok(target(online_ads=20, ads_tolerance=2), profile(listings=full)))

    def test_response_never_grows(self):
        self.assertTrue(self.ok(target(response_minutes=180), profile(response_minutes=180)))
        self.assertTrue(self.ok(target(response_minutes=180), profile(response_minutes=10)))
        self.assertFalse(self.ok(target(response_minutes=180), profile(response_minutes=360)))
        self.assertTrue(self.ok(target(response_minutes=None), profile(response_minutes=2880)))
        m = sb.strict_match(target(name="Privat"), profile(response_minutes=None), CFG)
        self.assertEqual(m.level, "partial")                # не удалось проверить — не «точное»

    def test_name_is_signal(self):
        self.assertEqual(sb.strict_match(target(name="Privat"), profile(), CFG).level, "exact")
        self.assertEqual(sb.strict_match(target(name="Privaat"), profile(), CFG).level, "partial")
        self.assertEqual(sb.strict_match(target(), profile(), CFG).level, "partial")

    def test_weak_target_needs_name(self):
        weak = target(badges={}, followers=0, online_ads=None, response_minutes=None)
        self.assertFalse(weak.name_optional())
        empty = profile(badges={}, followers=None, response_minutes=None)
        self.assertFalse(self.ok(weak, empty))                                    # иначе подошли бы сотни
        self.assertTrue(self.ok(target(name="Privat", badges={}, followers=0, online_ads=None,
                                       response_minutes=None), empty))


class TestInput(unittest.TestCase):
    def test_text(self):
        t = sb.parse_target_text("Имя: Manuela\nДата регистрации: 08.02.2017\nПлашки: top, sehr freundlich\n"
                                 "Подписчики: 8\nОбъявления онлайн: 3\nВремя ответа: 3 ч\n"
                                 "Погрешность объявлений: ±5")
        self.assertEqual((t.name, t.followers, t.online_ads, t.response_minutes, t.ads_tolerance),
                         ("Manuela", 8, 3, 180, 5))
        self.assertEqual(sb.StrictTarget.from_dict(t.to_dict()), t)
        t = sb.parse_target_text("Manuela 08.02.2017 top", default_tolerance=4)
        self.assertEqual((t.response_minutes, t.ads_tolerance), (None, 4))
        self.assertIsNone(sb.parse_target_text("привет"))

    def test_ocr(self):
        d = sb.parse_ocr("MM\nMax Mustermann\n3 Anzeigen online / 40 gesamt\nPrivater Nutzer\n"
                         "Aktiv seit 07.03.2018\nAntwortet in der Regel innerhalb von 3 Stunden\n8 Follower\n"
                         "TOP Zufriedenheit  Freundlich  Sehr zuverlässig")
        self.assertEqual((d["name"], d["aktiv_seit"], d["followers"], d["online_ads"], d["response_minutes"]),
                         ("Max Mustermann", "07.03.2018", 8, 3, 180))
        self.assertEqual(d["badges"], BADGES)


class FakeFetcher:
    def __init__(self, profiles):
        self.profiles = profiles

    def get(self, uid, stop=None):
        p = self.profiles.get(uid)
        return kb.FetchResult("ok", profile=p) if p else kb.FetchResult("missing")


class TestScannerAndBot(unittest.TestCase):
    CHAT = "42"

    def profiles(self):
        ps = {100 + i: profile(user_id=100 + i, name=f"User {i}", badges={}, followers=None,
                               response_minutes=None, listings={}) for i in range(60)}
        ps[130] = profile(user_id=130, name="Privat", followers=9, response_minutes=60)        # он, сменил имя
        ps[131] = profile(user_id=131, name="Max Mustermann", followers=8, response_minutes=720)  # ответ вырос
        ps[132] = profile(user_id=132, name="Anna", followers=30)                               # не те подписчики
        return ps

    def test_scanner_finds_renamed(self):
        found = []
        job = kb.Job(target=target().to_dict(), first=100, last=159)
        sb.StrictScanner(FakeFetcher(self.profiles()), CFG, threading.Event(), threading.Event(),
                         found.append, lambda: None).run(job)
        self.assertEqual([m.user_id for m in found], [130])
        self.assertEqual(job.checked, 60)

    def make_bot(self, tmp):
        bot = sb.StrictBot(dict(CFG, data_dir=tmp, allowed_chat_ids=[self.CHAT], telegram_token="x"))
        bot.tg = FakeTG()
        return bot

    def test_card_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, started = self.make_bot(tmp), []
            bot.submit_search = lambda t: started.append(t) or "ok"
            tg = bot.tg
            bot.handle_text(self.CHAT, "Имя: Max Mustermann\nДата регистрации: 07.03.2018\n"
                                       "Плашки: top, freundlich, sehr zuverlässig\nПодписчики: 8")
            self.assertIn("Погрешность объявлений: 2", tg.sent[-1])
            bot.handle_callback(self.CHAT, tg.data("Объявления"), 1)
            bot.handle_text(self.CHAT, "4")
            bot.handle_callback(self.CHAT, tg.data("Погрешность"), 1)
            bot.handle_callback(self.CHAT, tg.data("±5"), 1)
            bot.handle_callback(self.CHAT, tg.data("Ответ"), 1)
            bot.handle_text(self.CHAT, "12 часов")
            self.assertIn("Время ответа: 12 ч (или меньше)", tg.sent[-1])
            bot.handle_callback(self.CHAT, tg.data("Погрешность"), 1)
            bot.handle_text(self.CHAT, "0")
            bot.handle_callback(self.CHAT, tg.data("Искать"), 1)
            t = started[0]
            self.assertIsInstance(t, sb.StrictTarget)
            self.assertEqual((t.followers, t.online_ads, t.ads_tolerance, t.response_minutes), (8, 4, 0, 720))

    def test_weak_card_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.handle_text(self.CHAT, "Max 07.03.2018")
            self.assertIn("Мало примет", bot.tg.sent[-1])

    def test_match_messages_are_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self.make_bot(tmp)
            bot.cfg["max_live_matches"] = 2
            run = kb.SearchRun(1, target())
            for uid in range(1, 6):
                m = kb.Match("partial", uid, "X")
                run.job.matches.append({"user_id": uid})
                bot._on_match(run, m)
            self.assertEqual(sum("возможно, сменил имя" in s for s in bot.tg.sent), 2)
            self.assertEqual(sum("примет слишком мало" in s for s in bot.tg.sent), 1)


if __name__ == "__main__":
    unittest.main()
