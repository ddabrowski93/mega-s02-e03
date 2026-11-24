"""Offline synthetic tests only: never load private results or invoke inference/HTTP."""

import importlib.util
import copy
from decimal import Decimal
import io
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


class PrivateStateTests(unittest.TestCase):
    def setUp(self):
        for target, replacement in (("STATE", {"exchanges": []}), ("audit", lambda *args: None),
                                    ("CHECKPOINT_CERT", None)):
            patcher = patch.object(job2, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)


def synthetic_ledger(verified=False):
    context = [{"segment": 0, "quote": "All times are UTC plus two."}]
    result = {"segments": [{"segment": 0, "events": []}, {"segment": 1, "events": [
        {"evidence": "Line C stalled at 06:20", "occurrence": 0, "context": context},
        {"evidence": "reset at 06:21.", "occurrence": 0,
         "context": context + [{"segment": 1, "quote": "Line C"}]},
    ]}]}
    if verified:
        result.update({"complete_and_exact": True, "uncertain": False, "issues": []})
        for row in result["segments"]:
            row["verified"] = True
    return result


def synthetic_transcript():
    return [{"at": "0:01", "text": "All times are UTC plus two."},
            {"at": "00:07", "text": "Line C stalled at 06:20 and reset at 06:21."}]


class LedgerTests(PrivateStateTests):
    def test_multi_event_grounding_clock_timezone_and_original_offset(self):
        notes = job2.notes_from_ledger(synthetic_ledger(True), synthetic_transcript(), verified=True)
        self.assertEqual(len(notes), 2)
        self.assertIn("stalled at 06:20", notes[0])
        self.assertIn("reset at 06:21.", notes[1])
        self.assertTrue(all("UTC plus two" in note and note.endswith("@00:07") for note in notes))
        self.assertIn("Line C", notes[1])

    def test_every_segment_must_be_present_ordered_and_verified(self):
        for mutation in ("missing", "order", "unverified", "bool_index"):
            ledger = synthetic_ledger(True)
            if mutation == "missing":
                ledger["segments"].pop(0)
            elif mutation == "order":
                ledger["segments"].reverse()
            elif mutation == "unverified":
                ledger["segments"][0]["verified"] = False
            else:
                ledger["segments"][0]["segment"] = False
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                job2.notes_from_ledger(ledger, synthetic_transcript(), verified=True)

    def test_invented_context_evidence_and_duplicate_occurrence_rejected(self):
        for mutation in ("evidence", "context", "duplicate", "occurrence"):
            ledger = synthetic_ledger()
            event = ledger["segments"][1]["events"][0]
            if mutation == "evidence":
                event["evidence"] = "Invented fault"
            elif mutation == "context":
                event["context"][0]["quote"] = "UTC minus six"
            elif mutation == "duplicate":
                ledger["segments"][1]["events"].insert(1, copy.deepcopy(event))
            else:
                event["occurrence"] = 1
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                job2.notes_from_ledger(ledger, synthetic_transcript())

    def test_identical_repeated_occurrences_are_not_merged(self):
        text = "Line C reset."
        transcript = [{"at": "0:07", "text": text + " " + text}]
        ledger = {"segments": [{"segment": 0, "events": [
            {"evidence": text, "occurrence": n, "context": []} for n in (0, 1)]}]}
        notes = job2.notes_from_ledger(ledger, transcript)
        self.assertEqual(notes, [text + " @0:07", text + " @0:07"])

    def test_confirmation_counts_repeats_and_rewrites_missing_then_searches(self):
        note = "Synthetic repeated hold @0:07"
        self.assertEqual(job2.missing_notes([note, note], {"notes": [{"text": note}]}), [note])
        client = unittest.mock.Mock()
        client.call.side_effect = [{"notes": [note]}, {}, {"notes": [note, note]}]
        job2.confirm_notes(client, [note, note])
        self.assertEqual([c.args for c in client.call.call_args_list], [
            ("memo_search", {}), ("memo_write", {"text": note}), ("memo_search", {})])

    def test_persistently_missing_notes_stop_after_second_search(self):
        client = unittest.mock.Mock()
        client.call.side_effect = [{"notes": []}, {}, {"notes": []}]
        with self.assertRaises(RuntimeError):
            job2.confirm_notes(client, ["Synthetic fault @0:07"])
        self.assertEqual(client.call.call_count, 3)


def completion_reply(content="{}", **overrides):
    reply = {"model": job2.MODEL, "choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200, "cost": 0.001}}
    reply.update(overrides)
    manager = unittest.mock.MagicMock()
    manager.__enter__.return_value.read.return_value = job2.json_bytes(reply)
    return manager


class OpenRouterTests(PrivateStateTests):
    def setUp(self):
        super().setUp()
        self.key = "synthetic-api-key-not-a-real-credential"
        self.client = job2.OpenRouterClient(api_key=self.key)
        self.messages = [{"role": "user", "content": "Synthetic transcript only."}]
        self.schema = job2.ledger_schema()

    def test_two_requests_total_fixed_model_privacy_prices_and_no_auth_in_state(self):
        snapshots = []
        with patch.object(job2.HTTP, "open", side_effect=[completion_reply(), completion_reply()]) as opened, \
                patch.object(job2, "checkpoint", side_effect=lambda: snapshots.append(copy.deepcopy(job2.STATE))):
            self.client.complete(self.messages, self.schema)
            job2.OpenRouterClient(api_key=self.key).complete(self.messages, self.schema)
            with self.assertRaises(RuntimeError):
                self.client.complete(self.messages, self.schema)
        self.assertEqual(opened.call_count, 2)
        request = opened.call_args_list[0].args[0]
        self.assertEqual(request.full_url, job2.OPENROUTER_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.key)
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "openai/gpt-4.1-mini")
        self.assertEqual(payload["provider"], {
            "max_price": {"prompt": 0.4, "completion": 1.6}, "require_parameters": True,
            "data_collection": "deny", "allow_fallbacks": False})
        self.assertEqual(payload["plugins"], [])
        self.assertNotIn("tools", payload)
        self.assertNotIn("models", payload)
        self.assertEqual(payload["max_tokens"], 8000)
        self.assertEqual(snapshots[0]["openrouter"]["requests"][0]["status"], "pending")
        self.assertGreater(Decimal(snapshots[0]["openrouter"]["reserved_usd"]), 0)
        self.assertEqual(self.client.budget["reported_usd"], "0.002")
        self.assertNotIn(self.key, json.dumps(job2.STATE))

    def test_utf8_byte_limit_not_character_limit(self):
        messages = [{"role": "user", "content": "ą" * 30_001}]
        self.assertLess(len(json.dumps(messages, ensure_ascii=False)), job2.MAX_MESSAGE_BYTES)
        with self.assertRaisesRegex(RuntimeError, "UTF-8"):
            self.client.complete(messages, self.schema)
        self.assertFalse(self.client.budget["requests"])

    def test_reservations_and_caps_are_checked_before_network(self):
        with patch.object(job2, "BUDGET_USD", Decimal("0.000001")), self.assertRaisesRegex(RuntimeError, "budget"):
            self.client.complete(self.messages, self.schema)
        for tokens in (0, True, 8001):
            with self.subTest(tokens=tokens), self.assertRaises(RuntimeError):
                self.client.complete(self.messages, self.schema, max_tokens=tokens)
        with self.assertRaisesRegex(RuntimeError, "overhead"):
            self.client.complete(self.messages, {"description": "x" * 9000})
        self.assertFalse(self.client.budget["requests"])
        bound = 2 * ((60_000 + 8192) * Decimal("0.0000004") + 8000 * Decimal("0.0000016"))
        self.assertEqual(bound, Decimal("0.0801536"))

    def test_timeout_reserves_attempt_and_never_retries_or_refunds(self):
        with patch.object(job2.HTTP, "open", side_effect=TimeoutError(self.key)) as opened:
            with self.assertRaises(RuntimeError):
                self.client.complete(self.messages, self.schema)
            reserved = self.client.budget["reserved_usd"]
            with self.assertRaises(RuntimeError):
                job2.OpenRouterClient(api_key=self.key).complete(self.messages, self.schema)
        self.assertEqual(opened.call_count, 1)
        self.assertGreater(Decimal(reserved), 0)
        self.assertEqual(self.client.budget["reserved_usd"], reserved)
        self.assertNotIn(self.key, json.dumps(job2.STATE))

    def test_missing_or_excess_usage_halts_without_additional_request(self):
        for usage in ({"prompt_tokens": 1}, {"prompt_tokens": 1, "completion_tokens": 8001,
                                              "total_tokens": 8002, "cost": 0.001}):
            with self.subTest(usage=usage), patch.object(job2, "STATE", {"exchanges": []}):
                client = job2.OpenRouterClient(api_key=self.key)
                with patch.object(job2.HTTP, "open", return_value=completion_reply(usage=usage)) as opened:
                    with self.assertRaises(RuntimeError):
                        client.complete(self.messages, self.schema)
                    with self.assertRaises(RuntimeError):
                        client.complete(self.messages, self.schema)
                self.assertEqual(opened.call_count, 1)

    def test_reported_cost_over_bound_is_recorded_then_stops(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200, "cost": 1.1}
        with patch.object(job2.HTTP, "open", return_value=completion_reply(usage=usage)) as opened:
            with self.assertRaises(RuntimeError):
                self.client.complete(self.messages, self.schema)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.client.budget["reported_usd"], "1.1")
        self.assertTrue(self.client.budget["halted"])

    def test_wrong_model_or_truncated_completion_does_not_fallback(self):
        for reply in (completion_reply(model="different-model"), completion_reply(choices=[{
                "finish_reason": "length", "message": {"content": "partial"}}])):
            with patch.object(job2, "STATE", {"exchanges": []}):
                client = job2.OpenRouterClient(api_key=self.key)
                with patch.object(job2.HTTP, "open", return_value=reply) as opened:
                    with self.assertRaises(RuntimeError):
                        client.complete(self.messages, self.schema)
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(client.budget["reported_usd"], "0.001")

    def test_http_error_usage_is_recorded_without_auth_or_retry(self):
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0}
        error = job2.urllib.error.HTTPError(job2.OPENROUTER_URL, 429, self.key, {},
                                          io.BytesIO(job2.json_bytes({"usage": usage})))
        with patch.object(job2.HTTP, "open", side_effect=error) as opened:
            with self.assertRaises(RuntimeError):
                self.client.complete(self.messages, self.schema)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.client.budget["reported_usd"], "0")
        self.assertNotIn(self.key, json.dumps(job2.STATE))

    def test_redirect_is_forbidden(self):
        with self.assertRaises(RuntimeError):
            job2.NoRedirect().redirect_request(None, None, 307, "redirect", {}, "https://example.invalid")

    def test_key_echo_is_redacted_and_mcp_secrets_are_blocked(self):
        with patch.object(job2.HTTP, "open", return_value=completion_reply(self.key)):
            self.assertEqual(self.client.complete(self.messages, self.schema), "[REDACTED]")
        self.assertNotIn(self.key, json.dumps(job2.STATE))
        job2.STATE["step"] = "synthetic-private-step-value"
        with self.assertRaisesRegex(RuntimeError, "sensitive"):
            self.client.complete([{"role": "user", "content": job2.STATE["step"]}], self.schema)
        with patch.dict(job2.os.environ, {"MEGA_JOB2_URL": "https://example.invalid/private-capability"}):
            with self.assertRaisesRegex(RuntimeError, "sensitive"):
                self.client.complete([{"role": "user", "content": job2.os.environ["MEGA_JOB2_URL"]}], self.schema)

    def test_two_call_extraction_can_correct_omissions_and_projects_only_transcript(self):
        transcript = synthetic_transcript()
        transcript[0]["internal_step"] = "extra-private-field-never-send"
        first = synthetic_ledger()
        first["segments"][1]["events"].pop()
        job2.STATE["step"] = "current-private-step-never-send"
        with patch.object(job2, "OpenRouterClient", return_value=self.client), \
                patch.object(job2.HTTP, "open", side_effect=[
                    completion_reply(json.dumps(first)), completion_reply(json.dumps(synthetic_ledger(True)))]) as opened:
            notes = job2.extract_notes("Synthetic skill; one note per occurrence.", transcript)
        self.assertEqual(len(notes), 2)
        self.assertEqual(opened.call_count, 2)
        for call in opened.call_args_list:
            body = json.loads(call.args[0].data)
            messages = json.dumps(body["messages"])
            self.assertNotIn("current-private-step-never-send", messages)
            self.assertNotIn("extra-private-field-never-send", messages)

    def test_second_call_may_repair_malformed_candidate_but_never_a_third(self):
        bad_final = synthetic_ledger(True)
        bad_final["uncertain"] = True
        with patch.object(job2, "OpenRouterClient", return_value=self.client), \
                patch.object(job2.HTTP, "open", side_effect=[
                    completion_reply("not valid JSON"), completion_reply(json.dumps(bad_final))]) as opened:
            with self.assertRaisesRegex(RuntimeError, "review incomplete"):
                job2.extract_notes("Synthetic skill.", synthetic_transcript())
        self.assertEqual(opened.call_count, 2)
        self.assertIn("candidate_validation", job2.STATE)
        self.assertNotIn("notes", job2.STATE)


    def test_final_verification_failures_never_publish_notes(self):
        for mutation in ("issues", "incomplete", "unverified", "ungrounded", "missing", "json"):
            final = synthetic_ledger(True)
            if mutation == "issues":
                final["issues"] = ["Unresolved scope"]
            elif mutation == "incomplete":
                final["complete_and_exact"] = False
            elif mutation == "unverified":
                final["segments"][0]["verified"] = False
            elif mutation == "ungrounded":
                final["segments"][1]["events"][0]["evidence"] = "Invented fault"
            elif mutation == "missing":
                final["segments"].pop()
            final_text = "not JSON" if mutation == "json" else json.dumps(final)
            with self.subTest(mutation=mutation), patch.object(job2, "STATE", {"exchanges": []}), \
                    patch.object(job2.OpenRouterClient, "complete", side_effect=[
                        json.dumps(synthetic_ledger()), final_text]) as complete, \
                    patch.dict(job2.os.environ, {"OPENROUTER_API_KEY": self.key}):
                with self.assertRaises((RuntimeError, ValueError)):
                    job2.extract_notes("Synthetic skill.", synthetic_transcript())
                self.assertEqual(complete.call_count, 2)
                self.assertNotIn("notes", job2.STATE)

    def test_cancelled_request_keeps_pending_reservation_and_cannot_retry(self):
        with patch.object(job2.HTTP, "open", side_effect=job2.WorkerStopped()) as opened:
            with self.assertRaises(job2.WorkerStopped):
                self.client.complete(self.messages, self.schema)
            with self.assertRaisesRegex(RuntimeError, "unresolved"):
                self.client.complete(self.messages, self.schema)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self.client.budget["requests"][0]["status"], "pending")
        self.assertGreater(Decimal(self.client.budget["reserved_usd"]), 0)


class ApprovalTests(unittest.TestCase):
    def test_missing_approval_or_key_blocks_before_encryption_and_job_calls(self):
        for approval, key in (("false", "synthetic-key"), ("", "synthetic-key"), ("true", "")):
            with self.subTest(approval=approval, has_key=bool(key)), \
                    patch.dict(job2.os.environ, {"GITHUB_ACTIONS": "true",
                        "RUNNER_ENVIRONMENT": "github-hosted", "MEGA_APPROVE_OPENROUTER": approval,
                        "OPENROUTER_API_KEY": key}), \
                    patch.object(job2, "encrypt_result") as encrypt, \
                    patch.object(job2, "execute_job") as execute, patch("builtins.print"):
                self.assertEqual(job2.main(), 1)
                encrypt.assert_not_called()
                execute.assert_not_called()


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


class DiagnosticTests(unittest.TestCase):
    def test_diagnostic_redacts_api_key(self):
        key = "synthetic-private-provider-key"
        with patch.dict(job2.os.environ, {"OPENROUTER_API_KEY": key}):
            diagnostic = job2.failure_diagnostics(RuntimeError("echoed " + key))
        self.assertEqual(diagnostic["message"], "echoed [REDACTED]")
        self.assertNotIn(key, json.dumps(diagnostic))

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
