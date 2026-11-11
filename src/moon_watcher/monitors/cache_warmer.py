"""Warms the cache by visiting every URL listed in the sitemap."""

import logging
import time
from urllib.parse import urlparse

import defusedxml.ElementTree as ET
import requests

from moon_watcher import database as db
from moon_watcher.config import (
    DELAY_BETWEEN_REQUESTS,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

logger = logging.getLogger(__name__)

# sitemap index can point to other sitemaps, don't follow them forever
MAX_SITEMAP_DEPTH = 3
# real sitemaps are capped at 50 MB by the spec, but nobody needs that much here
MAX_SITEMAP_BYTES = 10 * 1024 * 1024


class CacheWarmer:
    def __init__(self, site_url: str | None = None):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.site_url: str | None = None
        self.sitemap_url: str | None = None
        if site_url:
            self.set_site_url(site_url)

    def set_site_url(self, site_url: str) -> None:
        self.site_url = site_url.rstrip("/")
        candidates = [
            f"{self.site_url}/sitemap.xml",
            f"{self.site_url}/sitemap_index.xml",
            f"{self.site_url}/wp-sitemap.xml",
        ]
        for url in candidates:
            try:
                r = self.session.head(url, timeout=5)
                if r.status_code == 200:
                    self.sitemap_url = url
                    logger.info("Found sitemap: %s", url)
                    return
            except requests.RequestException:
                continue
        self.sitemap_url = candidates[0]
        logger.warning("No sitemap detected, defaulting to %s", self.sitemap_url)

    def _same_host(self, url: str) -> bool:
        return urlparse(url).netloc == urlparse(self.site_url).netloc

    def _fetch_xml(self, url: str) -> bytes | None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = self.session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
                try:
                    r.raise_for_status()
                    data = b""
                    for chunk in r.iter_content(64 * 1024):
                        data += chunk
                        if len(data) > MAX_SITEMAP_BYTES:
                            logger.warning("Sitemap %s is bigger than %d MB, skipping",
                                           url, MAX_SITEMAP_BYTES // 1024 // 1024)
                            return None
                    return data
                finally:
                    r.close()
            except requests.RequestException as e:
                logger.warning("Sitemap fetch %s attempt %d: %s", url, attempt, e)
                if attempt < MAX_RETRIES:
                    time.sleep(5 * attempt)
        return None

    def _parse_sitemap(self, xml_bytes: bytes, depth: int = 0) -> list[str]:
        ns = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            logger.error("XML parse error: %s", e)
            return []

        sub_sitemaps = root.findall(".//ns:sitemap/ns:loc", ns)
        if sub_sitemaps:
            if depth >= MAX_SITEMAP_DEPTH:
                logger.warning("Sitemap index nested too deep, stopping")
                return []
            urls: list[str] = []
            for loc in sub_sitemaps:
                child_url = (loc.text or "").strip()
                # a sitemap is user-controlled data, don't let it send us to other hosts
                if not child_url.startswith("http") or not self._same_host(child_url):
                    logger.warning("Skipping sitemap on another host: %s", child_url)
                    continue
                child_xml = self._fetch_xml(child_url)
                if child_xml:
                    urls.extend(self._parse_sitemap(child_xml, depth + 1))
                time.sleep(1)
            return urls

        return [loc.text.strip() for loc in root.findall(".//ns:loc", ns) if loc.text]

    def get_urls(self) -> list[str]:
        if not self.sitemap_url:
            return []
        xml_bytes = self._fetch_xml(self.sitemap_url)
        if not xml_bytes:
            return []

        seen: set[str] = set()
        result: list[str] = []
        for url in self._parse_sitemap(xml_bytes):
            if not url.startswith("http"):
                continue
            if not self._same_host(url):
                continue
            if url not in seen:
                seen.add(url)
                result.append(url)
        return result

    def _warm_url(self, url: str) -> tuple[bool, int, float]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                t0 = time.time()
                r = self.session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                elapsed = time.time() - t0
                if r.status_code < 400:
                    return True, r.status_code, elapsed
                logger.warning("%s returned %d", url, r.status_code)
                return False, r.status_code, elapsed
            except requests.RequestException as e:
                logger.warning("%s attempt %d: %s", url, attempt, e)
                if attempt < MAX_RETRIES:
                    time.sleep(3 * attempt)
        return False, 0, 0.0

    def run_warmup(self, progress_callback=None) -> dict:
        if not self.site_url:
            return {"success": False, "message": "No site URL configured"}

        urls = self.get_urls()
        if not urls:
            db.log_event("WARNING", "warmup", "No URLs in sitemap", self.site_url)
            return {"success": False, "message": "No URLs found in sitemap"}

        run_id = db.start_warmup_run(self.site_url)
        db.log_event("INFO", "warmup", f"Started warmup of {len(urls)} pages", self.site_url)

        t_start = time.time()
        ok_count = fail_count = 0
        total_load = 0.0

        for i, url in enumerate(urls, 1):
            success, _code, load_time = self._warm_url(url)
            if success:
                ok_count += 1
                total_load += load_time
            else:
                fail_count += 1

            if progress_callback and i % 10 == 0:
                progress_callback(i, len(urls), i / len(urls) * 100)

            if i < len(urls):
                time.sleep(DELAY_BETWEEN_REQUESTS)

        duration = time.time() - t_start
        avg_load = total_load / ok_count if ok_count else 0.0

        db.finish_warmup_run(run_id, len(urls), ok_count, fail_count, duration, avg_load)
        db.log_event(
            "INFO", "warmup",
            f"Finished: {ok_count}/{len(urls)} OK in {duration:.0f}s",
            self.site_url,
        )

        return {
            "success": True,
            "total_urls": len(urls),
            "successful": ok_count,
            "failed": fail_count,
            "duration": duration,
            "avg_load_time": avg_load,
        }


def format_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds // 60:.0f}m {seconds % 60:.0f}s"
    return f"{seconds // 3600:.0f}h {(seconds % 3600) // 60:.0f}m"
