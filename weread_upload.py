#!/usr/bin/env python3
"""E-Ink time-only preparation; guarded single upload requires explicit approval.

SPDX-License-Identifier: AGPL-3.0-only
Protocol/signature transformation adapted from MiuRead readtime_protocol.lua
@ ee9d6f24768349d428230036087c379dd957b30c and AnkioTomas/moon
@ 43b2181da6eb5285b62114da85e1413d2f8464e4 (GPL-3.0).
See THIRD_PARTY_NOTICES.md and licenses/GPL-3.0.txt.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from weread_readonly import BridgeError, VERSION_HEADERS, Transport as ReadonlyTransport, load_auth

DEFAULT_GUEST_TOKEN = "5ecdcfd7f"  # Public protocol constant, not an account token.
REMAP = bytes([
    52,202,85,64,29,182,147,198,49,48,41,53,50,167,184,17,
    194,181,22,250,139,177,36,164,16,144,4,233,8,248,59,138,
    156,140,68,249,188,92,105,226,161,218,210,211,117,137,247,30,
    45,80,86,215,114,83,191,34,251,32,15,1,46,69,135,110,
    102,72,242,224,205,254,103,169,67,244,148,81,206,165,74,238,
    19,38,142,204,170,51,20,93,14,57,187,207,145,43,129,77,
    234,153,236,26,44,133,197,217,54,116,75,24,225,241,61,157,
    65,159,180,23,13,214,76,190,220,175,151,40,119,240,98,255,
    113,193,200,39,143,108,104,168,155,230,89,28,27,18,9,152,
    78,63,6,55,0,186,31,10,25,47,201,213,208,87,73,111,
    253,37,228,97,12,66,203,150,100,95,219,173,96,35,141,154,
    109,195,196,94,62,185,146,106,189,91,7,127,118,149,237,79,
    171,132,122,128,231,120,199,229,235,115,131,107,252,56,70,125,
    71,101,179,82,99,58,5,209,239,163,166,222,158,60,2,174,
    178,123,160,246,243,42,192,172,134,3,90,84,11,245,130,212,
    126,227,223,176,216,221,33,232,124,136,162,121,88,112,183,21,
])


def integer(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("Invalid integer reading-time value")
    return value


def zone(value):
    if not isinstance(value, str) or not re.fullmatch(r"[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("Missing standard UTC offset, for example +08:00")
    return value


def rotate(value):
    checksum = 0
    for byte in value:
        checksum ^= byte
    offset = (checksum % 11) % len(value)
    return value[-offset:] + value[:-offset] if offset else value


def sign(parts):
    encoded = [b"5a6f1"] + [str(part).encode("utf-8").translate(REMAP) for part in parts]
    first = hashlib.sha256(rotate(b"".join(sorted(encoded)))).hexdigest().encode("ascii")
    return hashlib.sha256(rotate(first)).hexdigest()


def hours(rows):
    buckets, intervals = {}, []
    for row in rows:
        start = integer(row["start_time"], 1, 4102444800)
        duration = integer(row["duration"], 1, 86400)
        offset = zone(row["time_zone"])
        intervals.append((start, start + duration))
        at, remaining = start, duration
        while remaining:
            begin = at - at % 3600
            count = min(remaining, begin + 3600 - at)
            key = (begin, offset)
            bucket = buckets.setdefault(key, {"startOfHour": begin, "timeZone": offset, "readingTime": 0, "ttsTime": 0})
            bucket["readingTime"] += count
            if bucket["readingTime"] > 3600:
                raise ValueError("Reading-time hour exceeds 3600 seconds")
            at, remaining = at + count, remaining - count
    intervals.sort()
    if any(right[0] < left[1] for left, right in zip(intervals, intervals[1:])):
        raise ValueError("Overlapping samples must be resolved before preparation")
    return [buckets[key] for key in sorted(buckets)]


def build_batch(context, book_id, rows, *, now_s=None, now_ms=None, inner_random=None, outer_random=None):
    """Pure payload constructor; context has no accessToken/refreshToken fields."""
    if not rows or not isinstance(book_id, str) or not book_id:
        raise ValueError("Nonempty book id and sample rows are required")
    now_s = int(time.time()) if now_s is None else integer(now_s, 1, 4102444800)
    now_ms = now_s * 1000 if now_ms is None else integer(now_ms, 1000, 4102444800999)
    inner_random = secrets.randbelow(1000) if inner_random is None else integer(inner_random, 0, 999)
    outer_random = secrets.randbelow(1000) if outer_random is None else integer(outer_random, 0, 999)
    config_token = context.get("config_token")
    guest_token = context.get("guest_token", DEFAULT_GUEST_TOKEN)
    if not all(isinstance(v, str) and v for v in (context.get("vid"), context.get("deviceId"), config_token, guest_token)):
        raise ValueError("Missing signing context; do not guess account/configuration values")
    creation_zone = zone(context.get("record_create_time_zone"))
    versions = {integer(row.get("book_version", 0), 0, 2147483647) for row in rows}
    if len(versions) != 1:
        raise ValueError("Different book versions must be separate batches")
    risk = 0
    for row in rows:
        risk |= integer(row.get("risk", 0), 0, 2147483647)
    buckets = hours(rows)
    total = sum(bucket["readingTime"] for bucket in buckets)
    body = {
        "bookId": book_id, "bookVersion": versions.pop(), "appId": "", "deviceId": context["deviceId"],
        "installId": "", "summary": "", "progress": 0, "currentProgress": 0,
        "autoTime": 0, "isLecture": 0, "isResendReadingInfo": 1, "lectureTime": 0,
        "lectureTextTime": 0, "novalTime": 0, "ttsTime": 0, "finish": False,
        "risk": risk, "readingTime": total, "recordCreateTimeZone": creation_zone,
        "timestamp": now_s, "random": inner_random, "hours": buckets,
    }
    parts = [context["vid"], body["deviceId"], "", book_id, str(risk), creation_zone, str(total), "0"]
    parts.extend("_".join(str(bucket[k]) for k in ("startOfHour", "timeZone", "readingTime", "ttsTime")) for bucket in buckets)
    body["signature"] = sign([guest_token, inner_random, now_s, "_".join(parts)])
    return {"books": [body], "timestamp": now_ms, "random": outer_random,
            "signature": hashlib.sha256(f"{now_ms}{config_token}{outer_random}".encode()).hexdigest()}


def sample_identity(sample):
    """Independent of changing signatures/randoms, based on source evidence."""
    book_id = sample.get("book_id")
    source_hash = sample.get("source_sha256")
    rows = sample.get("rows")
    if not isinstance(book_id, str) or not book_id or not re.fullmatch(r"[a-f0-9]{64}", str(source_hash)):
        raise ValueError("Sample needs book_id and original source SHA256")
    if not isinstance(rows, list) or not rows or sample.get("evidence_kind") != "native_counted_event":
        raise ValueError("Only explicitly sourced native counted events are eligible")
    records = []
    for row in rows:
        source_id = row.get("source_record_id")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("Each sample row needs its original record id")
        records.append({"source_record_id": source_id, "start_time": row["start_time"],
                        "duration": row["duration"], "time_zone": row["time_zone"]})
    if len({record["source_record_id"] for record in records}) != len(records):
        raise ValueError("Duplicate original record id")
    hours(rows)  # Validate times, seconds, offsets and non-overlap.
    # Snapshot files may grow or be copied: keep provenance in the manifest,
    # but don't let a different enclosing file hash turn the same event into new time.
    canonical = json.dumps({"book_id": book_id,
                            "records": sorted(records, key=lambda row: row["source_record_id"])},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "kindle-" + hashlib.sha256(canonical.encode()).hexdigest()


def prepare(sample, context=None):
    sample_id = sample_identity(sample)
    rows = sample["rows"]
    total = sum(row["duration"] for row in rows)
    if total > 60:
        raise ValueError("This initial trial tool only prepares at most 60 sourced seconds")
    packet = build_batch(context, sample["book_id"], rows) if context else None
    source_segment_id = hashlib.sha256(json.dumps([
        {key: row[key] for key in ("source_record_id", "start_time", "duration", "time_zone")}
        for row in sorted(rows, key=lambda item: item["source_record_id"])],
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "sample_id": sample_id, "state": "prepared_unsent", "network_supported": True,
        "source_segment_id": source_segment_id,
        "source_sha256": sample["source_sha256"], "book_id": sample["book_id"],
        "reading_seconds": total, "hours": hours(rows),
        "book_version_sources": sorted({row.get("book_version_source", "unavailable") for row in rows}),
        "book_versions": sorted({row.get("book_version", 0) for row in rows}),
        "signing_context_available": context is not None,
        "payload": packet,
        "limits": ["Dry-run is not approval for upload", "Commit requires the exact approved sample id",
                   "Unknown POST results must never be automatically replayed",
                   "A stable id cannot by itself prove the server deduplicates requests"],
    }


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".payload-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Never overwrite an existing preparation.
    finally:
        Path(temporary).unlink(missing_ok=True)


def verify_evidence(sample):
    """This first-trial route accepts a single conservatively floored native interval."""
    if len(sample["rows"]) != 1:
        raise ValueError("First trial needs one original event")
    source = Path(sample["source_path"])
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sample["source_sha256"]:
        raise ValueError("Original source changed")
    line = raw.decode("utf-8", errors="replace").splitlines()[sample["source_line"] - 1]
    row = sample["rows"][0]
    if hashlib.sha256(line.encode()).hexdigest() != row["source_record_id"]:
        raise ValueError("Original record mismatch")
    if "Verdict:Processed" not in line:
        raise ValueError("Rejected/non-counted event")
    match = re.search(r"IntervalTime:(\d+),", line)
    if not match or row["duration"] * 1000 > int(match.group(1)):
        raise ValueError("Sample exceeds counted original interval")
    stamp = dt.datetime.strptime(line[:13], "%y%m%d:%H%M%S").replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    end = int(stamp.timestamp())
    if row["time_zone"] != "+08:00" or row["start_time"] + row["duration"] != end:
        raise ValueError("First trial must end at the source's Chinese wall-clock second")
    if row["start_time"] < end - int(match.group(1)) // 1000:
        raise ValueError("Sample is outside conservatively floored source interval")


def safe_diagnostics(value=None, status_code=None):
    value = value if isinstance(value, dict) else {}
    result = {"status_code": status_code, "response_keys": sorted(
        key for key in value if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", key)
        and key != "_safe_transport_status")[:50]}
    for key in ("succ", "errCode", "errcode"):
        field = value.get(key)
        if isinstance(field, (bool, int)) or (isinstance(field, str) and re.fullmatch(r"-?[0-9]{1,12}", field)):
            result[key] = field
    return result


class UploadError(BridgeError):
    def __init__(self, diagnostic):
        super().__init__("网络/响应未明确完成；没有自动重试。")
        self.diagnostic = diagnostic


class UploadTransport:
    """Separate closed allowlist. No redirection, retries, refresh or Web fallback."""
    def __init__(self, credentials):
        self.credentials = credentials
        # Reuse only its CA-verified opener (including macOS system CA fallback),
        # never its route table or request method.
        self.opener = ReadonlyTransport(timeout=20).opener

    def call(self, method, path, *, query=None, body=None):
        allowed = {("GET", "/config"), ("GET", "/feature"), ("GET", "/book/getProgress"),
                   ("POST", "/book/batchUploadProgress")}
        if (method, path) not in allowed:
            raise BridgeError("请求不在独立时长传输层允许列表。")
        url = "https://i.weread.qq.com" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = dict(VERSION_HEADERS, vid=self.credentials["vid"], accessToken=self.credentials["accessToken"])
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json; charset=UTF-8"
            data = json.dumps(body, separators=(",", ":")).encode()
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        status_code = None
        try:
            with self.opener.open(request, timeout=20) as response:
                status_code = response.status
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("large response")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("response shape")
        except urllib.error.HTTPError as exc:
            diagnostic = safe_diagnostics(status_code=exc.code)
            try:
                raw_error = exc.read(65537)
                if len(raw_error) <= 65536:
                    diagnostic = safe_diagnostics(json.loads(raw_error), exc.code)
            except Exception:
                pass
            raise UploadError(diagnostic) from None
        except Exception as exc:
            diagnostic = safe_diagnostics(status_code=status_code)
            diagnostic["error_type"] = type(exc).__name__
            raise UploadError(diagnostic) from None
        value["_safe_transport_status"] = status_code
        return value


def position(value):
    for _ in range(4):
        if not isinstance(value, dict):
            break
        if "chapterUid" in value and "chapterOffset" in value:
            return {"readingTime": int(value["readingTime"]), "progress": int(value["progress"]), "chapterUid": int(value["chapterUid"]),
                    "chapterOffset": int(value["chapterOffset"])}
        value = value.get("book", value.get("data", value.get("result")))
    raise ValueError("Missing baseline progress")


def commit_once(sample, approved_id, credentials, ledger_path, transport=None):
    """Historical protocol test helper; the supported writer is SyncEngine.

    Not exposed by the CLI: it lacks the unified account/day verification and
    handoff locking of the web workflow. Retained for synthetic regression only.
    """
    prepared = prepare(sample)
    sample_id = prepared["sample_id"]
    if approved_id != sample_id:
        raise ValueError("Approved id does not exactly match this sample")
    verify_evidence(sample)
    transport = transport or UploadTransport(credentials)
    before = position(transport.call("GET", "/book/getProgress", query={"bookId": sample["book_id"]}))
    if before != sample["expected_baseline"]:
        raise ValueError("Live baseline changed; obtain a new review before upload")
    config = transport.call("GET", "/config", query={"token": 1})
    feature = transport.call("GET", "/feature", query={"synckey": 0})
    feature_map = feature.get("feature", {})
    if not isinstance(feature_map, dict):
        raise ValueError("Invalid feature response")
    guest = feature_map.get("guest_token") or DEFAULT_GUEST_TOKEN
    context = {"vid": credentials["vid"], "deviceId": credentials["deviceId"],
               "config_token": config.get("token"), "guest_token": guest,
               "record_create_time_zone": "+08:00"}
    packet = build_batch(context, sample["book_id"], sample["rows"])
    payload_hash = hashlib.sha256(json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    account_hash = hashlib.sha256(credentials["vid"].encode()).hexdigest()
    row = sample["rows"][0]
    ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ledger_path.is_symlink():
        raise ValueError("Ledger symlink refused")
    # Create privately before SQLite opens it. Existing ledgers are never reset.
    fd = os.open(ledger_path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(ledger_path, 0o600)
    connection = sqlite3.connect(ledger_path, timeout=5)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS attempts (sample_id TEXT PRIMARY KEY, account_hash TEXT, record_id TEXT, start_time INTEGER, end_time INTEGER, state TEXT, payload_sha256 TEXT, seconds INTEGER, selected_book_id TEXT, source_segment_id TEXT, diagnostic_json TEXT)")
        if "diagnostic_json" not in {info[1] for info in connection.execute("PRAGMA table_info(attempts)")}:
            connection.execute("ALTER TABLE attempts ADD COLUMN diagnostic_json TEXT")
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT 1 FROM attempts WHERE sample_id=? OR (account_hash=? AND record_id=? AND start_time<? AND end_time>?)",
            (sample_id, account_hash, row["source_record_id"], row["start_time"] + row["duration"], row["start_time"])).fetchone()
        if existing:
            connection.rollback()
            raise ValueError("This sample or overlapping source interval has already been dispatched; no replay")
        connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)", (sample_id, account_hash, row["source_record_id"],
            row["start_time"], row["start_time"] + row["duration"], "dispatching", payload_hash, row["duration"],
            sample["book_id"], prepared["source_segment_id"], None))
        connection.commit()  # Durable ownership BEFORE the only POST; crash leaves replay blocked.
        state = "unknown"
        diagnostic = safe_diagnostics()
        try:
            result = transport.call("POST", "/book/batchUploadProgress", body=packet)
            diagnostic = safe_diagnostics(result, result.get("_safe_transport_status"))
            if result.get("succ") is True or (type(result.get("succ")) is int and result["succ"] == 1):
                state = "confirmed"
        except UploadError as exc:
            diagnostic = exc.diagnostic
        except Exception as exc:
            diagnostic["error_type"] = type(exc).__name__
        # A failed connection can follow a committed write. Never resend.
        connection.execute("UPDATE attempts SET state=?, diagnostic_json=? WHERE sample_id=?", (state, json.dumps(diagnostic), sample_id))
        connection.commit()
        after, verification = None, "not_checked"
        try:
            after = position(transport.call("GET", "/book/getProgress", query={"bookId": sample["book_id"]}))
            unchanged = (after["progress"], after["chapterUid"], after["chapterOffset"]) == (before["progress"], before["chapterUid"], before["chapterOffset"])
            delta = after["readingTime"] - before["readingTime"]
            verification = "matched" if state == "confirmed" and unchanged and delta == row["duration"] else "needs_review"
        except Exception:
            verification = "readback_failed"
        if state == "confirmed" and verification != "matched":
            state = "server_ack_only"
            connection.execute("UPDATE attempts SET state=? WHERE sample_id=?", (state, sample_id))
            connection.commit()
        return {"sample_id": sample_id, "state": state, "seconds": row["duration"], "before": before,
                "after": after, "verification": verification, "replay_allowed": False, "safe_response": diagnostic}
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线协议样本预备工具。实际写入请使用网页版同步预览和确认。")
    parser.add_argument("--sample", type=Path, help="来自真实原生counted事件的小样本JSON，最多60秒")
    parser.add_argument("--context", type=Path, help="可选私有签名配置JSON；不接受accessToken或refreshToken")
    parser.add_argument("--output", type=Path, help="可选0600保存完整预备包；已有文件不覆盖")
    parser.add_argument("--commit", action="store_true", help="已停用的旧式写入选项；使用网页版同步预览和确认")
    parser.add_argument("--approve-sample-id", help="用户已批准的精确sample_id")
    parser.add_argument("--credentials", type=Path, default=Path(__file__).parent / ".weread" / "auth.json")
    parser.add_argument("--ledger", type=Path, default=Path(__file__).parent / ".weread" / "upload-ledger.sqlite")
    args = parser.parse_args(argv)
    if args.commit:
        print("旧式单片段写入入口已停用。请启动网页版，通过同步预览与确认提交，保留统一账本及设备交接保护。")
        return 1
    if not args.sample:
        parser.print_help()
        return 0
    try:
        sample = json.loads(args.sample.read_text(encoding="utf-8"))
        verify_evidence(sample)
        context = json.loads(args.context.read_text(encoding="utf-8")) if args.context else None
        if context and any(key in context for key in ("accessToken", "refreshToken", "cookies")):
            raise ValueError("Signing context must not contain login tokens or cookies")
        result = prepare(sample, context)
        if args.output:
            private_json(args.output, result)
        preview = {key: result[key] for key in ("sample_id", "source_segment_id", "book_id", "state", "network_supported", "reading_seconds",
                   "hours", "book_versions", "book_version_sources", "signing_context_available", "limits")}
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, BridgeError, sqlite3.Error):
        # Do not echo private context, account identifiers or token-bearing values.
        print("操作停止：请核对来源/精确批准ID/基线/队列状态；没有自动重试，勿重放未知提交。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
