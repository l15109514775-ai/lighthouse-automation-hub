"""HTTP boundary regression; no audio, worker execution, or external requests."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request


class IngestHeaderTests(unittest.TestCase):
    def test_rejected_requests_never_start_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {
                'SUBTITLE_INGEST_TOKEN': 'synthetic-ingest-test-token',
                'SUBTITLE_WORK_ROOT': temporary,
            }):
                spec = importlib.util.spec_from_file_location(
                    'isolated_ingest_test', Path(__file__).with_name('ingest.py'))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            server = module.ThreadingHTTPServer(('127.0.0.1', 0), module.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            token = {'X-Token': 'synthetic-ingest-test-token'}
            cases = [
                ('/ingest', {}, 403),
                ('/unknown', token, 404),
                ('/ingest', token, 400),
                ('/ingest', {**token, 'X-Claim': '!'}, 400),
                ('/ingest', {**token, 'X-Claim': 'bm90LWpzb24='}, 400),
            ]
            try:
                with patch.object(module.subprocess, 'run') as worker:
                    for path, headers, expected in cases:
                        with self.subTest(path=path, headers=list(headers), expected=expected):
                            request = urllib.request.Request(
                                f'http://127.0.0.1:{server.server_port}{path}',
                                data=b'', headers=headers, method='POST')
                            with self.assertRaises(urllib.error.HTTPError) as raised:
                                opener.open(request, timeout=5)
                            self.assertEqual(raised.exception.code, expected)
                            json.loads(raised.exception.read())
                            raised.exception.close()
                    worker.assert_not_called()
                self.assertEqual(list(Path(temporary).iterdir()), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
