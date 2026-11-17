"""Shared test scaffolding: an isolated data dir, config path and time zone."""

import os
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def local_ts(text):
    """'2026-09-30 23:30' in the test time zone as a unix timestamp."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()


class IsolatedTestCase(unittest.TestCase):
    """Points the tracker at a throwaway data dir and config, in Europe/Warsaw."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cc-statusline-test-")
        self._saved = {k: os.environ.get(k) for k in ("CC_STATUSLINE_DATA_DIR", "CC_STATUSLINE_CONFIG", "TZ")}
        os.environ["CC_STATUSLINE_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["CC_STATUSLINE_CONFIG"] = os.path.join(self.tmp, "config.toml")
        os.environ["TZ"] = "Europe/Warsaw"
        time.tzset()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        time.tzset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, text):
        with open(os.environ["CC_STATUSLINE_CONFIG"], "w", encoding="utf-8") as f:
            f.write(text)


class Redirector:
    """A server on a free port that answers every request with `status` and Location `target` + path.

    It records each request's method, path and headers, to show what a client sent before it was redirected.
    """

    def __init__(self, target, status=302):
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _redirect(self):
                fake.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers)})
                self.send_response(status)
                self.send_header("Location", target.rstrip("/") + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _redirect

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
