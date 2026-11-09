from unittest.mock import MagicMock

from moon_watcher.monitors import cache_warmer
from moon_watcher.monitors.cache_warmer import CacheWarmer

NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


def make_warmer():
    # skip set_site_url, it does real HEAD requests
    w = CacheWarmer()
    w.site_url = "https://example.com"
    return w


def urlset(*urls):
    items = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<urlset xmlns="{NS}">{items}</urlset>'.encode()


def index(*urls):
    items = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return f'<sitemapindex xmlns="{NS}">{items}</sitemapindex>'.encode()


def test_plain_sitemap():
    w = make_warmer()
    xml = urlset("https://example.com/a", "https://example.com/b")
    assert w._parse_sitemap(xml) == ["https://example.com/a", "https://example.com/b"]


def test_child_sitemap_on_other_host_is_not_fetched(monkeypatch):
    monkeypatch.setattr(cache_warmer.time, "sleep", lambda s: None)
    w = make_warmer()
    w._fetch_xml = MagicMock(return_value=urlset("https://example.com/ok"))

    xml = index("http://169.254.169.254/latest/meta-data", "https://example.com/sitemap-1.xml")
    urls = w._parse_sitemap(xml)

    assert urls == ["https://example.com/ok"]
    w._fetch_xml.assert_called_once_with("https://example.com/sitemap-1.xml")


def test_nesting_depth_is_limited(monkeypatch):
    monkeypatch.setattr(cache_warmer.time, "sleep", lambda s: None)
    w = make_warmer()
    # every child is an index pointing at itself
    w._fetch_xml = MagicMock(return_value=index("https://example.com/loop.xml"))

    assert w._parse_sitemap(index("https://example.com/loop.xml")) == []
    assert w._fetch_xml.call_count == cache_warmer.MAX_SITEMAP_DEPTH


def test_empty_loc_does_not_crash():
    w = make_warmer()
    xml = f'<urlset xmlns="{NS}"><url><loc></loc></url><url><loc>https://example.com/a</loc></url></urlset>'.encode()
    assert w._parse_sitemap(xml) == ["https://example.com/a"]


def test_foreign_urls_are_dropped_from_result():
    w = make_warmer()
    w.sitemap_url = "https://example.com/sitemap.xml"
    w._fetch_xml = MagicMock(return_value=urlset("https://example.com/a", "https://evil.test/x"))
    assert w.get_urls() == ["https://example.com/a"]


def test_huge_sitemap_is_rejected(monkeypatch):
    monkeypatch.setattr(cache_warmer, "MAX_SITEMAP_BYTES", 100)
    w = make_warmer()
    resp = MagicMock()
    resp.iter_content.return_value = [b"x" * 60, b"x" * 60]
    w.session.get = MagicMock(return_value=resp)

    assert w._fetch_xml("https://example.com/sitemap.xml") is None
    resp.close.assert_called()
