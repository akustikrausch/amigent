"""Aminet from the PC: search, readme, download.

The PC has the fast line, so it fetches; the Amiga gets the archive (packed,
which is the cheap way over a slow wire) and unpacks it with its own LhA,
unlzx or unzip. Without one of those the PC unpacks .lha and .zip itself
and sends the files. Search results come from the site's HTML table.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

SITE = "https://aminet.net"
AGENT = "Amigent (Aminet client)"
TIMEOUT = 30
MAX_ARCHIVE = 64 * 2**20

ROW = re.compile(r'<tr class="[^"]*pkg_row[^"]*"[^>]*>(.*?)</tr>', re.S)
CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
LINK = re.compile(r'href="([^"]+)"')
TAG = re.compile(r"<[^>]+>")
PACKAGE = re.compile(r"^[a-z]+/[a-z0-9]+/[^/\s]+\.(lha|lzh|lzx|zip|adf|adz|dms|tar\.gz|tgz|readme)$", re.I)


@dataclass
class Package:
    path: str  # dir/sub/name.lha
    description: str
    size: str
    date: str
    downloads: int
    arch: str

    @property
    def readme(self) -> str:
        return re.sub(r"\.[^.]+$", ".readme", self.path)


def _get(url: str, limit: int = MAX_ARCHIVE, tries: int = 2) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    for attempt in range(tries):
        with urllib.request.urlopen(req, timeout=TIMEOUT) as answer:  # nosec: fixed site
            expected = answer.headers.get("Content-Length")
            data = answer.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"{url} is larger than {limit >> 20} MB")
        if not expected or not expected.isdigit() or len(data) == int(expected):
            return data
    raise ValueError(f"{url}: download cut short ({len(data)} of {expected} bytes)")


def _text(cell: str) -> str:
    return html.unescape(TAG.sub("", cell)).strip()


def parse_search(page: str) -> list[Package]:
    """Rows of a search or directory page: name, dls, dir, size, date, arch, description."""
    found = []
    for row in ROW.findall(page):
        cells = CELL.findall(row)
        if len(cells) < 8:
            continue
        link = LINK.search(cells[0])
        if not link:
            continue
        path = link.group(1).lstrip("/")
        arch = re.findall(r'alt="([^" ]+) icon"', cells[6])
        description = _text(cells[7]).replace(" - (readme)", "").strip()
        try:
            downloads = int(_text(cells[3]) or 0)
        except ValueError:
            downloads = 0
        found.append(Package(path, description, _text(cells[4]), _text(cells[5]), downloads,
                             ",".join(arch)))
    return found


def search(query: str, limit: int = 20) -> list[Package]:
    url = f"{SITE}/search?query={urllib.parse.quote(query)}"
    return parse_search(_get(url, 4 * 2**20).decode("latin-1"))[:limit]


def check_path(path: str) -> str:
    path = path.strip().lstrip("/")
    if path.startswith(("http://", "https://")):
        path = urllib.parse.urlparse(path).path.lstrip("/")
    if path.startswith("package/"):
        raise ValueError("give the archive path, e.g. 'game/strat/foo.lha', not the package page")
    if not PACKAGE.match(path) or ".." in path:
        raise ValueError(f"{path!r} does not look like an Aminet archive path (dir/sub/name.lha)")
    return path


def readme(path: str) -> str:
    path = check_path(path)
    return _get(f"{SITE}/{re.sub(r'[.][^.]+$', '.readme', path)}", 2**20).decode("latin-1")


def download(path: str) -> bytes:
    return _get(f"{SITE}/{check_path(path)}")


def tool_for(name: str) -> str:
    """Which Amiga command unpacks this archive: lha, unlzx or unzip; '' for none."""
    lower = name.lower()
    if lower.endswith((".lha", ".lzh")):
        return "lha"
    if lower.endswith(".lzx"):
        return "unlzx"
    if lower.endswith(".zip"):
        return "unzip"
    return ""


def unpack_line(tool: str, archive: str, target: str) -> str:
    """The AmigaDOS command line that unpacks archive into the folder target."""
    folder = target if target.endswith((":", "/")) else target + "/"
    if tool == "lha":
        return f'lha -q -m x "{archive}" "{folder}"'
    if tool == "unlzx":
        return f'unlzx -m x "{archive}" "{folder}"'
    if tool == "unzip":
        return f'unzip -o -qq "{archive}" -d "{target}"'
    raise ValueError(f"no way to unpack with {tool!r}")
