import unittest
from pilot.extract import belongs_to, date_bounds, make_windows, timestamp


class ExtractionTests(unittest.TestCase):
    def test_single_event_windows_include_every_event(self):
        rows = [{"id":str(i), "event_index":i, "created_at":"2026-08-11 17:00:00", "data":{}}
                for i in range(65)]
        windows, tail = make_windows(rows, {"id":"A", "name":"Agent"}, {"name":"Goal"}, 1)
        self.assertEqual(len(windows), 65)
        self.assertTrue(all(len(window["events"]) == 1 for window in windows))
        self.assertEqual(tail, [])

    def test_pacific_dates_include_full_last_day(self):
        start, stop = date_bounds("2026-08-11", "2026-08-13")
        self.assertEqual(start.isoformat(), "2026-08-11T07:00:00+00:00")
        self.assertEqual(stop.isoformat(), "2026-08-14T07:00:00+00:00")
        self.assertTrue(start <= timestamp("2026-08-14 06:59:59") < stop)
        self.assertFalse(start <= timestamp("2026-08-14 07:00:00") < stop)

    def test_attribution_does_not_score_incoming_or_mentions(self):
        self.assertTrue(belongs_to({"data":{"actionType":"AGENT_TALK", "speakerId":"A"}}, "A"))
        self.assertTrue(belongs_to({"data":{"actionType":"CONSOLIDATE", "agentId":"A"}}, "A"))
        self.assertFalse(belongs_to({"data":{"actionType":"USER_TALK", "speakerId":"A"}}, "A"))
        self.assertFalse(belongs_to({"data":{"actionType":"AGENT_TALK", "speakerId":"B", "content":"A"}}, "A"))

    def test_windows_use_canonical_order_preserve_events_and_separate_tail(self):
        rows = [{"id":str(i), "event_index":i, "created_at":"2026-08-11 17:00:00",
                 "data":{"actionType":"CONSOLIDATE", "agentId":"A", "output":{"notes":"keep all"}}}
                for i in reversed(range(45))]
        windows, tail = make_windows(rows, {"id":"A", "name":"Agent"}, {"name":"Goal"}, 20)
        self.assertEqual([len(w["events"]) for w in windows], [20,20])
        self.assertEqual([r["event_index"] for w in windows for r in w["events"]], list(range(40)))
        self.assertEqual([r["event_index"] for r in tail], list(range(40,45)))
        self.assertEqual(windows[0]["events"][0]["data"]["output"], {"notes":"keep all"})
        with self.assertRaises(ValueError):
            make_windows(rows, {"id":"A", "name":"Agent"}, {"name":"Goal"}, 0)


if __name__ == "__main__":
    unittest.main()
