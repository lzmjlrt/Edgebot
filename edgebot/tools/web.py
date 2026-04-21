"""
edgebot/tools/web.py - Web fetch + web search tools.

- SSRF-filtered via edgebot.security.network.validate_url_target
- Zero extra deps (uses httpx which is a transitive dep of litellm)
- Search backends tried in order: Tavily, SerpAPI, DuckDuckGo (HTML scrape)
"""

import html
import json
import os
import re
import urllib.parse
from html.parser import HTMLParser

import httpx

from edgebot.security.network import validate_url_target

_USER_AGENT = "Edgebot/1.0 (+https://github.com/; Python httpx)"
_MAX_BYTES = 500_000
_MAX_CHARS = 50_000
_TIMEOUT = 10.0


class _TextExtractor(HTMLParser):
    """Strip tags, keep visible text; skip script/style/etc; collapse whitespace."""

    _SKIP = {"script", "style", "noscript", "svg", "iframe", "head"}
    _BLOCK = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "hr"}

    def __init__(self):
        super().__init__()
        self._buf: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._buf.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._buf.append(data)

    def text(self) -> str:
        raw = html.unescape("".join(self._buf))
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw)
        return raw.strip()


def run_web_fetch(url: str) -> str:
    """Fetch a URL and return its visible text (truncated to ~50KB)."""
    err = validate_url_target(url)
    if err:
        return f"Error: {err}"
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        ) as c:
            r = c.get(url)
    except Exception as e:
        return f"Error: {e}"
    if r.status_code >= 400:
        return f"Error: HTTP {r.status_code} for {url}"
    body = r.content[:_MAX_BYTES]
    encoding = r.encoding or "utf-8"
    ctype = r.headers.get("content-type", "").lower()
    if "html" in ctype or body.lstrip()[:1] == b"<":
        parser = _TextExtractor()
        try:
            parser.feed(body.decode(encoding, errors="replace"))
            text = parser.text()
        except Exception as e:
            text = f"[html parse error: {e}]\n" + body.decode(encoding, errors="replace")
    else:
        text = body.decode(encoding, errors="replace")
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + f"\n\n[... truncated, {len(text) - _MAX_CHARS} chars more ...]"
    return f"[{r.status_code}] {url}\n\n{text}"


def _search_tavily(query: str, n: int) -> list[dict] | None:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return None
    try:
        r = httpx.post(
            "https://api.tavily.com/search",
            json={"api_key": key, "query": query, "max_results": n,
                  "search_depth": "basic"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return [{"title": x.get("title", ""),
                 "url": x.get("url", ""),
                 "snippet": x.get("content", "")}
                for x in data.get("results", [])[:n]]
    except Exception:
        return None


def _search_serpapi(query: str, n: int) -> list[dict] | None:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return None
    try:
        r = httpx.get(
            "https://serpapi.com/search",
            params={"engine": "google", "q": query, "api_key": key,
                    "num": n, "hl": "zh-CN"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        return [{"title": x.get("title", ""),
                 "url": x.get("link", ""),
                 "snippet": x.get("snippet", "")}
                for x in data.get("organic_results", [])[:n]]
    except Exception:
        return None


_TAG_RE = re.compile(r"<[^>]+>")
_DDG_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.S,
)


def _search_ddg(query: str, n: int) -> list[dict]:
    """Fallback: scrape DuckDuckGo HTML endpoint. No key, no new deps."""
    try:
        r = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
        r.raise_for_status()
    except Exception as e:
        return [{"title": "error", "url": "", "snippet": str(e)}]
    results = []
    for m in _DDG_RE.finditer(r.text):
        url, title, snippet = m.groups()
        # DDG wraps outbound URLs through its redirector; unwrap.
        if url.startswith("//duckduckgo.com/l/") or url.startswith("/l/") \
                or "duckduckgo.com/l/?" in url:
            qs = urllib.parse.urlparse(url).query
            real = urllib.parse.parse_qs(qs).get("uddg", [""])[0]
            url = urllib.parse.unquote(real) or url
        results.append({
            "title": html.unescape(_TAG_RE.sub("", title)).strip(),
            "url": url,
            "snippet": html.unescape(_TAG_RE.sub("", snippet)).strip(),
        })
        if len(results) >= n:
            break
    return results


def run_web_search(query: str, max_results: int = 5) -> str:
    """Try Tavily → SerpAPI → DDG in that order. Return pretty JSON string."""
    for backend_name, fn in (("tavily", _search_tavily), ("serpapi", _search_serpapi)):
        hits = fn(query, max_results)
        if hits is not None:
            return json.dumps(
                {"backend": backend_name, "query": query, "results": hits},
                indent=2, ensure_ascii=False,
            )
    hits = _search_ddg(query, max_results)
    return json.dumps(
        {"backend": "ddg", "query": query, "results": hits},
        indent=2, ensure_ascii=False,
    )
