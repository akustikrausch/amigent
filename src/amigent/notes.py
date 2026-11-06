"""What the agent learned about programs on one Amiga, kept for the next session.

A note belongs to a topic, usually a program ("settlers2", "protracker"), and
says something worth knowing before using it again: the stack it needs, where
a button sits, which requester comes up. amiga_start shows the notes of the
program it starts. Stored per Amiga as JSON next to keys and certificates.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path, PurePosixPath

from . import secret

MAX_NOTES = 200  # per topic; the oldest go first


def topic_of(text: str) -> str:
    """A topic name from a program path or name: 'SYS:Games/Foo/Foo.exe' -> 'foo'."""
    text = text.strip()
    if text.startswith('"'):
        word = text[1:].split('"', 1)[0]
    else:
        word = text.split()[0] if text else ""
    word = word.split(":")[-1]
    name = PurePosixPath(word).name if word else ""
    name = re.sub(r"\.(exe|info|rexx|lha)$", "", name, flags=re.IGNORECASE)
    return name.lower()


class Notes:
    def __init__(self, path: Path):
        self.path = path

    def _all(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    def add(self, topic: str, text: str) -> int:
        topic = topic.strip().lower()
        if not topic or not text.strip():
            raise ValueError("a note needs a topic and a text")
        data = self._all()
        entries = data.setdefault(topic, [])
        if any(e["text"] == text.strip() for e in entries):
            return len(entries)
        entries.append({"text": text.strip(), "at": time.strftime("%Y-%m-%d")})
        data[topic] = entries[-MAX_NOTES:]
        self._save(data)
        return len(data[topic])

    def remove(self, topic: str, contains: str = "") -> int:
        """Drop the topic's notes that contain the text (all of them without); how many went."""
        topic = topic.strip().lower()
        data = self._all()
        entries = data.get(topic, [])
        keep = [e for e in entries if contains and contains.lower() not in e["text"].lower()]
        gone = len(entries) - len(keep)
        if keep:
            data[topic] = keep
        else:
            data.pop(topic, None)
        self._save(data)
        return gone

    def get(self, topic: str) -> list[str]:
        return [e["text"] for e in self._all().get(topic.strip().lower(), [])]

    def topics(self) -> dict[str, int]:
        return {t: len(v) for t, v in sorted(self._all().items())}

    def search(self, words: str) -> dict[str, list[str]]:
        needle = words.lower()
        return {t: [e["text"] for e in v if needle in e["text"].lower() or needle in t]
                for t, v in self._all().items()
                if needle in t or any(needle in e["text"].lower() for e in v)}


def for_amiga(name: str) -> Notes:
    return Notes(secret.FOLDER / "notes" / f"{secret.safe(name or 'Amiga')}.json")
