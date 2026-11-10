"""把磁盘上的知识库读成「原文段落」。

一个文件 = 一个文档；段落号是原文里的自然段序号，证据引用直接指回这里。
"""

from __future__ import annotations

import re
from pathlib import Path

from .text import chunk_text

SUPPORTED_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".text",
    ".rst",
    ".org",
    ".log",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
}

_FRONT_MATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.S)


def discover(kb_dir: Path) -> list[Path]:
    if not kb_dir.exists():
        return []
    files: list[Path] = []
    for path in sorted(kb_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        relative = path.relative_to(kb_dir)
        if any(part.startswith(".") for part in relative.parts):
            continue
        files.append(path)
    return files


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def strip_front_matter(text: str) -> str:
    return _FRONT_MATTER.sub("", text, count=1)


def _doc_id(path: Path, kb_dir: Path, used: set[str]) -> str:
    relative = path.relative_to(kb_dir).with_suffix("")
    raw = "-".join(relative.parts)
    base = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", raw).strip("-") or "doc"
    doc = base
    suffix = 2
    while doc in used:
        doc = f"{base}-{suffix}"
        suffix += 1
    used.add(doc)
    return doc


def build_chunks(
    kb_dir: Path,
    *,
    min_chars: int = 140,
    max_chars: int = 1200,
) -> tuple[list[dict], list[dict]]:
    """返回 (chunks, sources)。

    chunk id 形如 ``atlas-deploy#3``（单段）或 ``atlas-deploy#3-5``（合并了 3..5 段），
    既能当引用角标，也能用 ``wiki get`` 反向取回原文。
    """
    used: set[str] = set()
    chunks: list[dict] = []
    sources: list[dict] = []

    for path in discover(kb_dir):
        text = strip_front_matter(read_text(path))
        if not text.strip():
            continue
        doc = _doc_id(path, kb_dir, used)
        relative = path.relative_to(kb_dir).as_posix()
        stat = path.stat()
        sources.append(
            {
                "path": relative,
                "doc": doc,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )
        for block in chunk_text(text, min_chars=min_chars, max_chars=max_chars):
            start, end = block["para"], block["end_para"]
            chunk_id = f"{doc}#{start}" if start == end else f"{doc}#{start}-{end}"
            chunks.append(
                {
                    "id": chunk_id,
                    "doc": doc,
                    "path": relative,
                    "para": start,
                    "end_para": end,
                    "text": block["text"],
                }
            )
    return chunks, sources
