"""Offline synthetic tests only: never load private results or invoke inference/HTTP."""

import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "job2", Path(__file__).resolve().parents[1] / "worker" / "job2.py")
job2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(job2)


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
        self.transcript = [
            {"at": "0:01", "text": "All times here are UTC plus two."},
            {"at": "0:07", "text": "Line C stalled at 06:20. Line C reset at 06:21."},
            {"at": "0:11", "text": "Line C stalled at 06:23."},
        ]
        self.ledger = [{"segment": 0, "events": []}, {"segment": 1, "events": [
            {"evidence": "Line C stalled at 06:20.", "context": [
                {"segment": 0, "quote": "All times here are UTC plus two."}]},
            {"evidence": "Line C reset at 06:21.", "context": [
                {"segment": 0, "quote": "All times here are UTC plus two."}]},
        ]}, {"segment": 2, "events": [
            {"evidence": "Line C stalled at 06:23.", "context": [
                {"segment": 0, "quote": "All times here are UTC plus two."}]},
        ]}]

    def test_multiple_events_repeats_clocks_and_timezone(self):
        notes = job2.notes_from_ledger(self.ledger, self.transcript)
        self.assertEqual(len(notes), 3)
        self.assertEqual(notes[0], "Line C stalled at 06:20. [context: All times here are UTC plus two.] @0:07")
        self.assertTrue(notes[1].endswith("@0:07"))
        self.assertTrue(notes[2].endswith("@0:11"))

    def test_omitted_segment_is_not_silently_accepted(self):
        with self.assertRaises(RuntimeError):
            job2.notes_from_ledger(self.ledger[1:], self.transcript)

    def test_invented_evidence_or_timezone_rejected(self):
        self.ledger[1]["events"][0]["context"][0]["quote"] = "UTC minus five"
        with self.assertRaises(RuntimeError):
            job2.notes_from_ledger(self.ledger, self.transcript)
        self.ledger[1]["events"][0]["context"] = []
        self.ledger[1]["events"][0]["evidence"] = "Invented fault"
        with self.assertRaises(RuntimeError):
            job2.notes_from_ledger(self.ledger, self.transcript)

    def test_every_segment_requires_positive_independent_audit(self):
        audit = {"segments": [{"segment": i, "complete_and_exact": True, "issues": []}
                               for i in range(3)]}
        self.assertTrue(job2.coverage_verified(audit, 3))
        audit["segments"][1]["issues"] = ["Synthetic missing reset"]
        self.assertFalse(job2.coverage_verified(audit, 3))
        self.assertFalse(job2.coverage_verified({"segments": []}, 3))

    def test_confirmation_counts_repeated_notes(self):
        note = "Synthetic repeated hold @0:07"
        self.assertEqual(job2.missing_notes([note, note], {"notes": [{"text": note}]}), [note])
        self.assertEqual(job2.missing_notes([note, note], {"notes": [note, note]}), [])
        self.assertEqual(job2.missing_notes([note], {"notes": [note + " extra"]}), [note])


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
