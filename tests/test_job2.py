"""Offline synthetic tests only: never load private results or invoke inference/HTTP."""

import importlib.util
import json
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "job2", Path(__file__).resolve().parents[1] / "worker" / "job2.py")
job2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(job2)


NETWORK_GUARD = patch.object(job2.HTTP, "open", side_effect=AssertionError("Offline test: HTTP forbidden"))


def setUpModule():
    NETWORK_GUARD.start()


def tearDownModule():
    NETWORK_GUARD.stop()


class OffsetTests(unittest.TestCase):
    def test_at_is_preserved_exactly(self):
        for offset in ("0:07", "00:07", "12:59", "105:00"):
            self.assertEqual(job2.segment_offset({"at": offset}), offset)

    def test_integral_start_is_exact(self):
        for raw in (67, 67.0, "67.000"):
            self.assertEqual(job2.segment_offset({"start": raw}), "1:07")
        self.assertEqual(job2.segment_offset({"at": "00:07", "start": 7}), "00:07")

    def test_never_round_or_guess(self):
        for segment in ({}, {"at": "0:7"}, {"at": "0:60"}, {"at": "-1:00"},
                        {"at": " 0:07"}, {"at": "0:07.5"}, {"at": None},
                        {"start": 7.25}, {"start": -1}, {"start": True},
                        {"start": "NaN"}, {"start": float("inf")},
                        {"at": "0:07", "start": 8}):
            with self.subTest(segment=segment), self.assertRaises(RuntimeError):
                job2.segment_offset(segment)

    def test_transcript_wrappers_and_unchanged_data(self):
        segments = [{"at": "0:07", "text": "Synthetic line C stalled."},
                    {"start": 70, "text": "Synthetic line D reset."}]
        original = json.dumps(segments)
        self.assertIs(job2.segments_from({"data": {"segments": segments}}), segments)
        self.assertEqual(json.dumps(segments), original)

    def test_ambiguous_or_bad_transcript_rejected(self):
        for value in ({"segments": []}, {"segments": ["bad"]},
                      {"segments": [{"at": "0:07"}]},
                      {"segments": [{"text": "No offset"}]},
                      [{"segments": [{"at": "0:07", "text": "x"}]},
                       {"segments": [{"at": "0:08", "text": "y"}]}]):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                job2.segments_from(value)


def synthetic_tools():
    specifications = [
        ("transcribe", {"url": {"type": "string"}}, ["url"]),
        ("memo_write", {"text": {"type": "string"}, "step": {"type": "string"}}, ["text", "step"]),
        ("memo_search", {"query": {"type": "string"}, "step": {"type": "string"}}, ["step"]),
        ("submit", {"result": {"type": "string"}, "skillVersion": {"type": "string"},
                    "step": {"type": "string"}}, ["result", "skillVersion", "step"]),
    ]
    return [{"name": name, "inputSchema": {"type": "object", "properties": props,
                                           "required": required}}
            for name, props, required in specifications]


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.audit = patch.object(job2, "audit")
        self.audit.start()
        self.addCleanup(self.audit.stop)
        self.state = patch.object(job2, "STATE", {"exchanges": []})
        self.state.start()
        self.addCleanup(self.state.stop)
        self.client = job2.MCP("https://example.invalid/synthetic")
        self.client.tools = synthetic_tools()
        self.requests = []

        def rpc(method, params):
            self.assertEqual(method, "tools/call")
            self.requests.append(params)
            result = ({"accepted": True, "receipt": "synthetic-receipt"}
                      if params["name"] == "submit"
                      else {"step": f"synthetic-step-{len(self.requests)}"})
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        self.client.rpc = rpc

    def test_exact_schema_binding_and_latest_step(self):
        self.client.call("transcribe", {"url": "https://example.invalid/audio"})
        self.client.call("memo_write", {"text": "Synthetic fault @0:07"})
        self.client.call("memo_search", {})
        result = self.client.call("submit", {"notes": "Synthetic fault @0:07", "skillVersion": "1"})
        self.assertTrue(result["accepted"])
        self.assertEqual(self.requests[1]["arguments"],
                         {"text": "Synthetic fault @0:07", "step": "synthetic-step-1"})
        self.assertEqual(self.requests[2]["arguments"], {"step": "synthetic-step-2"})
        self.assertEqual(self.requests[3]["arguments"], {
            "result": "Synthetic fault @0:07", "skillVersion": "1", "step": "synthetic-step-3"})
        self.assertEqual(job2.STATE["step"], "synthetic-step-3")
        self.assertEqual(json.loads(job2.STATE["exchanges"][-1]["reply"]["content"][0]["text"])["receipt"],
                         "synthetic-receipt")

    def test_unknown_required_field_fails_before_request(self):
        self.client.tools[0]["inputSchema"]["properties"]["unknown"] = {"type": "string"}
        self.client.tools[0]["inputSchema"]["required"].append("unknown")
        with self.assertRaises(RuntimeError):
            self.client.call("transcribe", {"url": "https://example.invalid/audio"})
        self.assertFalse(self.requests)

    def test_missing_or_unbound_step_fails_before_request(self):
        with self.assertRaises(RuntimeError):
            self.client.call("memo_search", {})
        self.client.token = "synthetic-step"
        self.client.tools[2]["inputSchema"] = {"properties": {}}
        with self.assertRaises(RuntimeError):
            self.client.call("memo_search", {})
        self.assertFalse(self.requests)

    def test_stale_step_fails(self):
        self.client.token = "stale"
        self.client.rpc = lambda *args: {"structuredContent": {"step": "stale"}}
        with self.assertRaises(RuntimeError):
            self.client.call("memo_search", {})

    def test_sse_and_structured_content(self):
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        self.assertEqual(job2.decode_events(
            ("event: message\r\ndata: " + json.dumps(payload) + "\r\n\r\n").encode()), [payload])
        self.assertEqual(job2.unpack({"structuredContent": {"step": "synthetic"}}),
                         {"step": "synthetic"})


class LedgerTests(unittest.TestCase):
    def setUp(self):
        for target, replacement in (("STATE", {"exchanges": []}), ("audit", lambda *args: None),
                                    ("CHECKPOINT_CERT", None)):
            patcher = patch.object(job2, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.transcript = [
            {"at": "0:01", "text": "All times here are UTC plus two."},
            {"at": "0:07", "text": "Line C stalled at 06:20 and reset at 06:21."},
            {"at": "0:11", "text": "Line C stalled at 06:23."},
        ]
        self.ledger = [[], [[0, 5], [6, 9]], [[0, 5]]]
        self.units = job2.source_units(self.transcript)

    def test_multiple_events_repeats_clocks_and_timezone(self):
        notes = job2.render_events(self.ledger, self.units, self.transcript)
        self.assertEqual(len(notes), 3)
        self.assertEqual(notes[0], "Line C stalled at 06:20 [context: All times here are UTC plus two.] @0:07")
        self.assertIn("reset at 06:21.", notes[1])
        self.assertIn("Line C", notes[1])
        self.assertIn("All times here are UTC plus two.", notes[1])
        self.assertTrue(notes[1].endswith("@0:07"))
        self.assertTrue(notes[2].endswith("@0:11"))

    def test_unit_source_coordinates_and_sentence_splitting(self):
        transcript = [{"at": "00:07", "text": "  Hello.  Line C stalled; then reset.  "}]
        units = job2.source_units(transcript)
        self.assertEqual([u["text"] for u in units], ["Hello.", "Line C stalled;", "then reset."])
        for unit in units:
            text = transcript[unit["segment"]]["text"]
            self.assertEqual(text[unit["start"]:unit["start"] + len(unit["text"])], unit["text"])
        notes = job2.render_events([[], [[0, 3]], [[0, 2]]], units, transcript)
        self.assertEqual(len(notes), 2)
        self.assertTrue(all(note.endswith("@00:07") for note in notes))

    def test_omitted_unit_is_not_silently_accepted(self):
        with self.assertRaises(RuntimeError):
            job2.render_events(self.ledger[1:], self.units, self.transcript)

    def test_bad_or_overlapping_ranges_rejected(self):
        for ranges in ([[0, 100]], [[-1, 2]], [[True, 2]], [[3, 2]],
                       [[0, 4], [3, 5]], [[0, 0]], "invented"):
            with self.subTest(ranges=ranges), self.assertRaises(RuntimeError):
                job2.event_spans([[], ranges, [[0, 5]]], self.units)

    def test_compact_pipeline_has_only_two_small_passes(self):
        calls = []
        def infer(prompt, budget):
            calls.append(budget)
            return self.ledger if len(calls) == 1 else [1, 1, 1]
        with patch.object(job2, "audit") as audit:
            notes = job2.compact_extract("synthetic skill", self.transcript, infer)
        self.assertEqual(len(notes), 3)
        self.assertEqual(calls, [512, 41])
        self.assertEqual(job2.STATE["local_verification"], [1, 1, 1])
        self.assertEqual([call.args for call in audit.call_args_list], [
            ("inference_select", "started"), ("inference_verify", "started"),
            ("inference_complete", "started")])

    def test_every_unit_requires_positive_independent_audit(self):
        for audit in ([1, 0, 1], [1, 1], [True, True, True], {"ok": True}):
            responses = iter([self.ledger, audit])
            with self.subTest(audit=audit), self.assertRaises(RuntimeError):
                job2.compact_extract("synthetic skill", self.transcript, lambda *args: next(responses))

    def test_clocks_are_not_inherited_or_guessed_between_events(self):
        transcript = [{"at": "0:07", "text": "stalled 06:20 reset"}]
        with self.assertRaises(RuntimeError):
            job2.render_events([[[0, 1], [2, 3]]], job2.source_units(transcript), transcript)

    def test_explicit_context_change_is_preserved(self):
        transcript = [{"at": "0:00", "text": "All times are UTC plus two."},
                      {"at": "0:03", "text": "Line C stalled at 06:20."},
                      {"at": "0:06", "text": "From here, times are UTC plus one."},
                      {"at": "0:09", "text": "Line C reset at 06:21."}]
        notes = job2.render_events([[], [[0, 5]], [], [[0, 5]]], job2.source_units(transcript), transcript)
        self.assertIn("UTC plus two", notes[0])
        self.assertNotIn("UTC plus one", notes[0])
        self.assertIn("UTC plus one", notes[1])

    def test_identical_occurrences_are_not_deduplicated(self):
        transcript = [{"at": "0:07", "text": "Line C reset. Line C reset."}]
        notes = job2.render_events([[[0, 3]], [[0, 3]]], job2.source_units(transcript), transcript)
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[0], notes[1])

    def test_confirmation_counts_repeated_notes(self):
        note = "Synthetic repeated hold @0:07"
        self.assertEqual(job2.missing_notes([note, note], {"notes": [{"text": note}]}), [note])
        self.assertEqual(job2.missing_notes([note, note], {"notes": [note, note]}), [])
        self.assertEqual(job2.missing_notes([note], {"notes": [note + " extra"]}), [note])


class CheckpointTests(unittest.TestCase):
    def test_rpc_reply_is_checkpointed_before_decoding(self):
        client = job2.MCP("https://example.invalid/synthetic")
        snapshots = []
        response = SimpleNamespace(headers={}, read=lambda size: b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}')
        manager = unittest.mock.MagicMock()
        manager.__enter__.return_value = response
        with patch.object(job2, "STATE", {"exchanges": []}), \
                patch.object(job2.HTTP, "open", return_value=manager), \
                patch.object(job2, "checkpoint", side_effect=lambda: snapshots.append(json.loads(json.dumps(job2.STATE)))):
            self.assertEqual(client.rpc("synthetic/read", {}), {"ok": True})
        self.assertIn("pending_rpc", snapshots[0])
        self.assertIn("rpc_log", snapshots[1])
        self.assertNotIn("pending_rpc", snapshots[1])

    def test_atomic_encryption_keeps_last_good_checkpoint_on_failure(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            output = Path(tmp)
            result = output / "result.cms"
            result.write_bytes(b"last-good-ciphertext")
            def failed(command, **kwargs):
                Path(command[command.index("-out") + 1]).write_bytes(b"incomplete")
                return SimpleNamespace(returncode=1)
            with patch.object(job2, "OUT", output), patch.dict(job2.os.environ, {"RUNNER_TEMP": tmp}), \
                    patch.object(job2.subprocess, "run", side_effect=failed):
                with self.assertRaises(RuntimeError):
                    job2.encrypt_result("synthetic public certificate")
            self.assertEqual(result.read_bytes(), b"last-good-ciphertext")
            self.assertEqual(sorted(p.name for p in output.iterdir()), ["result.cms"])

    def test_atomic_encryption_publishes_completed_ciphertext_only(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as tmp:
            output = Path(tmp)
            result = output / "result.cms"
            result.write_bytes(b"old-ciphertext")
            def success(command, **kwargs):
                self.assertEqual(result.read_bytes(), b"old-ciphertext")
                self.assertEqual(json.loads(kwargs["input"])["status"], "synthetic-private-state")
                Path(command[command.index("-out") + 1]).write_bytes(b"new-ciphertext")
                return SimpleNamespace(returncode=0)
            with patch.object(job2, "OUT", output), patch.dict(job2.os.environ, {"RUNNER_TEMP": tmp}), \
                    patch.object(job2, "STATE", {"status": "synthetic-private-state"}), \
                    patch.object(job2.subprocess, "run", side_effect=success):
                job2.encrypt_result("synthetic public certificate")
            self.assertEqual(result.read_bytes(), b"new-ciphertext")

    def test_sigterm_requests_stop_without_interrupting_checkpoint(self):
        with patch.object(job2, "CHECKPOINT_CERT", "synthetic public certificate"), \
                patch.object(job2, "CHECKPOINT_BUSY", False), patch.object(job2, "STOP_REQUESTED", False), \
                patch.object(job2, "SHUTTING_DOWN", False):
            def encrypt(cert):
                self.assertTrue(job2.CHECKPOINT_BUSY)
                job2.stop_handler(signal.SIGTERM, None)
                self.assertTrue(job2.STOP_REQUESTED)
            with patch.object(job2, "encrypt_result", side_effect=encrypt) as encrypted:
                with self.assertRaises(job2.WorkerStopped):
                    job2.checkpoint()
                encrypted.assert_called_once()
                self.assertFalse(job2.CHECKPOINT_BUSY)
            job2.SHUTTING_DOWN = True
            with patch.object(job2, "encrypt_result") as encrypted:
                job2.checkpoint()
                encrypted.assert_called_once()

    def test_sigterm_outside_checkpoint_interrupts(self):
        with patch.object(job2, "CHECKPOINT_BUSY", False), patch.object(job2, "STOP_REQUESTED", False), \
                patch.object(job2, "SHUTTING_DOWN", False):
            with self.assertRaises(job2.WorkerStopped):
                job2.stop_handler(signal.SIGTERM, None)
            self.assertTrue(job2.STOP_REQUESTED)


class BudgetTests(unittest.TestCase):
    def test_alarm_is_disarmed_and_restored_on_inference_failure(self):
        with patch.object(job2.signal, "signal", return_value="synthetic-previous-handler") as handler, \
                patch.object(job2.signal, "setitimer") as timer, \
                patch.object(job2, "load_and_extract", side_effect=job2.InferenceTimeout("synthetic timeout")):
            with self.assertRaises(job2.InferenceTimeout):
                job2.extract_notes("synthetic skill", [])
        self.assertEqual([call.args for call in timer.call_args_list], [
            (signal.ITIMER_REAL, job2.INFERENCE_SECONDS), (signal.ITIMER_REAL, 0)])
        self.assertEqual(handler.call_args_list[-1].args,
                         (signal.SIGALRM, "synthetic-previous-handler"))

    def test_generation_and_wall_clock_bounds(self):
        with patch.object(job2.time, "monotonic", return_value=100):
            budget = job2.GenerationBudget(100)
            self.assertEqual(budget.allowed_tokens(512), 512)
            budget.used = job2.MAX_GENERATED_TOKENS
            with self.assertRaises(RuntimeError):
                budget.allowed_tokens(1)
            budget.started = -1000
            with self.assertRaises(job2.InferenceTimeout):
                budget.remaining_seconds()

    def test_source_unit_bounds_never_truncate(self):
        with patch.object(job2, "MAX_UNITS", 1), self.assertRaises(RuntimeError):
            job2.source_units([{"at": "0:00", "text": "Hello. Goodbye."}])

    def test_only_complete_json_is_accepted(self):
        self.assertEqual(job2.parse_json_output("```json\n[[],[[0,3]]]\n```"), [[], [[0, 3]]])
        with self.assertRaises(ValueError):
            job2.parse_json_output("[[],[[0,")


class DiagnosticTests(unittest.TestCase):
    def test_traceback_has_no_locals_or_source_lines(self):
        try:
            raise RuntimeError("synthetic private diagnostic")
        except RuntimeError as exc:
            diagnostic = job2.failure_diagnostics(exc)
        self.assertEqual(diagnostic["type"], "RuntimeError")
        self.assertEqual(diagnostic["message"], "synthetic private diagnostic")
        self.assertTrue(diagnostic["frames"])
        for frame in diagnostic["frames"]:
            self.assertEqual(set(frame), {"file", "function", "line"})
            self.assertEqual(frame["file"], Path(frame["file"]).name)


if __name__ == "__main__":
    unittest.main()
