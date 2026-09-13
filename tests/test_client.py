"""End-to-end test of the HTTP crawler against a local stand-in server."""

import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from threads_crawler import client as client_module
from threads_crawler.client import ThreadsClient, ThreadsError

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
HTML = (FIXTURES / "profile.html").read_text(encoding="utf-8")
GRAPHQL = (FIXTURES / "graphql_page.json").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def _send(self, status, body, content_type="text/html; charset=utf-8"):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/@example_user":
            self._send(200, HTML)
        elif self.path.startswith("/post/"):
            self._send(200, HTML)
        else:
            self._send(404, "not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        assert "lsd=AbCdEf123456" in body, "the LSD token must be forwarded"
        self._send(200, GRAPHQL, "application/json")


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    original_base, original_graphql = client_module.BASE_URL, client_module.GRAPHQL_URL
    client_module.BASE_URL = base
    client_module.GRAPHQL_URL = f"{base}/graphql/query"
    yield base
    client_module.BASE_URL, client_module.GRAPHQL_URL = original_base, original_graphql
    httpd.shutdown()


def test_crawl_profile_reads_the_first_page(server):
    posts = list(ThreadsClient(delay=0).crawl_profile("@example_user", limit=10))
    assert [p.author.username for p in posts] == ["example_user", "another_user"]
    assert posts[0].text.startswith("今天測試")


def test_limit_is_respected(server):
    assert len(list(ThreadsClient(delay=0).crawl_profile("example_user", limit=1))) == 1


def test_pagination_stops_when_graphql_has_no_new_posts(server):
    # The stub returns a post already seen on page 1 and has_next_page=False.
    warnings = []
    posts = list(
        ThreadsClient(delay=0).crawl_profile(
            "example_user", limit=50, on_warning=warnings.append
        )
    )
    assert len(posts) == 2
    assert warnings == []


def test_missing_profile_raises(server):
    with pytest.raises(ThreadsError, match="not found"):
        ThreadsClient(delay=0).fetch_profile_html("nobody_here")


def test_fetch_single_post(server):
    posts = ThreadsClient(delay=0).fetch_post(
        "https://www.threads.com/@example_user/post/C9xAbCdEfGh"
    )
    assert posts[0].code == "C9xAbCdEfGh"
