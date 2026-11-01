//! Host-only adapter around upstream ReadingLog. Never calls its device/UI/update APIs.
use readinglog_native::{log, record::Record, sidecar};
use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fs;
use std::io::{BufRead, BufReader, Read};
use std::path::{Component, Path, PathBuf};

type Result<T> = std::result::Result<T, Box<dyn Error>>;

struct Args { logs: PathBuf, snapshot: PathBuf, out: PathBuf }

fn args() -> Result<Option<Args>> {
    let mut args = std::env::args().skip(1);
    let (mut logs, mut snapshot, mut out) = (None, None, None);
    while let Some(key) = args.next() {
        if key == "--help" || key == "-h" {
            println!("kindle-readinglog-export --logs LOG_FILE_OR_DIRECTORY --snapshot COPIED_SNAPSHOT --out OUTPUT_DIRECTORY\nOnly reads copied logs and sidecars. No network or Kindle writes. Dates remain on the Kindle's local clock; timezone requires separate validation.");
            return Ok(None);
        }
        let value = args.next().ok_or("missing argument value")?;
        match key.as_str() {
            "--logs" => logs = Some(PathBuf::from(value)),
            "--snapshot" => snapshot = Some(PathBuf::from(value)),
            "--out" => out = Some(PathBuf::from(value)),
            _ => return Err(format!("unknown argument: {key}").into()),
        }
    }
    Ok(Some(Args {
        logs: logs.ok_or("--logs is required")?,
        snapshot: snapshot.ok_or("--snapshot is required")?,
        out: out.ok_or("--out is required")?,
    }))
}

fn collect_files(path: &Path, out: &mut Vec<PathBuf>) -> Result<()> {
    let md = fs::symlink_metadata(path)?;
    if md.file_type().is_symlink() { return Ok(()); }
    if md.is_file() { out.push(path.to_path_buf()); }
    else if md.is_dir() {
        for child in fs::read_dir(path)? { collect_files(&child?.path(), out)?; }
    }
    Ok(())
}

fn normalize(lines: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut unique = BTreeSet::new();
    for line in lines {
        let line = line.strip_prefix("[!]").unwrap_or(&line).trim_end_matches('\r');
        if log::family(line).is_some() && log::line::stamp(line).is_some() {
            unique.insert(line.to_string());
        }
    }
    unique.into_iter().collect()
}

fn collect_logs(path: &Path) -> Result<(Vec<String>, usize, Vec<(i64, i64, i64)>)> {
    let mut files = Vec::new();
    collect_files(path, &mut files)?;
    files.sort();
    let nowhere = path.join(".readinglog-no-such-directory");
    let mut events = Vec::new();
    let mut identities = BTreeSet::new();
    for file in &files {
        // This upstream API auto-detects gzip and bounds each line to 64 KiB.
        let got = log::source::collect_from(file, &nowhere, &nowhere, "", &mut |_, _| {});
        events.extend(got.lines);
        // BufferedLogger can flush an old counter much later. It supplies
        // identity evidence only and is NEVER passed to the sitting parser.
        let mut input = BufReader::new(fs::File::open(file)?);
        let gzipped = input.fill_buf()?.starts_with(&[0x1f, 0x8b]);
        let stream: Box<dyn Read> = if gzipped { Box::new(flate2::read::GzDecoder::new(input)) } else { Box::new(input) };
        let mut source = BufReader::new(stream.take(64 * 1024 * 1024));
        let mut line = Vec::new();
        loop {
            line.clear();
            if source.read_until(b'\n', &mut line)? == 0 { break; }
            if line.len() > 64 * 1024 { continue; }
            identities.extend(buffered_counters(&String::from_utf8_lossy(&line)));
        }
    }
    Ok((normalize(events), files.len(), identities.into_iter().collect()))
}

fn buffered_counters(line: &str) -> Vec<(i64, i64, i64)> {
    let Some((_, payload)) = line.split_once(": E BufferedLogger:Error::") else { return Vec::new(); };
    if log::line::stamp(line).is_none() { return Vec::new(); }
    // One flushed line may concatenate several pages. Never combine a first
    // page's counter with the next page's end position or chapter end.
    payload.split("PageStartPos:").skip(1).filter_map(|page| {
        let end = log::line::end_position(page)?;
        let ms = log::line::field(page, "TotalTime")?;
        let words = log::line::field(page, "TotalWords")?;
        (end > 0 && ms > 0 && words > 0).then_some((end, ms, words))
    }).collect()
}

fn recover_buffered(record: &mut Record, cards: &[sidecar::Counter], identities: &[(i64, i64, i64)]) -> usize {
    // Preserve all books already identified by ordinary timer events.
    let unknown: BTreeSet<i64> = record.sittings.iter().filter_map(|s| {
        let ep = record.extent_of(s.end_position);
        record.book_for(ep, s.asin.as_deref()).is_none().then_some(s.end_position)
    }).collect();
    let mut by_end: BTreeMap<i64, BTreeSet<(i64, i64)>> = BTreeMap::new();
    for &(end, ms, words) in identities {
        if unknown.contains(&end) { by_end.entry(end).or_default().insert((ms, words)); }
    }
    for (end, values) in by_end {
        let largest = values.iter().map(|(ms, _)| *ms).max().unwrap();
        let latest: Vec<_> = values.into_iter().filter(|(ms, _)| *ms == largest).collect();
        if latest.len() != 1 { continue; }
        let (ms, words) = latest[0];
        match record.counters.iter_mut().find(|(ep, _, _)| *ep == end) {
            Some(held) if held.1 < ms => *held = (end, ms, words),
            Some(_) => {},
            None => record.counters.push((end, ms, words)),
        }
    }
    record.counters.sort_unstable();
    // Upstream refuses non-unique (milliseconds, words) matches in either direction.
    record.recover(cards)
}

fn csv_cell(value: impl AsRef<str>) -> String {
    format!("\"{}\"", value.as_ref().replace('"', "\"\""))
}

fn csv_row(values: &[String]) -> String {
    values.iter().map(csv_cell).collect::<Vec<_>>().join(",") + "\n"
}

/// Upstream has already allocated native counter increments to hours and
/// split midnight. Never reconstruct this allocation from session wall time.
fn checked_hours(sitting: &log::sitting::Sitting) -> std::result::Result<(), String> {
    let mut seen = BTreeSet::new();
    let mut total = 0i64;
    if sitting.seconds < 0 { return Err("negative session seconds".into()); }
    for &(hour, seconds) in &sitting.hours {
        if hour > 23 || seconds < 0 || !seen.insert(hour) {
            return Err("invalid or duplicate upstream hour bucket".into());
        }
        total = total.checked_add(seconds).ok_or("hour total overflow")?;
    }
    if total != sitting.seconds {
        return Err(format!("upstream hours sum to {total}, session seconds are {}", sitting.seconds));
    }
    Ok(())
}

fn machine_exports(record: &Record, provenance: &serde_json::Value) -> Result<(serde_json::Value, serde_json::Value)> {
    use serde_json::{Value, json};
    let mut sessions = Vec::new();
    let mut buckets: BTreeMap<(String, String, u8), (String, i64, Vec<usize>, Vec<Value>)> = BTreeMap::new();
    for (session_index, sitting) in record.sittings.iter().enumerate() {
        let book_extent = record.extent_of(sitting.end_position);
        let book = record.book_for(book_extent, sitting.asin.as_deref());
        let title = book.map(|b| b.title.as_str()).unwrap_or("");
        let author = book.map(|b| b.author.as_str()).unwrap_or("");
        let source_key = book.map(|b| b.cde_key.as_str()).or(sitting.asin.as_deref()).unwrap_or("");
        let validation = checked_hours(sitting);
        let day = sitting.started_at.get(..10).ok_or("upstream returned invalid timestamp")?;
        let counted = sitting.measure == log::sitting::Measure::Counted;
        let eligible = book.is_some() && counted && validation.is_ok() && sitting.seconds > 0;
        sessions.push(json!({
            "session_index": session_index,
            "status": if book.is_some() { "matched_exact_sidecar_counter" } else { "unmatched" },
            "matched": book.is_some(),
            "title": title,
            "author": author,
            "source_key": source_key,
            "end_position": sitting.end_position,
            "book_extent": book_extent,
            "started_at": sitting.started_at,
            "ended_at": sitting.ended_at,
            "seconds": sitting.seconds,
            "measure": sitting.measure.as_str(),
            "hours": sitting.hours,
            "start_counter_ms": sitting.start_counter_ms,
            "end_counter_ms": sitting.end_counter_ms,
            "start_words": sitting.start_words,
            "end_words": sitting.end_words,
            "hour_allocation_valid": validation.is_ok(),
            "validation_error": validation.err(),
            "sync_eligible_before_clock_and_mapping_checks": eligible,
            "clock_verified": false,
            "utc_offset_seconds": Value::Null,
            "provenance": provenance,
        }));
        if !eligible { continue; }
        for &(hour, seconds) in &sitting.hours {
            if seconds <= 0 { continue; }
            let bucket = buckets.entry((day.to_string(), source_key.to_string(), hour))
                .or_insert_with(|| (title.to_string(), 0, Vec::new(), Vec::new()));
            if bucket.0 != title { return Err("one source key names inconsistent titles".into()); }
            bucket.1 = bucket.1.checked_add(seconds).ok_or("hour bucket total overflow")?;
            bucket.2.push(session_index);
            bucket.3.push(json!({
                "session_index": session_index,
                "end_position": sitting.end_position,
                "book_extent": book_extent,
                "start_counter_ms": sitting.start_counter_ms,
                "end_counter_ms": sitting.end_counter_ms,
                "start_words": sitting.start_words,
                "end_words": sitting.end_words,
            }));
        }
    }
    let hours = buckets.into_iter().map(|((day, source_key, hour), (title, seconds, session_indexes, evidence))| json!({
        "local_date": day,
        "source_key": source_key,
        "title": title,
        "measure": "counted",
        "hour": hour,
        "seconds": seconds,
        "session_indexes": session_indexes,
        "source_counter_evidence": evidence,
        "clock_verified": false,
        "utc_offset_seconds": Value::Null,
        "provenance": provenance,
    })).collect::<Vec<_>>();
    Ok((Value::Array(sessions), Value::Array(hours)))
}

fn titles(snapshot: &Path) -> Result<BTreeMap<String, (String, String)>> {
    let file = snapshot.join("metadata.calibre");
    if !file.is_file() { return Ok(BTreeMap::new()); }
    let data: serde_json::Value = serde_json::from_slice(&fs::read(file)?)?;
    let mut candidates: BTreeMap<String, BTreeSet<(String, String)>> = BTreeMap::new();
    if let Some(rows) = data.as_array() {
        for row in rows {
            let (Some(path), Some(title)) = (row["lpath"].as_str(), row["title"].as_str()) else { continue; };
            let Some(stem) = Path::new(path).file_stem().and_then(|s| s.to_str()) else { continue; };
            let author = row["authors"].as_array().map(|v| v.iter().filter_map(|x| x.as_str()).collect::<Vec<_>>().join("; ")).unwrap_or_default();
            candidates.entry(stem.to_owned()).or_default().insert((title.to_owned(), author));
        }
    }
    Ok(candidates.into_iter().filter_map(|(k, v)| {
        if v.len() == 1 { Some((k, v.into_iter().next().unwrap())) } else { None }
    }).collect())
}

/// Resolve existing ancestors and symlinks without creating any directory.
fn prospective_path(path: &Path) -> Result<PathBuf> {
    let absolute = if path.is_absolute() { path.to_path_buf() } else { std::env::current_dir()?.join(path) };
    let mut normalized = PathBuf::new();
    for part in absolute.components() {
        match part {
            Component::ParentDir => { normalized.pop(); },
            Component::CurDir => {},
            other => normalized.push(other.as_os_str()),
        }
    }
    let mut ancestor = normalized.as_path();
    let mut tail = Vec::new();
    while !ancestor.exists() {
        tail.push(ancestor.file_name().ok_or("output has no existing ancestor")?.to_os_string());
        ancestor = ancestor.parent().ok_or("output has no existing ancestor")?;
    }
    let mut resolved = fs::canonicalize(ancestor)?;
    for part in tail.into_iter().rev() { resolved.push(part); }
    Ok(resolved)
}

fn check_output(output: &Path, logs: &Path, snapshot: &Path) -> Result<PathBuf> {
    let resolved = prospective_path(output)?;
    if resolved.starts_with("/Volumes") || resolved.starts_with(logs) || resolved.starts_with(snapshot) {
        return Err("Output must be on the Mac and outside the input trees".into());
    }
    Ok(resolved)
}

fn export(args: Args) -> Result<()> {
    let logs_path = fs::canonicalize(&args.logs)?;
    let snapshot = fs::canonicalize(&args.snapshot)?;
    for input in [&logs_path, &snapshot] {
        if input.starts_with("/Volumes") { return Err("Use a Mac copy, not the mounted device, as input".into()); }
    }
    let output = check_output(&args.out, &logs_path, &snapshot)?;
    fs::create_dir_all(&output)?;
    let (lines, files, buffered) = collect_logs(&logs_path)?;
    let mut record = Record::default();
    record.absorb(&lines, "");
    let cards = sidecar::read(&snapshot.join("documents"), true, None);
    // Match only unique, exactly equal (total milliseconds, total words) pairs.
    // Do not assign a book by fuzzy title, nearest time, or shared end position alone.
    let recovered_initial = record.recover(&cards.counters);
    let recovered_buffered = recover_buffered(&mut record, &cards.counters, &buffered);
    let recovered = recovered_initial + recovered_buffered;
    record.note_timers(&cards.counters);
    let title_map = titles(&snapshot)?;
    for book in &mut record.books {
        if let Some((title, author)) = title_map.get(&book.cde_key) {
            book.title = title.clone(); book.author = author.clone();
        }
    }

    let mut source_files = Vec::new();
    collect_files(&logs_path, &mut source_files)?;
    source_files.sort();
    let provenance = serde_json::json!({
        "snapshot": snapshot,
        "logs": source_files,
        "upstream_repo": "https://github.com/huangziwei/readinglog",
        "upstream_commit": "3d5d5b394818387c42a35834e743d1b13aec6b60",
        "hour_allocation": "upstream Sitting.hours; not redistributed across wall-clock session spans",
        "coverage": "retained logs only; missing days are unknown",
        "identity": "unique exact native milliseconds and word counters; BufferedLogger is identity-only",
    });
    let (sessions_json, hour_totals_json) = machine_exports(&record, &provenance)?;

    let header = "\u{feff}status,title,author,start_kindle_local,end_kindle_local,seconds,measure,end_position,source_key,timezone_status\n";
    let mut sessions = header.to_string();
    let mut unmatched = header.to_string();
    let mut measured: BTreeMap<(String, String, String), i64> = BTreeMap::new();
    let mut estimated: BTreeMap<(String, String, String, String), i64> = BTreeMap::new();
    let mut matched_count = 0;
    for sitting in &record.sittings {
        let extent = record.extent_of(sitting.end_position);
        let book = record.book_for(extent, sitting.asin.as_deref());
        let (status, title, author, key) = match book {
            Some(b) => ("matched_exact_sidecar_counter", b.title.clone(), b.author.clone(), b.cde_key.clone()),
            None => ("unmatched", String::new(), String::new(), sitting.asin.clone().unwrap_or_default()),
        };
        let row = csv_row(&[
            status.into(), title.clone(), author, sitting.started_at.clone(), sitting.ended_at.clone(),
            sitting.seconds.to_string(), sitting.measure.as_str().into(), extent.to_string(), key.clone(),
            "Kindle local clock; UTC offset unverified".into(),
        ]);
        sessions.push_str(&row);
        if book.is_none() { unmatched.push_str(&row); continue; }
        matched_count += 1;
        if sitting.seconds <= 0 { continue; }
        let day = sitting.started_at.get(..10).ok_or("upstream returned invalid timestamp")?.to_string();
        match sitting.measure {
            log::sitting::Measure::Counted => *measured.entry((day, key, title)).or_default() += sitting.seconds,
            _ => *estimated.entry((day, key, title, sitting.measure.as_str().into())).or_default() += sitting.seconds,
        }
    }
    let mut daily = "\u{feff}date_kindle_local,source_key,title,seconds,measure,coverage,timezone_status\n".to_string();
    for ((day, key, title), secs) in &measured {
        daily.push_str(&csv_row(&[day.clone(), key.clone(), title.clone(), secs.to_string(), "counted".into(), "retained logs only; missing time is unknown".into(), "unverified Kindle local clock".into()]));
    }
    let mut estimates = "\u{feff}date_kindle_local,source_key,title,seconds,measure,coverage,timezone_status\n".to_string();
    for ((day, key, title, measure), secs) in &estimated {
        estimates.push_str(&csv_row(&[day.clone(), key.clone(), title.clone(), secs.to_string(), measure.clone(), "estimate from retained logs; not a complete daily ledger".into(), "unverified Kindle local clock".into()]));
    }
    fs::write(output.join("sessions.csv"), sessions)?;
    fs::write(output.join("unmatched_sessions.csv"), unmatched)?;
    fs::write(output.join("daily_measured.csv"), daily)?;
    fs::write(output.join("daily_estimated.csv"), estimates)?;
    fs::write(output.join("sessions.tsv"), record.text())?;
    fs::write(output.join("sessions.json"), serde_json::to_vec_pretty(&sessions_json)?)?;
    fs::write(output.join("hour_totals.json"), serde_json::to_vec_pretty(&hour_totals_json)?)?;
    fs::write(output.join("marker_lines.log"), lines.join("\n") + "\n")?;
    let summary = format!("ReadingLog upstream adapter\nsource files: {files}\nunique recognized timestamped lines: {}\nsidecar counters: {}\nexact counter matches: {recovered}\nordinary timer identity matches: {recovered_initial}\nbuffered identity-only matches: {recovered_buffered}\nparsed sessions: {}\nmatched sessions: {matched_count}\nunmatched sessions: {}\nmeasured book/day rows: {}\nestimated book/day/measure rows: {}\n\nOnly retained logs were parsed. No zero rows are manufactured for missing days. Dates remain on Kindle local clock; timezone must be checked before upload. BufferedLogger counters identify books only; delayed flushes add no time or session. Existing cumulative timer.model values are not assigned to their file modification date. Nothing was uploaded or written to Kindle.\n", lines.len(), cards.counters.len(), record.sittings.len(), record.sittings.len() - matched_count, measured.len(), estimated.len());
    fs::write(output.join("summary.txt"), &summary)?;
    print!("{summary}");
    Ok(())
}

fn main() {
    let result = args().and_then(|args| match args { Some(args) => export(args), None => Ok(()) });
    if let Err(error) = result { eprintln!("Error: {error}"); std::process::exit(1); }
}

#[cfg(test)]
mod tests {
    use super::*;
    const LOG: [&str; 3] = [
        "260906:192401 java[1]: I ReadingTimerController:Information::OpenBook,StoredBookData:TimeRead:329 sec. WPM:0. Version:0;",
        "260906:192404 cvm[6144]: I ReadingTimerController:Information::NextPage,Verdict:Processed,PageStartPos:HTMLPosition:7731097,IntervalTime:785,IntervalWords:12,TotalTime:329785,TotalWords:1905,CurrentPos:HTMLPosition:7731097,EndPos:HTMLPosition:19886489,PosLeft:12155392,%Left:0.6112;",
        "260906:192425 cvm[6144]: I ReadingTimerController:Information::NextPage,Verdict:Processed,PageStartPos:HTMLPosition:7731725,IntervalTime:21217,IntervalWords:172,TotalTime:351002,TotalWords:2077,CurrentPos:HTMLPosition:7731725,EndPos:HTMLPosition:19886489,PosLeft:12154764,%Left:0.6111;",
    ];
    #[test]
    fn duplicate_dump_lines_do_not_double_count() {
        let lines = normalize(LOG.iter().chain(LOG.iter()).map(|x| x.to_string()));
        assert_eq!(lines.len(), 3);
        let mut record = Record::default(); record.absorb(&lines, "");
        assert_eq!(record.sittings.len(), 1); assert_eq!(record.sittings[0].seconds, 22);
        assert_eq!(record.sittings[0].started_at, "2026-09-06T19:24:01");
        let cards = [sidecar::Counter { file: "A".into(), total_ms: 351002, words: 2077, covered: 0.0 }];
        assert_eq!(record.recover(&cards), 1);
        let (sessions, buckets) = machine_exports(&record, &serde_json::json!({})).unwrap();
        assert_eq!(sessions.as_array().unwrap().len(), 1);
        assert_eq!(buckets.as_array().unwrap().iter().map(|v| v["seconds"].as_i64().unwrap()).sum::<i64>(), 22);
    }
    #[test]
    fn ambiguous_counter_is_not_assigned_to_a_book() {
        let lines = normalize(LOG.iter().map(|x| x.to_string()));
        let mut record = Record::default(); record.absorb(&lines, "");
        let cards = vec![
            sidecar::Counter { file: "A".into(), total_ms: 351002, words: 2077, covered: 0.0 },
            sidecar::Counter { file: "B".into(), total_ms: 351002, words: 2077, covered: 0.0 },
        ];
        assert_eq!(record.recover(&cards), 0);
        assert!(record.book_for(19886489, None).is_none());
    }
    #[test]
    fn unique_counter_recovery_is_exact() {
        let lines = normalize(LOG.iter().map(|x| x.to_string()));
        let mut record = Record::default(); record.absorb(&lines, "");
        let card = sidecar::Counter { file: "A".into(), total_ms: 351002, words: 2077, covered: 0.0 };
        assert_eq!(record.recover(&[card]), 1);
        assert_eq!(record.book_for(19886489, None).unwrap().cde_key, "A");
    }
    #[test]
    fn upstream_splits_a_midnight_counter_increment_between_days() {
        let lines = normalize([
            LOG[0].replace("260906:192401", "261101:235950"),
            LOG[1].replace("260906:192404", "261101:235955").replace("IntervalTime:785", "IntervalTime:5000").replace("TotalTime:329785", "TotalTime:334000"),
            LOG[2].replace("260906:192425", "261102:000010").replace("IntervalTime:21217", "IntervalTime:15000").replace("TotalTime:351002", "TotalTime:349000"),
        ]);
        let mut record = Record::default(); record.absorb(&lines, "");
        assert_eq!(record.sittings.len(), 2);
        assert_eq!(record.sittings[0].started_at.get(..10), Some("2026-11-01"));
        assert_eq!(record.sittings[1].started_at.get(..10), Some("2026-11-02"));
        assert_eq!(record.sittings.iter().map(|s| s.seconds).collect::<Vec<_>>(), vec![10, 10]);
        record.books.push(readinglog_native::record::Book {
            extent: 19886489, cde_key: "A".into(), title: "A Book".into(), ..Default::default()
        });
        let (_, hours) = machine_exports(&record, &serde_json::json!({})).unwrap();
        assert_eq!(hours.as_array().unwrap().len(), 2);
        assert_eq!(hours[0]["local_date"], "2026-11-01");
        assert_eq!(hours[0]["hour"], 23);
        assert_eq!(hours[0]["seconds"], 10);
        assert_eq!(hours[1]["local_date"], "2026-11-02");
        assert_eq!(hours[1]["hour"], 0);
        assert_eq!(hours[1]["seconds"], 10);
    }
    #[test]
    fn forbidden_output_is_rejected_without_creating_it() {
        let candidate = Path::new("/Volumes/Kindle/__readinglog_must_not_create__");
        assert!(check_output(candidate, Path::new("/tmp/logs"), Path::new("/tmp/snapshot")).is_err());
        assert!(!candidate.exists());
    }
    #[test]
    fn delayed_buffered_counter_recovers_identity_without_adding_time() {
        let mut record = Record::default();
        record.absorb(&normalize(LOG.iter().map(|s| s.to_string())), "");
        let before = record.sittings.clone();
        let delayed = "260906:221627 cvm[6144]: E BufferedLogger:Error::PageStartPos:HTMLPosition:7731725,IntervalTime:600009,TotalTime:999999,TotalWords:7777,EndPos:HTMLPosition:19886489,NextTOCEntryPosition:HTMLPosition:7800000,EndPos:HTMLPosition:7800000,PageStartPos:HTMLPosition:7731725,IntervalTime:46547,GlobalTime:999999,EndPos:HTMLPosition:19886489;";
        assert!(normalize([delayed.to_string()]).is_empty());
        let identities = buffered_counters(delayed);
        assert_eq!(identities, vec![(19886489, 999999, 7777)]);
        let cards = [sidecar::Counter { file: "known-book".into(), total_ms: 999999, words: 7777, covered: 0.0 }];
        assert_eq!(recover_buffered(&mut record, &cards, &identities), 1);
        assert_eq!(record.sittings, before);
        assert_eq!(record.sittings[0].seconds, 22);
        assert_eq!(record.book_for(19886489, None).unwrap().cde_key, "known-book");
    }
    #[test]
    fn buffered_identity_still_refuses_counter_collisions() {
        let mut record = Record::default();
        record.absorb(&normalize(LOG.iter().map(|s| s.to_string())), "");
        let cards = [
            sidecar::Counter { file: "A".into(), total_ms: 999999, words: 7777, covered: 0.0 },
            sidecar::Counter { file: "B".into(), total_ms: 999999, words: 7777, covered: 0.0 },
        ];
        assert_eq!(recover_buffered(&mut record, &cards, &[(19886489, 999999, 7777)]), 0);
        assert!(record.book_for(19886489, None).is_none());
    }
    #[test]
    fn machine_hours_preserve_upstream_allocation_and_do_not_spread_wall_time() {
        let mut record = Record::default();
        record.sittings.push(log::sitting::Sitting {
            started_at: "2026-11-01T18:00:00".into(),
            ended_at: "2026-11-01T19:59:00".into(),
            end_position: 123,
            seconds: 60,
            hours: vec![(18, 10), (19, 50)],
            start_counter_ms: Some(1000),
            end_counter_ms: Some(61000),
            start_words: Some(100), end_words: Some(200),
            ..Default::default()
        });
        record.books.push(readinglog_native::record::Book {
            extent: 123, cde_key: "A".into(), title: "A Book".into(), ..Default::default()
        });
        let (sessions, buckets) = machine_exports(&record, &serde_json::json!({"snapshot": "copied"})).unwrap();
        assert_eq!(sessions[0]["hours"], serde_json::json!([[18, 10], [19, 50]]));
        assert_eq!(buckets[0]["seconds"], 10);
        assert_eq!(buckets[1]["seconds"], 50);
        assert_eq!(buckets[0]["source_counter_evidence"][0]["book_extent"], 123);
        assert_eq!(buckets[0]["clock_verified"], false);
        assert!(buckets[0]["utc_offset_seconds"].is_null());
    }
    #[test]
    fn machine_hours_reject_invalid_allocation_and_estimates_for_upload() {
        let mut record = Record::default();
        record.books.push(readinglog_native::record::Book {
            extent: 123, cde_key: "A".into(), title: "A Book".into(), ..Default::default()
        });
        record.sittings.push(log::sitting::Sitting {
            started_at: "2026-11-01T18:00:00".into(), ended_at: "2026-11-01T18:01:00".into(),
            end_position: 123, seconds: 60, hours: vec![(18, 59)], ..Default::default()
        });
        let (sessions, buckets) = machine_exports(&record, &serde_json::json!({})).unwrap();
        assert_eq!(sessions[0]["hour_allocation_valid"], false);
        assert_eq!(buckets.as_array().unwrap().len(), 0);
        record.sittings[0].hours = vec![(18, 60)];
        record.sittings[0].measure = log::sitting::Measure::Awake;
        let (sessions, buckets) = machine_exports(&record, &serde_json::json!({})).unwrap();
        assert_eq!(sessions[0]["hour_allocation_valid"], true);
        assert_eq!(sessions[0]["measure"], "awake");
        assert_eq!(buckets.as_array().unwrap().len(), 0);
    }
}
