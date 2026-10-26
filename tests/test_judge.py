"""Offline tests: no API calls or credentials required."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from pilot import judge


def window(window_id="w1"):
    return {
        "window_id": window_id,
        "agent_id": "agent-1",
        "agent_name": "Claude Sonnet 4.5",
        "start_at": "2026-08-11T09:00:00Z",
        "end_at": "2026-08-11T09:30:00Z",
        "goal": {"name": "Twitter followers", "description": "Increase Twitter followers"},
        "events": [{"id": "e1", "data": {"text": "Evidence"}}],
    }


def response(value=2, model="test/model", justification="The event supports the assigned goal."):
    return {"model": model, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"score": value, "justification": justification})}}], "usage": {"prompt_tokens": 42}}


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "windows.jsonl"
        self.prompt = self.root / "prompt.txt"
        self.output = self.root / "results/judgements.jsonl"
        self.prompt.write_text("Judge only assigned goal and events.", encoding="utf-8")
        self.input.write_text(judge.encode(window()) + "\n", encoding="utf-8")
        self.args = ["--input", str(self.input), "--prompt", str(self.prompt), "--output", str(self.output), "--model", "test/model"]

    def run_main(self, extra=()):
        with redirect_stdout(io.StringIO()):
            return judge.main(self.args + list(extra))

    def test_default_dry_run_keeps_all_evidence_and_needs_no_key(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(judge, "urlopen") as opener:
            self.assertEqual(self.run_main(), 0)
            opener.assert_not_called()
        record = json.loads((self.output.parent / "requests.jsonl").read_text())
        body = record["request"]
        self.assertEqual(json.loads(body["messages"][1]["content"]), {"goal": window()["goal"], "events": window()["events"]})
        self.assertNotIn("tools", body)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 8192)
        self.assertTrue(body["provider"]["require_parameters"])
        self.assertEqual(body["plugins"], [{"id": "context-compression", "enabled": False}])
        schema = body["response_format"]["json_schema"]["schema"]
        self.assertEqual(set(schema["properties"]), {"score", "justification"})
        self.assertEqual(set(schema["required"]), {"score", "justification"})
        self.assertEqual(schema["properties"]["score"]["anyOf"], [
            {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            {"type": "string", "enum": [judge.ABSTAIN]},
        ])
        self.assertFalse(self.output.exists())

    def test_run_requires_key(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(judge, "urlopen") as opener:
            with self.assertRaisesRegex(ValueError, "OPENROUTER_API_KEY"):
                self.run_main(["--run"])
            opener.assert_not_called()

    def test_http_request_and_resume_preserve_response(self):
        raw = response(judge.ABSTAIN)
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "offline-secret"}), patch.object(judge, "urlopen") as opener:
            opener.return_value.__enter__.return_value.read.return_value = json.dumps(raw).encode()
            self.run_main(["--run"])
            self.run_main(["--run"])
            self.assertEqual(opener.call_count, 1)
            request = opener.call_args.args[0]
            self.assertEqual(request.full_url, judge.ENDPOINT)
            self.assertEqual(request.get_header("Authorization"), "Bearer offline-secret")
        records = self.output.read_text().splitlines()
        self.assertEqual(len(records), 1)
        record = json.loads(records[0])
        self.assertEqual(record["score"], judge.ABSTAIN)
        self.assertEqual(record["justification"], "The event supports the assigned goal.")
        self.assertEqual(record["raw_response"], raw)
        self.assertEqual(record["usage"], raw["usage"])
        self.assertNotIn("offline-secret", self.output.read_text())
        for key in ("input_sha256", "window_sha256", "request_sha256", "prompt_sha256"):
            self.assertEqual(len(record[key]), 64)

    def test_resume_rejects_changed_prompt_input_and_model(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test"}), patch.object(judge, "call_openrouter", return_value=response()):
            self.run_main(["--run"])
        original_prompt = self.prompt.read_text()
        self.prompt.write_text("Changed judge instructions")
        with self.assertRaisesRegex(ValueError, "prompt_sha256 changed"):
            self.run_main()
        self.prompt.write_text(original_prompt)
        original_input = self.input.read_text()
        self.input.write_text(original_input + judge.encode(window("w2")) + "\n")
        with self.assertRaisesRegex(ValueError, "input_sha256 changed"):
            self.run_main()
        self.input.write_text(original_input)
        with self.assertRaisesRegex(ValueError, "requested_model changed"):
            self.run_main(["--model", "other/model"])

    def test_limit_counts_new_windows_only(self):
        self.input.write_text("\n".join(judge.encode(window(f"w{i}")) for i in range(3)) + "\n")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test"}), patch.object(judge, "call_openrouter", return_value=response()) as api:
            self.run_main(["--run", "--limit", "1"])
            self.run_main(["--run", "--limit", "1"])
            self.assertEqual(api.call_count, 2)
        self.assertEqual([json.loads(line)["window_id"] for line in self.output.read_text().splitlines()], ["w0", "w1"])

    def test_invalid_responses_stop_and_preserve_failure(self):
        for bad in (response(True), response(1.0), response(6), response("2"), response(model="")):
            with self.subTest(bad=bad), patch.dict("os.environ", {"OPENROUTER_API_KEY": "test"}), patch.object(judge, "call_openrouter", return_value=bad):
                with self.assertRaises(judge.JudgeError):
                    self.run_main(["--run"])
                self.assertEqual(self.output.read_text(), "")
        errors = [json.loads(line) for line in self.output.with_suffix(".errors.jsonl").read_text().splitlines()]
        self.assertEqual(len(errors), 5)
        self.assertEqual(errors[-1]["raw_response"]["model"], "")

    def test_requires_exact_two_fields_and_nonempty_justification(self):
        self.assertEqual(judge.parse_judgement(response(3)), {
            "score": 3, "justification": "The event supports the assigned goal.",
        })
        for content in (
            {"score": 2},
            {"score": 2, "justification": " "},
            {"score": 2, "justification": None},
            {"score": 2, "justification": "Evidence", "extra": True},
            {"judgement": 2},
        ):
            raw = response()
            raw["choices"][0]["message"]["content"] = json.dumps(content)
            with self.subTest(content=content), self.assertRaises(judge.JudgeError):
                judge.parse_judgement(raw)

    def test_resume_rejects_missing_saved_justification(self):
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test"}), patch.object(judge, "call_openrouter", return_value=response()):
            self.run_main(["--run"])
        record = json.loads(self.output.read_text())
        del record["justification"]
        self.output.write_text(judge.encode(record) + "\n")
        with self.assertRaisesRegex(ValueError, "Invalid saved judgement"):
            self.run_main()

    def test_alias_accepted_but_returned_model_change_rejected_across_resume(self):
        self.input.write_text("\n".join(judge.encode(window(f"w{i}")) for i in range(2)) + "\n")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test"}), patch.object(judge, "call_openrouter", return_value=response(model="test/canonical-model")):
            self.run_main(["--run", "--limit", "1"])
        record = json.loads(self.output.read_text())
        self.assertEqual(record["requested_model"], "test/model")
        self.assertEqual(record["returned_model"], "test/canonical-model")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test"}), patch.object(judge, "call_openrouter", return_value=response(model="test/different-model")):
            with self.assertRaisesRegex(judge.JudgeError, "Returned model changed"):
                self.run_main(["--run"])
        self.assertEqual(len(self.output.read_text().splitlines()), 1)

    def test_returned_model_change_rejected_within_run(self):
        self.input.write_text("\n".join(judge.encode(window(f"w{i}")) for i in range(2)) + "\n")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test"}), patch.object(judge, "call_openrouter", side_effect=[response(model="test/canonical-model"), response(model="test/different-model")]):
            with self.assertRaisesRegex(judge.JudgeError, "Returned model changed"):
                self.run_main(["--run"])
        self.assertEqual(len(self.output.read_text().splitlines()), 1)

    def test_rate_limit_retries_but_client_error_does_not(self):
        error = HTTPError(judge.ENDPOINT, 429, "rate limited", {"Retry-After": "2"}, io.BytesIO(b'{"error":"limited"}'))
        with patch.object(judge, "urlopen") as opener, patch.object(judge.time, "sleep") as sleep:
            opener.side_effect = [error, opener.return_value]
            opener.return_value.__enter__.return_value.read.return_value = json.dumps(response()).encode()
            judge.call_openrouter({}, "test")
            self.assertEqual(opener.call_count, 2)
            sleep.assert_called_once_with(2)
        error = HTTPError(judge.ENDPOINT, 400, "bad request", {}, io.BytesIO(b"bad request"))
        with patch.object(judge, "urlopen", side_effect=error) as opener, patch.object(judge.time, "sleep") as sleep:
            with self.assertRaisesRegex(judge.JudgeError, "HTTP 400"):
                judge.call_openrouter({}, "test")
            self.assertEqual(opener.call_count, 1)
            sleep.assert_not_called()

    def test_network_retry_is_bounded(self):
        with patch.object(judge, "urlopen", side_effect=URLError("offline")) as opener, patch.object(judge.time, "sleep"):
            with self.assertRaisesRegex(judge.JudgeError, "3 attempts"):
                judge.call_openrouter({}, "test")
            self.assertEqual(opener.call_count, 3)

    def test_rejects_incomplete_windows_and_invalid_resume_file(self):
        short = window()
        short["events"].pop()
        self.input.write_text(judge.encode(short) + "\n")
        with self.assertRaisesRegex(ValueError, "exactly 1"):
            self.run_main()
        self.input.write_text(judge.encode(window()) + "\n")
        self.output.parent.mkdir()
        self.output.write_text('{"window_id":')
        with self.assertRaisesRegex(ValueError, "repair it before resuming"):
            self.run_main()


if __name__ == "__main__":
    unittest.main()
