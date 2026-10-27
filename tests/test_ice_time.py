"""Tests for ice-time. Run: python -m unittest discover -s tests

Fixtures are made up ("Sample Arena", made-up ids); no real rink data.
"""
import datetime as dt
import importlib
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.dirname(HERE))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ice-time-test-")
ice = importlib.import_module("ice_time")


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return f.read()


class ParsePage(unittest.TestCase):
    def test_reads_schedule_and_name(self):
        info = ice.parse_activity_page(fixture("sample_goalie.html"))
        self.assertEqual(info["name"], "Sample Arena - Adult Drop In (GOALIE) 26/27")
        self.assertEqual(len(info["sessions"]), 3)
        s = info["sessions"][1]
        self.assertEqual((s["start"], s["end"], s["enrolled"], s["max"], s["remaining"]),
                         ("2026-10-28T11:45:00", "2026-10-28T13:15:00", 2, 3, 1))
        self.assertEqual(info["sessions"][0]["facility"], "Sample Arena")  # trailing space trimmed

    def test_brackets_inside_strings_do_not_break_it(self):
        # the fixture's descriptions contain ] } and escaped quotes
        self.assertEqual(len(ice.parse_activity_page(fixture("sample_goalie.html"))["sessions"]), 3)

    def test_page_without_schedule_is_a_clear_error(self):
        with self.assertRaises(ice.ParseError):
            ice.parse_activity_page("<html><body>Maintenance</body></html>")

    def test_name_falls_back_to_heading(self):
        page = fixture("sample_goalie.html").replace('"Name":"Sample Arena - Adult Drop In (GOALIE) 26/27",', "")
        self.assertEqual(ice.parse_activity_page(page)["name"], "Sample Arena - Adult Drop In (GOALIE) 26/27")


class Describe(unittest.TestCase):
    def guess(self, name):
        return ice.describe_activity(name, [])

    def test_sections_roles_rink_season(self):
        g = self.guess("Sample Arena - Adult Drop In (GOALIE) 26/27")
        self.assertEqual(g, {"section": "Drop-in", "role": "goalie", "rink": "Sample Arena", "season": "26/27"})
        self.assertEqual(self.guess("Sample Arena - Adult Stick & Puck (SKATER) 26/27")["section"], "Stick & puck")
        self.assertEqual(self.guess("Sample Arena - Stick and Puck 26/27")["section"], "Stick & puck")
        self.assertEqual(self.guess("Sample Arena - Parent & Child 26/27")["role"], "all")
        self.assertEqual(self.guess("Sample Arena - Youth Open Hockey 13-18 26/27")["section"], "Youth open hockey")
        self.assertEqual(self.guess("Sample Arena - Freestyle")["section"], "")

    def test_rink_from_sessions_when_name_has_none(self):
        g = ice.describe_activity("Open Skate", [{"facility": "Rink B"}])
        self.assertEqual(g["rink"], "Rink B")

    def test_current_season_turns_over_july_1(self):
        self.assertEqual(ice.current_season(dt.date(2026, 6, 30)), "25/26")
        self.assertEqual(ice.current_season(dt.date(2026, 7, 1)), "26/27")
        self.assertEqual(ice.current_season(dt.date(2027, 1, 15)), "26/27")


class Links(unittest.TestCase):
    def test_accepts_registration_links_only(self):
        self.assertEqual(ice.normalize_link("https://club.finnlyconnect.com/registration/activityitemv2/123"),
                         "https://club.finnlyconnect.com/registration/activityitemv2/123")
        # older-style links are read through the newer page
        self.assertEqual(ice.normalize_link("https://club.finnlyconnect.com/registration/activityitem/123"),
                         "https://club.finnlyconnect.com/registration/activityitemv2/123")
        for bad in ["http://club.finnlyconnect.com/registration/activityitemv2/123",
                    "https://evil.example.com/registration/activityitemv2/123",
                    "https://club.finnlyconnect.com.evil.example/registration/activityitemv2/1",
                    "https://club.finnlyconnect.com/admin/1", "file:///etc/passwd", ""]:
            self.assertIsNone(ice.normalize_link(bad), bad)


class Feed(unittest.TestCase):
    def setUp(self):
        ice.CACHE.clear()
        g = ice.parse_activity_page(fixture("sample_goalie.html"))
        s = ice.parse_activity_page(fixture("sample_skater.html"))
        ice.save_sources([
            {"id": "g", "url": "https://club.finnlyconnect.com/registration/activityitemv2/12345",
             "label": g["name"], "section": "Drop-in", "role": "goalie", "rink": "Sample Arena", "season": "26/27", "enabled": True},
            {"id": "s", "url": "https://club.finnlyconnect.com/registration/activityitemv2/12344",
             "label": s["name"], "section": "Drop-in", "role": "skater", "rink": "Sample Arena", "season": "26/27", "enabled": True},
        ])
        ice.CACHE["g"] = {"sessions": g["sessions"], "name": g["name"], "okAt": "2026-10-26T20:00:00+00:00", "error": None}
        ice.CACHE["s"] = {"sessions": s["sessions"], "name": s["name"], "okAt": "2026-10-26T20:00:00+00:00", "error": None}

    def test_skaters_and_goalies_combine_by_start(self):
        feed = ice.build_feed(7, now_local=dt.datetime(2026, 10, 26, 17, 0))
        self.assertEqual([s["name"] for s in feed["sections"]], ["Drop-in"])
        rows = feed["sections"][0]["sessions"]
        self.assertEqual(len(rows), 2)  # Nov 15 is past the 7-day window
        first = rows[0]
        self.assertEqual(first["start"], "2026-10-27T06:00:00")
        self.assertEqual(first["minutes"], 105)
        self.assertEqual(first["skaters"]["enrolled"], 1)
        self.assertEqual(first["skaters"]["max"], 24)
        self.assertEqual(first["goalies"], {"enrolled": 0, "max": 3, "remaining": 3})
        self.assertIsNone(first["players"])
        self.assertEqual(set(first["links"]), {"skaters", "goalies"})
        self.assertEqual(feed["warnings"], [])

    def test_window_starts_now(self):
        feed = ice.build_feed(7, now_local=dt.datetime(2026, 10, 27, 8, 0))  # first session over
        self.assertEqual(feed["sections"][0]["sessions"][0]["start"], "2026-10-28T11:45:00")

    def test_errors_and_old_seasons_are_reported(self):
        ice.CACHE["s"]["error"] = "The site answered 503."
        feed = ice.build_feed(7, now_local=dt.datetime(2026, 10, 26, 17, 0))
        self.assertTrue(any("503" in w for w in feed["warnings"]))
        later = ice.build_feed(7, now_local=dt.datetime(2027, 8, 1, 9, 0))
        self.assertTrue(any("26/27 season" in w for w in later["warnings"]))

    def test_disabled_sources_are_left_out(self):
        srcs = ice.load_sources()
        srcs[1]["enabled"] = False
        ice.save_sources(srcs)
        rows = ice.build_feed(7, now_local=dt.datetime(2026, 10, 26, 17, 0))["sections"][0]["sessions"]
        self.assertIsNone(rows[0]["skaters"])


class Privileges(unittest.TestCase):
    def test_not_root_does_nothing(self):
        # Tests don't run as root in CI; started as any user it must not try
        # to change owner or user.
        if os.getuid() == 0:
            self.skipTest("running as root")
        ice.drop_privileges()
        self.assertNotEqual(os.getuid(), 0)


if __name__ == "__main__":
    unittest.main()
