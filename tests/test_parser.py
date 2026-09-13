import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from threads_crawler import parser
from threads_crawler.exporter import write

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
HTML = (FIXTURES / "profile.html").read_text(encoding="utf-8")
GRAPHQL = json.loads((FIXTURES / "graphql_page.json").read_text(encoding="utf-8"))


def test_extracts_both_posts_from_html():
    posts = parser.extract_posts(HTML)
    assert [p.id for p in posts] == ["3456789012345678901", "3456789012345678902"]


def test_post_fields_are_mapped():
    post = parser.extract_posts(HTML)[0]
    assert post.author.username == "example_user"
    assert post.author.is_verified is True
    assert post.text == "今天測試 ThreadsCrawler，抓得到文章 ✅"
    assert post.url == "https://www.threads.com/@example_user/post/C9xAbCdEfGh"
    assert post.like_count == 1234
    assert post.reply_count == 42
    assert post.repost_count == 7
    assert post.quote_count == 3
    assert post.link_url == "https://example.com/article"
    assert post.published_at.year == 2024
    assert post.is_reply is False


def test_picks_the_largest_image_candidate():
    post = parser.extract_posts(HTML)[0]
    assert [(m.kind, m.url) for m in post.media] == [
        ("image", "https://cdn.example.com/large.jpg")
    ]


def test_video_and_reply_metadata():
    reply = parser.extract_posts(HTML)[1]
    assert reply.is_reply is True
    assert reply.reply_to == "example_user"
    assert reply.media[0].kind == "video"


def test_malformed_script_blocks_are_skipped():
    # The fixture contains a deliberately truncated JSON block.
    assert len(list(parser.iter_embedded_json(HTML))) == 3


def test_tokens_and_ids():
    assert parser.extract_lsd_token(HTML) == "AbCdEf123456"
    assert parser.extract_user_id(HTML, "example_user") == "314159"


def test_graphql_response_parses_the_same_way():
    posts = parser.extract_posts(GRAPHQL)
    assert len(posts) == 1
    assert posts[0].author.username == "example_user"
    assert parser.extract_page_info(GRAPHQL) == ("CURSOR_B", False)


def test_page_info_from_html():
    assert parser.extract_page_info(list(parser.iter_embedded_json(HTML))) == (
        "CURSOR_A",
        True,
    )


def test_duplicates_are_collapsed():
    combined = list(parser.iter_embedded_json(HTML)) + [GRAPHQL]
    assert len(parser.extract_posts(combined)) == 2


def test_json_nested_as_a_string_is_followed():
    inner = json.dumps({"thread_items": [{"post": {
        "pk": "1", "code": "abc", "caption": {"text": "hi"},
        "user": {"username": "u", "pk": "9"}, "text_post_app_info": {},
    }}]})
    assert parser.extract_posts({"payload": inner})[0].text == "hi"


def test_exports(tmp_path):
    posts = parser.extract_posts(HTML)
    for fmt, needle in (("json", '"like_count": 1234'), ("jsonl", '"id"'), ("csv", "example_user")):
        out = tmp_path / f"out.{fmt}"
        assert write(posts, fmt, str(out)) == 2
        assert needle in out.read_text(encoding="utf-8")
