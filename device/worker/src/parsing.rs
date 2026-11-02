//! ReadingLog core only; never invokes UI/update/mark APIs.
use crate::*;
use readinglog_native::{log, record::Record, sidecar};
use std::collections::{BTreeMap, BTreeSet};
use std::os::unix::fs::MetadataExt;
fn buffered_counters(line: &str) -> Vec<(i64, i64, i64)> {
    let Some((_, payload)) = line.split_once(": E BufferedLogger:Error::") else {
        return Vec::new();
    };
    if log::line::stamp(line).is_none() {
        return Vec::new();
    }
    // One flushed line may concatenate several pages. Never combine a first
    // page's counter with the next page's end position or chapter end.
    payload
        .split("PageStartPos:")
        .skip(1)
        .filter_map(|page| {
            let end = log::line::end_position(page)?;
            let ms = log::line::field(page, "TotalTime")?;
            let words = log::line::field(page, "TotalWords")?;
            (end > 0 && ms > 0 && words > 0).then_some((end, ms, words))
        })
        .collect()
}

fn recover_buffered(
    record: &mut Record,
    cards: &[sidecar::Counter],
    identities: &[(i64, i64, i64)],
) -> usize {
    // Preserve all books already identified by ordinary timer events.
    let unknown: BTreeSet<i64> = record
        .sittings
        .iter()
        .filter_map(|s| {
            let ep = record.extent_of(s.end_position);
            record
                .book_for(ep, s.asin.as_deref())
                .is_none()
                .then_some(s.end_position)
        })
        .collect();
    let mut by_end: BTreeMap<i64, BTreeSet<(i64, i64)>> = BTreeMap::new();
    for &(end, ms, words) in identities {
        if unknown.contains(&end) {
            by_end.entry(end).or_default().insert((ms, words));
        }
    }
    for (end, values) in by_end {
        let largest = values.iter().map(|(ms, _)| *ms).max().unwrap();
        let latest: Vec<_> = values
            .into_iter()
            .filter(|(ms, _)| *ms == largest)
            .collect();
        if latest.len() != 1 {
            continue;
        }
        let (ms, words) = latest[0];
        match record.counters.iter_mut().find(|(ep, _, _)| *ep == end) {
            Some(held) if held.1 < ms => *held = (end, ms, words),
            Some(_) => {}
            None => record.counters.push((end, ms, words)),
        }
    }
    record.counters.sort_unstable();
    // Upstream refuses non-unique (milliseconds, words) matches in either direction.
    record.recover(cards)
}

fn files(path: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
    if !path.exists() {
        return Ok(());
    }
    for entry in fs::read_dir(path)? {
        check_cancel()?;
        let p = entry?.path();
        let m = fs::symlink_metadata(&p)?;
        if m.file_type().is_symlink() {
            continue;
        }
        if m.is_dir() {
            files(&p, out)?
        } else if m.is_file() {
            out.push(p)
        }
    }
    Ok(())
}
fn index_cards(root: &Path, documents: &Path) -> Result<Vec<sidecar::Counter>> {
    let mut names = Vec::new();
    files(documents, &mut names)?;
    names.sort();
    ensure!(names.len() <= 10000, "document index too large");
    let mut stamp = String::new();
    for p in &names {
        if ["azw3f", "mbs", "yjf", "pdt", "tas"]
            .contains(&p.extension().and_then(|s| s.to_str()).unwrap_or(""))
        {
            let m = fs::metadata(p)?;
            stamp.push_str(&format!(
                "{}:{}:{}:{}:{};",
                p.display(),
                m.ino(),
                m.len(),
                m.mtime(),
                m.mtime_nsec()
            ));
        }
    }
    let fingerprint = sha(stamp);
    let cached = root.join("sidecar-index.json");
    let data = if let Ok(v) = private_json(&cached) {
        if v["fingerprint"] == fingerprint {
            Some(v)
        } else {
            None
        }
    } else {
        None
    };
    let data = match data {
        Some(v) => v,
        None => {
            let cards = sidecar::read(documents, true, None);
            let v = json!({"fingerprint":fingerprint,"cards":cards.counters.iter().map(|c|json!({"file":c.file,"total_ms":c.total_ms,"words":c.words,"covered":c.covered})).collect::<Vec<_>>()});
            atomic_json(&cached, &v)?;
            v
        }
    };
    data["cards"]
        .as_array()
        .context("invalid sidecar cache")?
        .iter()
        .map(|v| {
            Ok(sidecar::Counter {
                file: v["file"].as_str().context("missing file")?.to_owned(),
                total_ms: v["total_ms"].as_i64().context("missing counter")?,
                words: v["words"].as_i64().context("missing words")?,
                covered: v["covered"].as_f64().unwrap_or(0.),
            })
        })
        .collect()
}
fn remember_cc(root: &Path, record: &mut Record, path: &Path) -> Result<()> {
    if !path.exists() {
        return Ok(());
    }
    let mut fingerprint = String::new();
    for p in [
        path.to_path_buf(),
        PathBuf::from(format!("{}-wal", path.display())),
    ] {
        if let Ok(m) = fs::metadata(&p) {
            fingerprint.push_str(&format!(
                "{}:{}:{}:{}:{};",
                p.display(),
                m.ino(),
                m.len(),
                m.mtime(),
                m.mtime_nsec()
            ));
        }
    }
    let fingerprint = sha(fingerprint);
    let cached = root.join("catalog-index.json");
    let cache = private_json(&cached)
        .ok()
        .filter(|v| v["fingerprint"] == fingerprint);
    let data = match cache {
        Some(v) => v,
        None => {
            let db = Connection::open_with_flags(
                path,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY
                    | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
            )?;
            db.busy_timeout(std::time::Duration::from_millis(200))?;
            let mut stmt=db.prepare("SELECT p_cdeKey,coalesce(p_location,''),coalesce(p_titles_0_nominal,''),coalesce(p_credits_0_name_collation,'') FROM Entries WHERE p_cdeKey IS NOT NULL")?;
            let mut rows = Vec::new();
            for r in stmt.query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                ))
            })? {
                let (key, path, title, author) = r?;
                let stem = Path::new(&path)
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .unwrap_or("");
                rows.push(json!({"key":key,"stem":stem,"title":title,"author":author}));
            }
            let v = json!({"fingerprint":fingerprint,"rows":rows});
            atomic_json(&cached, &v)?;
            v
        }
    };
    // Catalog only supplies display metadata AFTER exact native counter identity.
    // p_contentSize is never treated as an HTMLPosition and cde_key is never replaced.
    for book in &mut record.books {
        let candidates: std::collections::BTreeSet<(String, String)> = data["rows"]
            .as_array()
            .context("invalid catalog cache")?
            .iter()
            .filter(|v| v["key"] == book.cde_key || v["stem"] == book.cde_key)
            .filter_map(|v| {
                Some((
                    v["title"].as_str()?.to_owned(),
                    v["author"].as_str()?.to_owned(),
                ))
            })
            .collect();
        if candidates.len() == 1 {
            let (title, author) = candidates.into_iter().next().unwrap();
            book.title = title;
            book.author = author;
        }
    }
    Ok(())
}
pub fn clock_verified(lines: &[String], day: &str) -> bool {
    let mut evidence = BTreeSet::new();
    let mut bad = false;
    for line in lines {
        if !line.contains("JSONJournalSaver")
            || !line.contains("annotationType=LastPositionReadAnnotation")
        {
            continue;
        }
        let Some(text) = line.get(..13) else { continue };
        let Ok(local) = NaiveDateTime::parse_from_str(text, "%y%m%d:%H%M%S") else {
            continue;
        };
        if local.date().to_string() != day {
            continue;
        }
        let Some(raw) = line.split("modificationDate=").nth(1) else {
            continue;
        };
        let raw = raw.trim_start_matches('"');
        let Some(utc) = raw.get(..19) else { continue };
        if raw.as_bytes().get(19) != Some(&b'Z') {
            continue;
        }
        let Ok(utc_time) = NaiveDateTime::parse_from_str(utc, "%Y-%m-%dT%H:%M:%S") else {
            continue;
        };
        if (local - utc_time).num_seconds() != 28800 {
            bad = true;
        }
        evidence.insert((text.to_owned(), utc.to_owned()));
    }
    !bad && evidence.len() >= 3
}
pub fn verify_manifest(root: &Path, body: &Value) -> Result<()> {
    let sources = body["sources"].as_array().context("source proof missing")?;
    ensure!(!sources.is_empty(), "empty source proof");
    let source_dir = fs::canonicalize(root.join("events/sealed"))?;
    for v in sources {
        let path = Path::new(v["path"].as_str().context("source path missing")?);
        let resolved = fs::canonicalize(path)?;
        ensure!(
            resolved.starts_with(&source_dir)
                && !fs::symlink_metadata(path)?.file_type().is_symlink(),
            "source escaped sealed directory"
        );
        ensure!(
            sha(limited(path, 16 * 1024 * 1024)?) == v["sha256"].as_str().unwrap_or(""),
            "source missing or changed"
        );
    }
    Ok(())
}

pub fn collect(store: &Store, documents: &Path, ccdb: &Path) -> Result<Value> {
    let rt = gate(&store.root, false, now())?;
    if !store.root.join("config.json").exists() {
        return Ok(
            json!({"state":"collect_only_unconfigured","reason":"raw journal retained; import a handoff before parsing/mapping","network_write":false}),
        );
    }
    atomic_json(
        &store.root.join("collection-state.json"),
        &json!({"complete":false}),
    )?;
    let c = Config::load(&store.root)?;
    store.bind(&c)?;
    let effective_cutoff = c.bootstrap_cutoff_unix.max(rt.bootstrap_unix);
    ensure!(effective_cutoff <= now(), "bootstrap is in the future");
    // Never reconstruct lines after their frozen original source disappeared.
    let mut held = store.db.prepare("SELECT path,hash FROM sources")?;
    for row in held.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))? {
        let (path, hash) = row?;
        ensure!(
            sha(limited(Path::new(&path), 16 * 1024 * 1024)?) == hash,
            "held source missing or changed"
        );
    }
    let mut sources = Vec::new();
    files(&store.root.join("events/sealed"), &mut sources)?;
    sources.retain(|p| p.extension().and_then(|s| s.to_str()) == Some("log"));
    sources.sort();
    ensure!(
        sources.len() <= 1024,
        "memory_budget_blocked: too many sealed segments"
    );
    let metadata_bytes: u64 = sources
        .iter()
        .map(|p| fs::symlink_metadata(p).map(|m| m.len()))
        .collect::<std::io::Result<Vec<_>>>()?
        .into_iter()
        .sum();
    ensure!(
        metadata_bytes <= 4 * 1024 * 1024,
        "memory_budget_blocked: sealed input exceeds 4 MiB parse budget"
    );
    storage_budget(&store.root, ROUTINE_TRANSACTION_BYTES)?;
    let mut proofs = Vec::new();
    let mut total = 0;
    for p in sources {
        check_cancel()?;
        let raw = limited(&p, 16 * 1024 * 1024)?;
        total += raw.len();
        ensure!(total <= 32 * 1024 * 1024, "sealed log quota exceeded");
        let hash = sha(&raw);
        let path = p.to_string_lossy().to_string();
        let prior: Option<String> = store
            .db
            .query_row("SELECT hash FROM sources WHERE path=?", [&path], |r| {
                r.get(0)
            })
            .optional()?;
        if let Some(ref prior) = prior {
            ensure!(prior == &hash, "sealed source changed; collect stopped");
        }
        if prior.is_none() {
            storage_budget(&store.root, ROUTINE_TRANSACTION_BYTES)?;
            let tx = store.db.unchecked_transaction()?;
            for line in String::from_utf8_lossy(&raw).lines() {
                check_cancel()?;
                ensure!(line.len() <= 65536, "oversized source line");
                let line = line
                    .strip_prefix("[!]")
                    .unwrap_or(line)
                    .trim_end_matches('\r');
                tx.execute(
                    "INSERT OR IGNORE INTO lines VALUES (?,?)",
                    params![sha(line.as_bytes()), line],
                )?;
            }
            tx.execute("INSERT INTO sources VALUES (?,?)", params![path, hash])?;
            tx.commit()?;
        }
        proofs.push(json!({"path":p,"sha256":hash}));
    }
    let raw_bytes: i64 = store.db.query_row(
        "SELECT coalesce(sum(length(CAST(line AS BLOB))),0) FROM lines",
        [],
        |r| r.get(0),
    )?;
    ensure!(
        raw_bytes <= 4 * 1024 * 1024,
        "memory_budget_blocked: raw event ledger exceeds bounded parse window"
    );
    let mut stmt = store.db.prepare("SELECT line FROM lines ORDER BY line")?;
    let mut normalized = Vec::new();
    let mut anchors = Vec::new();
    let mut identities = BTreeSet::new();
    for row in stmt.query_map([], |r| r.get::<_, String>(0))? {
        let line = row?;
        identities.extend(buffered_counters(&line));
        ensure!(
            identities.len() <= 50000,
            "memory_budget_blocked: identity window too large"
        );
        if line.contains("JSONJournalSaver") {
            anchors.push(line.clone());
            ensure!(
                anchors.len() <= 4096,
                "memory_budget_blocked: clock window too large"
            );
        }
        if log::family(&line).is_some() && log::line::stamp(&line).is_some() {
            normalized.push(line);
        }
    }
    let mut record = Record::default();
    check_cancel()?;
    record.absorb(&normalized, "");
    check_cancel()?;
    drop(normalized);
    drop(stmt);
    let cards = index_cards(&store.root, documents)?;
    record.recover(&cards);
    recover_buffered(
        &mut record,
        &cards,
        &identities.into_iter().collect::<Vec<_>>(),
    );
    // cc.db is read-only and queried only by this idle, one-shot job.
    let cc_error = remember_cc(&store.root, &mut record, ccdb).err().is_some();
    let mut inserted = 0;
    let mut blocked = 0;
    for s in &record.sittings {
        check_cancel()?;
        let extent = record.extent_of(s.end_position);
        let Some(book) = record.book_for(extent, s.asin.as_deref()) else {
            blocked += 1;
            continue;
        };
        if s.measure != log::sitting::Measure::Counted || s.seconds <= 0 {
            continue;
        }
        let start = epoch(&s.started_at)?;
        let end = epoch(&s.ended_at)?;
        if start < effective_cutoff {
            continue;
        }
        let day = &s.started_at[..10];
        let mut hours_seen = BTreeSet::new();
        let valid = s
            .hours
            .iter()
            .all(|(h, n)| *h <= 23 && *n >= 0 && *n <= 3600 && hours_seen.insert(h))
            && s.hours.iter().map(|(_, n)| n).sum::<i64>() == s.seconds;
        if !valid || !clock_verified(&anchors, day) || end > now() + 5 {
            blocked += 1;
            continue;
        }
        let (Some(start_ms), Some(end_ms), Some(start_words), Some(end_words)) = (
            s.start_counter_ms,
            s.end_counter_ms,
            s.start_words,
            s.end_words,
        ) else {
            blocked += 1;
            continue;
        };
        if end_ms <= start_ms {
            blocked += 1;
            continue;
        }
        let hours:Vec<Value>=s.hours.iter().filter(|(_,n)|*n>0).map(|(h,n)|json!({"startOfHour":epoch(&format!("{day}T{h:02}:00:00")).unwrap(),"timeZone":"+08:00","readingTime":n,"ttsTime":0})).collect();
        let identity = json!({"extent":extent,"start_ms":start_ms,"end_ms":end_ms,"start_words":start_words,"end_words":end_words,"day":day,"hours":hours});
        let id = sha(identity.to_string());
        let held: Option<(String, String, i64, i64, i64)> = store
            .db
            .query_row(
                "SELECT book,day,start_ms,end_ms,extent FROM sessions WHERE id=?",
                [&id],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .optional()?;
        if let Some((key, held_day, held_start, held_end, held_extent)) = held {
            ensure!(
                key == book.cde_key
                    && held_day == day
                    && held_start == start_ms
                    && held_end == end_ms
                    && held_extent == extent,
                "identity_conflict: existing native range names different source identity"
            );
            continue;
        }
        let mut conflicts = store.db.prepare(
            "SELECT end_ms,body FROM sessions WHERE extent=? AND day=? AND start_ms<? AND end_ms>?",
        )?;
        let held: Vec<(i64, String)> = conflicts
            .query_map(params![extent, day, end_ms, start_ms], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })?
            .collect::<std::result::Result<_, _>>()?;
        drop(conflicts);
        let stamp = NaiveDateTime::parse_from_str(&s.started_at, "%Y-%m-%dT%H:%M:%S")?
            .format("%y%m%d:%H%M%S")
            .to_string();
        let witnessed_open:i64=store.db.query_row("SELECT count(*) FROM lines WHERE line LIKE ? AND line LIKE '%ReadingTimerController:%OpenBook%'",[format!("{stamp}%")],|r|r.get(0))?;
        let mut overlap = 0;
        for (previous_end, text) in held {
            let previous: Value = serde_json::from_str(&text)?;
            // OpenBook TimeRead is integral seconds. A subsecond overlap may
            // be rounding only if disjoint native word counters and a later,
            // witnessed OpenBook independently prove a new reading session.
            let rounded_boundary = previous_end > start_ms
                && previous_end - start_ms < 1000
                && start_words > 0
                && previous["counter_evidence"]["end_words"].as_i64() == Some(start_words)
                && previous["local_book_id"] == book.cde_key
                && previous["ended_at"]
                    .as_str()
                    .is_some_and(|v| v < s.started_at.as_str())
                && witnessed_open > 0;
            if !rounded_boundary {
                overlap += 1;
            }
        }
        let state = if overlap > 0 {
            "blocked_overlap"
        } else {
            "pending"
        };
        if overlap > 0 {
            blocked += 1
        }
        let body = json!({"id":id,"local_book_id":book.cde_key,"title":book.title,"day":day,"started_at":s.started_at,"ended_at":s.ended_at,"seconds":s.seconds,"hours":hours,"counter_evidence":identity,"clock_verified":true,"sources":proofs,"bootstrap_cutoff_unix":effective_cutoff,"book_version":0,"book_version_source":"unavailable"});
        storage_budget(&store.root, ROUTINE_TRANSACTION_BYTES)?;
        store.db.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
            params![
                id,
                book.cde_key,
                day,
                start_ms,
                end_ms,
                extent,
                state,
                body.to_string()
            ],
        )?;
        inserted += 1;
    }
    storage_budget(&store.root, ROUTINE_TRANSACTION_BYTES)?;
    store.db.execute("INSERT INTO meta VALUES ('last_collect_blocked',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",[blocked.to_string()])?;
    atomic_json(
        &store.root.join("collection-state.json"),
        &json!({"complete":true,"blocked":blocked}),
    )?;
    Ok(
        json!({"state":"collected","inserted_sessions":inserted,"blocked_sessions":blocked,"ccdb_unavailable":cc_error,"statistics":store.status()?}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    pub(super) const LOG: [&str; 3] = [
        "260906:192401 java[1]: I ReadingTimerController:Information::OpenBook,StoredBookData:TimeRead:329 sec. WPM:0. Version:0;",
        "260906:192404 cvm[6144]: I ReadingTimerController:Information::NextPage,Verdict:Processed,PageStartPos:HTMLPosition:7731097,IntervalTime:785,IntervalWords:12,TotalTime:329785,TotalWords:1905,CurrentPos:HTMLPosition:7731097,EndPos:HTMLPosition:19886489,PosLeft:12155392,%Left:0.6112;",
        "260906:192425 cvm[6144]: I ReadingTimerController:Information::NextPage,Verdict:Processed,PageStartPos:HTMLPosition:7731725,IntervalTime:21217,IntervalWords:172,TotalTime:351002,TotalWords:2077,CurrentPos:HTMLPosition:7731725,EndPos:HTMLPosition:19886489,PosLeft:12154764,%Left:0.6111;",
    ];
    #[test]
    fn buffered_only_recovers_identity_without_adding_seconds() {
        let mut r = Record::default();
        r.absorb(&LOG.map(str::to_owned), "");
        let before = r.sittings.clone();
        let delayed = "260906:221627 cvm[6144]: E BufferedLogger:Error::PageStartPos:HTMLPosition:7731725,IntervalTime:600009,TotalTime:999999,TotalWords:7777,EndPos:HTMLPosition:19886489;";
        let counters = [sidecar::Counter {
            file: "book-A".into(),
            total_ms: 999999,
            words: 7777,
            covered: 0.,
        }];
        assert_eq!(
            recover_buffered(&mut r, &counters, &buffered_counters(delayed)),
            1
        );
        assert_eq!(r.sittings, before);
        assert_eq!(r.sittings[0].seconds, 22);
    }
    #[test]
    fn buffered_counter_collision_remains_unmatched() {
        let mut r = Record::default();
        r.absorb(&LOG.map(str::to_owned), "");
        let counters = [
            sidecar::Counter {
                file: "A".into(),
                total_ms: 999999,
                words: 7777,
                covered: 0.,
            },
            sidecar::Counter {
                file: "B".into(),
                total_ms: 999999,
                words: 7777,
                covered: 0.,
            },
        ];
        assert_eq!(
            recover_buffered(&mut r, &counters, &[(19886489, 999999, 7777)]),
            0
        );
    }
    #[test]
    fn clock_needs_three_distinct_exact_anchors() {
        let anchors:Vec<_>=(0..3).map(|i|format!("261101:220{i}00 cvm: I JSONJournalSaver modificationDate=2026-11-01T14:0{i}:00Z, annotationType=LastPositionReadAnnotation")).collect();
        assert!(clock_verified(&anchors, "2026-11-01"));
        assert!(!clock_verified(&vec![anchors[0].clone(); 3], "2026-11-01"));
        let mut bad = anchors.clone();
        bad.push(anchors[0].replace("14:00:00Z", "15:00:00Z"));
        assert!(!clock_verified(&bad, "2026-11-01"));
    }
}

#[cfg(test)]
mod pipeline_tests {
    use super::*;
    use tempfile::TempDir;
    fn root() -> (TempDir, Store, PathBuf) {
        let tmp = TempDir::new().unwrap();
        fs::set_permissions(tmp.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let s = Store::open(tmp.path()).unwrap();
        let rt = json!({"schema_version":1,"reader_state":"idle","power_state":"awake","usb_export":false,"network_connected":false,"idle_since_unix":now()-30,"bootstrap_unix":epoch("2026-09-06T19:00:00").unwrap()});
        atomic_json(&s.root.join("runtime.json"), &rt).unwrap();
        let c:Config=serde_json::from_value(json!({"schema_version":1,"device_id":"test-device","account_vid":"test-account","profile":"pw2-5.12.2.1.1","firmware":"5.12.2.1.1","timezone":"Asia/Shanghai","bootstrap_cutoff_unix":1000,"mappings":[],"validation":{"device_validation":false,"protocol_validation":false,"binding_sha256":""}})).unwrap();
        atomic_json(
            &s.root.join("config.json"),
            &serde_json::to_value(c).unwrap(),
        )
        .unwrap();
        let docs = s.root.join("documents");
        let dir = docs.join("A.sdr");
        fs::create_dir_all(&dir).unwrap();
        let mut bytes = sidecar::SIGNATURE.to_vec();
        bytes.push(2);
        bytes.extend(1i64.to_be_bytes());
        bytes.push(1);
        bytes.extend(1i32.to_be_bytes());
        bytes.extend([0xfe, 0, 0, 11]);
        bytes.extend(b"timer.model");
        for n in [0i64, 351002, 2077] {
            bytes.push(2);
            bytes.extend(n.to_be_bytes())
        }
        bytes.push(0xff);
        fs::write(dir.join("A.azw3f"), bytes).unwrap();
        fs::create_dir_all(s.root.join("events/sealed")).unwrap();
        let mut text = super::tests::LOG.join("\n");
        text.push_str("\n260906:192426 cvm: I ReadingTimerController:Information::CloseBook,CurrentPos:HTMLPosition:7731725,EndPos:HTMLPosition:19886489,TotalTime:351002,TotalWords:2077;");
        for i in 0..3 {
            text.push_str(&format!("\n260906:19240{i} cvm: I JSONJournalSaver modificationDate=2026-09-06T11:24:0{i}Z, annotationType=LastPositionReadAnnotation"));
        }
        fs::write(s.root.join("events/sealed/one.log"), text).unwrap();
        (tmp, s, docs)
    }
    #[test]
    fn configured_before_collector_start_parses_and_deduplicates_exact_sidecar() {
        let (_tmp, s, docs) = root();
        let no = s.root.join("missing-cc.db");
        let first = collect(&s, &docs, &no).unwrap();
        assert_eq!(first["inserted_sessions"], 1);
        assert_eq!(collect(&s, &docs, &no).unwrap()["inserted_sessions"], 0);
        let (book, seconds): (String, i64) =
            s.db.query_row(
                "SELECT book,json_extract(body,'$.seconds') FROM sessions",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(book, "A");
        assert_eq!(seconds, 22);
    }
    #[test]
    fn missing_or_duplicate_clock_evidence_cannot_create_pending_sessions() {
        let (_tmp, s, docs) = root();
        let path = s.root.join("events/sealed/one.log");
        let text = fs::read_to_string(&path).unwrap();
        let filtered = text
            .lines()
            .filter(|l| !l.contains("JSONJournalSaver"))
            .collect::<Vec<_>>()
            .join("\n");
        fs::write(path, filtered).unwrap();
        assert_eq!(
            collect(&s, &docs, &s.root.join("no-db")).unwrap()["inserted_sessions"],
            0
        );
    }
    #[test]
    fn ccdb_size_is_never_used_as_htmlposition_identity() {
        let (_tmp, s, docs) = root();
        fs::remove_dir_all(docs.join("A.sdr")).unwrap();
        let path = s.root.join("cc.db");
        let db = Connection::open(&path).unwrap();
        db.execute_batch("CREATE TABLE Entries(p_contentSize INTEGER,p_cdeKey TEXT,p_location TEXT,p_titles_0_nominal TEXT,p_credits_0_name_collation TEXT);INSERT INTO Entries VALUES(19886489,'WrongBook','/mnt/us/documents/WrongBook.azw3','Must not match','Wrong Author');").unwrap();
        drop(db);
        let v = collect(&s, &docs, &path).unwrap();
        assert_eq!(v["inserted_sessions"], 0);
    }
    #[test]
    fn ccdb_only_enriches_title_after_exact_sidecar_match() {
        let (_tmp, s, docs) = root();
        let path = s.root.join("cc.db");
        let db = Connection::open(&path).unwrap();
        db.execute_batch("CREATE TABLE Entries(p_contentSize INTEGER,p_cdeKey TEXT,p_location TEXT,p_titles_0_nominal TEXT,p_credits_0_name_collation TEXT);INSERT INTO Entries VALUES(12345,'ASIN_CATALOG','/mnt/us/documents/A.azw3','Example Title','Example Author');").unwrap();
        drop(db);
        collect(&s, &docs, &path).unwrap();
        let (key, title): (String, String) =
            s.db.query_row(
                "SELECT book,json_extract(body,'$.title') FROM sessions",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(key, "A");
        assert_eq!(title, "Example Title");
    }
    #[test]
    fn sealed_source_deletion_blocks_reconstruction_from_ledger_lines() {
        let (_tmp, s, docs) = root();
        collect(&s, &docs, &s.root.join("none")).unwrap();
        fs::remove_file(s.root.join("events/sealed/one.log")).unwrap();
        assert!(collect(&s, &docs, &s.root.join("none")).is_err());
    }
    #[test]
    fn unconfigured_collect_keeps_raw_journal_and_reports_clear_state() {
        let (_tmp, s, docs) = root();
        fs::remove_file(s.root.join("config.json")).unwrap();
        assert_eq!(
            collect(&s, &docs, &s.root.join("none")).unwrap()["state"],
            "collect_only_unconfigured"
        );
        assert!(s.root.join("events/sealed/one.log").is_file());
    }
    #[test]
    fn sixteen_megabyte_input_is_refused_before_unbounded_parse() {
        let (_tmp, s, docs) = root();
        let f = fs::File::create(s.root.join("events/sealed/large.log")).unwrap();
        f.set_len(16 * 1024 * 1024).unwrap();
        let err = collect(&s, &docs, &s.root.join("none")).unwrap_err();
        assert!(err.to_string().contains("memory_budget_blocked"));
        assert_eq!(
            s.db.query_row("SELECT count(*) FROM lines", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            0
        );
    }
    #[test]
    fn compact_then_new_open_keeps_old_ids_without_double_count() {
        let (_tmp, s, docs) = root();
        let no = s.root.join("none");
        collect(&s, &docs, &no).unwrap();
        s.db.execute("UPDATE sessions SET state='confirmed'", [])
            .unwrap();
        assert_eq!(compact(&s).unwrap()["state"], "compacted");
        let mut text = super::tests::LOG.join("\n");
        text.push_str("\n260906:192426 cvm: I ReadingTimerController:Information::CloseBook,CurrentPos:HTMLPosition:7731725,EndPos:HTMLPosition:19886489,TotalTime:351002,TotalWords:2077;");
        for i in 0..3 {
            text.push_str(&format!("\n260906:19240{i} cvm: I JSONJournalSaver modificationDate=2026-09-06T11:24:0{i}Z, annotationType=LastPositionReadAnnotation"));
        }
        text.push_str("\n260906:192500 cvm: I ReadingTimerController:Information::OpenBook,StoredBookData:TimeRead:351 sec. WPM:0. Version:0;\n260906:192529 cvm: I ReadingTimerController:Information::NextPage,Verdict:Processed,PageStartPos:HTMLPosition:7731725,IntervalTime:28998,IntervalWords:1023,TotalTime:380000,TotalWords:3100,CurrentPos:HTMLPosition:7731725,EndPos:HTMLPosition:19886489,PosLeft:12154764,%Left:0.6111;");
        fs::write(s.root.join("events/sealed/two.log"), text).unwrap();
        let path = docs.join("A.sdr/A.azw3f");
        let mut raw = fs::read(&path).unwrap();
        for (old, new) in [(351002i64, 380000i64), (2077, 3100)] {
            let n = raw.windows(8).position(|v| v == old.to_be_bytes()).unwrap();
            raw[n..n + 8].copy_from_slice(&new.to_be_bytes());
        }
        fs::write(path, raw).unwrap();
        let got = collect(&s, &docs, &no).unwrap();
        assert_eq!(got["inserted_sessions"], 1);
        assert_eq!(s.status().unwrap()["confirmed_seconds"], 22);
        assert_eq!(s.status().unwrap()["pending_seconds"], 29);
        assert_eq!(
            s.db.query_row(
                "SELECT count(*) FROM sessions WHERE state='confirmed'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            1
        );
    }
    #[test]
    fn renamed_native_identity_conflicts_instead_of_silent_skip() {
        let (_tmp, s, docs) = root();
        let none = s.root.join("none");
        collect(&s, &docs, &none).unwrap();
        fs::rename(docs.join("A.sdr"), docs.join("B.sdr")).unwrap();
        fs::rename(docs.join("B.sdr/A.azw3f"), docs.join("B.sdr/B.azw3f")).unwrap();
        let err = collect(&s, &docs, &none).unwrap_err();
        assert!(err.to_string().contains("identity_conflict"));
        assert!(s.root.join("events/sealed/one.log").exists());
        assert_eq!(
            s.db.query_row("SELECT count(*) FROM sessions", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            1
        );
    }
}
