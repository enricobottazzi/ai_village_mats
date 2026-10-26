"""Small synthetic fixtures test plot mechanics, never pilot outcomes."""

import json
import math
from pathlib import Path
import tempfile
import unittest

from pilot import plot


def window(number, end_at="2026-08-11T18:00:00Z"):
    return {"window_id": f"synthetic-{number}", "agent_name": "Synthetic test agent", "end_at": end_at}


class PlotTests(unittest.TestCase):
    def test_pacific_conversion_uses_window_end(self):
        points = plot.prepare_points([window(1)], [{"window_id": "synthetic-1", "score": 2, "justification": "Mostly aligned."}])
        self.assertEqual(points[0].at.isoformat(), "2026-08-11T11:00:00-07:00")
        with self.assertRaises(ValueError):
            plot.parse_time("2026-08-11T18:00:00")

    def test_abstentions_and_missing_results_are_gaps_without_extra_markers(self):
        windows = [window(1), window(2, "2026-08-11T18:01:00Z"), window(3, "2026-08-11T18:02:00Z"), window(4, "2026-08-11T18:03:00Z")]
        results = [{"window_id": "synthetic-1", "score": 1, "justification": "Aligned."}, {"window_id": "synthetic-2", "score": plot.ABSTAIN, "justification": "Insufficient evidence."}, {"window_id": "synthetic-4", "score": 4, "justification": "Mostly unrelated."}]
        points = plot.prepare_points(windows, results)
        self.assertEqual([point.judged for point in points], [True, True, False, True])
        self.assertTrue(math.isnan(points[1].score))
        self.assertTrue(math.isnan(points[2].score))
        figure = plot.make_figure(points, "Synthetic test agent")
        self.addCleanup(self.close, figure)
        line_scores = figure.axes[0].lines[0].get_ydata()
        self.assertTrue(math.isnan(line_scores[1]))
        self.assertTrue(math.isnan(line_scores[2]))
        self.assertEqual(len(figure.axes[0].collections), 0)
        self.assertEqual(figure.axes[0].get_yticks().tolist(), [1, 2, 3, 4, 5])
        self.assertIn("* Gaps indicate abstention from the judge.", figure.texts[-1].get_text())
        self.assertIn("Partial run", figure.texts[-1].get_text())

    def test_days_share_one_axis_with_18_hour_ticks_and_reminder_annotation(self):
        windows = [window(1), window(2, "2026-08-12T18:00:00Z")]
        results = [{"window_id": "synthetic-1", "score": 2, "justification": "Mostly aligned."}, {"window_id": "synthetic-2", "score": 1, "justification": "Aligned."}]
        points = plot.prepare_points(windows, results)
        figure = plot.make_figure(points, "Synthetic test agent", "2026-08-12T18:00:00Z")
        self.addCleanup(self.close, figure)
        self.assertEqual(len(figure.axes), 1)
        self.assertEqual(len(figure.axes[0].lines[0].get_xdata()), 1)
        self.assertEqual(len(figure.axes[0].lines[1].get_xdata()), 1)
        self.assertEqual(figure.axes[0].lines[-1].get_xdata()[0], figure.axes[0].lines[1].get_xdata()[0])
        labels = [label.get_text() for label in figure.axes[0].get_xticklabels()]
        self.assertEqual(labels.count("18:00"), 2)
        self.assertTrue(any("11 Aug" in label for label in labels))
        self.assertTrue(any("12 Aug" in label for label in labels))
        ticks = figure.axes[0].get_xticks()
        self.assertLess(ticks[4] - ticks[3], ticks[1] - ticks[0])
        self.assertEqual(figure.axes[0].get_xlabel(), "")

    def test_cross_night_window_preserves_start_day_without_inventing_score(self):
        crossing = window(1, "2026-08-12T18:00:00Z")
        crossing["start_at"] = "2026-08-11T17:00:00Z"
        points = plot.prepare_points([crossing], [{"window_id": "synthetic-1", "score": 3, "justification": "Partly aligned."}])
        figure = plot.make_figure(points, "Synthetic test agent")
        self.addCleanup(self.close, figure)
        self.assertEqual(len(figure.axes), 1)
        self.assertEqual(len(figure.axes[0].lines[0].get_xdata()), 0)
        self.assertEqual(len(figure.axes[0].lines[1].get_xdata()), 1)

    def test_missing_results_and_invalid_scores_fail(self):
        with self.assertRaisesRegex(ValueError, "No judge results"):
            plot.prepare_points([window(1)], [])
        for invalid in (0, 6, "2", True):
            with self.assertRaises(ValueError):
                plot.prepare_points([window(1)], [{"window_id": "synthetic-1", "score": invalid, "justification": "Synthetic invalid score."}])

    def test_outputs_are_created_from_supplied_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            windows_path, results_path = directory / "windows.jsonl", directory / "judgements.jsonl"
            windows_path.write_text(json.dumps(window(1)) + "\n", encoding="utf-8")
            results_path.write_text(json.dumps({"window_id": "synthetic-1", "score": 3, "justification": "Partly aligned."}) + "\n", encoding="utf-8")
            outputs = plot.plot_trajectory(windows_path, results_path, directory / "synthetic-trajectory")
            self.assertEqual([path.suffix for path in outputs], [".png"])
            self.assertTrue(all(path.stat().st_size > 0 for path in outputs))
            self.assertFalse((directory / "synthetic-trajectory.pdf").exists())

    @staticmethod
    def close(figure):
        import matplotlib.pyplot as plt

        plt.close(figure)


if __name__ == "__main__":
    unittest.main()
