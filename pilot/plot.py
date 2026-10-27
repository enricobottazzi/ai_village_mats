#!/usr/bin/env python3
"""Plot judge scores as a PNG with abstention gaps and compressed nights."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo


PACIFIC = ZoneInfo("America/Los_Angeles")
ABSTAIN = "ABSTAIN_INSUFFICIENT_EVIDENCE"
HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Point:
    window_id: str
    at: datetime
    score: float
    judged: bool
    start_at: datetime | None = None


def parse_time(value: str) -> datetime:
    """Require an explicit offset, then convert to Pacific time."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp requires a UTC offset: {value!r}")
    return parsed.astimezone(PACIFIC)


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        raise ValueError(f"Missing input file: {path}")
    rows = []
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON in {path}:{number}") from exc
    return rows


def prepare_points(windows: list[dict], results: list[dict]) -> list[Point]:
    if not windows:
        raise ValueError("There are no observation windows to plot.")
    if not results:
        raise ValueError("No judge results yet. Run the judge before plotting.")
    window_ids = [row["window_id"] for row in windows]
    if len(set(window_ids)) != len(window_ids):
        raise ValueError("Duplicate window IDs in observation windows.")
    by_id = {}
    for row in results:
        window_id = row["window_id"]
        if window_id not in window_ids:
            raise ValueError(f"Result references an unknown window: {window_id}")
        if window_id in by_id:
            raise ValueError(f"Duplicate judge result: {window_id}")
        value = row["score"]
        if value != ABSTAIN and (type(value) is not int or not 1 <= value <= 5):
            raise ValueError(f"Invalid score for {window_id}: {value!r}")
        by_id[window_id] = value
    points = []
    for window in windows:
        window_id = window["window_id"]
        value = by_id.get(window_id)
        score = float(value) if type(value) is int else math.nan
        # End time places cross-night windows at an actual observed event.
        points.append(Point(
            window_id, parse_time(window["end_at"]), score, window_id in by_id,
            parse_time(window.get("start_at", window["end_at"])),
        ))
    return sorted(points, key=lambda point: point.at)


def make_figure(points: list[Point], agent_name: str, reminder_at: str | None = None):
    os.environ.setdefault("MPLCONFIGDIR", str(HERE.parent / ".cache/matplotlib"))
    os.environ.setdefault("XDG_CACHE_HOME", str(HERE.parent / ".cache"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not points:
        raise ValueError("There are no observation windows to plot.")
    reminder = parse_time(reminder_at) if reminder_at else None
    observed_days = set()
    for point in points:
        day = (point.start_at or point.at).date()
        while day <= point.at.date():
            observed_days.add(day)
            day += timedelta(days=1)
    days = sorted(observed_days)
    ticks = [datetime.combine(day, time(hour), PACIFIC) for day in days for hour in (9, 12, 15, 18)]
    hours = [
        (at - datetime.combine(at.date(), time(), PACIFIC)).total_seconds() / 3600
        for point in points for at in (point.start_at or point.at, point.at)
    ]
    lower_hour, upper_hour = min(9, min(hours)), max(18, max(hours))
    day_width, night_gap = upper_hour - lower_hour, 1.5
    day_indices = {day: index for index, day in enumerate(days)}

    def display_x(at):
        hour = (at - datetime.combine(at.date(), time(), PACIFIC)).total_seconds() / 3600
        return day_indices[at.date()] * (day_width + night_gap) + hour - lower_hour

    with plt.rc_context({
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "axes.linewidth": 0.6,
    }):
        figure, axis = plt.subplots(figsize=(9.6, 3.1))
        for day in days:
            day_points = [point for point in points if point.at.date() == day]
            # Separate daily lines avoid implying observations overnight.
            # NaNs break the line at missing judgements and abstentions.
            axis.plot(
                [display_x(point.at) for point in day_points],
                [point.score for point in day_points],
                color="black", linewidth=1.05, marker="o", markersize=2.6,
            )

        axis.set_xlim(-0.35, (len(days) - 1) * (day_width + night_gap) + day_width + 0.35)
        axis.set_ylim(0.75, 5.35)
        axis.set_yticks([1, 2, 3, 4, 5])
        axis.set_yticklabels(["1  goal aligned", "2", "3", "4", "5  goal misaligned"])
        axis.set_xticks([display_x(tick) for tick in ticks])
        axis.set_xticklabels([tick.strftime("%H:%M\n%d %b") if tick.hour == 9 else tick.strftime("%H:%M") for tick in ticks])
        for index in range(len(days) - 1):
            gap_center = index * (day_width + night_gap) + day_width + night_gap / 2
            axis.text(gap_center, 0.04, "☾", transform=axis.get_xaxis_transform(), ha="center", va="center", fontsize=14, color="0.4", bbox={"facecolor": "white", "edgecolor": "none", "pad": 0.2})
        for score in range(1, 6):
            axis.axhline(score, color="0.9", linewidth=0.5, zorder=0)
        axis.set_axisbelow(True)
        axis.spines[["top", "right"]].set_visible(False)
        axis.tick_params(width=0.6, length=3, labelsize=8)
        axis.set_title(agent_name, fontsize=11, pad=13)
        axis.set_ylabel("Drift score", fontsize=9)
        if not any(math.isfinite(point.score) for point in points):
            axis.text(0.5, 0.5, "No scored events", ha="center", va="center", transform=axis.transAxes, color="0.45", fontsize=8)
        if reminder and days[0] <= reminder.date() <= days[-1]:
            axis.axvline(display_x(reminder), color="0.4", linestyle="--", linewidth=0.8)
            axis.text(display_x(reminder), 0.98, " Reminder", transform=axis.get_xaxis_transform(), va="top", fontsize=7.5, color="0.3")

        judged = sum(point.judged for point in points)
        note = "* Gaps indicate abstention from the judge."
        if judged < len(points):
            note += f" Partial run: {judged}/{len(points)} events judged; missing results are gaps."
        figure.text(0.19, 0.025, note, fontsize=7.5, color="0.35")
        figure.subplots_adjust(left=0.19, right=0.985, bottom=0.21, top=0.86)
    return figure


def plot_trajectory(windows_path: Path, results_path: Path, output_prefix: Path, reminder_at: str | None = None) -> list[Path]:
    windows, results = read_jsonl(windows_path), read_jsonl(results_path)
    points = prepare_points(windows, results)
    agent_name = windows[0].get("agent_name", "Goal alignment trajectory")
    figure = make_figure(points, agent_name, reminder_at)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    output = output_prefix.with_suffix(".png")
    try:
        figure.savefig(output, dpi=300, facecolor="white")
    finally:
        import matplotlib.pyplot as plt

        plt.close(figure)
    return [output]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=Path, default=HERE / "data/windows.jsonl")
    parser.add_argument("--results", type=Path, default=HERE / "results/judgements.jsonl")
    parser.add_argument("--output-prefix", type=Path, default=HERE / "results/trajectory")
    parser.add_argument("--reminder-at", help="Optional ISO timestamp with UTC offset; a plot annotation only.")
    args = parser.parse_args()
    reminder_at = args.reminder_at
    metadata = args.windows.parent / "metadata.json"
    if reminder_at is None and metadata.is_file():
        reminder_at = json.loads(metadata.read_text(encoding="utf-8")).get("reminder_at")
    try:
        outputs = plot_trajectory(args.windows, args.results, args.output_prefix, reminder_at)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    for output in outputs:
        print(output)


if __name__ == "__main__":
    main()
