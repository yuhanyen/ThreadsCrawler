"""Command line interface for ThreadsCrawler."""

from __future__ import annotations

import argparse
import sys

from . import exporter
from .client import ThreadsClient, ThreadsError


def _warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="threads-crawler",
        description="Scrape public posts from threads.com.",
    )
    ap.add_argument("target", help="a @username, a profile URL, or a post URL/shortcode")
    ap.add_argument("-n", "--limit", type=int, default=50, help="max posts (default: 50)")
    ap.add_argument(
        "-m",
        "--mode",
        choices=("auto", "http", "browser"),
        default="auto",
        help="auto: HTTP first, browser if it comes up short (default)",
    )
    ap.add_argument("-f", "--format", choices=("json", "jsonl", "csv"), default="json")
    ap.add_argument("-o", "--output", help="output file (default: stdout)")
    ap.add_argument("--cookies", default="", help="cookie string or cookies.txt path for logged-in access")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between requests (default: 1.5)")
    ap.add_argument("--doc-id", default="", help="override the GraphQL persisted-query id")
    ap.add_argument("--raw", action="store_true", help="include the raw payload for each post")
    ap.add_argument("--headful", action="store_true", help="browser mode: show the window")
    return ap


def _is_post_target(target: str) -> bool:
    return "/post/" in target


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    client_kwargs = {"cookie": args.cookies, "delay": args.delay}
    if args.doc_id:
        client_kwargs["doc_id"] = args.doc_id

    try:
        if _is_post_target(args.target):
            client = ThreadsClient(**client_kwargs)
            posts = client.fetch_post(args.target, keep_raw=args.raw)
        else:
            posts = []
            if args.mode in ("auto", "http"):
                client = ThreadsClient(**client_kwargs)
                try:
                    posts = list(
                        client.crawl_profile(
                            args.target,
                            limit=args.limit,
                            keep_raw=args.raw,
                            on_warning=_warn,
                        )
                    )
                except ThreadsError as exc:
                    if args.mode == "http":
                        raise
                    _warn(f"HTTP mode failed ({exc}); falling back to the browser")

            if args.mode == "browser" or (args.mode == "auto" and len(posts) < args.limit):
                from .browser import BrowserUnavailable, crawl_profile

                try:
                    browser_posts = crawl_profile(
                        args.target,
                        limit=args.limit,
                        cookie=args.cookies,
                        headless=not args.headful,
                        keep_raw=args.raw,
                        on_warning=_warn,
                    )
                except BrowserUnavailable as exc:
                    if args.mode == "browser":
                        raise
                    _warn(str(exc))
                    browser_posts = []
                if len(browser_posts) > len(posts):
                    posts = browser_posts
    except ThreadsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    count = exporter.write(posts, args.format, args.output, include_raw=args.raw)
    if args.output:
        print(f"wrote {count} posts to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
