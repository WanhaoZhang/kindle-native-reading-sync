"""Offline-only protocol and single-dispatch tests. No account file is read."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import weread_upload as upload

ROOT = Path(__file__).resolve().parent

FAKE_AUTH = {"vid": "synthetic-vid", "deviceId": "synthetic-device", "accessToken": "synthetic-access"}


class MockTransport:
    def __init__(self, outcome="matched"):
        self.calls, self.outcome, self.written = [], outcome, False
    def call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/book/getProgress":
            return {"book": {"readingTime": 113 if self.written and self.outcome == "matched" else 100, "progress": 10,
                             "chapterUid": 2, "chapterOffset": 7}}
        if path == "/config": return {"token": "synthetic-config"}
        if path == "/feature": return {"feature": {"guest_token": "synthetic-guest"}}
        if path == "/book/batchUploadProgress":
            self.written = True
            if self.outcome == "unknown": raise TimeoutError("synthetic failure")
            if self.outcome == "rejected": return {"succ": 0, "errCode": -123, "accessToken": "SYNTHETIC-DO-NOT-LOG", "_safe_transport_status": 200}
            return {"succ": 1}
        raise AssertionError("unexpected path")


class OfflineUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        folder=Path(self.tmp.name);source=folder/"synthetic.log"
        line="261101:221325 cvm: I HTMLPosition Verdict:Processed, IntervalTime:13881, TotalTime:13881, TotalWords:100"
        source.write_text(line+"\n")
        self.sample={"book_id":"example-book","book_title":"Synthetic Book","evidence_kind":"native_counted_event",
            "source_path":str(source),"source_sha256":hashlib.sha256(source.read_bytes()).hexdigest(),"source_line":1,
            "expected_baseline":{"readingTime":100,"progress":10,"chapterUid":2,"chapterOffset":7},
            "rows":[{"source_record_id":hashlib.sha256(line.encode()).hexdigest(),"start_time":1793542392,
                     "duration":13,"time_zone":"+08:00","book_version":0,"book_version_source":"unavailable","risk":0}]}
        self.sample_file=folder/"sample.json";self.sample_file.write_text(json.dumps(self.sample))

    def test_real_source_record_matches_and_dryrun_never_loads_auth_or_network(self):
        upload.verify_evidence(self.sample)
        with patch.object(upload, "load_auth") as auth, patch.object(upload, "UploadTransport") as transport:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(upload.main(["--sample", str(self.sample_file)]), 0)
            auth.assert_not_called()
            transport.assert_not_called()
        self.assertEqual(upload.prepare(self.sample)["reading_seconds"], 13)

    def test_reference_signature_vector(self):
        payload = "393727764_35645832171399234841676621530100678335_35645832171399234841676621530100678335_CB_00K1XI1aSDlZ76f75hG9n7JG_0_+08:00_81_0_1792317600_+08:00_81_0"
        self.assertEqual(upload.sign([upload.DEFAULT_GUEST_TOKEN, "821", "1792318161", payload]),
                         "dfd6f2ceb1e4a5d1152c6d19fd9c9f84115cad69989ee799faec377d05d8a833")

    def test_time_only_dto_and_independent_signature_units(self):
        context = {**FAKE_AUTH, "config_token": "config", "guest_token": "guest", "record_create_time_zone": "+08:00"}
        packet = upload.build_batch(context, "example-book", self.sample["rows"], now_s=1793544000,
                                    now_ms=1793544000456, inner_random=7, outer_random=3)
        body = packet["books"][0]
        self.assertEqual((body["readingTime"], body["bookVersion"], body["hours"][0]["startOfHour"]), (13, 0, 1793541600))
        self.assertEqual(body["timestamp"], 1793544000)
        self.assertEqual(packet["timestamp"], 1793544000456)
        self.assertEqual(packet["signature"], hashlib.sha256(b"1793544000456config3").hexdigest())
        self.assertTrue(set(body).isdisjoint({"chapterUid", "chapterIdx", "chapterOffset", "voiceType"}))

    def test_stable_source_identity_survives_snapshot_hash_and_mapping_change(self):
        second = copy.deepcopy(self.sample)
        second["source_sha256"] = "f" * 64
        self.assertEqual(upload.sample_identity(second), upload.sample_identity(self.sample))
        second["book_id"] = "another-book"
        self.assertEqual(upload.prepare(second)["source_segment_id"], upload.prepare(self.sample)["source_segment_id"])

    def test_unknown_single_post_and_replay_blocked_even_after_book_mapping_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger, transport = Path(tmp) / "ledger.sqlite", MockTransport("unknown")
            result = upload.commit_once(self.sample, upload.sample_identity(self.sample), FAKE_AUTH, ledger, transport)
            self.assertEqual(result["state"], "unknown")
            self.assertFalse(result["replay_allowed"])
            self.assertEqual(sum(method == "POST" for method, _, _ in transport.calls), 1)
            for sample in (self.sample, {**self.sample, "book_id": "different-book"}):
                retry = MockTransport()
                with self.assertRaises(ValueError):
                    upload.commit_once(sample, upload.sample_identity(sample), FAKE_AUTH, ledger, retry)
                self.assertFalse(any(method == "POST" for method, _, _ in retry.calls))
            self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)

    def test_success_ack_without_cloud_increment_is_ack_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = upload.commit_once(self.sample, upload.sample_identity(self.sample), FAKE_AUTH,
                                        Path(tmp) / "ledger.sqlite", MockTransport("ack_only"))
            self.assertEqual(result["state"], "server_ack_only")
            self.assertEqual(result["verification"], "needs_review")

    def test_success_with_expected_increment_and_unchanged_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = upload.commit_once(self.sample, upload.sample_identity(self.sample), FAKE_AUTH,
                                        Path(tmp) / "ledger.sqlite", MockTransport())
            self.assertEqual((result["state"], result["verification"]), ("confirmed", "matched"))

    def test_wrong_approval_or_changed_baseline_never_dispatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MockTransport()
            with self.assertRaises(ValueError):
                upload.commit_once(self.sample, "not-approved", FAKE_AUTH, Path(tmp) / "ledger.sqlite", client)
            self.assertEqual(client.calls, [])
            modified = copy.deepcopy(self.sample)
            modified["expected_baseline"]["readingTime"] = 7320
            with self.assertRaises(ValueError):
                upload.commit_once(modified, upload.sample_identity(modified), FAKE_AUTH, Path(tmp) / "ledger.sqlite", client)
            self.assertFalse(any(method == "POST" for method, _, _ in client.calls))

    def test_uploader_reuses_verified_ca_opener(self):
        with patch.object(upload, "ReadonlyTransport") as readonly:
            client = upload.UploadTransport(FAKE_AUTH)
            self.assertIs(client.opener, readonly.return_value.opener)
            readonly.assert_called_once_with(timeout=20)

    def test_non_success_retains_only_safe_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = upload.commit_once(self.sample, upload.sample_identity(self.sample), FAKE_AUTH,
                                        Path(tmp) / "ledger.sqlite", MockTransport("rejected"))
            self.assertEqual(result["state"], "unknown")
            self.assertEqual(result["safe_response"]["errCode"], -123)
            self.assertEqual(result["safe_response"]["status_code"], 200)
            self.assertIn("accessToken", result["safe_response"]["response_keys"])
            self.assertNotIn("SYNTHETIC-DO-NOT-LOG", json.dumps(result))

    def test_overlapping_or_duplicate_source_samples_refused(self):
        row = self.sample["rows"][0]
        with self.assertRaises(ValueError): upload.hours([row, row])
        modified = copy.deepcopy(self.sample)
        modified["rows"][0]["duration"] = 14
        with self.assertRaises(ValueError): upload.verify_evidence(modified)


if __name__ == "__main__": unittest.main()
