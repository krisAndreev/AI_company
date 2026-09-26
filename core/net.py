"""
Safe HTTP for code-controlled tools. The AI never supplies a URL to these functions:
URLs come from config (API endpoints) or from a search provider's results.

- request(): API calls. Allowlisted hosts only. https is required, except for
  loopback / private-LAN hosts (a local image server, another PC running Ollama).
  Retries rate limits (429) and temporary server errors with backoff.
- fetch_public(): reading public web pages for research. http/https on ports 80/443,
  every hop's DNS answer must be a PUBLIC address (blocks SSRF to this PC / the LAN)
  and the connection is pinned to the checked address (no DNS-rebinding window).
  Redirects are re-checked, size and content type are limited.
Errors never echo request headers (they may contain API keys).
"""

import gzip
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.parse
import urllib.robotparser
import zlib
from dataclasses import dataclass

USER_AGENT = "AICompanyResearchBot/1.0 (small-business research; respects robots.txt)"
_RETRY_STATUS = {429, 502, 503, 504}


class ToolError(Exception):
    """A tool call that was refused or failed. The message is safe to log and show."""


# --- API requests -------------------------------------------------------------------------

def _is_local_host(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def check_api_url(url: str, allowed_hosts: list[str]) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname not in allowed_hosts:
        raise ToolError(f"refused host {parsed.hostname!r} (not in allowlist)")
    if parsed.scheme == "https":
        return parsed
    if parsed.scheme == "http" and _is_local_host(parsed.hostname or ""):
        return parsed
    raise ToolError(f"refused {parsed.scheme} URL to {parsed.hostname} (https required)")


def request(method: str, url: str, allowed_hosts: list[str], *, params: dict | None = None,
            json_body: dict | None = None, form: dict | None = None,
            headers: dict | None = None, timeout: float = 60, retries: int = 2,
            expect: str = "json", max_bytes: int = 60_000_000):
    """One API call. expect='json' returns the parsed body, 'bytes' raw bytes, 'text' str."""
    parsed = check_api_url(url, allowed_hosts)
    query = "&".join(q for q in (parsed.query, urllib.parse.urlencode(params or {})) if q)
    path = (parsed.path or "/") + ("?" + query if query else "")
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", **(headers or {})}
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    host = parsed.hostname
    attempt = 0
    while True:
        attempt += 1
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(host, parsed.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = _read_limited(resp, max_bytes)
            status = resp.status
            retry_after = resp.getheader("Retry-After")
        except ToolError:
            raise
        except (OSError, http.client.HTTPException) as e:
            if attempt <= retries:
                time.sleep(2 * attempt)
                continue
            raise ToolError(f"request to {host} failed: {type(e).__name__}") from e
        finally:
            conn.close()
        if status in _RETRY_STATUS and attempt <= retries:
            wait = _retry_seconds(retry_after, attempt)
            time.sleep(wait)
            continue
        if status >= 400:
            raise ToolError(f"HTTP {status} from {host}: {_error_hint(data)}")
        if expect == "bytes":
            return data
        if expect == "text":
            return data.decode("utf-8", "replace")
        try:
            return json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ToolError(f"{host} returned invalid JSON") from e


def _retry_seconds(header: str | None, attempt: int) -> float:
    try:
        return min(30.0, max(1.0, float(header)))
    except (TypeError, ValueError):
        return min(30.0, 2.0 * 2 ** attempt)


def _error_hint(data: bytes) -> str:
    """A short, key-free description of an API error body."""
    try:
        err = json.loads(data)
        if isinstance(err, dict):
            e = err.get("error", err.get("detail", err.get("message", err)))
            if isinstance(e, dict):
                e = e.get("message", e)
            return str(e)[:200]
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return data[:120].decode("utf-8", "replace")


def _read_limited(resp, max_bytes: int) -> bytes:
    raw = resp.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ToolError(f"response larger than {max_bytes} bytes")
    if (resp.getheader("Content-Encoding") or "").lower() == "gzip":
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = d.decompress(raw, max_bytes)
        if d.unconsumed_tail:
            raise ToolError(f"decompressed response larger than {max_bytes} bytes")
    return raw


# --- public web pages ---------------------------------------------------------------------

@dataclass
class FetchedPage:
    url: str            # final URL after redirects
    status: int
    content_type: str
    text: str           # decoded body
    truncated: bool


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def resolve_public(host: str, port: int) -> str:
    """Resolve host; refuse unless EVERY address is public. Returns the address to use."""
    try:
        ipaddress.ip_address(host)
        raise ToolError(f"refused IP-literal URL host {host}")
    except ValueError:
        pass
    if host.endswith((".local", ".internal", ".lan", ".home", ".localhost")) or "." not in host:
        raise ToolError(f"refused non-public host {host}")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ToolError(f"cannot resolve {host}") from e
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise ToolError(f"refused {host}: resolves to a non-public address")
        ips.append(ip)
    if not ips:
        raise ToolError(f"cannot resolve {host}")
    return str(sorted(ips, key=lambda i: i.version)[0])  # IPv4 first


def check_public_url(url: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolError(f"refused {parsed.scheme or 'relative'} URL")
    if parsed.username or parsed.password:
        raise ToolError("refused URL with credentials")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443):
        raise ToolError(f"refused port {port}")
    if not parsed.hostname:
        raise ToolError("refused URL without host")
    return parsed


def fetch_public(url: str, *, max_bytes: int = 1_500_000, timeout: float = 15,
                 max_redirects: int = 4,
                 accept: tuple[str, ...] = ("text/html", "application/xhtml+xml", "text/plain"),
                 user_agent: str = USER_AGENT) -> FetchedPage:
    for _ in range(max_redirects + 1):
        parsed = check_public_url(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ip = resolve_public(parsed.hostname, port)
        cls = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
        conn = cls(parsed.hostname, ip, port, timeout)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        try:
            conn.request("GET", path, headers={
                "User-Agent": user_agent, "Accept": ",".join(accept) + ";q=0.9,*/*;q=0.1",
                "Accept-Encoding": "gzip", "Accept-Language": "en;q=0.9,*;q=0.5"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                resp.read(1000)
                if not location:
                    raise ToolError(f"redirect without location from {parsed.hostname}")
                url = urllib.parse.urljoin(url, location)
                continue
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if resp.status >= 400:
                raise ToolError(f"HTTP {resp.status} from {parsed.hostname}")
            if ctype and ctype not in accept:
                raise ToolError(f"refused content type {ctype!r} from {parsed.hostname}")
            raw = resp.read(max_bytes + 1)
            truncated = len(raw) > max_bytes
            raw = raw[:max_bytes]
            if (resp.getheader("Content-Encoding") or "").lower() == "gzip":
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                try:
                    raw = d.decompress(raw, max_bytes * 4)
                except zlib.error as e:
                    raise ToolError(f"bad gzip data from {parsed.hostname}") from e
            text = raw.decode(_charset(resp.getheader("Content-Type"), raw), errors="replace")
            return FetchedPage(url, resp.status, ctype or "text/html", text, truncated)
        except (OSError, http.client.HTTPException) as e:
            raise ToolError(f"fetch from {parsed.hostname} failed: {type(e).__name__}") from e
        finally:
            conn.close()
    raise ToolError("too many redirects")


def _charset(content_type: str | None, raw: bytes) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if not m:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
    name = (m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1)) if m else "utf-8"
    try:
        "".encode(name)
        return name
    except LookupError:
        return "utf-8"


class RobotsCache:
    """robots.txt per site (fetched with the same public-only rules). Missing = allowed."""

    def __init__(self, fetch=fetch_public, user_agent: str = USER_AGENT):
        self.fetch = fetch
        self.user_agent = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        site = f"{parsed.scheme}://{parsed.netloc}"
        if site not in self._cache:
            parser = None
            try:
                page = self.fetch(site + "/robots.txt", max_bytes=300_000, timeout=8,
                                  accept=("text/plain", "text/html"))
                if page.content_type == "text/plain":
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(page.text.splitlines())
            except ToolError:
                parser = None
            self._cache[site] = parser
        parser = self._cache[site]
        return True if parser is None else parser.can_fetch(self.user_agent, url)


def gunzip_if_needed(data: bytes) -> bytes:
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data
