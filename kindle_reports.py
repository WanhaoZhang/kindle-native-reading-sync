"""Merge retained-log daily statistics into the local report; no network access."""
import csv
import hashlib
import html
import json
from pathlib import Path


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def duration(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes} 分 {seconds:02d} 秒"


def merge_log_report(base, summary):
    export, results = base / "exports", base / "log-results"
    daily = read_csv(results / "daily_measured.csv")
    sessions = read_csv(results / "sessions.csv")
    estimates = read_csv(results / "daily_estimated.csv")
    models = read_csv(export / "book-model-time.csv")
    unmatched = [row for row in sessions if row["status"] == "unmatched"]
    validation_file = base / "log-validation.json"
    known = {}
    if validation_file.is_file():
        validation = json.loads(validation_file.read_text(encoding="utf-8"))
        known = {row["sha256"]: row.get("utc_offset_seconds") for row in validation.get("logs", [])}
    # Calibration applies to exact source bytes, not all future Kindle logs.
    log_files = [p for p in (base / "logs-import").rglob("*") if p.is_file() and not p.name.startswith("._")]
    calibrated = bool(log_files) and all(known.get(hashlib.sha256(p.read_bytes()).hexdigest()) == 28800 for p in log_files)
    timezone = "Asia/Shanghai（原日志 UTC 对照及试读已验证）" if calibrated else "Kindle 本地时钟（时区未完全核实）"
    translated = [{"日期_设备本地": row["date_kindle_local"], "书名": row["title"],
                   "书籍键": row["source_key"], "阅读秒数": row["seconds"],
                   "阅读用时": duration(row["seconds"]), "来源": "原生日志计时模型增量 counted",
                   "时区状态": timezone, "覆盖说明": "仅覆盖保留日志，缺失日期未知"} for row in daily]
    with (export / "daily-reading.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        fields = ["日期_设备本地", "书名", "书籍键", "阅读秒数", "阅读用时", "来源", "时区状态", "覆盖说明"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(translated)
    summary.update({"daily_reading_status": "partial" if daily else "missing",
                    "daily_reading_reason": "按书按日汇总了保留系统日志中的模型计数，无法推断日志之外的历史。",
                    "daily_reading_rows": len(daily), "log_sessions": len(sessions),
                    "unmatched_log_sessions": len(unmatched), "estimated_daily_rows": len(estimates),
                    "daily_timezone": timezone, "daily_timezone_calibrated": calibrated,
                    "measured_daily_seconds": sum(int(row["seconds"]) for row in daily),
                    "daily_reading_source": "huangziwei/readinglog native library with identity-only BufferedLogger adapter",
                    "uploaded_to_weread": False})
    sync_file = base / 'weread-sync-status.json'
    sync = json.loads(sync_file.read_text(encoding='utf-8')) if sync_file.is_file() else {}
    summary['weread_connection'] = sync
    summary['uploaded_to_weread'] = int(sync.get('uploaded_seconds', 0)) > 0
    (export / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    esc = lambda value: html.escape(str(value), quote=True)
    sync_message = sync.get('message') or '尚未登录微信读书，也未上传任何数据。'
    daily_cells = "".join(f"<tr><td>{esc(row['date_kindle_local'])}</td><td>{esc(row['title'])}</td><td>{duration(row['seconds'])}</td></tr>" for row in daily)
    session_cells = "".join(f"<tr><td>{esc(row['title'] or '书籍尚未关联')}</td><td>{esc(row['start_kindle_local'].replace('T', ' '))}</td><td>{esc(row['end_kindle_local'][11:])}</td><td>{duration(row['seconds'])}</td></tr>" for row in sessions)
    model_cells = "".join(f"<tr><td>{esc(row['书名'])}</td><td>{float(row['计时模型累计分钟']):.2f} 分钟</td></tr>" for row in models)
    content = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kindle 每本书每日阅读统计</title><style>
body{{font:16px/1.7 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;color:#24342d;background:#f6f8f4;max-width:1080px;margin:36px auto;padding:0 24px}}h1{{font-size:30px}}h2{{font-size:22px;margin-top:32px}}.notice{{background:#fff0ce;padding:18px 24px;border-radius:12px}}.total{{font-size:22px;color:#17684c}}table{{width:100%;border-collapse:collapse;background:white;font-size:14px}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #dce2db;vertical-align:top}}a{{color:#17684c}}small{{color:#65726b}}.scroll{{overflow-x:auto}}code{{overflow-wrap:anywhere}}</style>
<h1>Kindle 每本书每日阅读统计</h1>
<p>采集时间：{esc(summary['snapshot_captured_at'])}<br>时间基准：{esc(timezone)}</p>
<div class="notice"><b>已恢复保留日志中的逐日记录，覆盖仍不完整。</b><br>共 {len(sessions)} 段会话，{len(daily)} 条书籍/日期记录，{len(unmatched)} 段尚未关联。没有日志的日期是未知，不能当成 0 分钟；未将书籍累计总量归入今天。</div>
<p class="total">已关联的日志模型计时合计：{duration(summary['measured_daily_seconds'])}</p>
<h2>每本书、每天</h2><div class="scroll"><table><thead><tr><th>日期</th><th>书名</th><th>模型计时</th></tr></thead><tbody>{daily_cells}</tbody></table></div>
<p><a href="daily-reading.csv">下载中文每日统计 CSV</a> · <a href="../log-results/daily_measured.csv">原始日汇总</a> · <a href="summary.json">检查结果 JSON</a></p>
<h2>阅读会话</h2><div class="scroll"><table><thead><tr><th>书名</th><th>开始（设备时钟）</th><th>结束</th><th>模型计时</th></tr></thead><tbody>{session_cells}</tbody></table></div>
<p>计时来自 Kindle 阅读模型，可能漏计，也可能包含停留，不能当作眼睛实际阅读用时的精确测量。会话起止是日志事件边界，两者与模型起算边界并不总一致。</p>
<p>未关联会话和其他估算口径分别保存在 <a href="../log-results/unmatched_sessions.csv">unmatched_sessions.csv</a>、<a href="../log-results/daily_estimated.csv">daily_estimated.csv</a>，没有混入上表。</p>
<h2>微信读书同步</h2><p>{esc(sync_message)} 开源 E-Ink 离线接口有保留历史小时的实现，完整批量同步仍需逐项核验。<a href="../weread-sync-notes.md">查看技术说明</a>。</p>
<details><summary>另有 {len(models)} 本书的累计模型读数（不是每日用时）</summary><table><thead><tr><th>书名</th><th>模型累计</th></tr></thead><tbody>{model_cells}</tbody></table><p><a href="book-model-time.csv">下载累计模型 CSV</a></p></details>
<p><small>解析：<a href="https://github.com/huangziwei/readinglog">Reading Log</a> / <a href="https://github.com/zevisvei/kindle-reading-dashboard">KRDS</a>。副本、日志和结果均保存在本 Mac。</small></p></html>"""
    (export / "report.html").write_text(content, encoding="utf-8")
    print(f"中文每日统计已更新：{export / 'report.html'}")
