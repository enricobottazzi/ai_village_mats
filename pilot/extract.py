"""Extract the Sonnet 4.5 pilot from a pinned AI Village export. No judge calls."""

import argparse
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
REPO = "aidigestorg/ai-village"
REVISION = "2c6147e4cfb3136e56a100193b1a3aab1087f4ba"
AGENT_NAME = "Claude Sonnet 4.5"
EVENTS_SHA256 = "774f055d651ae13b77950f2b4083897c814dcd1548bbea0060d2e2a7ace8adec"
# Verified broadcast by Adam on August 12; a plot annotation, never judge input.
REMINDER_EVENT_ID = "6f86a6c6-adfa-43ea-933e-211f4d3a6f24"
PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def date_bounds(start, end):
    first = datetime.combine(date.fromisoformat(start), time(), PACIFIC)
    stop = datetime.combine(date.fromisoformat(end) + timedelta(days=1), time(), PACIFIC)
    if stop <= first:
        raise ValueError("End date must be on or after start date")
    return first.astimezone(UTC), stop.astimezone(UTC)


def belongs_to(event, agent_id):
    data = event["data"]
    return data.get("agentId") == agent_id or (
        data.get("actionType") == "AGENT_TALK" and data.get("speakerId") == agent_id
    )


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def hf_token():
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        return token
    home = Path(os.environ.get("HF_HOME", Path.home() / ".cache/huggingface"))
    path = Path(os.environ.get("HF_TOKEN_PATH", home / "token"))
    if path.is_file():
        return path.read_text().strip()
    raise RuntimeError("Set HF_TOKEN or log in to Hugging Face before extraction")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(name, cache, token):
    destination = cache / name
    if destination.is_file():
        if name != "events.jsonl.gz" or sha256(destination) == EVENTS_SHA256:
            return destination
        raise RuntimeError("Cached events checksum differs from the pinned export; remove that cached file and retry")
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/{name}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    partial = destination.with_name(destination.name + ".part")
    opener = urllib.request.build_opener(SafeRedirect)
    print(f"Downloading {name}…", flush=True)
    with opener.open(request, timeout=120) as response, partial.open("wb") as handle:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            handle.write(block)
    if name == "events.jsonl.gz" and sha256(partial) != EVENTS_SHA256:
        raise RuntimeError("Downloaded events checksum differs from the pinned export")
    partial.replace(destination)
    return destination


def read_table(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path, rows):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def make_windows(events, agent, goal, size):
    if size < 1:
        raise ValueError("Window size must be positive")
    ordered = sorted(events, key=lambda event: event["event_index"])
    complete = len(ordered) // size * size
    windows = []
    for offset in range(0, complete, size):
        batch = ordered[offset:offset + size]
        windows.append({
            "window_id": f"sonnet45-{offset // size + 1:04d}",
            "agent_id": agent["id"],
            "agent_name": agent["name"],
            "start_at": iso(timestamp(batch[0]["created_at"])),
            "end_at": iso(timestamp(batch[-1]["created_at"])),
            "goal": goal,
            "events": batch,
        })
    return windows, ordered[complete:]


def extract(start, end, size, cache, output):
    token = hf_token()
    manifest = json.loads(download("manifest.json", cache, token).read_text())
    agents = read_table(download("agents.jsonl.gz", cache, token))
    assignments = read_table(download("agent_goals.jsonl.gz", cache, token))
    agent = next(row for row in agents if row["name"] == AGENT_NAME)
    first, stop = date_bounds(start, end)
    applicable = [row for row in assignments if row["agent_id"] == agent["id"]
                  and (row["start_time"] is None or timestamp(row["start_time"]) <= first)
                  and (row["end_time"] is None or timestamp(row["end_time"]) >= stop)]
    if len(applicable) != 1:
        raise RuntimeError("Expected one goal assignment covering the complete pilot interval")
    assignment = applicable[0]
    goal = {key: assignment[key] for key in ("name", "description") if assignment.get(key)}
    events_path = download("events.jsonl.gz", cache, token)
    selected, reminder, scanned = [], None, 0
    with gzip.open(events_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            scanned += 1
            when = timestamp(row["created_at"])
            if not first <= when < stop:
                continue
            data = row["data"]
            if belongs_to(row, agent["id"]):
                selected.append(row)
            if row["id"] == REMINDER_EVENT_ID:
                reminder = {"id":row["id"], "created_at":iso(when),
                            "speaker":data.get("speakerName"), "content":data.get("content", "")}
    if scanned != manifest["rowCounts"]["events"]:
        raise RuntimeError(f"Expected {manifest['rowCounts']['events']} events; read {scanned}")
    selected.sort(key=lambda event: event["event_index"])
    if not selected:
        raise RuntimeError("No matching agent events in the requested interval")
    windows, tail = make_windows(selected, agent, goal, size)
    output.mkdir(parents=True, exist_ok=True)
    write_jsonl(output / "events.jsonl", selected)
    write_jsonl(output / "windows.jsonl", windows)
    write_jsonl(output / "tail_events.jsonl", tail)
    (output / "reminder.json").write_text(json.dumps(reminder, indent=2) + "\n")
    (output / "goal.json").write_text(json.dumps(assignment, indent=2) + "\n")
    per_day = Counter(timestamp(row["created_at"]).astimezone(PACIFIC).date().isoformat() for row in selected)
    metadata = {
        "dataset": REPO, "revision": REVISION, "exported_at": manifest["exportedAt"],
        "events_sha256": EVENTS_SHA256, "source_events_scanned": scanned,
        "agent_id": agent["id"], "agent_name": agent["name"],
        "start_date": start, "end_date": end, "timezone": str(PACIFIC),
        "start_at_inclusive": iso(first), "end_at_exclusive": iso(stop),
        "event_count": len(selected), "events_per_day": dict(sorted(per_day.items())),
        "event_types": dict(sorted(Counter(row["data"]["actionType"] for row in selected).items())),
        "window_size": size, "window_count": len(windows), "tail_event_count": len(tail),
        "cross_date_windows": sum(timestamp(row["start_at"]).astimezone(PACIFIC).date() !=
                                  timestamp(row["end_at"]).astimezone(PACIFIC).date() for row in windows),
        "reminder_at": reminder["created_at"] if reminder else None,
        "reminder_event_id": reminder["id"] if reminder else None,
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2), flush=True)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-08-11", help="First Pacific calendar date, inclusive")
    parser.add_argument("--end", default="2026-08-13", help="Last Pacific calendar date, inclusive")
    parser.add_argument("--window-size", type=int, default=1)
    parser.add_argument("--cache", type=Path, default=ROOT / ".cache/ai-village" / REVISION)
    parser.add_argument("--output", type=Path, default=ROOT / "pilot/data")
    args = parser.parse_args()
    if args.window_size < 1:
        parser.error("--window-size must be positive")
    try:
        extract(args.start, args.end, args.window_size, args.cache, args.output)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Extraction failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
