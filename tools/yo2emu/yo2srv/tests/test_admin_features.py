"""Pruebas unitarias de las operaciones que usa la pestaña avanzada del launcher."""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_duelpass():
    p = os.path.join(ROOT, "research", "persistencia", "duelpass.py")
    spec = importlib.util.spec_from_file_location("test_duelpass_module", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_events():
    p = os.path.join(ROOT, "research", "eventos", "proposal.py")
    spec = importlib.util.spec_from_file_location("test_events_module", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAdminFeatures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yo2-admin-features-")
        self.dp = load_duelpass()
        self.ev = load_events()
        self.ev.install({"STATE_PATH": os.path.join(self.tmp, "state.json"), "log": lambda *_: None})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_duelpass_create_void_and_take(self):
        codes = self.dp.create(self.tmp, 2, dpp=30, carta=12, mazo=1, nota="evento")
        self.assertEqual(len(codes), 2)
        rows = self.dp.load(self.tmp)
        self.assertEqual(rows[codes[0]]["estado"], "libre")
        self.assertEqual(self.dp.take(self.tmp, codes[0], {"cuenta": "a", "hueco": 0})[0], self.dp.RES_OK)
        self.assertEqual(self.dp.take(self.tmp, codes[0], {})[0], self.dp.RES_USED)
        self.assertEqual(self.dp.void(self.tmp, codes[1]), "libre")
        self.assertEqual(self.dp.take(self.tmp, codes[1], {})[0], self.dp.RES_VOID)

    def test_start_gifts_are_read_with_limits(self):
        with open(self.dp.start_path(self.tmp), "w", encoding="utf-8") as f:
            json.dump({"mp": 42, "dpp": 17, "baul": "deck", "extra": "ignored"}, f)
        self.assertEqual(self.dp.start_values(self.tmp), {"mp": 42, "dpp": 17, "baul": "deck"})

    def test_event_rules_sessions_and_blocked_reconstructions(self):
        self.ev.save_rule("gunslinger", {"5": 101, "6": 102, "7": 103}, valid_card=lambda x: x < 200)
        session = self.ev.start("gunslinger", 9)
        self.assertEqual((session["familia"], session["serial"]), ("gunslinger", 9))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, self.ev.FILE_NAME)))
        with self.assertRaises(ValueError):
            self.ev.start("ultimate", 1)

    def test_gunslinger_streak_resets_and_rewards_once(self):
        self.ev.save_rule("gunslinger", {"5": 101, "6": 102, "7": 103})
        self.ev.start("gunslinger", 2)
        p = {}
        for n in range(1, 5):
            self.assertEqual(self.ev.on_pvp_result(p, 7, "win", f"d{n}"), [])
        self.assertEqual(self.ev.on_pvp_result(p, 7, "lose", "loss"), [])
        for n in range(5):
            out = self.ev.on_pvp_result(p, 7, "win", f"r{n}")
        self.assertEqual(out, [{"milestone": "5", "card": 101}])
        self.assertEqual(self.ev.on_pvp_result(p, 7, "win", "r4"), [])
        reward = self.ev.take_reward(p)
        self.assertEqual(reward["cards"], [101])
        self.assertIsNone(self.ev.take_reward(p))
        self.ev.on_pvp_result(p, 7, "lose", "loss2")
        for n in range(5):
            out = self.ev.on_pvp_result(p, 7, "win", f"again{n}")
        self.assertEqual(out, [{"milestone": "5", "card": 101}])

    def test_duelist_trial_repeats_four_five_cycle(self):
        self.ev.save_rule("duelist_trial", {"4": 201, "5": 202})
        self.ev.start("duelist_trial", 3)
        p, hits = {}, []
        for n in range(1, 11):
            hits.extend(self.ev.on_pvp_result(p, 9, "win", f"d{n}"))
        self.assertEqual([x["milestone"] for x in hits], ["4", "5", "4", "5"])
        self.assertEqual([self.ev.take_reward(p)["cards"][0] for _ in range(4)], [201, 202, 201, 202])

    def test_championship_claim_is_separate(self):
        self.ev.save_rule("championship", {"campeon": 301, "subcampeon": 302})
        self.ev.start("championship", 4)
        p = {"name": "Winner"}
        self.assertEqual(self.ev.championship_award(p, "campeon"), [301])
        self.assertIsNone(self.ev.take_reward(p, championship=False))
        self.assertEqual(self.ev.take_reward(p, championship=True)["cards"], [301])
        self.assertEqual(self.ev.championship_award(p, "campeon"), [])

    def test_evolution_stars_count_round_wins_even_when_match_is_lost(self):
        self.ev.save_rule("evolution", {"10": 310, "20": 320, "30": 330})
        self.ev.start("evolution", 5)
        p = {"name": "Evolutionist"}
        self.assertEqual(self.ev.on_pvp_result(p, 17, "win", "m1g1", match=False, game_number=1), [])
        self.ev.on_pvp_result(p, 17, "win", "m1g1", match=True, game_number=1)
        self.ev.on_pvp_result(p, 17, "lose", "m1g2", match=True, game_number=2)
        self.ev.on_pvp_result(p, 17, "lose", "m1g3", match=True, game_number=3)
        progress = p["events"]["evolution:5"]
        self.assertEqual(progress["estrellas"], 2)  # perdió el match, conserva las 2 de la ronda ganada

        hits = []
        for key, game in (("m2g1", 1), ("m2g2", 2), ("m3g1", 1), ("m3g2", 2),
                          ("m4g1", 1), ("m4g2", 2), ("m5g1", 1), ("m5g2", 2),
                          ("m6g1", 1), ("m6g2", 2)):
            hits.extend(self.ev.on_pvp_result(p, 17, "win", key, match=True, game_number=game))
        self.assertEqual([x["milestone"] for x in hits], ["10", "20", "30"])
        self.assertEqual(progress["estrellas"], 32)
        self.assertEqual([self.ev.take_reward(p)["cards"][0] for _ in range(3)], [310, 320, 330])


if __name__ == "__main__":
    unittest.main()
