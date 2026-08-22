"""Local paper cache under Path.home() / '.paperfetch'. Never Path('~')."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

TEXT_FLOOR = 500
MAX_KEY_LEN = 180
MAX_CHARS = 12_000

_BAD_CHARS = '<>:"\\|?*'
_RESERVED = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$",
    re.IGNORECASE,
)
_DOI_PREFIX = re.compile(
    r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)",
    re.IGNORECASE,
)


def cache_root() -> Path:
    return Path.home() / ".paperfetch"


def looks_like_doi(raw: str) -> bool:
    s = (raw or "").strip()
    s = _DOI_PREFIX.sub("", s)
    s = s.strip().strip("/")
    s = s.lower()
    return s.startswith("10.")


def normalize_doi(raw: str) -> str:
    s = (raw or "").strip()
    s = _DOI_PREFIX.sub("", s)
    s = s.strip().strip("/")
    s = s.lower()
    if not s.startswith("10."):
        raise ValueError("not a DOI")
    return s


def folder_key(doi: str) -> str:
    """Safe single folder name. Always pass a normalised DOI, never the raw token."""
    key = doi.replace("/", "%2F")
    for ch in _BAD_CHARS:
        key = key.replace(ch, "")
    key = key.rstrip(". ")
    if not key or _RESERVED.match(key):
        key = "_" + (key or "doi")
    if len(key) > MAX_KEY_LEN:
        key = hashlib.sha256(doi.encode("utf-8")).hexdigest()
    return key


def paper_dir(doi: str) -> Path:
    return cache_root() / "cache" / folder_key(doi)


def pdf_path(doi: str) -> Path:
    return paper_dir(doi) / "paper.pdf"


def text_path(doi: str) -> Path:
    return paper_dir(doi) / "text.txt"


def meta_path(doi: str) -> Path:
    return paper_dir(doi) / "meta.json"


def read_meta(doi: str) -> dict:
    path = meta_path(doi)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(doi: str, data: dict) -> None:
    folder = paper_dir(doi)
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"doi": doi, **data}
    meta_path(doi).write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def text_chars(doi: str) -> int:
    path = text_path(doi)
    if not path.is_file():
        return 0
    return len(path.read_text(encoding="utf-8").strip())


def cache_ok(doi: str) -> bool:
    return text_chars(doi) >= TEXT_FLOOR


def cache_inventory() -> dict:
    cdir = cache_root() / "cache"
    cached_count = 0
    cached_chars = 0
    unreadable_count = 0
    try:
        if cdir.is_dir():
            for d in cdir.iterdir():
                try:
                    if not d.is_dir():
                        continue
                except OSError:
                    continue
                txt = d / "text.txt"
                t_len = 0
                try:
                    if txt.is_file():
                        t_len = len(txt.read_text(encoding="utf-8").strip())
                except (OSError, UnicodeDecodeError):
                    t_len = 0
                if t_len >= TEXT_FLOOR:
                    cached_count += 1
                    cached_chars += t_len
                else:
                    try:
                        if (d / "paper.pdf").is_file():
                            unreadable_count += 1
                    except OSError:
                        pass
    except OSError:
        pass
    return {
        "cached": {"count": cached_count, "chars": cached_chars},
        "unreadable": {"count": unreadable_count},
    }


