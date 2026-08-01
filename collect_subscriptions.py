#!/usr/bin/env python3
"""Fetch URLs from a public Telegram post and merge their textual contents.

Design goals:
- No third-party Python dependencies.
- Keep the last successful content when an individual source is temporarily down.
- Reject loopback/private/link-local destinations and validate redirects.
- Bound response sizes so one source cannot fill the runner or repository.
- Produce deterministic generated files, so unchanged runs create no commit.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import sys
import time
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
TRAILING_URL_PUNCTUATION = ".,;:!?)]}>،؛。؛»”’"
BLOCKED_HOSTS = {
    "t.me",
    "www.t.me",
    "telegram.me",
    "www.telegram.me",
    "telegram.dog",
    "www.telegram.dog",
    "telegram.org",
    "www.telegram.org",
}
USER_AGENT = "telegram-subscription-collector/1.0 (+GitHub Actions)"
CHUNK_SIZE = 64 * 1024


class CollectorError(RuntimeError):
    """Expected collector failure with a human-readable explanation."""


class UnsafeURLError(CollectorError):
    """Raised when a URL targets a non-public network destination."""


class ResponseTooLargeError(CollectorError):
    """Raised when a response crosses the configured byte limit."""


class LinkHTMLParser(HTMLParser):
    """Collect links globally and, when available, from one exact Telegram post."""

    VOID_TAGS = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    )

    def __init__(self, target_post: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.target_post = target_post
        self.depth = 0
        self.target_depth: int | None = None
        self.found_target = False
        self.hrefs: list[str] = []
        self.text_chunks: list[str] = []
        self.target_hrefs: list[str] = []
        self.target_text_chunks: list[str] = []

    def _capture_href(self, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key.lower() != "href" or not value:
                continue
            self.hrefs.append(value)
            if self.target_depth is not None:
                self.target_hrefs.append(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower_tag = tag.lower()
        if lower_tag not in self.VOID_TAGS:
            self.depth += 1

        attrs_dict = {key.lower(): value for key, value in attrs}
        if (
            self.target_post
            and attrs_dict.get("data-post") == self.target_post
            and self.target_depth is None
        ):
            self.target_depth = self.depth
            self.found_target = True

        if lower_tag == "a":
            self._capture_href(attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._capture_href(attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.VOID_TAGS:
            return
        if self.target_depth is not None and self.depth == self.target_depth:
            self.target_depth = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if not data:
            return
        self.text_chunks.append(data)
        if self.target_depth is not None:
            self.target_text_chunks.append(data)


@dataclass(frozen=True)
class FetchResult:
    url: str
    ok: bool
    content: str | None
    byte_count: int
    sha256: str | None
    content_type: str | None
    error: str | None


class SafeRedirectHandler(HTTPRedirectHandler):
    """Validate every redirect before urllib follows it."""

    def __init__(self, blocked_hosts: frozenset[str] = frozenset(BLOCKED_HOSTS)) -> None:
        super().__init__()
        self.blocked_hosts = blocked_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        absolute_url = urljoin(req.full_url, newurl)
        validate_public_http_url(absolute_url, blocked_hosts=self.blocked_hosts)
        return super().redirect_request(req, fp, code, msg, headers, absolute_url)


def is_globally_routable_ip(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def validate_public_http_url(
    url: str, *, blocked_hosts: frozenset[str] = frozenset(BLOCKED_HOSTS)
) -> None:
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsafeURLError(f"unsupported URL scheme: {parts.scheme or '(missing)'}")
    if not parts.hostname:
        raise UnsafeURLError("URL has no hostname")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials inside URLs are not allowed")

    host = parts.hostname.rstrip(".")
    if host.lower() in blocked_hosts:
        raise UnsafeURLError(f"blocked navigation host is not a subscription source: {host}")

    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError(f"invalid port: {exc}") from exc

    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise CollectorError(f"DNS lookup failed for {host}: {exc}") from exc

    resolved_ips = {item[4][0] for item in addresses}
    if not resolved_ips:
        raise CollectorError(f"DNS lookup returned no addresses for {host}")

    unsafe = sorted(ip for ip in resolved_ips if not is_globally_routable_ip(ip))
    if unsafe:
        raise UnsafeURLError(
            f"hostname {host} resolved to non-public address(es): {', '.join(unsafe)}"
        )


def canonicalize_url(raw_url: str) -> str | None:
    value = html.unescape(raw_url).replace("\u200b", "").strip()
    value = value.rstrip(TRAILING_URL_PUNCTUATION)
    if not value:
        return None

    try:
        parts = urlsplit(value)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None

    host = parts.hostname.rstrip(".").lower()
    if host in BLOCKED_HOSTS:
        return None

    try:
        port = parts.port
    except ValueError:
        return None

    # Preserve non-default ports. Remove URL fragments because they are never sent
    # to the server and can cause duplicate downloads of the same resource.
    if ":" in host and not host.startswith("["):
        host_for_netloc = f"[{host}]"
    else:
        host_for_netloc = host

    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{host_for_netloc}:{port}"
    else:
        netloc = host_for_netloc

    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def deduplicate_urls(urls: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_url in urls:
        canonical = canonicalize_url(raw_url)
        if canonical and canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


def extract_urls_from_html(document: str, target_post: str | None = None) -> list[str]:
    parser = LinkHTMLParser(target_post=target_post)
    parser.feed(document)
    parser.close()

    candidates: list[str] = []
    hrefs = parser.target_hrefs if parser.found_target else parser.hrefs
    text_chunks = parser.target_text_chunks if parser.found_target else parser.text_chunks
    candidates.extend(hrefs)

    visible_text = "\n".join(text_chunks)
    candidates.extend(URL_RE.findall(visible_text))

    # Telegram sometimes places a URL in encoded HTML or attributes rather than
    # visible text. Use the global compatibility pass only when no exact target
    # container was found, otherwise neighboring posts could leak into the list.
    if not parser.found_target:
        candidates.extend(URL_RE.findall(html.unescape(document)))
    return deduplicate_urls(candidates)



def telegram_post_key(message_url: str) -> str:
    parts = urlsplit(message_url)
    path_parts = [part for part in parts.path.split("/") if part]
    if parts.hostname not in {"t.me", "www.t.me"} or len(path_parts) < 2:
        raise CollectorError("message URL must look like https://t.me/channel/message_id")

    channel, message_id = path_parts[0], path_parts[1]
    if not message_id.isdigit():
        raise CollectorError("Telegram message ID must be numeric")
    return f"{channel}/{message_id}"

def telegram_fetch_candidates(message_url: str) -> list[str]:
    post_key = telegram_post_key(message_url)
    channel, message_id = post_key.split("/", 1)
    base = f"https://t.me/{channel}/{message_id}"
    return [
        f"{base}?embed=1&mode=tme",
        f"https://t.me/s/{channel}/{message_id}",
    ]


def build_request(url: str) -> Request:
    return Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/plain,text/html,application/json,application/yaml,application/octet-stream;q=0.8,*/*;q=0.5",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        },
        method="GET",
    )


def read_limited(response, max_bytes: int) -> bytes:
    declared_length = response.headers.get("Content-Length")
    if declared_length:
        try:
            if int(declared_length) > max_bytes:
                raise ResponseTooLargeError(
                    f"declared Content-Length {declared_length} exceeds {max_bytes} bytes"
                )
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError(f"response exceeded {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def response_charset(headers: Message) -> str | None:
    try:
        return headers.get_content_charset()
    except (AttributeError, LookupError):
        return None


def decode_text(payload: bytes, headers: Message) -> str:
    if b"\x00" in payload[:8192]:
        raise CollectorError("response appears to be binary data")

    encodings: list[str] = []
    declared = response_charset(headers)
    if declared:
        encodings.append(declared)
    encodings.extend(["utf-8-sig", "utf-8"])

    tried: set[str] = set()
    for encoding in encodings:
        normalized = encoding.lower()
        if normalized in tried:
            continue
        tried.add(normalized)
        try:
            return payload.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue

    # Preserve every byte even for incorrectly labelled legacy servers.
    return payload.decode("utf-8", errors="replace")


def normalize_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    normalized = normalized.strip("\n")
    return normalized + "\n" if normalized else ""


def should_retry_http_status(status: int) -> bool:
    return status in {408, 425, 429} or 500 <= status <= 599


def fetch_text_once(
    url: str,
    timeout: float,
    max_bytes: int,
    *,
    allow_telegram_hosts: bool = False,
) -> tuple[str, int, str | None]:
    blocked_hosts = frozenset() if allow_telegram_hosts else frozenset(BLOCKED_HOSTS)
    validate_public_http_url(url, blocked_hosts=blocked_hosts)
    opener = build_opener(SafeRedirectHandler(blocked_hosts))
    request = build_request(url)
    with opener.open(request, timeout=timeout) as response:
        payload = read_limited(response, max_bytes)
        content_type = response.headers.get("Content-Type")
        text = normalize_text(decode_text(payload, response.headers))
        if not text:
            raise CollectorError("response body is empty")
        return text, len(payload), content_type


def fetch_with_retries(
    url: str,
    timeout: float,
    max_bytes: int,
    retries: int,
    *,
    allow_telegram_hosts: bool = False,
) -> FetchResult:
    errors: list[str] = []

    for attempt in range(1, retries + 1):
        try:
            content, byte_count, content_type = fetch_text_once(
                url, timeout, max_bytes, allow_telegram_hosts=allow_telegram_hosts
            )
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            return FetchResult(url, True, content, byte_count, digest, content_type, None)
        except HTTPError as exc:
            message = f"HTTP {exc.code}: {exc.reason}"
            errors.append(message)
            if not should_retry_http_status(exc.code):
                break
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = min(float(retry_after), 30.0) if retry_after else float(2 ** (attempt - 1))
            except ValueError:
                delay = float(2 ** (attempt - 1))
        except (URLError, TimeoutError, socket.timeout, CollectorError, OSError) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            delay = float(2 ** (attempt - 1))

        if attempt < retries:
            time.sleep(delay)

    return FetchResult(
        url=url,
        ok=False,
        content=None,
        byte_count=0,
        sha256=None,
        content_type=None,
        error=" | ".join(errors[-3:]) or "unknown fetch failure",
    )


def fetch_telegram_urls(
    message_url: str,
    timeout: float,
    max_bytes: int,
    retries: int,
) -> tuple[list[str], str | None]:
    errors: list[str] = []
    target_post = telegram_post_key(message_url)
    for candidate in telegram_fetch_candidates(message_url):
        result = fetch_with_retries(
            candidate,
            timeout,
            max_bytes,
            retries,
            allow_telegram_hosts=True,
        )
        if not result.ok or result.content is None:
            errors.append(f"{candidate}: {result.error}")
            continue

        urls = extract_urls_from_html(result.content, target_post=target_post)
        if urls:
            return urls, None
        errors.append(f"{candidate}: no external HTTP(S) links found")

    return [], " ; ".join(errors)


def read_url_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    candidates: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidates.extend(URL_RE.findall(stripped))
    return deduplicate_urls(candidates)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_write_json(path: Path, data: object) -> None:
    content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    atomic_write_text(path, content)


def source_filename(url: str) -> str:
    return f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.txt"


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def human_mib(byte_count: int) -> str:
    return f"{byte_count / (1024 * 1024):.2f} MiB"


def write_summary(
    path: Path | None,
    *,
    url_origin: str,
    total: int,
    fresh: int,
    stale: int,
    failed: int,
    merged_bytes: int,
    telegram_warning: str | None,
    failures: list[tuple[str, str]],
) -> None:
    if path is None:
        return

    lines = [
        "## Subscription collector",
        "",
        f"- URL list source: **{url_origin}**",
        f"- Discovered sources: **{total}**",
        f"- Fresh downloads: **{fresh}**",
        f"- Reused cached copies: **{stale}**",
        f"- Unavailable with no cache: **{failed}**",
        f"- Combined output size: **{human_mib(merged_bytes)}**",
    ]

    if telegram_warning:
        lines.extend(["", "> [!WARNING]", f"> {telegram_warning}"])

    if failures:
        lines.extend(["", "### Source warnings", ""])
        for url, error in failures[:20]:
            safe_error = error.replace("\n", " ")
            lines.append(f"- `{url}` — {safe_error}")
        if len(failures) > 20:
            lines.append(f"- …and {len(failures) - 20} more. See `subscriptions/manifest.json`.")

    atomic_write_text(path, "\n".join(lines) + "\n")


def collect(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    source_dir = output_dir / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)

    source_list_path = output_dir / "source-urls.txt"
    fallback_path = Path(args.fallback_file)
    summary_path = Path(args.summary_file) if args.summary_file else None

    max_source_bytes = int(args.max_source_mib * 1024 * 1024)
    max_total_bytes = int(args.max_total_mib * 1024 * 1024)

    telegram_urls, telegram_error = fetch_telegram_urls(
        args.message_url,
        args.timeout,
        min(max_source_bytes, 4 * 1024 * 1024),
        args.retries,
    )

    if telegram_urls:
        urls = telegram_urls
        url_origin = "live Telegram message"
    else:
        previous_urls = read_url_file(source_list_path)
        if previous_urls:
            urls = previous_urls
            url_origin = "previous successful URL list"
        else:
            urls = read_url_file(fallback_path)
            url_origin = "checked-in fallback URL list"

    if not urls:
        write_summary(
            summary_path,
            url_origin="none",
            total=0,
            fresh=0,
            stale=0,
            failed=0,
            merged_bytes=0,
            telegram_warning=telegram_error,
            failures=[],
        )
        raise CollectorError("no source URLs are available from Telegram, previous state, or fallback")

    atomic_write_text(source_list_path, "\n".join(urls) + "\n")

    results_by_url: dict[str, FetchResult] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(
                fetch_with_retries,
                url,
                args.timeout,
                max_source_bytes,
                args.retries,
            ): url
            for url in urls
        }
        for future in concurrent.futures.as_completed(future_map):
            url = future_map[future]
            try:
                results_by_url[url] = future.result()
            except Exception as exc:  # Defensive: one worker must not abort all others.
                results_by_url[url] = FetchResult(
                    url=url,
                    ok=False,
                    content=None,
                    byte_count=0,
                    sha256=None,
                    content_type=None,
                    error=f"unexpected worker error: {type(exc).__name__}: {exc}",
                )

    manifest_sources: list[dict[str, object]] = []
    merged_parts: list[str] = []
    active_files: set[str] = set()
    fresh_count = 0
    stale_count = 0
    failed_count = 0
    failures: list[tuple[str, str]] = []
    total_merged_bytes = 0

    for url in urls:
        result = results_by_url[url]
        filename = source_filename(url)
        relative_file = f"sources/{filename}"
        cache_path = source_dir / filename
        active_files.add(filename)

        status: str
        content: str | None
        error = result.error
        content_type = result.content_type

        if result.ok and result.content is not None:
            content = result.content
            atomic_write_text(cache_path, content)
            status = "fresh"
            fresh_count += 1
        elif cache_path.exists():
            content = cache_path.read_text(encoding="utf-8")
            status = "stale-cache"
            stale_count += 1
            failures.append((url, error or "download failed; cached copy reused"))
        else:
            content = None
            status = "failed-no-cache"
            failed_count += 1
            failures.append((url, error or "download failed and no cache exists"))

        if content is not None:
            encoded_size = len(content.encode("utf-8"))
            total_merged_bytes += encoded_size
            if total_merged_bytes > max_total_bytes:
                raise ResponseTooLargeError(
                    f"combined output exceeded configured limit of {max_total_bytes} bytes"
                )
            merged_parts.append(content.rstrip("\n"))
            digest = sha256_text(content)
            manifest_byte_count = encoded_size
        else:
            digest = None
            manifest_byte_count = 0

        manifest_sources.append(
            {
                "url": url,
                "status": status,
                "file": relative_file if content is not None else None,
                "bytes": manifest_byte_count,
                "sha256": digest,
                "content_type": content_type,
                "error": error,
            }
        )

    if not merged_parts:
        write_summary(
            summary_path,
            url_origin=url_origin,
            total=len(urls),
            fresh=fresh_count,
            stale=stale_count,
            failed=failed_count,
            merged_bytes=0,
            telegram_warning=telegram_error,
            failures=failures,
        )
        raise CollectorError("all sources failed and no cached content was available")

    # Remove caches for URLs that disappeared from the current message/list.
    for cache_path in source_dir.glob("*.txt"):
        if cache_path.name not in active_files:
            cache_path.unlink()

    merged = "\n\n".join(merged_parts) + "\n"
    atomic_write_text(output_dir / "all.txt", merged)

    manifest = {
        "schema_version": 1,
        "message_url": args.message_url,
        "url_list_origin": url_origin,
        "source_count": len(urls),
        "fresh_count": fresh_count,
        "stale_cache_count": stale_count,
        "failed_no_cache_count": failed_count,
        "combined_bytes": len(merged.encode("utf-8")),
        "sources": manifest_sources,
    }
    atomic_write_json(output_dir / "manifest.json", manifest)

    write_summary(
        summary_path,
        url_origin=url_origin,
        total=len(urls),
        fresh=fresh_count,
        stale=stale_count,
        failed=failed_count,
        merged_bytes=len(merged.encode("utf-8")),
        telegram_warning=telegram_error,
        failures=failures,
    )

    print(
        f"Collected {len(urls)} sources: {fresh_count} fresh, "
        f"{stale_count} cached, {failed_count} unavailable."
    )
    print(f"Wrote {output_dir / 'all.txt'} ({human_mib(len(merged.encode('utf-8')))})")
    return 0


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message-url", required=True)
    parser.add_argument("--fallback-file", required=True)
    parser.add_argument("--output-dir", default="subscriptions")
    parser.add_argument("--summary-file")
    parser.add_argument("--workers", type=positive_int, default=8)
    parser.add_argument("--timeout", type=positive_float, default=25.0)
    parser.add_argument("--retries", type=positive_int, default=3)
    parser.add_argument("--max-source-mib", type=positive_float, default=12.0)
    parser.add_argument("--max-total-mib", type=positive_float, default=80.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return collect(args)
    except CollectorError as exc:
        print(f"collector error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"collector I/O error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("collector interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
