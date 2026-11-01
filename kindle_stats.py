#!/usr/bin/env python3
"""Read native Kindle counters on the Mac; never write to the Kindle.

KRDS decoding is provided by John Howell / kindle-reading-dashboard (GPLv3).
This wrapper deliberately does not invoke the upstream CLI, which writes JSON
next to its input. It imports only the decoder and keeps exports on this Mac.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

BASE = Path(__file__).resolve().parent
METRICS = {".azw3f", ".mbs", ".yjf"}
SIDECARS = METRICS | {".azw3r", ".mbp1", ".yjr"}
ZONE = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
NOTE = "Kindle 计时模型的累计读数，未经实际计时校验；不是完整历史或每日阅读账本。"


def now():
    return dt.datetime.now(ZONE)


def json_write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def csv_write(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def under(path, parent):
    return path.resolve().is_relative_to(parent.resolve())


def snapshot_device(mount):
    mount = mount.resolve()
    if not (mount / "documents").is_dir():
        raise ValueError(f"没有识别到 Kindle documents 目录：{mount}")
    if under(BASE, mount):
        raise ValueError("工具目录不能位于 Kindle 中。")
    captured = now()
    folder = BASE / "snapshots" / captured.strftime("%Y%m%d-%H%M%S-%f")
    folder.mkdir(parents=True, exist_ok=False)
    paths = []
    for path in (mount / "documents").rglob("*"):
        if not path.is_file() or path.is_symlink() or path.name.startswith("._"):
            continue
        lower = path.name.lower()
        is_dump = any(word in lower for word in ("all_system_logs", "system_logs", "dumpmessages", "dump_logs"))
        if path.suffix.lower() in SIDECARS or (is_dump and path.suffix.lower() in {".txt", ".log", ".gz", ".zip"}):
            paths.append(path)
    for relative in ("metadata.calibre", "system/version.txt", "system/readingstreams/readingstreams.db"):
        path = mount / relative
        if path.is_file() and not path.is_symlink():
            paths.append(path)
    logbackup = mount / "system/logbackup"
    if logbackup.is_dir():
        paths.extend(path for path in logbackup.rglob("*") if path.is_file() and not path.is_symlink() and not path.name.startswith("._"))
    manifest = []
    for path in sorted(set(paths)):
        if not under(path, mount):
            continue
        relative = path.relative_to(mount)
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        manifest.append({"path": str(relative), "bytes": target.stat().st_size,
                         "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    json_write(folder / "manifest.json", {"captured_at": captured.isoformat(), "timezone": "Asia/Shanghai",
               "source": "USB mass storage (read-only copy)", "files": manifest})
    (BASE / "latest-snapshot.txt").write_text(str(folder) + "\n", encoding="utf-8")
    return folder


def metadata_lookup(snapshot, warnings):
    path = snapshot / "metadata.calibre"
    if not path.exists():
        warnings.append("没有 metadata.calibre，书名将使用文件名。")
        return {}, {}
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError("预期 Calibre 元数据为数组")
    except (OSError, ValueError) as error:
        warnings.append(f"Calibre 元数据读取失败：{error}")
        return {}, {}
    by_path, by_stem = {}, {}
    for record in records:
        if not isinstance(record, dict) or not record.get("lpath"):
            continue
        path = Path(record["lpath"])
        by_path[path.with_suffix("").as_posix()] = record
        by_stem.setdefault(path.stem, []).append(record)
    return by_path, by_stem


class WarningCollector(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def decoder():
    path = BASE / "vendor/kindle-reading-dashboard/reading-metadata/scripts/krds.py"
    spec = importlib.util.spec_from_file_location("kindle_krds", path)
    module = importlib.util.module_from_spec(spec)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old
    return module.KindleReaderDataStore


def parse_snapshot(snapshot):
    warnings, errors, rows, events = [], [], [], []
    by_path, by_stem = metadata_lookup(snapshot, warnings)
    decode = decoder()
    logger = logging.getLogger("kindle_stats_decoder")
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.handlers.clear()
    handler = WarningCollector()
    logger.addHandler(handler)
    files = sorted(path for path in (snapshot / "documents").rglob("*")
                   if path.is_file() and not path.name.startswith("._") and path.suffix.lower() in METRICS)
    for path in files:
        relative = path.relative_to(snapshot / "documents")
        try:
            data = decode(logger, path.read_bytes()).deserialize()
            model = data.get("timer.model")
            if not isinstance(model, dict):
                errors.append({"source": str(relative), "error": "timer.model 缺失或未完整解析"})
                continue
            counter = model.get("totalTime")
            if not isinstance(counter, int) or isinstance(counter, bool) or counter < 0:
                raise ValueError("totalTime 不是非负整数")
            key = relative.parent.with_suffix("").as_posix() if relative.parent.suffix == ".sdr" else relative.with_suffix("").as_posix()
            record = by_path.get(key)
            if not record and len(by_stem.get(path.stem, [])) == 1:
                record = by_stem[path.stem][0]
            record = record or {}
            title = record.get("title") or path.stem
            authors = record.get("authors") or []
            if isinstance(authors, str):
                authors = [authors]
            history = data.get("page.history.store")
            if not isinstance(history, list):
                history = []
                warnings.append(f"{title}：page.history.store 缺失或未解析。")
            valid_events = [event for event in history if isinstance(event, dict) and event.get("time")]
            rows.append({"书名": title, "作者": "、".join(map(str, authors)), "计时模型累计毫秒": counter,
                         "计时模型累计分钟": round(counter / 60000, 4), "模型累计词数": model.get("totalWords", ""),
                         "模型记录的比例": model.get("totalPercent", ""), "翻页历史事件数": len(valid_events),
                         "书籍键": key, "数据源": str(relative), "口径说明": NOTE})
            for event in valid_events:
                events.append({"书名": title, "书籍键": key, "事件时间_Asia_Shanghai": event["time"],
                               "位置": event.get("position", ""), "数据源": str(relative),
                               "口径说明": "翻页事件时间，不等于该页阅读时长。"})
        except Exception as error:
            errors.append({"source": str(relative), "error": f"{type(error).__name__}: {error}"})
    warnings.extend(handler.messages)
    # Two format sidecars for one book must not silently double its total.
    groups = {}
    for row in rows:
        groups.setdefault(row["书籍键"], []).append(row)
    rows = []
    for key, choices in groups.items():
        choices.sort(key=lambda row: row["计时模型累计毫秒"], reverse=True)
        rows.append(choices[0])
        if len(choices) > 1:
            warnings.append(f"{choices[0]['书名']}：存在多个计时文件；仅显示最高计数，未求和。")
    rows.sort(key=lambda row: (-row["计时模型累计毫秒"], row["书名"]))
    manifest_path = snapshot / "manifest.json"
    captured_at = None
    if manifest_path.is_file():
        captured_at = json.loads(manifest_path.read_text(encoding="utf-8")).get("captured_at")
    pending_count = None
    db = snapshot / "system/readingstreams/readingstreams.db"
    if db.is_file():
        try:
            with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
                pending_count = connection.execute("SELECT count(*) FROM pendingmessages").fetchone()[0]
        except sqlite3.Error as error:
            warnings.append(f"readingstreams 数据库无法读取：{error}")
    log_paths = [str(path.relative_to(snapshot)) for path in snapshot.rglob("*")
                 if path.is_file() and ("logbackup" in path.parts or "all_system_logs" in path.name.lower() or "dumpmessages" in path.name.lower())]
    summary = {"generated_at": now().isoformat(), "snapshot": str(snapshot), "snapshot_captured_at": captured_at,
               "timezone": "Asia/Shanghai", "metrics_files_found": len(files), "books_with_model": len(rows),
               "model_total_time_ms": sum(row["计时模型累计毫秒"] for row in rows),
               "page_history_events": len(events), "daily_reading_status": "missing",
               "daily_reading_reason": "没有经验证的逐日阅读片段；累计模型、最后阅读日期和文件修改时间均不能分配为每日时长。",
               "daily_reading_rows": 0, "missing_is_not_zero": True, "pending_readingstream_messages": pending_count,
               "copied_log_candidates": log_paths, "counter_meaning": NOTE, "warnings": warnings, "parse_errors": errors,
               "source_project": "https://github.com/zevisvei/kindle-reading-dashboard",
               "upstream_scripts_run_separately": ["reading_stats.py", "page_history.py"]}
    return rows, events, summary


def report(rows, summary, export):
    esc = lambda value: html.escape(str(value), quote=True)
    cells = "".join("<tr>" + "".join(f"<td>{esc(value)}</td>" for value in
                    (row["书名"], row["作者"], f"{row['计时模型累计毫秒'] / 60000:.2f}", row["翻页历史事件数"])) + "</tr>" for row in rows)
    problems = "".join(f"<li>{esc(message)}</li>" for message in summary["warnings"])
    if summary["parse_errors"]:
        problems += "".join(f"<li>{esc(item['source'])}：{esc(item['error'])}</li>" for item in summary["parse_errors"])
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kindle 原生阅读统计检查</title><style>body{{font:16px/1.65 -apple-system,BlinkMacSystemFont,\"PingFang SC\",sans-serif;color:#20302a;background:#f7f8f5;margin:40px auto;padding:0 24px;max-width:1100px}}h1{{font-size:30px}}.notice{{background:#fff4d6;padding:18px 22px;border-radius:12px}}table{{width:100%;border-collapse:collapse;background:white;font-size:14px}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #ddd;vertical-align:top}}td:first-child{{max-width:540px}}a{{color:#17684c}}small{{color:#65726b}}code{{overflow-wrap:anywhere}}</style>
<h1>Kindle 原生阅读统计检查</h1><p><small>副本采集时间：{esc(summary['snapshot_captured_at'] or '未知')} · 时区：Asia/Shanghai</small></p>
<div class="notice"><b>每日阅读时长：数据缺失，不能还原为每日账本。</b><br>当前读到 {len(rows)} 本书的计时模型，翻页历史共 {summary['page_history_events']} 条。空记录不是每日阅读 0 分钟。{esc(NOTE)}</div>
<p>以下累计数值不分配到今天，也不按最后阅读日期归档。完整的逐日统计需要带有时间和书籍身份的原始阅读片段。</p>
<table><thead><tr><th>书名</th><th>作者</th><th>模型累计分钟</th><th>翻页历史条数</th></tr></thead><tbody>{cells}</tbody></table>
<p><a href="book-model-time.csv">下载累计模型 CSV</a> · <a href="page-events.csv">翻页事件 CSV</a> · <a href="daily-reading.csv">每日时长 CSV（目前仅表头）</a> · <a href="summary.json">检查结果 JSON</a></p>
<p>已复制候选日志：{len(summary['copied_log_candidates'])} 个。readingstreams 待发送记录：{esc(summary['pending_readingstream_messages'])}。</p>
{('<ul>' + problems + '</ul>') if problems else '<p>计时文件解析未报错。</p>'}
<small>解析来源：<a href="https://github.com/zevisvei/kindle-reading-dashboard">kindle-reading-dashboard / KRDS</a>。本工具不访问微信读书账号，不上传时长。副本与报告保存在 Mac：<code>{esc(export)}</code></small></html>"""


def main():
    parser = argparse.ArgumentParser(description="只读复制 Kindle 原生计时文件，在 Mac 解析并导出；不上传。")
    parser.add_argument("--mount", type=Path, default=Path("/Volumes/Kindle"), help="Kindle 挂载位置")
    parser.add_argument("--snapshot", type=Path, help="仅解析已有 Mac 副本，不访问设备")
    args = parser.parse_args()
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    snapshot = args.snapshot.expanduser().resolve() if args.snapshot else snapshot_device(args.mount.expanduser())
    if under(snapshot, args.mount.expanduser()) or str(snapshot).startswith("/Volumes/"):
        parser.error("--snapshot 必须是 Mac 上的副本，不能直接使用设备目录。")
    if not (snapshot / "documents").is_dir():
        parser.error(f"副本里没有 documents 目录：{snapshot}")
    rows, events, summary = parse_snapshot(snapshot)
    export = BASE / "exports"
    export.mkdir(exist_ok=True)
    fields = ["书名", "作者", "计时模型累计毫秒", "计时模型累计分钟", "模型累计词数", "模型记录的比例", "翻页历史事件数", "书籍键", "数据源", "口径说明"]
    csv_write(export / "book-model-time.csv", fields, rows)
    csv_write(export / "page-events.csv", ["书名", "书籍键", "事件时间_Asia_Shanghai", "位置", "数据源", "口径说明"], events)
    csv_write(export / "daily-reading.csv", ["日期_Asia_Shanghai", "书名", "书籍键", "阅读毫秒", "来源", "置信说明"], [])
    json_write(export / "summary.json", summary)
    (export / "report.html").write_text(report(rows, summary, export), encoding="utf-8")
    print(f"已解析 {len(rows)} 本书的计时模型，翻页历史 {len(events)} 条。")
    print("每日阅读时长：数据缺失（不是 0 分钟），没有生成虚构日期或上传数据。")
    print(f"报告：{export / 'report.html'}")
    print(f"数据副本：{snapshot}")
    if summary["parse_errors"]:
        print(f"解析失败：{len(summary['parse_errors'])} 个；请查看 summary.json。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"操作未完成：{error}", file=sys.stderr)
        raise SystemExit(1)
