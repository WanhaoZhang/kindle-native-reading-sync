#!/usr/bin/env python3
"""Copy native Kindle counters/logs, then invoke the audited open-source parsers."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from kindle_reports import merge_log_report

BASE = Path(__file__).resolve().parent


def main():
    # The snapshot tool writes only into this Mac project directory.
    result = subprocess.run([sys.executable, str(BASE / "kindle_stats.py"), *sys.argv[1:]])
    if result.returncode:
        return result.returncode
    summary = json.loads((BASE / "exports/summary.json").read_text(encoding="utf-8"))
    snapshot = Path(summary["snapshot"]).resolve()
    logs = []
    for name in summary["copied_log_candidates"]:
        path = snapshot / name
        if path.suffix.lower() in {".txt", ".log", ".gz"}:
            logs.append(path)
    if not logs:
        print("\n尚无可解析的系统日志。请在 Kindle 搜索框执行 ;dm，接回 Mac 后再运行。")
        print("每日数据仍缺失；累计模型数据已导出。")
        return 0
    binary = BASE / "readinglog-cli/target/debug/kindle-readinglog-export"
    if not binary.is_file():
        print("日志已备份，但 ReadingLog 解析程序不存在；请参照 readinglog-cli/README.md 编译。", file=sys.stderr)
        return 2
    log_input = BASE / "logs-import" / snapshot.name
    log_input.mkdir(parents=True, exist_ok=True)
    for path in logs:
        # Preserve the snapshot-relative layout without reading arbitrary paths.
        relative = path.resolve().relative_to(snapshot)
        target = log_input / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # Keep previously exported logs as well; upstream deduplicates repeated dumps.
    result = subprocess.run([str(binary), "--logs", str(BASE / "logs-import"), "--snapshot", str(snapshot),
                             "--out", str(BASE / "log-results")])
    if result.returncode == 0:
        merge_log_report(BASE, summary)
    print("\n按书、按日的日志结果：", BASE / "log-results")
    print("日志日期采用 Kindle 本地时钟，时区需对照实际阅读时间确认；本工具没有上传数据。")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
