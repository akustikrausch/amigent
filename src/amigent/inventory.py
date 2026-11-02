"""Parse the output of amiga/amigent-scan and compare two scans."""

from __future__ import annotations


def parse(text: str) -> dict[str, dict]:
    """Turn '@path|size' records, each followed by Version output, into a dict."""
    items: dict[str, dict] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("@") and "|" in line:
            path, _, size = line[1:].rpartition("|")
            current = items[path] = {"size": int(size) if size.isdigit() else 0, "version": ""}
        elif current is not None and line.strip() and not current["version"]:
            if not _is_failure(line):
                current["version"] = line.strip()
    return items


def _is_failure(line: str) -> bool:
    return line.startswith(("Could not find version", "Objekt nicht gefunden")) or (
        "fehlgeschlagen" in line
    )


def diff(old: dict[str, dict], new: dict[str, dict]) -> dict[str, list]:
    changed = [
        {"path": p, "old": old[p]["version"], "new": new[p]["version"]}
        for p in sorted(old.keys() & new.keys())
        if old[p]["version"] != new[p]["version"]
    ]
    return {
        "changed": changed,
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
    }
