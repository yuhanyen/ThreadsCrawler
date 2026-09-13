"""HTTP crawler for public Threads profiles and posts."""

from __future__ import annotations

import json
import os
import random
import time
from http.cookies import SimpleCookie
from typing import Iterator

import requests

from . import parser
from .models import Post

BASE_URL = "https://www.threads.com"
GRAPHQL_URL = f"{BASE_URL}/graphql/query"

# Public web app id the Threads front-end sends on every GraphQL call.
IG_APP_ID = "238260118697367"

# Persisted-query id for the "posts" tab of a profile.  Meta rotates these
# without notice; override with THREADS_PROFILE_DOC_ID when pagination starts
# returning errors (see README for how to read the current one off the site).
DEFAULT_PROFILE_DOC_ID = os.environ.get("THREADS_PROFILE_DOC_ID", "7357407591006790")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class ThreadsError(RuntimeError):
    pass


class RateLimited(ThreadsError):
    pass


def _clean_username(username: str) -> str:
    username = username.strip()
    if username.startswith("http"):
        username = username.rstrip("/").rsplit("/", 1)[-1]
    return username.lstrip("@")


def post_code_from_url(value: str) -> str:
    """Accept a full post URL or a bare shortcode and return the shortcode."""
    value = value.strip().rstrip("/")
    if "/post/" in value:
        return value.rsplit("/post/", 1)[-1].split("?")[0].split("/")[0]
    return value.lstrip("@")


class ThreadsClient:
    """Fetches Threads content over plain HTTP, no browser required.

    Public profiles render server-side, so the first page of posts comes out of
    the HTML itself.  Deeper pages come from the same GraphQL endpoint the web
    app uses.
    """

    def __init__(
        self,
        cookie: str = "",
        delay: float = 1.5,
        timeout: float = 30.0,
        max_retries: int = 3,
        doc_id: str = DEFAULT_PROFILE_DOC_ID,
    ) -> None:
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.doc_id = doc_id
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
            }
        )
        cookie = cookie or os.environ.get("THREADS_COOKIE", "")
        if cookie:
            self._load_cookie(cookie)

    def _load_cookie(self, cookie: str) -> None:
        if os.path.exists(cookie):
            with open(cookie, encoding="utf-8") as handle:
                cookie = handle.read()
        if "\t" in cookie:  # Netscape cookies.txt export
            for line in cookie.splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                fields = line.split("\t")
                if len(fields) >= 7:
                    self.session.cookies.set(fields[5], fields[6], domain=fields[0])
            return
        jar = SimpleCookie()
        jar.load(cookie.strip())
        for key, morsel in jar.items():
            self.session.cookies.set(key, morsel.value, domain=".threads.com")

    def _sleep(self) -> None:
        if self.delay > 0:
            time.sleep(self.delay * random.uniform(0.8, 1.3))

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
            except requests.RequestException as exc:  # network hiccup
                last_error = exc
            else:
                if response.status_code == 429:
                    raise RateLimited(
                        "Threads returned HTTP 429 — slow down (--delay) or "
                        "authenticate with --cookies."
                    )
                if response.status_code in (500, 502, 503, 504):
                    last_error = ThreadsError(f"HTTP {response.status_code}")
                else:
                    return response
            time.sleep(2 ** attempt)
        raise ThreadsError(f"request to {url} failed: {last_error}")

    # ------------------------------------------------------------------ pages

    def fetch_profile_html(self, username: str) -> str:
        username = _clean_username(username)
        response = self._request("GET", f"{BASE_URL}/@{username}")
        if response.status_code == 404:
            raise ThreadsError(f"profile @{username} not found")
        if response.status_code != 200:
            raise ThreadsError(
                f"profile @{username} returned HTTP {response.status_code}"
            )
        return response.text

    def fetch_post(self, url_or_code: str, keep_raw: bool = False) -> list[Post]:
        """Fetch a single post plus the replies rendered with it."""
        code = post_code_from_url(url_or_code)
        response = self._request("GET", f"{BASE_URL}/post/{code}")
        if response.status_code != 200:
            raise ThreadsError(f"post {code} returned HTTP {response.status_code}")
        return parser.extract_posts(response.text, keep_raw=keep_raw)

    # --------------------------------------------------------------- graphql

    def _graphql(self, html: str, user_id: str, cursor: str) -> dict:
        lsd = parser.extract_lsd_token(html)
        if not lsd:
            raise ThreadsError("could not read the LSD token from the profile page")
        variables = {
            "userID": user_id,
            "first": 25,
            "after": cursor,
            "before": None,
            "last": None,
            "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": bool(
                self.session.cookies
            ),
            "__relay_internal__pv__BarcelonaIsThreadContextHeaderEnabledrelayprovider": False,
            "__relay_internal__pv__BarcelonaIsThreadContextHeaderFollowButtonEnabledrelayprovider": False,
        }
        response = self._request(
            "POST",
            GRAPHQL_URL,
            headers={
                "X-IG-App-ID": IG_APP_ID,
                "X-FB-LSD": lsd,
                "Content-Type": "application/x-www-form-urlencoded",
                "Sec-Fetch-Site": "same-origin",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/",
            },
            data={
                "lsd": lsd,
                "doc_id": self.doc_id,
                "variables": json.dumps(variables),
            },
        )
        if response.status_code != 200:
            raise ThreadsError(f"GraphQL returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ThreadsError(f"GraphQL returned non-JSON: {exc}") from exc
        if payload.get("errors"):
            raise ThreadsError(f"GraphQL errors: {payload['errors']}")
        return payload

    # ----------------------------------------------------------------- crawl

    def crawl_profile(
        self,
        username: str,
        limit: int = 50,
        keep_raw: bool = False,
        on_warning=None,
    ) -> Iterator[Post]:
        """Yield up to ``limit`` posts authored by ``username``.

        The first batch comes from the server-rendered page; further batches
        come from GraphQL.  If GraphQL is unavailable (rotated ``doc_id``,
        login wall) the already-scraped posts are still returned and a warning
        is reported through ``on_warning``.
        """
        username = _clean_username(username)
        html = self.fetch_profile_html(username)
        seen: set[str] = set()
        count = 0

        for post in parser.extract_posts(html, keep_raw=keep_raw):
            if post.id in seen:
                continue
            seen.add(post.id)
            count += 1
            yield post
            if count >= limit:
                return

        user_id = parser.extract_user_id(html, username)
        if not user_id:
            if on_warning:
                on_warning("could not determine the numeric user id; stopping after page 1")
            return

        cursor = ""
        while count < limit:
            self._sleep()
            try:
                payload = self._graphql(html, user_id, cursor)
            except ThreadsError as exc:
                if on_warning:
                    on_warning(f"pagination stopped: {exc}")
                return

            new_posts = 0
            for post in parser.extract_posts(payload, keep_raw=keep_raw):
                if post.id in seen:
                    continue
                seen.add(post.id)
                new_posts += 1
                count += 1
                yield post
                if count >= limit:
                    return

            cursor, has_next = parser.extract_page_info(payload)
            if not has_next or not cursor or new_posts == 0:
                return
