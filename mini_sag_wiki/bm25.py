"""一个零依赖的 BM25 索引，只索引原文段落。

存盘格式是纯 JSON：chunks 里带每段的词频表（tf），装载时重建倒排表。
这样「原文」永远是唯一事实来源，索引随时可以丢、随时可以重建。
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from .text import tokenize

INDEX_VERSION = 1
DEFAULT_K1 = 1.2
DEFAULT_B = 0.75


class IndexMissing(RuntimeError):
    """索引文件不存在。"""


class BM25Index:
    def __init__(
        self,
        chunks: list[dict],
        meta: dict | None = None,
        params: dict | None = None,
    ) -> None:
        self.chunks = chunks
        self.meta = meta or {}
        self.params = params or {"k1": DEFAULT_K1, "b": DEFAULT_B}
        self._build()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        total_len = 0
        for i, chunk in enumerate(self.chunks):
            tf = chunk.get("tf")
            if not tf:
                tf = {}
                for term in tokenize(chunk["text"]):
                    tf[term] = tf.get(term, 0) + 1
                chunk["tf"] = tf
            chunk["len"] = sum(tf.values())
            total_len += chunk["len"]
            for term, freq in tf.items():
                postings[term].append((i, freq))
        self.postings = dict(postings)
        self.n_chunks = len(self.chunks)
        self.avgdl = (total_len / self.n_chunks) if self.n_chunks else 0.0
        self.by_doc: dict[str, list[dict]] = defaultdict(list)
        for chunk in self.chunks:
            self.by_doc[chunk["doc"]].append(chunk)

    # ------------------------------------------------------------- parameters
    @property
    def k1(self) -> float:
        return float(self.params.get("k1", DEFAULT_K1))

    @property
    def b(self) -> float:
        return float(self.params.get("b", DEFAULT_B))

    def df(self, term: str) -> int:
        return len(self.postings.get(term, ()))

    def idf(self, term: str) -> float:
        df = self.df(term)
        if df == 0:
            return 0.0
        return math.log(1.0 + (self.n_chunks - df + 0.5) / (df + 0.5))

    # -------------------------------------------------------------- accessors
    def get(self, chunk_id: str) -> dict | None:
        for chunk in self.chunks:
            if chunk["id"] == chunk_id:
                return chunk
        return None

    def neighbors(self, chunk_id: str, distance: int = 1) -> list[dict]:
        """同一原文里紧邻的段落——线索靠段落串起来时用得上。"""
        chunk = self.get(chunk_id)
        if chunk is None:
            return []
        siblings = self.by_doc.get(chunk["doc"], [])
        try:
            pos = next(i for i, c in enumerate(siblings) if c["id"] == chunk_id)
        except StopIteration:
            return []
        out: list[dict] = []
        for step in range(1, max(1, distance) + 1):
            for index in (pos - step, pos + step):
                if 0 <= index < len(siblings):
                    out.append(siblings[index])
        return out

    # ----------------------------------------------------------------- search
    def search(
        self,
        query_terms: dict[str, float] | list[str],
        k: int = 10,
        exclude: set[str] | None = None,
    ) -> list[dict]:
        """返回 [{"chunk", "score", "matched"}]，按分数从高到低。"""
        exclude = exclude or set()
        weights: dict[str, float] = (
            {t: 1.0 for t in query_terms}
            if not isinstance(query_terms, dict)
            else dict(query_terms)
        )
        weights = {t: w for t, w in weights.items() if w > 0 and t in self.postings}
        if not weights or not self.n_chunks:
            return []

        scores: dict[int, float] = defaultdict(float)
        matched: dict[int, list[str]] = defaultdict(list)
        k1, b = self.k1, self.b
        avgdl = self.avgdl or 1.0

        for term, weight in weights.items():
            idf = self.idf(term)
            if idf <= 0:
                continue
            for index, freq in self.postings[term]:
                doc_len = self.chunks[index]["len"] or 1
                denominator = freq + k1 * (1.0 - b + b * doc_len / avgdl)
                scores[index] += weight * idf * freq * (k1 + 1.0) / denominator
                matched[index].append(term)

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        hits: list[dict] = []
        for index, score in ranked:
            chunk = self.chunks[index]
            if chunk["id"] in exclude:
                continue
            hits.append(
                {
                    "chunk": chunk,
                    "score": round(score, 6),
                    "matched": matched[index][:8],
                }
            )
            if len(hits) >= k:
                break
        return hits

    # ------------------------------------------------------------------ state
    def stats(self) -> dict[str, Any]:
        return {
            "chunks": self.n_chunks,
            "documents": len(self.by_doc),
            "terms": len(self.postings),
            "avg_chunk_terms": round(self.avgdl, 2),
            "built_at": self.meta.get("built_at"),
            "kb": self.meta.get("kb"),
            "params": self.params,
        }

    def sources(self) -> list[dict]:
        return list(self.meta.get("sources", []))

    def is_stale(self, kb_dir: Path, discover) -> bool:
        """KB 目录下的文件与索引记录对不上就算过期。"""
        recorded = {(s["path"], s["size"], s["mtime"]) for s in self.sources()}
        current = set()
        for path in discover(kb_dir):
            try:
                stat = path.stat()
            except OSError:
                continue
            current.add(
                (
                    path.relative_to(kb_dir).as_posix(),
                    stat.st_size,
                    int(stat.st_mtime),
                )
            )
        return recorded != current

    # ------------------------------------------------------------- persistence
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": INDEX_VERSION,
            "meta": self.meta,
            "params": self.params,
            "chunks": self.chunks,
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        if not path.exists():
            raise IndexMissing(f"索引不存在: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        version = payload.get("version")
        if version != INDEX_VERSION:
            raise IndexMissing(
                f"索引版本不匹配 ({version} != {INDEX_VERSION})，请重新运行 index"
            )
        return cls(
            payload.get("chunks", []),
            meta=payload.get("meta", {}),
            params=payload.get("params", {}),
        )

    @classmethod
    def build(
        cls,
        chunks: list[dict],
        *,
        kb: str,
        sources: list[dict],
        k1: float = DEFAULT_K1,
        b: float = DEFAULT_B,
    ) -> "BM25Index":
        meta = {
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "built_ts": int(time.time()),
            "kb": kb,
            "sources": sources,
        }
        return cls(chunks, meta=meta, params={"k1": k1, "b": b})
