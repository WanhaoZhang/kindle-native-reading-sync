"""All database writes occur in TemporaryDirectory; all HTTP is mocked."""
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from sync_engine import SyncEngine, TZ, hour_packet, reading_position
from weread_upload import sample_identity, build_batch

ROOT = Path(__file__).resolve().parent
AUTH = {"vid": "synthetic-account", "deviceId": "synthetic-device", "accessToken": "synthetic-token"}


class Backend:
    def __init__(self, outcomes=None):
        self.total, self.calls, self.outcomes = {}, [], list(outcomes or [])
        self.days, self.wrong_week, self.shelf = {}, False, []
        self.before_day_response = None
    def factory(self, credentials): return self
    def request(self, method, url, **kwargs):
        assert method == "GET"
        self.calls.append(("GET_READONLY",url,kwargs))
        if url.endswith("/store/search"): return {"books":[]}
        if url.endswith("/shelf/sync"): return {"books":self.shelf}
        if url.endswith("/readdata/detail"):
            if self.before_day_response:
                callback, self.before_day_response = self.before_day_response, None
                callback()
            stamp=datetime.fromtimestamp(kwargs["query"]["baseTime"],TZ).replace(hour=0,minute=0,second=0,microsecond=0)
            base=int((stamp-timedelta(days=stamp.weekday())).timestamp())
            return {"baseTime":base+604800 if self.wrong_week else base,
                    "readTimes":{str(k):v for k,v in self.days.items() if base<=k<base+604800}}
        raise AssertionError(url)
    def call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/book/getProgress":
            bid = kwargs["query"]["bookId"]
            return {"book": {"readingTime": self.total.get(bid, 100), "progress": 81, "chapterUid": 38, "chapterOffset": 667}}
        if path == "/config": return {"token": "synthetic-config"}
        if path == "/feature": return {"feature": {"guest_token": "synthetic-guest"}}
        if path == "/book/batchUploadProgress":
            body = kwargs["body"]["books"][0]; outcome = self.outcomes.pop(0) if self.outcomes else "success"
            if outcome == "timeout": raise TimeoutError("synthetic")
            if outcome in ("success","wrong_day"):
                self.total[body["bookId"]] = self.total.get(body["bookId"], 100) + body["readingTime"]
                for h in body["hours"]:
                    date=datetime.fromtimestamp(h["startOfHour"],TZ).replace(hour=0,minute=0,second=0,microsecond=0)
                    epoch=int(date.timestamp())+(86400 if outcome=="wrong_day" else 0)
                    self.days[epoch]=self.days.get(epoch,0)+h["readingTime"]
            return {"succ": 1}
        raise AssertionError(path)


class EngineTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch("sync_engine.time.sleep")
        sleeper.start(); self.addCleanup(sleeper.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.folder = self.root / "log-results"; self.folder.mkdir()
        self.log = self.root / "source.log"
        self.log.write_text("\n".join(f"261101:220{i}00 cvm: I JSONJournalSaver modificationDate=2026-11-01T14:0{i}:00Z, annotationType=LastPositionReadAnnotation" for i in range(3)))
        self.backend = Backend()
        self.engine = SyncEngine(self.root, credentials=AUTH, transport_factory=self.backend.factory, readonly_factory=lambda:self.backend)
        self.rows, self.sessions = [], []

    def tearDown(self): self.tmp.cleanup()

    def add(self, key="A", seconds=1200, hour=22, extent=10000):
        i = len(self.sessions)
        evidence = dict(book_extent=extent, start_counter_ms=0, end_counter_ms=seconds*1000,
                        start_words=0, end_words=100, session_index=i)
        self.sessions.append(dict(source_key=key, title=key, author="test", measure="counted", matched=True,
                                  hour_allocation_valid=True, hours=[[hour,seconds]], **evidence))
        self.rows.append(dict(source_key=key, title=key, local_date="2026-11-01", hour=hour, seconds=seconds,
                              measure="counted", session_indexes=[i], source_counter_evidence=[evidence],
                              provenance={"logs":[str(self.log)],"snapshot":str(self.root)}))

    def imported(self):
        (self.folder/"sessions.json").write_text(json.dumps(self.sessions))
        (self.folder/"hour_totals.json").write_text(json.dumps(self.rows))
        return self.engine.import_results()

    def test_cumulative_import_missing_days_and_idempotence(self):
        self.add(seconds=100);self.imported(); self.imported()
        self.assertEqual(self.engine.status()["observed_seconds"],100)
        self.rows=[];self.sessions=[];self.imported()
        self.assertEqual(self.engine.list_books()[0]["days"][0]["observed_seconds"],100)
        self.assertEqual(len(self.engine.list_books()[0]["days"]),1)
        self.assertTrue(any("消失" in r for r in self.engine.list_books()[0]["blocked_reasons"]))

    def test_monotonic_decrease_blocks_preserving_observed(self):
        self.add(seconds=100);self.imported();self.rows[0]["seconds"]=90;self.imported()
        book=self.engine.list_books()[0]
        self.assertEqual(book["observed_seconds"],100)
        self.assertTrue(any("减少" in r for r in book["blocked_reasons"]))

    def test_duplicate_dump_clock_evidence_does_not_count_as_three(self):
        self.add(seconds=20)
        self.log.write_text(self.log.read_text().splitlines()[0])
        paths=[]
        for i in range(3):
            path=self.root/f"duplicate{i}.log";path.write_bytes(self.log.read_bytes());paths.append(str(path))
        self.rows[0]["provenance"]["logs"]=paths
        self.imported();self.engine.save_mapping("A","123","target")
        self.assertTrue(self.engine.list_books()[0]["blocked"])
        self.assertEqual(self.engine.prepare()["state"],"blocked")

    def test_multibatch_commit_and_immutable_replay_protection(self):
        self.add();self.imported();self.engine.save_mapping("A","123","target")
        plan=self.engine.prepare(max_seconds=600)
        self.assertEqual(plan["total_seconds"],1200);self.assertEqual(len(plan["batches"]),2)
        with self.assertRaises(ValueError):self.engine.commit(plan["plan_id"])
        self.assertEqual(self.backend.calls,[])
        result=self.engine.commit(plan["plan_id"],True)
        self.assertEqual(result["state"],"confirmed")
        self.assertEqual(self.engine.status()["confirmed_seconds"],1200)
        self.assertEqual(self.engine.prepare()["state"],"empty")
        with self.assertRaises(ValueError):self.engine.commit(plan["plan_id"],True)
        with self.assertRaises(ValueError):self.engine.save_mapping("A","different")
        packets=[kw["body"] for m,p,kw in self.backend.calls if m=="POST"]
        self.assertEqual(len(packets),2)
        self.assertTrue(all(p["books"][0]["hours"][0]["startOfHour"]==1793541600 for p in packets))
        self.assertTrue(all("chapterUid" not in p["books"][0] for p in packets))

    def test_partial_success_unknown_blocks_remainder(self):
        self.add();self.imported();self.engine.save_mapping("A","123")
        self.backend.outcomes=["success","timeout"]
        result=self.engine.commit(self.engine.prepare()["plan_id"],True)
        self.assertEqual((result["state"],result["confirmed_seconds"]),("partial",600))
        self.assertEqual(self.engine.status()["unknown_batches"],1)
        self.assertEqual(self.engine.prepare()["state"],"blocked")
        self.assertEqual(sum(m=="POST" for m,_,_ in self.backend.calls),2)

    def test_first_upload_with_explicit_empty_progress_is_verified(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        original = self.backend.call
        self.backend.total["123"] = 0
        def call(method, path, **kwargs):
            if path == "/book/getProgress":
                if self.backend.total["123"] == 0:
                    return {"bookId":"123","book":{"recordReadingTime":0}}
                return {"bookId":"123","book":{"readingTime":self.backend.total["123"],
                        "progress":0,"chapterUid":0,"chapterOffset":0}}
            return original(method,path,**kwargs)
        self.backend.call = call
        result=self.engine.commit(self.engine.prepare()["plan_id"],True)
        self.assertEqual((result["state"],result["confirmed_seconds"]),("confirmed",20))
        self.assertEqual(result["results"][0]["before"]["chapterUid"],None)
        self.assertEqual(result["results"][0]["after"]["chapterUid"],0)

    def test_malformed_or_mismatched_progress_cannot_be_assumed_empty(self):
        for book in ({},{"recordReadingTime":False},{"recordReadingTime":1},
                     {"readingTime":-1},{"readingTime":0,"chapterUid":0}):
            with self.subTest(book=book),self.assertRaises(ValueError):
                reading_position({"book":book},"123")
        with self.assertRaises(ValueError):
            reading_position({"bookId":"456","book":{"recordReadingTime":0}},"123")

    def test_empty_progress_does_not_allow_chapter_position_to_jump(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        original=self.backend.call;self.backend.total["123"]=0
        def call(method,path,**kwargs):
            if path=="/book/getProgress" and self.backend.total["123"]==0:
                return {"bookId":"123","book":{"recordReadingTime":0}}
            return original(method,path,**kwargs)
        self.backend.call=call
        result=self.engine.commit(self.engine.prepare()["plan_id"],True)
        self.assertEqual(result["state"],"needs_review")
        self.assertEqual(self.engine.status()["confirmed_seconds"],0)

    def test_ack_only_not_counted_as_credit(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        self.backend.outcomes=["ack_only"]
        result=self.engine.commit(self.engine.prepare()["plan_id"],True)
        self.assertEqual(result["state"],"needs_review")
        self.assertEqual(self.engine.status()["confirmed_seconds"],0)
        self.assertTrue(self.engine.list_books()[0]["blocked"])

    def test_stale_progress_readback_retries_get_without_reposting(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        original=self.backend.call;stale=[True]
        def call(method,path,**kwargs):
            if path=="/book/getProgress" and "123" in self.backend.total and stale:
                stale.pop()
                return {"book":{"readingTime":100,"progress":81,"chapterUid":38,"chapterOffset":667}}
            return original(method,path,**kwargs)
        self.backend.call=call
        result=self.engine.commit(self.engine.prepare()["plan_id"],True)
        self.assertEqual(result["state"],"confirmed")
        self.assertEqual(sum(m=="POST" for m,_,_ in self.backend.calls),1)

    def test_readonly_reconcile_delayed_ack_credits_once_and_unlocks_remaining(self):
        self.add(seconds=700);self.imported();self.engine.save_mapping("A","123")
        self.backend.outcomes=["ack_only"]
        result=self.engine.commit(self.engine.prepare()["plan_id"],True)
        bid=result["results"][0]["batch_id"]
        self.assertEqual(self.engine.reconcile(bid)["state"],"needs_review")
        self.backend.total["123"]=700
        day=int(datetime(2026,11,1,tzinfo=TZ).timestamp())
        self.backend.days[day]=600
        checked=self.engine.reconcile(bid)
        self.assertEqual((checked["state"],checked["verification"]),("confirmed","matched_on_readonly_recheck"))
        self.engine.reconcile(bid)
        self.assertEqual(self.engine.status()["confirmed_seconds"],600)
        self.assertEqual(self.engine.prepare()["total_seconds"],100)
        self.assertEqual(sum(m=="POST" for m,_,_ in self.backend.calls),1)

    def test_reconcile_unknown_never_replays_or_credits(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        self.backend.outcomes=["timeout"]
        result=self.engine.commit(self.engine.prepare()["plan_id"],True)
        with self.assertRaises(ValueError):self.engine.reconcile(result["results"][0]["batch_id"])
        self.assertEqual(sum(m=="POST" for m,_,_ in self.backend.calls),1)
        self.assertEqual(self.engine.status()["confirmed_seconds"],0)

    def test_mapping_change_invalidates_plan_account_isolation(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        plan=self.engine.prepare();self.engine.save_mapping("A","456")
        self.assertEqual(self.engine.commit(plan["plan_id"],True)["state"],"blocked")
        self.assertFalse(any(m=="POST" for m,_,_ in self.backend.calls))
        self.engine._injected_credentials={**AUTH,"vid":"other-synthetic-account"}
        self.assertIsNone(self.engine.list_books()[0]["weread_book_id"])

    def test_device_handoff_fence_blocks_mac_before_any_request(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        plan=self.engine.prepare()
        (self.root/".weread/device-handoff.json").write_text('{"state":"prepared"}')
        result=self.engine.commit(plan["plan_id"],True)
        self.assertEqual(result["state"],"blocked")
        self.assertEqual(self.backend.calls,[])

    def test_device_handoff_during_preflight_prevents_post(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        plan=self.engine.prepare()
        self.backend.before_day_response=lambda:(self.root/".weread/device-handoff.json").write_text("partial-fence")
        result=self.engine.commit(plan["plan_id"],True)
        self.assertEqual(result["state"],"blocked")
        self.assertFalse(any(method=="POST" for method,_,_ in self.backend.calls))

    def test_changed_hour_assignment_same_counter_blocks(self):
        self.add(seconds=20);self.imported()
        self.rows[0]["hour"]=21;self.sessions[0]["hours"]=[[21,20]];self.imported()
        self.assertTrue(any("归属变化" in r for r in self.engine.list_books()[0]["blocked_reasons"]))

    def test_renamed_source_with_grown_counter_cannot_duplicate_old_interval(self):
        self.add(seconds=100);self.imported()
        self.rows[0]["source_key"]="renamed-A";self.sessions[0]["source_key"]="renamed-A"
        self.rows[0]["seconds"]=120;self.sessions[0]["hours"]=[[22,120]]
        self.rows[0]["source_counter_evidence"][0]["end_counter_ms"]=120000
        self.imported()
        renamed=next(b for b in self.engine.list_books() if b["local_book_id"]=="renamed-A")
        self.assertTrue(any("重叠" in r for r in renamed["blocked_reasons"]))

    def test_generated_four_book_import_is_portable_and_account_isolated(self):
        for key,seconds,extent in [("A",278,10000),("B",867,20000),("C",85,30000),("D",1637,40000)]:
            self.add(key=key,seconds=seconds,extent=extent)
        self.imported();self.imported()
        self.engine.save_mapping("A","example-book","Example Book")
        status=self.engine.status()
        self.assertEqual((status["observed_seconds"],status["confirmed_seconds"],status["pending_seconds"]),(2867,0,2867))
        self.assertEqual(self.engine.prepare()["total_seconds"],278)
        self.assertEqual(self.backend.calls,[])
        self.engine._injected_credentials={**AUTH,"vid":"other-account"}
        self.imported()
        self.assertTrue(all(b["weread_book_id"] is None for b in self.engine.list_books()))

    def test_same_hour_new_counted_session_increases_without_false_reassignment(self):
        self.add(seconds=100);self.imported();self.add(seconds=20)
        self.sessions[1].update(start_counter_ms=100000,end_counter_ms=120000,start_words=100,end_words=200)
        self.rows[1]["source_counter_evidence"][0].update(start_counter_ms=100000,end_counter_ms=120000,start_words=100,end_words=200)
        self.rows[0]["seconds"]=120;self.rows[0]["session_indexes"]=[0,1]
        self.rows[0]["source_counter_evidence"].extend(self.rows[1]["source_counter_evidence"]);self.rows.pop()
        self.imported();self.engine.save_mapping("A","123")
        self.assertFalse(self.engine.list_books()[0]["blocked"])
        self.assertEqual(self.engine.prepare()["total_seconds"],120)

    def test_wrong_historical_date_credit_is_needs_review(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        self.backend.outcomes=["wrong_day"]
        result=self.engine.commit(self.engine.prepare()["plan_id"],True)
        self.assertEqual(result["state"],"needs_review")
        self.assertEqual(self.engine.status()["confirmed_seconds"],0)
        self.assertEqual(self.engine.prepare()["state"],"blocked")

    def test_server_ignoring_requested_week_blocks_before_post(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        self.backend.wrong_week=True
        result=self.engine.commit(self.engine.prepare()["plan_id"],True)
        self.assertEqual(result["state"],"blocked")
        self.assertFalse(any(m=="POST" for m,_,_ in self.backend.calls))

    def test_private_imported_cb_candidates_merge_only_matching_shelf_items(self):
        self.backend.shelf=[{"bookId":"CB_personal","title":"导入的自传","author":"author"},
                            {"bookId":"CB_unrelated","title":"另一部书","author":"other"}]
        candidates=self.engine.search_candidates("自传")
        self.assertEqual([c["bookId"] for c in candidates],["CB_personal"])
        self.assertTrue(candidates[0]["in_shelf"])

    def test_direct_hour_dto_matches_pure_protocol_without_wall_clock_distribution(self):
        hours=[{"startOfHour":1793541600,"timeZone":"+08:00","readingTime":7,"ttsTime":0},
               {"startOfHour":1793545200,"timeZone":"+08:00","readingTime":31,"ttsTime":0}]
        with patch("sync_engine.time.time",return_value=1793548800),patch("sync_engine.secrets.randbelow",side_effect=[7,3]):
            packet=hour_packet(AUTH,"123",hours,{"token":"config"},{"feature":{"guest_token":"guest"}})
        expected=build_batch({"vid":AUTH["vid"],"deviceId":AUTH["deviceId"],"config_token":"config","guest_token":"guest","record_create_time_zone":"+08:00"},
            "123",[{"start_time":h["startOfHour"],"duration":h["readingTime"],"time_zone":"+08:00"} for h in hours],
            now_s=1793548800,now_ms=1793548800000,inner_random=7,outer_random=3)
        self.assertEqual(packet,expected)
        self.assertEqual(packet["books"][0]["hours"],hours)

    def test_deleted_source_after_prepare_blocks_without_post(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        plan=self.engine.prepare();manifest=plan["batches"][0]["source_manifest"]
        self.assertEqual(manifest,[{"path":str(self.log),"sha256":hashlib.sha256(self.log.read_bytes()).hexdigest()}])
        self.log.unlink()
        result=self.engine.commit(plan["plan_id"],True)
        self.assertEqual(result["results"][0]["verification"],"preflight_failed_no_post")
        self.assertEqual(self.backend.calls,[])

    def test_modified_source_during_preflight_blocks_at_second_validation(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        plan=self.engine.prepare()
        self.backend.before_day_response=lambda:self.log.write_text(self.log.read_text()+"\nchanged after preflight")
        result=self.engine.commit(plan["plan_id"],True)
        self.assertEqual(result["results"][0]["verification"],"preflight_failed_no_post")
        self.assertTrue(self.backend.calls)
        self.assertFalse(any(method=="POST" for method,_,_ in self.backend.calls))
        with self.engine._db() as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],0)

    def test_source_change_between_import_and_prepare_is_blocked(self):
        self.add(seconds=20);self.imported();self.engine.save_mapping("A","123")
        self.log.write_text(self.log.read_text()+"\nchanged before prepare")
        self.assertEqual(self.engine.prepare()["state"],"blocked")
        self.assertEqual(self.backend.calls,[])


if __name__=="__main__":unittest.main()
