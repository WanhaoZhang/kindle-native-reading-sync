import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from local_app import LocalApp, make_handler


class FakeEngine:
    def __init__(self):
        self.commits = []

    def status(self):
        return {'authenticated': False, 'observed_seconds': 20, 'confirmed_seconds': 0,
                'pending_seconds': 20}

    def list_books(self):
        return [{'local_book_id': 'local-A', 'title': '测试书', 'days': [
            {'date': '2026-11-01', 'observed_seconds': 20, 'confirmed_seconds': 0, 'pending_seconds': 20}]}]

    def commit(self, plan_id, confirmed=False):
        self.commits.append((plan_id, confirmed))
        return {'plan_id': plan_id, 'state': 'confirmed'}


class LocalAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        (self.base / 'local-web').mkdir()
        (self.base / 'local-web/index.html').write_text('<html>local tool</html>')
        (self.base / '.weread').mkdir()
        (self.base / '.weread/auth.json').write_text('SECRET_MUST_NOT_BE_SERVED')
        self.engine = FakeEngine()
        self.app = LocalApp(self.base, self.engine)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, value=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        body = json.dumps(value) if value is not None else None
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def mutation_headers(self):
        return {'Origin': f'http://127.0.0.1:{self.port}', 'X-CSRF-Token': self.app.csrf,
                'Content-Type': 'application/json'}

    def test_private_files_and_traversal_are_not_served(self):
        for path in ['/.weread/auth.json', '/../.weread/auth.json', '/%2e%2e/.weread/auth.json']:
            code, data = self.request('GET', path)
            self.assertEqual(code, 404)
            self.assertNotIn(b'SECRET', data)

    def test_external_host_and_cross_origin_post_are_rejected(self):
        code, _ = self.request('GET', '/api/status', headers={'Host': f'attacker.invalid:{self.port}'})
        self.assertEqual(code, 403)
        headers = self.mutation_headers()
        headers['Origin'] = 'https://external.invalid'
        code, _ = self.request('POST', '/api/commit', {'plan_id': 'p', 'confirmed': True}, headers)
        self.assertEqual(code, 403)
        self.assertEqual(self.engine.commits, [])

    def test_commit_needs_both_csrf_and_explicit_confirm(self):
        code, _ = self.request('POST', '/api/commit', {'plan_id': 'p', 'confirmed': True},
                               {'Origin': f'http://127.0.0.1:{self.port}', 'Content-Type': 'application/json'})
        self.assertEqual(code, 403)
        code, _ = self.request('POST', '/api/commit', {'plan_id': 'p', 'confirmed': False}, self.mutation_headers())
        self.assertEqual(code, 400)
        self.assertEqual(self.engine.commits, [])

    def test_only_confirmed_post_dispatches_to_fake_engine(self):
        code, raw = self.request('POST', '/api/commit', {'plan_id': 'p', 'confirmed': True}, self.mutation_headers())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['data']['state'], 'confirmed')
        self.assertEqual(self.engine.commits, [('p', True)])

    def test_state_uses_accumulated_days_and_never_commits_on_get(self):
        code, raw = self.request('GET', '/api/status')
        data = json.loads(raw)['data']
        self.assertEqual(code, 200)
        self.assertEqual(data['daily'][0]['阅读秒数'], 20)
        self.assertEqual(data['coverage']['start'], '2026-11-01')
        self.assertEqual(self.engine.commits, [])


if __name__ == '__main__':
    unittest.main()
