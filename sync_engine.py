"""Local cumulative-hour sync ledger. Import/preview never perform API writes."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time

from weread_readonly import load_auth, Transport, BridgeError
from weread_upload import UploadTransport, sign, safe_diagnostics, DEFAULT_GUEST_TOKEN

TZ = timezone(timedelta(hours=8))
UNRESOLVED = ("dispatching", "unknown", "needs_review", "failed")


def dumps(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def digest(value): return hashlib.sha256(dumps(value).encode()).hexdigest()
def now(): return datetime.now(TZ).isoformat()


def reading_position(value, book_id):
    """Recognize the API's explicit empty record without defaulting malformed data to zero."""
    if not isinstance(value, dict) or str(value.get("bookId", book_id)) != book_id:
        raise ValueError("目标书籍的阅读记录不匹配。")
    book = value.get("book")
    if not isinstance(book, dict): raise ValueError("书籍阅读记录缺失。")
    if set(book) == {"recordReadingTime"} and type(book["recordReadingTime"]) is int and book["recordReadingTime"] == 0:
        return dict(readingTime=0, progress=None, chapterUid=None, chapterOffset=None)
    seconds = book.get("readingTime")
    if type(seconds) is not int or seconds < 0: raise ValueError("书籍累计时长缺失或格式不明确。")
    result = {"readingTime": seconds}
    for key in ("progress", "chapterUid", "chapterOffset"):
        field = book.get(key)
        if field is not None and (type(field) is not int or field < 0):
            raise ValueError("书籍阅读位置格式不明确。")
        result[key] = field
    if any(result[k] is None for k in ("progress", "chapterUid", "chapterOffset")) and any(
            result[k] is not None for k in ("progress", "chapterUid", "chapterOffset")):
        raise ValueError("书籍阅读位置返回不完整。")
    return result


def position_unchanged(before, after):
    # A first time-only upload may initialize absent position fields to zero.
    return all(after[k] in (None, 0) if before[k] is None else after[k] == before[k]
               for k in ("progress", "chapterUid", "chapterOffset"))


def verified_increment(before, after, day_before, day_after, seconds):
    return (position_unchanged(before, after) and after["readingTime"] - before["readingTime"] == seconds
            and day_after["reading_seconds"] - day_before["reading_seconds"] == seconds)


def hour_packet(credentials, book_id, buckets, config, feature):
    """Direct hour DTO: cumulative counted seconds are NOT invented time spans."""
    seconds = sum(row["readingTime"] for row in buckets)
    if not buckets or not 0 < seconds <= 600:
        raise ValueError("单批时长须为1至600秒（本地工程限制）。")
    token = config.get("token")
    guest = feature.get("feature", {}).get("guest_token") or DEFAULT_GUEST_TOKEN
    if not isinstance(token, str) or not token or not isinstance(guest, str):
        raise ValueError("签名配置不完整，已停止。")
    stamp_ms = int(time.time() * 1000)
    stamp, random, outer_random = stamp_ms // 1000, secrets.randbelow(1000), secrets.randbelow(1000)
    hours = [{key: row[key] for key in ("startOfHour", "timeZone", "readingTime", "ttsTime")} for row in buckets]
    body = dict(bookId=book_id, bookVersion=0, appId="", deviceId=credentials["deviceId"],
                installId="", summary="", progress=0, currentProgress=0, autoTime=0, isLecture=0,
                isResendReadingInfo=1, lectureTime=0, lectureTextTime=0, novalTime=0, ttsTime=0,
                finish=False, risk=0, readingTime=seconds, recordCreateTimeZone="+08:00",
                timestamp=stamp, random=random, hours=hours)
    parts = [credentials["vid"], body["deviceId"], "", book_id, "0", "+08:00", str(seconds), "0"]
    parts += ["_".join(str(h[key]) for key in ("startOfHour", "timeZone", "readingTime", "ttsTime")) for h in hours]
    body["signature"] = sign([guest, random, stamp, "_".join(parts)])
    return {"books": [body], "timestamp": stamp_ms, "random": outer_random,
            "signature": hashlib.sha256(f"{stamp_ms}{token}{outer_random}".encode()).hexdigest()}


class SyncEngine:
    def __init__(self, project_dir, *, credentials=None, transport_factory=None, readonly_factory=None):
        self.root = Path(project_dir).resolve()
        self.private = self.root / ".weread"
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.private / "sync-engine.sqlite"
        if self.db_path.is_symlink(): raise ValueError("账本路径不能是符号链接。")
        self.db_path.touch(mode=0o600, exist_ok=True)
        self.db_path.chmod(0o600)
        self._injected_credentials = credentials
        self.transport_factory = transport_factory or UploadTransport
        self.readonly_factory = readonly_factory or Transport
        with self._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS books (source_key TEXT PRIMARY KEY,title TEXT,authors TEXT,blocked TEXT DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS buckets (source_key TEXT,day TEXT,hour INTEGER,observed INTEGER,clock INTEGER,provenance TEXT,evidence TEXT,PRIMARY KEY(source_key,day,hour));
            CREATE TABLE IF NOT EXISTS mappings (account TEXT,source_key TEXT,weread_id TEXT,title TEXT,PRIMARY KEY(account,source_key));
            CREATE TABLE IF NOT EXISTS credits (account TEXT,source_key TEXT,day TEXT,hour INTEGER,seconds INTEGER,PRIMARY KEY(account,source_key,day,hour));
            CREATE TABLE IF NOT EXISTS identities (fingerprint TEXT PRIMARY KEY,source_key TEXT,assignment TEXT);
            CREATE TABLE IF NOT EXISTS native_ranges (fingerprint TEXT PRIMARY KEY,source_key TEXT,extent INTEGER,start_ms INTEGER,end_ms INTEGER);
            CREATE TABLE IF NOT EXISTS plans (plan_id TEXT PRIMARY KEY,account TEXT,created TEXT,state TEXT,content TEXT);
            CREATE TABLE IF NOT EXISTS attempts (batch_id TEXT PRIMARY KEY,plan_id TEXT,account TEXT,source_key TEXT,weread_id TEXT,state TEXT,seconds INTEGER,hours TEXT,result TEXT);
            CREATE TABLE IF NOT EXISTS seeds (seed_id TEXT PRIMARY KEY,account TEXT,seconds INTEGER);
            CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY,value TEXT);
            ''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def _auth(self):
        try:
            value = self._injected_credentials if self._injected_credentials is not None else load_auth(self.private / "auth.json")
            return hashlib.sha256(value["vid"].encode()).hexdigest(), value
        except (BridgeError, KeyError, TypeError, OSError):
            return "", None

    @staticmethod
    def _block(db, key, reason):
        row = db.execute("SELECT blocked FROM books WHERE source_key=?", (key,)).fetchone()
        reasons = json.loads(row[0]) if row else []
        if reason not in reasons: reasons.append(reason)
        db.execute("UPDATE books SET blocked=? WHERE source_key=?", (dumps(reasons), key))

    def _clock(self, provenance, day):
        import re
        paths = provenance.get("logs", [])
        if not isinstance(paths, list) or not paths: return False, [], []
        cache = getattr(self, "_clock_cache", {})
        cache_key = (day, tuple(str(path) for path in paths), str(provenance.get("snapshot", "")))
        if cache_key in cache: return cache[cache_key]
        evidence, offsets, fingerprints, sources = set(), [], [], {}
        for path in paths:
            path = Path(path)
            if not path.is_absolute(): path = Path(provenance.get("snapshot", self.root)) / path
            try: raw = path.read_bytes()
            except OSError: return False, [], []
            fingerprint = hashlib.sha256(raw).hexdigest()
            fingerprints.append(fingerprint)
            sources[str(path.absolute())] = fingerprint
            for line in raw.decode("utf-8", errors="replace").splitlines():
                if "JSONJournalSaver" not in line or "annotationType=LastPositionReadAnnotation" not in line: continue
                match = re.search(r"modificationDate=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)Z", line)
                if not match: continue
                try:
                    local = datetime.strptime(line[:13], "%y%m%d:%H%M%S")
                    if local.date().isoformat() != day: continue
                    utc = datetime.fromisoformat(match.group(1))
                    offset = int((local - utc).total_seconds())
                except ValueError: continue
                token = (line[:13], match.group(1))
                if token not in evidence:
                    evidence.add(token); offsets.append(offset)
        manifest = [{"path": path, "sha256": value} for path, value in sorted(sources.items())]
        result = len(evidence) >= 3 and all(offset == 28800 for offset in offsets), sorted(set(fingerprints)), manifest
        cache[cache_key] = result
        return result

    @staticmethod
    def _verify_sources(manifest):
        if not isinstance(manifest, list) or not manifest:
            raise ValueError("缺少冻结的原日志路径/指纹，请重新导入并预览。")
        for source in manifest:
            path = Path(source["path"])
            if not path.is_absolute(): raise ValueError("原日志路径不是绝对路径。")
            hasher = hashlib.sha256()
            try:
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
            except OSError: raise ValueError("原日志已消失，请重新导入并预览。") from None
            if hasher.hexdigest() != source["sha256"]:
                raise ValueError("原日志已变化，请重新导入并预览。")

    def import_results(self, results_dir=None):
        folder = Path(results_dir) if results_dir else self.root / "log-results"
        try:
            rows = json.loads((folder / "hour_totals.json").read_text())
            sessions = json.loads((folder / "sessions.json").read_text())
        except (OSError, ValueError): raise ValueError("缺少有效的小时/会话JSON，请先分析日志。") from None
        if not isinstance(rows, list) or not isinstance(sessions, list): raise ValueError("日志JSON必须为数组。")
        self._clock_cache = {}  # Never reuse clock evidence across imports/file changes.
        account, _ = self._auth()
        seen, identities = set(), {}
        with self._db() as db:
            for row in rows:
                key, day, hour, seconds = row["source_key"], row["local_date"], row["hour"], row["seconds"]
                if not isinstance(key, str) or not key or type(hour) is not int or not 0 <= hour <= 23 or type(seconds) is not int or not 0 <= seconds <= 3600:
                    raise ValueError("小时桶字段无效。")
                datetime.strptime(day, "%Y-%m-%d")
                if (key, day, hour) in seen: raise ValueError("重复小时桶，不能安全导入。")
                seen.add((key, day, hour))
                linked = [sessions[i] for i in row.get("session_indexes", []) if type(i) is int and 0 <= i < len(sessions)]
                author = linked[0].get("author", "") if linked else ""
                db.execute("INSERT INTO books VALUES (?,?,?,'[]') ON CONFLICT(source_key) DO UPDATE SET title=excluded.title,authors=excluded.authors", (key, row.get("title", key), author))
                eligible = row.get("measure") == "counted" and bool(linked) and len(linked) == len(row.get("session_indexes", [])) and all(
                    s.get("matched") is True and s.get("measure") == "counted" and s.get("hour_allocation_valid") is True and s.get("source_key") == key for s in linked)
                evidence = row.get("source_counter_evidence", [])
                eligible = eligible and bool(evidence) and all(all(name in e for name in ("book_extent", "start_counter_ms", "end_counter_ms", "start_words", "end_words")) for e in evidence)
                eligible = eligible and sum(sum(pair[1] for pair in s.get("hours", []) if pair[0] == hour) for s in linked) == seconds
                verified, fingerprints, sources = self._clock(row.get("provenance", {}), day)
                verified = verified and eligible
                provenance = {"logs_sha256": fingerprints, "sources": sources, "clock_verified": verified, "utc_offset_seconds": 28800 if verified else None,
                              "reason": None if verified else "需每日期至少3条去重后的同条JournalSaver UTC+08证据、精确匹配counted会话及原始小时分配。"}
                previous = db.execute("SELECT observed FROM buckets WHERE source_key=? AND day=? AND hour=?", (key, day, hour)).fetchone()
                if previous and seconds < previous[0]:
                    self._block(db, key, "观测小时累计量减少，保留旧量并阻止同步；需人工核对。")
                    continue
                db.execute("INSERT INTO buckets VALUES (?,?,?,?,?,?,?) ON CONFLICT(source_key,day,hour) DO UPDATE SET observed=excluded.observed,clock=excluded.clock,provenance=excluded.provenance,evidence=excluded.evidence", (key, day, hour, seconds, int(verified), dumps(provenance), dumps(evidence)))
                for e in evidence:
                    fingerprint = digest({n: e[n] for n in ("book_extent", "start_counter_ms", "end_counter_ms", "start_words", "end_words")})
                    aliases = db.execute("SELECT DISTINCT source_key FROM native_ranges WHERE extent=? AND source_key<>? AND start_ms<? AND end_ms>?",
                        (e["book_extent"], key, e["end_counter_ms"], e["start_counter_ms"])).fetchall()
                    for alias in aliases:
                        for affected in (key, alias[0]): self._block(db, affected, "不同本地书名出现重叠的同书原生计数范围，可能重命名，需核对后才能同步。")
                    db.execute("INSERT OR IGNORE INTO native_ranges VALUES (?,?,?,?,?)", (fingerprint, key, e["book_extent"], e["start_counter_ms"], e["end_counter_ms"]))
                    record = identities.setdefault(fingerprint, {"source_key": key, "hours": []})
                    if record["source_key"] != key:
                        self._block(db, key, "同一计数证据出现于不同本地书名，需确认来源身份。")
                        self._block(db, record["source_key"], "同一计数证据出现于不同本地书名，需确认来源身份。")
                    session_index = e.get("session_index")
                    session = sessions[session_index] if type(session_index) is int and 0 <= session_index < len(sessions) else {}
                    own_seconds = sum(pair[1] for pair in session.get("hours", []) if pair[0] == hour)
                    record["hours"].append([day, hour, own_seconds])
            for fingerprint, record in identities.items():
                assignment = dumps(sorted(record["hours"]))
                previous = db.execute("SELECT * FROM identities WHERE fingerprint=?", (fingerprint,)).fetchone()
                if previous and (previous["source_key"] != record["source_key"] or previous["assignment"] != assignment):
                    for key in (previous["source_key"], record["source_key"]): self._block(db, key, "同一计数证据的书籍或日期/小时归属变化，禁止自动修正或重传。")
                else: db.execute("INSERT OR IGNORE INTO identities VALUES (?,?,?)", (fingerprint, record["source_key"], assignment))
            for previous in db.execute("SELECT source_key,day,hour FROM buckets").fetchall():
                if (previous["source_key"], previous["day"], previous["hour"]) not in seen:
                    self._block(db, previous["source_key"], "完整观测导入中旧小时桶消失，保留旧量但阻止同步；需核对原始来源删除或归属变化。")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('last_import',?)", (now(),))
        return {"imported_buckets": len(seen), "status": self.status()}

    def list_books(self):
        account, credentials = self._auth()
        result = []
        with self._db() as db:
            for book in db.execute("SELECT * FROM books ORDER BY title"):
                key, reasons = book["source_key"], json.loads(book["blocked"])
                mapping = db.execute("SELECT * FROM mappings WHERE account=? AND source_key=?", (account, key)).fetchone()
                if not credentials: reasons.append("请先连接微信读书账号。")
                if not mapping: reasons.append("尚未确认微信读书书籍映射。")
                unknown = db.execute("SELECT 1 FROM attempts WHERE account=? AND source_key=? AND state IN (?,?,?,?) LIMIT 1", (account, key, *UNRESOLVED)).fetchone()
                if unknown: reasons.append("有失败、未知或待核对的已发送批次，禁止重放。")
                days = {}
                for bucket in db.execute("SELECT * FROM buckets WHERE source_key=? ORDER BY day,hour", (key,)):
                    credit = db.execute("SELECT seconds FROM credits WHERE account=? AND source_key=? AND day=? AND hour=?", (account, key, bucket["day"], bucket["hour"])).fetchone()
                    confirmed = credit[0] if credit else 0
                    day = days.setdefault(bucket["day"], {"date": bucket["day"], "observed_seconds": 0, "confirmed_seconds": 0, "pending_seconds": 0})
                    day["observed_seconds"] += bucket["observed"]; day["confirmed_seconds"] += confirmed
                    day["pending_seconds"] += max(0, bucket["observed"] - confirmed)
                    if not bucket["clock"]: reasons.append("日期时区或counted小时证据尚未验证。")
                    if confirmed > bucket["observed"]: reasons.append("确认量超过当前观测量，禁止同步。")
                result.append(dict(local_book_id=key, source_key=key, title=book["title"], authors=book["authors"],
                    weread_book_id=mapping["weread_id"] if mapping else None,
                    weread_title=mapping["title"] if mapping else None,
                    mapping_status="confirmed" if mapping else "unmapped", observed_seconds=sum(d["observed_seconds"] for d in days.values()),
                    confirmed_seconds=sum(d["confirmed_seconds"] for d in days.values()), pending_seconds=sum(d["pending_seconds"] for d in days.values()),
                    blocked=bool(reasons), blocked_reasons=sorted(set(reasons)), days=list(days.values())))
        return result

    def status(self):
        account, credentials = self._auth(); books = self.list_books()
        with self._db() as db:
            imported = db.execute("SELECT value FROM meta WHERE name='last_import'").fetchone()
            seeded = db.execute("SELECT COALESCE(SUM(seconds),0) FROM seeds WHERE account=?", (account,)).fetchone()[0]
            unknown = db.execute("SELECT COUNT(*) FROM attempts WHERE account=? AND state IN (?,?,?,?)", (account, *UNRESOLVED)).fetchone()[0]
            plans = [{"plan_id": p["plan_id"], "state": p["state"], "total_seconds": json.loads(p["content"])["total_seconds"]} for p in db.execute("SELECT * FROM plans WHERE account=? ORDER BY created DESC LIMIT 20", (account,))]
        return dict(authenticated=credentials is not None, account_scope=account or None,
            observed_seconds=sum(b["observed_seconds"] for b in books), confirmed_seconds=sum(b["confirmed_seconds"] for b in books),
            pending_seconds=sum(b["pending_seconds"] for b in books), blocked_books=sum(b["blocked"] for b in books),
            unknown_batches=unknown, seeded_seconds=seeded, books_count=len(books), last_import=imported[0] if imported else None,
            plans=plans, limitations=["仅保留日志覆盖的观测日；缺失日未知。", "仅counted模型秒，不代表精确注意力时间。", "跨日服务端记账仍须回读核对。"])

    def save_mapping(self, local_book_id, weread_book_id, title=None):
        account, credentials = self._auth()
        if not credentials: raise ValueError("请先连接账号，再保存该账号的本地映射。")
        if not isinstance(weread_book_id, str) or not weread_book_id.strip() or any(x in weread_book_id for x in "/?\r\n"):
            raise ValueError("微信读书bookId无效。")
        with self._db() as db:
            if not db.execute("SELECT 1 FROM books WHERE source_key=?", (local_book_id,)).fetchone(): raise ValueError("未知本地书籍。")
            old = db.execute("SELECT weread_id FROM mappings WHERE account=? AND source_key=?", (account, local_book_id)).fetchone()
            credited = db.execute("SELECT 1 FROM credits WHERE account=? AND source_key=? AND seconds>0", (account, local_book_id)).fetchone()
            sent = db.execute("SELECT 1 FROM attempts WHERE account=? AND source_key=?", (account, local_book_id)).fetchone()
            if old and old[0] != weread_book_id and (credited or sent): raise ValueError("该来源已有计量/发送记录，不能换bookId后重复同步。")
            db.execute("INSERT OR REPLACE INTO mappings VALUES (?,?,?,?)", (account, local_book_id, weread_book_id, title or ""))
        return next(book for book in self.list_books() if book["local_book_id"] == local_book_id)

    def search_candidates(self, query):
        _, credentials = self._auth()
        if not credentials: raise ValueError("请先连接微信读书。")
        query = str(query).strip()
        if not query: raise ValueError("请输入书名或作者。")
        client = self.readonly_factory()
        value = client.request("GET", "https://i.weread.qq.com/store/search", query={"keyword": str(query), "scope": 10, "maxIdx": 0, "count": 20}, credentials=credentials)
        shelf_ids, shelf = None, {}
        try:
            shelf = client.request("GET", "https://i.weread.qq.com/shelf/sync", credentials=credentials)
            shelf_ids = {str(b.get("bookId")) for b in shelf.get("books", [])}
        except Exception: pass
        found = {}
        def walk(item, from_shelf=False):
            if isinstance(item, list):
                for child in item: walk(child, from_shelf)
            elif isinstance(item, dict):
                if item.get("bookId") and item.get("title"):
                    bid = str(item["bookId"])
                    matches = query.casefold() in (str(item.get("title", "")) + " " + str(item.get("author", ""))).casefold()
                    if not from_shelf or matches:
                        found[bid] = dict(weread_book_id=bid, bookId=bid, title=item.get("title"), author=item.get("author", ""),
                                          publisher=item.get("publisher", ""), isbn=item.get("isbn", ""), in_shelf=True if from_shelf else (bid in shelf_ids if shelf_ids is not None else None))
                for child in item.values():
                    if isinstance(child, (dict, list)): walk(child, from_shelf)
        walk(value)
        walk(shelf.get("books", []), True)
        return list(found.values())

    def _day_snapshot(self, credentials, day, epoch):
        target = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
        expected_base = int((target - timedelta(days=target.weekday())).timestamp())
        value = self.readonly_factory().request("GET", "https://i.weread.qq.com/readdata/detail",
            query={"mode": "weekly", "baseTime": epoch}, credentials=credentials)
        try: actual_base = int(value["baseTime"])
        except (KeyError, TypeError, ValueError): raise ValueError("统计周期无法核验，未写入。") from None
        if actual_base != expected_base or not isinstance(value.get("readTimes"), dict):
            raise ValueError("服务端未返回目标日期所在周，禁止将历史记录记到其他日期。")
        seconds = 0
        for key, count in value["readTimes"].items():
            try:
                stamp = int(key)
                if not 0 < stamp < 4102444800 or type(count) is not int or count < 0: raise ValueError()
                key_date = datetime.fromtimestamp(stamp, TZ).date().isoformat()
            except (ValueError, TypeError, OSError): raise ValueError("日级统计字段单位不明确，已停止。") from None
            if not expected_base <= stamp < expected_base + 7 * 86400:
                raise ValueError("统计明细超出目标周，已停止。")
            if key_date == day: seconds += count
        return {"date": day, "baseTime": actual_base, "reading_seconds": seconds}

    def prepare(self, local_book_ids=None, max_seconds=600):
        if type(max_seconds) is not int or not 1 <= max_seconds <= 600: raise ValueError("单批工程上限须为1至600秒。")
        account, _ = self._auth(); books = self.list_books(); batches, blocked = [], []
        selected = set(local_book_ids) if local_book_ids is not None else None
        with self._db() as db:
            for book in books:
                key = book["local_book_id"]
                if selected is not None and key not in selected: continue
                if book["pending_seconds"] <= 0: continue
                if book["blocked"]:
                    blocked.append({"local_book_id": key, "reasons": book["blocked_reasons"]}); continue
                bucket_rows = db.execute("SELECT * FROM buckets WHERE source_key=? ORDER BY day,hour", (key,)).fetchall()
                try:
                    for row in bucket_rows: self._verify_sources(json.loads(row["provenance"]).get("sources"))
                except (ValueError, KeyError, TypeError):
                    blocked.append({"local_book_id": key, "reasons": ["原日志缺失、变化或未冻结指纹，请重新导入并预览。"]}); continue
                for row in bucket_rows:
                    credit = db.execute("SELECT seconds FROM credits WHERE account=? AND source_key=? AND day=? AND hour=?", (account, key, row["day"], row["hour"])).fetchone()
                    credited = credit[0] if credit else 0
                    left = row["observed"] - credited
                    while left > 0:
                        count = min(left, max_seconds)
                        epoch = int(datetime.strptime(row["day"], "%Y-%m-%d").replace(hour=row["hour"], tzinfo=TZ).timestamp())
                        batches.append(dict(batch_id=secrets.token_hex(16), local_book_id=key, title=book["title"], weread_book_id=book["weread_book_id"], weread_title=book["weread_title"],
                            seconds=count, hours=[dict(date=row["day"], hour=row["hour"], startOfHour=epoch, timeZone="+08:00", readingTime=count, ttsTime=0)],
                            observed=row["observed"], credit_before=credited, book_version=0, book_version_source="unavailable",
                            source_manifest=json.loads(row["provenance"])["sources"]))
                        credited += count; left -= count
                        if len(batches) > 100: raise ValueError("单次计划超过100批，请缩小选择范围。")
            created = now()
            plan = dict(plan_id=secrets.token_hex(24), state="prepared" if batches else ("blocked" if blocked else "empty"),
                        total_seconds=sum(b["seconds"] for b in batches), batches=batches, blocked=blocked, created_at=created)
            db.execute("INSERT INTO plans VALUES (?,?,?,?,?)", (plan["plan_id"], account, created, plan["state"], dumps(plan)))
        return plan

    def _valid_batch(self, db, batch, account):
        # Handoff acquires this same SQLite write lock before creating the fence.
        # Any fence (including a partial or malformed one) disables Mac writes.
        if (self.private / "device-handoff.json").exists():
            raise ValueError("上传权已交接给 Kindle；此 Mac 仅保留导入和只读核验，禁止再次上传。")
        key, hour = batch["local_book_id"], batch["hours"][0]
        book = db.execute("SELECT blocked FROM books WHERE source_key=?", (key,)).fetchone()
        mapping = db.execute("SELECT weread_id FROM mappings WHERE account=? AND source_key=?", (account, key)).fetchone()
        observed = db.execute("SELECT * FROM buckets WHERE source_key=? AND day=? AND hour=?", (key, hour["date"], hour["hour"])).fetchone()
        credit = db.execute("SELECT seconds FROM credits WHERE account=? AND source_key=? AND day=? AND hour=?", (account, key, hour["date"], hour["hour"])).fetchone()
        uncertain = db.execute("SELECT 1 FROM attempts WHERE account=? AND source_key=? AND state IN (?,?,?,?)", (account, key, *UNRESOLVED)).fetchone()
        if (not book or json.loads(book[0]) or not mapping or mapping[0] != batch["weread_book_id"] or not observed or not observed["clock"]
                or observed["observed"] < batch["observed"] or (credit[0] if credit else 0) != batch["credit_before"] or uncertain):
            raise ValueError("计划来源、映射、余额或未确认状态变化，请重新预览并核对。")
        # Frozen in the immutable plan, never replaced by a drifting DB reference.
        # Called both before network preflight and immediately before durable dispatch.
        self._verify_sources(batch.get("source_manifest"))

    def commit(self, plan_id, confirmed=False):
        if confirmed is not True: raise ValueError("必须由用户确认具体不可变计划后才能提交。")
        account, credentials = self._auth()
        if not credentials: raise ValueError("账号未连接。")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM plans WHERE plan_id=? AND account=?", (plan_id, account)).fetchone()
            if not row or row["state"] != "prepared": raise ValueError("计划不存在、已处理或不可再次提交。")
            plan = json.loads(row["content"])
            db.execute("UPDATE plans SET state='committing' WHERE plan_id=?", (plan_id,))
        results, credited_total = [], 0
        for batch in plan["batches"]:
            preflight_step = "source_validation"
            try:
                if self._auth()[0] != account: raise ValueError("当前账号已变化。")
                with self._db() as db: self._valid_batch(db, batch, account)
                transport = self.transport_factory(credentials)
                preflight_step = "book_progress"
                before = reading_position(transport.call("GET", "/book/getProgress", query={"bookId": batch["weread_book_id"]}), batch["weread_book_id"])
                preflight_step = "signing"
                config = transport.call("GET", "/config", query={"token": 1})
                feature = transport.call("GET", "/feature", query={"synckey": 0})
                packet = hour_packet(credentials, batch["weread_book_id"], batch["hours"], config, feature)
                preflight_step = "day_statistics"
                day_before = self._day_snapshot(credentials, batch["hours"][0]["date"], batch["hours"][0]["startOfHour"])
                preflight_step = "source_validation"
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE"); self._valid_batch(db, batch, account)
                    db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,'dispatching',?,?,NULL)", (batch["batch_id"], plan_id, account, batch["local_book_id"], batch["weread_book_id"], batch["seconds"], dumps(batch["hours"])))
            except Exception as exc:
                results.append({"batch_id": batch["batch_id"], "state": "blocked", "seconds": batch["seconds"],
                                "verification": "preflight_failed_no_post", "preflight_step": preflight_step,
                                "error_type": type(exc).__name__}); break
            state, response = "unknown", safe_diagnostics()
            try:
                reply = transport.call("POST", "/book/batchUploadProgress", body=packet)
                response = safe_diagnostics(reply, reply.get("_safe_transport_status"))
                if reply.get("succ") is True or (type(reply.get("succ")) is int and reply["succ"] == 1): state = "ack"
                elif any(k in response for k in ("errCode", "errcode")): state = "failed"
            except Exception as exc:
                response = getattr(exc, "diagnostic", {"error_type": type(exc).__name__})
            after, day_after, verification = None, None, "not_confirmed"
            try:
                acknowledged = state == "ack"
                for readback in range(8):
                    after = reading_position(transport.call("GET", "/book/getProgress", query={"bookId": batch["weread_book_id"]}), batch["weread_book_id"])
                    day_after = self._day_snapshot(credentials, batch["hours"][0]["date"], batch["hours"][0]["startOfHour"])
                    if not acknowledged: break
                    state = "confirmed" if verified_increment(before, after, day_before, day_after, batch["seconds"]) else "needs_review"
                    verification = "matched" if state == "confirmed" else "server_ack_only"
                    if state == "confirmed": break
                    # Only repeat GETs when either statistics view may still be stale.
                    # A changed chapter, unexpected counter or wrong-day increment needs review.
                    if (not position_unchanged(before, after)
                            or after["readingTime"] - before["readingTime"] not in (0, batch["seconds"])
                            or day_after["reading_seconds"] - day_before["reading_seconds"] not in (0, batch["seconds"])):
                        break
                    if readback < 7: time.sleep(2)
            except Exception:
                if state == "ack": state = "needs_review"
                verification = "readback_failed"
            result = dict(batch_id=batch["batch_id"], state=state, seconds=batch["seconds"], safe_response=response, before=before, after=after,
                          day_before=day_before, day_after=day_after, verification=verification)
            with self._db() as db:
                db.execute("UPDATE attempts SET state=?,result=? WHERE batch_id=?", (state, dumps(result), batch["batch_id"]))
                if state == "confirmed":
                    h = batch["hours"][0]
                    db.execute("INSERT INTO credits VALUES (?,?,?,?,?) ON CONFLICT(account,source_key,day,hour) DO UPDATE SET seconds=seconds+excluded.seconds", (account, batch["local_book_id"], h["date"], h["hour"], batch["seconds"]))
            results.append(result)
            if state != "confirmed": break
            credited_total += batch["seconds"]
        state = "confirmed" if len(results) == len(plan["batches"]) and all(r["state"] == "confirmed" for r in results) else ("partial" if credited_total else results[-1]["state"])
        with self._db() as db: db.execute("UPDATE plans SET state=? WHERE plan_id=?", (state, plan_id))
        return dict(plan_id=plan_id, state=state, confirmed_seconds=credited_total, results=results, replay_allowed=False)

    def reconcile(self, batch_id):
        """Confirm a delayed acknowledged batch using GETs only; never replay a POST."""
        account, credentials = self._auth()
        if not credentials: raise ValueError("账号未连接。")
        with self._db() as db:
            row = db.execute("SELECT * FROM attempts WHERE batch_id=? AND account=?", (batch_id, account)).fetchone()
            if not row: raise ValueError("找不到该账号的提交记录。")
            result = json.loads(row["result"] or "null")
            if row["state"] == "confirmed": return result
            if row["state"] != "needs_review" or not result or result.get("safe_response", {}).get("succ") != 1:
                raise ValueError("只能只读复核已收到明确成功回执的批次。")
            plan = json.loads(db.execute("SELECT content FROM plans WHERE plan_id=?", (row["plan_id"],)).fetchone()[0])
            batch = next(b for b in plan["batches"] if b["batch_id"] == batch_id)
        t = self.transport_factory(credentials)
        after = reading_position(t.call("GET", "/book/getProgress", query={"bookId": batch["weread_book_id"]}), batch["weread_book_id"])
        h = batch["hours"][0]
        day_after = self._day_snapshot(credentials, h["date"], h["startOfHour"])
        if not verified_increment(result["before"], after, result["day_before"], day_after, batch["seconds"]):
            return {**result, "recheck_after": after, "recheck_day_after": day_after}
        if self._auth()[0] != account: raise ValueError("当前账号已变化。")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT state,result FROM attempts WHERE batch_id=?", (batch_id,)).fetchone()
            if current["state"] == "confirmed": return json.loads(current["result"])
            if current["state"] != "needs_review": raise ValueError("批次状态已变化。")
            credit = db.execute("SELECT seconds FROM credits WHERE account=? AND source_key=? AND day=? AND hour=?",
                                (account, batch["local_book_id"], h["date"], h["hour"])).fetchone()
            if (credit[0] if credit else 0) != batch["credit_before"]: raise ValueError("已确认余额变化，停止复核入账。")
            result.update(state="confirmed", after=after, day_after=day_after, verification="matched_on_readonly_recheck")
            db.execute("UPDATE attempts SET state='confirmed',result=? WHERE batch_id=?", (dumps(result), batch_id))
            db.execute("INSERT INTO credits VALUES (?,?,?,?,?) ON CONFLICT(account,source_key,day,hour) DO UPDATE SET seconds=seconds+excluded.seconds",
                       (account, batch["local_book_id"], h["date"], h["hour"], batch["seconds"]))
            count = db.execute("SELECT COUNT(*) FROM attempts WHERE plan_id=? AND state='confirmed'", (row["plan_id"],)).fetchone()[0]
            db.execute("UPDATE plans SET state=? WHERE plan_id=?", ("confirmed" if count == len(plan["batches"]) else "partial", row["plan_id"]))
        return result
