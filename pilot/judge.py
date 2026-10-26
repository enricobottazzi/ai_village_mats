#!/usr/bin/env python3
"""Judge single-event windows through OpenRouter. Defaults to an offline dry run."""

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
ABSTAIN = "ABSTAIN_INSUFFICIENT_EVIDENCE"


class JudgeError(ValueError):
    def __init__(self, message, raw_response=None):
        super().__init__(message)
        self.raw_response = raw_response


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def read_windows(path):
    raw = path.read_bytes()
    windows = []
    seen = set()
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        window = json.loads(line)
        if not isinstance(window, dict):
            raise ValueError(f"Input line {line_number} must be an object")
        for key in ("window_id", "agent_id", "agent_name", "start_at", "end_at"):
            if not isinstance(window.get(key), str) or not window[key]:
                raise ValueError(f"Input line {line_number} requires a nonempty {key}")
        if window["window_id"] in seen:
            raise ValueError(f"Duplicate window_id: {window['window_id']}")
        if not isinstance(window.get("goal"), dict) or not window["goal"]:
            raise ValueError(f"Window {window['window_id']} requires an assigned goal")
        events = window.get("events")
        if not isinstance(events, list) or len(events) != 1 or not all(isinstance(event, dict) for event in events):
            raise ValueError(f"Window {window['window_id']} must contain exactly 1 event object")
        seen.add(window["window_id"])
        windows.append(window)
    if not windows:
        raise ValueError("Input contains no windows")
    return windows, sha256(raw)


def build_request(window, prompt, model):
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": encode({"goal": window["goal"], "events": window["events"]})},
        ],
        "temperature": 1 if model.startswith("google/gemini-3") else 0,
        "max_tokens": 8192,
        "provider": {"require_parameters": True},
        "plugins": [{"id": "context-compression", "enabled": False}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "goal_alignment",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "score": {"anyOf": [
                            {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                            {"type": "string", "enum": [ABSTAIN]},
                        ]},
                        "justification": {"type": "string"},
                    },
                    "required": ["score", "justification"],
                    "additionalProperties": False,
                },
            },
        },
    }


def metadata(window, request, prompt_hash, input_hash):
    return {
        **{key: window[key] for key in ("window_id", "agent_id", "agent_name", "start_at", "end_at")},
        "requested_model": request["model"],
        "prompt_sha256": prompt_hash,
        "input_sha256": input_hash,
        "window_sha256": sha256(encode(window).encode("utf-8")),
        "request_sha256": sha256(encode(request).encode("utf-8")),
    }


def valid_judgement(value):
    return (type(value) is int and 1 <= value <= 5) or (type(value) is str and value == ABSTAIN)


def valid_justification(value):
    return isinstance(value, str) and bool(value.strip())


def load_completed(path, expected):
    completed = set()
    returned_model = None
    if not path.exists():
        return completed, returned_model
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid results line {line_number}; repair it before resuming") from error
        window_id = record.get("window_id") if isinstance(record, dict) else None
        if window_id not in expected or window_id in completed:
            raise ValueError(f"Unknown or duplicate result window_id: {window_id}")
        for key in ("requested_model", "prompt_sha256", "input_sha256", "window_sha256", "request_sha256"):
            if record.get(key) != expected[window_id][key]:
                raise ValueError(f"Cannot resume {window_id}: {key} changed; use a separate output file")
        model = record.get("returned_model")
        if (not isinstance(model, str) or not model
                or not valid_judgement(record.get("score"))
                or not valid_justification(record.get("justification"))):
            raise ValueError(f"Invalid saved judgement or returned model for {window_id}")
        if returned_model is not None and model != returned_model:
            raise ValueError(f"Inconsistent returned models in saved results: {returned_model} and {model}")
        returned_model = model
        completed.add(window_id)
    return completed, returned_model


def retry_delay(error, attempt):
    value = error.headers.get("Retry-After") if isinstance(error, HTTPError) and error.headers else None
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = 2 ** attempt
        return min(60, max(0, delay))
    return 2 ** attempt


def call_openrouter(body, api_key):
    request = Request(
        ENDPOINT,
        data=encode(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as error:
                raise JudgeError("OpenRouter returned invalid JSON", raw) from error
            if not isinstance(parsed, dict) or "error" in parsed:
                raise JudgeError("OpenRouter returned an API error", parsed)
            return parsed
        except HTTPError as error:
            with error:
                raw = error.read().decode("utf-8", errors="replace")
            if attempt < 2 and (error.code == 429 or 500 <= error.code < 600):
                time.sleep(retry_delay(error, attempt))
                continue
            raise JudgeError(f"OpenRouter HTTP {error.code}", raw) from error
        except (URLError, OSError, HTTPException) as error:
            if attempt < 2:
                time.sleep(retry_delay(error, attempt))
                continue
            raise JudgeError(f"OpenRouter connection failed after 3 attempts: {error}") from error


def parse_judgement(response, expected_returned_model=None):
    returned_model = response.get("model")
    if not isinstance(returned_model, str) or not returned_model:
        raise JudgeError("OpenRouter response is missing its returned model", response)
    if expected_returned_model is not None and returned_model != expected_returned_model:
        raise JudgeError(f"Returned model changed from {expected_returned_model} to {returned_model}", response)
    try:
        choice = response["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ValueError(f"Unexpected finish_reason: {choice.get('finish_reason')}")
        result = json.loads(choice["message"]["content"])
        if (not isinstance(result, dict) or set(result) != {"score", "justification"}
                or not valid_judgement(result["score"])
                or not valid_justification(result["justification"])):
            raise ValueError("Expected score (1–5 or ABSTAIN_INSUFFICIENT_EVIDENCE) and a nonempty justification")
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise JudgeError(f"Invalid judge output: {error}", response) from error
    return result


def append_record(handle, record):
    handle.write(encode(record) + "\n")
    handle.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "data/windows.jsonl")
    parser.add_argument("--prompt", type=Path, default=ROOT / "judge_prompt.md")
    parser.add_argument("--output", type=Path, default=ROOT / "results/judgements.jsonl")
    parser.add_argument("--model", required=True, help="Exact OpenRouter model ID; recorded and checked on resume")
    parser.add_argument("--limit", type=int, help="Maximum number of new windows to process")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="Send paid API requests; never enabled by default")
    mode.add_argument("--dry-run", action="store_true", help="Write requests.jsonl offline (default)")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if not args.model.strip():
        parser.error("--model must be nonempty")
    windows, input_hash = read_windows(args.input)
    prompt_bytes = args.prompt.read_bytes()
    prompt = prompt_bytes.decode("utf-8")
    if not prompt.strip():
        raise ValueError("Judge prompt is empty")
    prompt_hash = sha256(prompt_bytes)
    requests = [build_request(window, prompt, args.model) for window in windows]
    records = [metadata(window, request, prompt_hash, input_hash) for window, request in zip(windows, requests)]
    completed, returned_model = load_completed(args.output, {record["window_id"]: record for record in records})
    pending = [(record, request) for record, request in zip(records, requests) if record["window_id"] not in completed]
    if args.limit is not None:
        pending = pending[:args.limit]
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if args.run and pending and not api_key:
        raise ValueError("Set OPENROUTER_API_KEY before using --run")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not args.run:
        dry_path = args.output.parent / "requests.jsonl"
        with dry_path.open("w", encoding="utf-8") as handle:
            for record, request in pending:
                append_record(handle, {**record, "request": request})
        print(f"Dry run: wrote {len(pending)} requests to {dry_path}; no API calls made.")
        return 0
    with args.output.open("a", encoding="utf-8") as handle:
        for index, (record, request) in enumerate(pending, 1):
            try:
                response = call_openrouter(request, api_key)
                judgement = parse_judgement(response, returned_model)
            except JudgeError as error:
                with args.output.with_suffix(".errors.jsonl").open("a", encoding="utf-8") as errors:
                    append_record(errors, {**record, "error": str(error), "raw_response": error.raw_response})
                raise
            returned_model = response["model"]
            append_record(handle, {
                **record,
                **judgement,
                "returned_model": response["model"],
                "usage": response.get("usage"),
                "raw_response": response,
                "judged_at": datetime.now(timezone.utc).isoformat(),
            })
            print(f"{index}/{len(pending)} {record['window_id']}: {judgement['score']}", flush=True)
    print(f"Saved {len(pending)} new judgements to {args.output} ({len(completed)} already complete).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
