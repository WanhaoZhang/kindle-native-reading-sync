#!/usr/bin/env python3
"""Loopback-only UI for the local Kindle reader log and WeRead sync workflow."""
import argparse
import csv
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from weread_readonly import BridgeError

BASE = Path(__file__).resolve().parent


class LocalApp:
    def __init__(self, base=BASE, engine=None):
        self.base = Path(base)
        if engine is None:
            from sync_engine import SyncEngine
            engine = SyncEngine(self.base)
        self.engine = engine
        self.lock = threading.RLock()
        self.csrf = secrets.token_urlsafe(32)
        self.login_process = None
        self.login_output = None
        self.qr_path = None
        self.login_started = None

    def json_file(self, relative, default=None):
        try:
            return json.loads((self.base / relative).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return default

    def login_status(self):
        process = self.login_process
        if process is None:
            return {'state': 'idle'}
        code = process.poll()
        if code is None:
            return {'state': 'waiting', 'qr_url': '/login-qr.png?v=' + str(self.login_started)
                    if self.qr_path and self.qr_path.is_file() else None,
                    'message': '请用微信扫描并在手机确认。'}
        if code == 0:
            return {'state': 'success', 'message': '微信读书连接成功。'}
        return {'state': 'error', 'message': '扫码未完成或已过期，请重新点击登录。'}

    def state(self):
        engine_state = self.engine.status()
        books = self.engine.list_books()
        summary = self.json_file('exports/summary.json', {})
        daily = []
        path = self.base / 'exports/daily-reading.csv'
        if path.is_file():
            with path.open(encoding='utf-8-sig', newline='') as stream:
                daily = list(csv.DictReader(stream))
        accumulated = []
        for book in books:
            for day in book.get('days', []):
                accumulated.append({'date': day.get('date'), 'title': book.get('title'),
                    'seconds': day.get('observed_seconds', 0),
                    'confirmed_seconds': day.get('confirmed_seconds', 0),
                    'pending_seconds': day.get('pending_seconds', 0),
                    'local_book_id': book.get('local_book_id'),
                    '日期_设备本地': day.get('date'), '书名': book.get('title'),
                    '阅读秒数': day.get('observed_seconds', 0),
                    '时区状态': '设备日志时区需经核实，未核实的批次不能同步'})
        if accumulated:
            daily = sorted(accumulated, key=lambda row: (row.get('date') or '', row.get('title') or ''), reverse=True)
        dates = sorted({row.get('日期_设备本地', '') for row in daily} - {''})
        return {'device': {'connected': (Path('/Volumes/Kindle') / 'documents').is_dir(),
                           'mount': '/Volumes/Kindle'},
                'auth': {'connected': bool(engine_state.get('authenticated')),
                         'login_state': self.login_status()['state']},
                'login': self.login_status(), 'engine': engine_state, 'books': books,
                'daily': daily, 'summary': summary,
                'coverage': {'start': dates[0] if dates else None,
                             'end': dates[-1] if dates else None, 'days': len(dates),
                             'warning': '仅统计保留日志。日志会覆盖，隔几天再导出不能保证完整；缺失日期不表示阅读为零。'}}

    def analyze(self, saved=False):
        command = [sys.executable, str(self.base / 'analyze_kindle.py')]
        if saved:
            try:
                snapshot = Path((self.base / 'latest-snapshot.txt').read_text().strip()).resolve()
            except OSError:
                raise ValueError('还没有保存的 Kindle 副本，请先连接设备导入。') from None
            if not snapshot.is_relative_to((self.base / 'snapshots').resolve()) or not snapshot.is_dir():
                raise ValueError('保存的副本路径无效。')
            command += ['--snapshot', str(snapshot)]
        elif not (Path('/Volumes/Kindle') / 'documents').is_dir():
            raise ValueError('尚未连接 Kindle。请先导出日志，再用 USB 接回 Mac。')
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise ValueError('Kindle 分析未完成；原始副本已保留，请查看本地分析工具。')
        imported = self.engine.import_results()
        return {'import': imported, 'state': self.state()}

    def start_login(self):
        if self.login_process and self.login_process.poll() is None:
            return self.login_status()
        private = self.base / '.weread'
        private.mkdir(mode=0o700, exist_ok=True)
        private.chmod(0o700)
        self.login_started = time.time_ns()
        self.qr_path = private / ('ui-login-' + str(self.login_started) + '.png')
        log_path = private / 'ui-login-worker.log'
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        if self.login_output:
            self.login_output.close()
        self.login_output = os.fdopen(fd, 'w')
        python = self.base / '.venv-bridge/bin/python'
        if not python.is_file():
            raise ValueError('本地扫码运行环境不存在，请运行项目安装步骤。')
        command = [str(python), str(self.base / 'weread_readonly.py'), 'login', '--online',
                   '--wait-seconds', '300', '--qr-png', str(self.qr_path)]
        if (private / 'auth.json').exists():
            command.append('--replace-auth')
        self.login_process = subprocess.Popen(command, stdout=self.login_output,
                                              stderr=self.login_output, cwd=self.base)
        return self.login_status()

    def post(self, path, body):
        with self.lock:
            if path == '/api/import':
                return self.analyze()
            if path == '/api/import_saved':
                return self.analyze(saved=True)
            if path == '/api/search':
                query = body.get('query', '')
                if not isinstance(query, str) or not 1 <= len(query.strip()) <= 160:
                    raise ValueError('请输入书名或作者。')
                return {'candidates': self.engine.search_candidates(query.strip())}
            if path == '/api/mapping':
                local, remote = body.get('local_book_id'), body.get('weread_book_id')
                if not isinstance(local, str) or not isinstance(remote, str) or not local or not remote:
                    raise ValueError('请选择一本本地书籍和对应微信读书版本。')
                mapping = self.engine.save_mapping(local, remote, body.get('title'))
                return {'mapping': mapping, 'state': self.state()}
            if path == '/api/prepare':
                ids = body.get('local_book_ids')
                if ids is not None and (not isinstance(ids, list) or not all(isinstance(x, str) for x in ids)):
                    raise ValueError('选择的书籍无效。')
                return self.engine.prepare(ids, max_seconds=600)
            if path == '/api/commit':
                if body.get('confirmed') is not True or not isinstance(body.get('plan_id'), str):
                    raise ValueError('请先预览具体书籍、日期和时长，再确认提交。')
                return self.engine.commit(body['plan_id'], confirmed=True)
            if path == '/api/login':
                return self.start_login()
            raise KeyError('未知操作')


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'KindleLocal/1'

        def log_message(self, *_):
            pass  # Never log request bodies, tokens, book queries or credentials.

        def allowed_host(self):
            port = self.server.server_address[1]
            return self.headers.get('Host', '') in {f'127.0.0.1:{port}', f'localhost:{port}'}

        def response(self, code, content, mime='application/json; charset=utf-8'):
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False).encode('utf-8')
            elif isinstance(content, str):
                content = content.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if not self.allowed_host():
                return self.response(403, {'ok': False, 'error': '此工具仅接受本机访问。'})
            path = urllib.parse.urlsplit(self.path).path
            if path == '/api/health':
                return self.response(200, {'app': 'kindle-reading-local', 'version': 1})
            if path == '/api/bootstrap':
                return self.response(200, {'csrf_token': app.csrf})
            if path == '/':
                return self.response(200, (app.base / 'local-web/index.html').read_bytes(), 'text/html; charset=utf-8')
            if path == '/api/status':
                try:
                    return self.response(200, {'ok': True, 'data': app.state()})
                except Exception:
                    return self.response(500, {'ok': False, 'error': '本地状态暂时不可用，请查看项目文件。'})
            if path == '/api/books':
                return self.response(200, {'ok': True, 'data': {'books': app.engine.list_books()}})
            if path == '/login-qr.png' and app.qr_path and app.qr_path.is_file():
                return self.response(200, app.qr_path.read_bytes(), 'image/png')
            allowed_exports = {'/exports/daily-reading.csv', '/exports/book-model-time.csv', '/exports/summary.json'}
            if path in allowed_exports:
                file = app.base / path[1:]
                if file.is_file():
                    return self.response(200, file.read_bytes(), 'text/csv; charset=utf-8' if path.endswith('.csv') else 'application/json; charset=utf-8')
            return self.response(404, {'ok': False, 'error': '找不到此页面。'})

        def do_POST(self):
            port = self.server.server_address[1]
            origins = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}
            if (not self.allowed_host() or self.headers.get('Origin') not in origins
                    or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), app.csrf)):
                return self.response(403, {'ok': False, 'error': '请求校验失败，请从本机页面重新操作。'})
            if self.headers.get_content_type() != 'application/json':
                return self.response(415, {'ok': False, 'error': '请求格式不正确。'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536:
                    raise ValueError('请求大小不正确。')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError('请求格式不正确。')
                result = app.post(urllib.parse.urlsplit(self.path).path, body)
                self.response(200, {'ok': True, 'data': result})
            except (ValueError, KeyError) as error:
                self.response(400, {'ok': False, 'error': str(error)[:300]})
            except subprocess.TimeoutExpired:
                self.response(504, {'ok': False, 'error': '分析超时，原始副本仍保存在本机。'})
            except BridgeError:
                self.response(502, {'ok': False, 'error': '微信读书连接未完成，请检查网络或重新扫码。'})
            except Exception:
                self.response(500, {'ok': False, 'error': '操作未完成。请保留现有数据，查看本地说明。'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='启动 Mac 本地 Kindle 阅读同步网页。')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args()
    app = LocalApp()
    if (app.base / 'log-results/hour_totals.json').is_file():
        try:
            app.engine.import_results()
        except (ValueError, OSError):
            print('保存的日志暂未导入，页面中可重新导入并查看状态。', flush=True)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app))
    except OSError:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/api/health', timeout=2) as response:
                same_app = json.load(response).get('app') == 'kindle-reading-local'
        except Exception:
            same_app = False
        if same_app:
            if args.open_browser:
                webbrowser.open(f'http://127.0.0.1:{args.port}')
            print(f'工具已经运行：http://127.0.0.1:{args.port}')
            return
        raise SystemExit(f'端口 {args.port} 已被占用，请关闭占用程序或指定 --port。')
    print(f'Kindle 阅读同步：http://127.0.0.1:{args.port}', flush=True)
    print('保留此窗口即可使用；按 Ctrl+C 关闭。', flush=True)
    if args.open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.login_process and app.login_process.poll() is None:
            app.login_process.terminate()
        if app.login_output:
            app.login_output.close()
        server.server_close()


if __name__ == '__main__':
    main()
