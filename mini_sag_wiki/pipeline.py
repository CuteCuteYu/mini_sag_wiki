"""检索主流程（本项目的核心）：

    1. 只对原文建 BM25 索引              -> bm25.py / corpus.py
    2. BM25 先粗筛                        -> Wiki.ask 里的 index.search(frontier, k=pool)
    3. JEV 判断哪些是有用证据             -> jev.judge_passages
    4. 从证据里挑出新关键词接着搜          -> extract_terms
    5. 线索靠原文关键词和段落串起来        -> clues / chain_queue / index.neighbors

第 4 步的关键词不是模型生成的，而是从**被认可的那段原文**里按 IDF 挑出来的词；
第 5 步除了换词再搜，还会沿着同一篇原文的相邻段落走一格——两者都只依赖原文本身，
所以不需要事先抽实体、建图谱。
"""

from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass

from .bm25 import BM25Index
from .jev import Jev
from .text import focus_snippet, snippet, terms, tokenize

SEED_WEIGHT = 0.45  # 原问题词在后续轮次的权重（保持话题锚点）
EXPAND_WEIGHT = 0.80  # 新挑出来的线索词权重（要能真正改变排序）


@dataclass
class AskConfig:
    pool: int = 12  # 每轮粗筛进 JEV 的候选段数
    target: int = 4  # 收集到多少条证据就收手
    rounds: int = 3  # 最多几轮「搜 -> 判 -> 扩词 -> 再搜」
    min_p: float = 0.50  # JEV 认可阈值
    terms_per_round: int = 4  # 每条证据里最多挑几个新线索词
    min_idf: float = 1.2  # 太常见的词当不了线索
    chain: int = 1  # 沿原文上下各看几段
    max_chars: int = 600  # 单条证据回给 AI 的字符上限（0 = 不截断）
    merge: bool = True  # 合并同一原文里相邻的证据段
    merge_max_chars: int = 1500  # 一次合并的字符上限
    merge_max_parts: int = 3  # 一次最多并几段
    max_evidence: int = 8  # 最终最多回几条证据
    workers: int = 4  # 并发问 JEV 的线程数
    debug: bool = False  # 输出被拒候选，便于调阈值


def extract_terms(
    text: str,
    idf,
    exclude: set[str],
    *,
    top: int = 4,
    min_idf: float = 1.2,
) -> list[str]:
    """从一段原文里挑「新线索词」：越罕见、在这段里出现越多，越可能是下一个入口。"""
    counts = Counter(tokenize(text))
    scored: list[tuple[float, str]] = []
    for term, freq in counts.items():
        if term in exclude or term.isdigit():
            continue
        # 英文三字母以内基本是 per / us / id 这类噪声；中文二元组本身就只有两个字
        if len(term) < (4 if term.isascii() else 2):
            continue
        weight = idf(term)
        if weight < min_idf:
            continue
        scored.append(((1.0 + math.log(freq)) * weight, term))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [term for _, term in scored[:top]]


@dataclass
class _Candidate:
    chunk: dict
    score: float
    matched: list[str]
    via: str
    origin: str
    p: float = 0.0

    @property
    def id(self) -> str:
        return self.chunk["id"]


def _span_id(doc: str, start: int, end: int) -> str:
    return f"{doc}#{start}" if start == end else f"{doc}#{start}-{end}"


def merge_evidence(
    items: list[dict], max_chars: int = 1500, max_parts: int = 3
) -> list[dict]:
    """把同一原文里相邻的证据段并成一块，避免把一句话拆成两条引用。

    合并有上限：超过 max_parts 段或 max_chars 字符就另起一条，否则一段
    链式召回会把整篇原文都并进来。
    """
    groups: list[dict] = []
    for item in sorted(items, key=lambda e: (e["source"], e["para"])):
        last = groups[-1] if groups else None
        if (
            last is not None
            and last["source"] == item["source"]
            and item["para"] <= last["end_para"] + 1
            and len(last["_children"]) < max_parts
            and len(last["text"]) + len(item["text"]) <= max_chars
        ):
            last["_children"].append(item)
            last["text"] = last["text"].rstrip() + "\n\n" + item["text"].lstrip()
            last["end_para"] = max(last["end_para"], item["end_para"])
            continue
        groups.append({**item, "_children": [item]})

    merged: list[dict] = []
    for group in groups:
        children = group.pop("_children")
        best = max(children, key=lambda e: (e["p"], e["bm25"]))
        group["id"] = _span_id(group["doc"], group["para"], group["end_para"])
        group["p"] = max(child["p"] for child in children)
        group["bm25"] = max(child["bm25"] for child in children)
        group["jev"] = {
            key: max(child["jev"][key] for child in children) for key in group["jev"]
        }
        group["via"] = best["via"]
        group["round"] = best["round"]
        group["parts"] = [child["id"] for child in children]
        merged.append(group)
    return merged


class Wiki:
    def __init__(self, index: BM25Index, jev: Jev | None = None) -> None:
        self.index = index
        self.jev = jev

    # ------------------------------------------------------------------ search
    def search(self, query: str, k: int = 10, max_chars: int = 300) -> dict:
        """只走 BM25 的粗筛，不花 JEV 的调用——调试和快速地形勘察用。"""
        query_terms = terms(query)
        hits = self.index.search({t: 1.0 for t in query_terms}, k=k)
        return {
            "schema": "mini_sag_wiki.search/1",
            "query": query,
            "terms": query_terms,
            "missing_terms": [t for t in query_terms if self.index.df(t) == 0],
            "hits": [
                {
                    "id": hit["chunk"]["id"],
                    "source": hit["chunk"]["path"],
                    "paras": [hit["chunk"]["para"], hit["chunk"]["end_para"]],
                    "score": hit["score"],
                    "matched": hit["matched"],
                    "text": snippet(hit["chunk"]["text"], max_chars),
                }
                for hit in hits
            ],
        }

    def get(self, ids: list[str], max_chars: int = 0) -> dict:
        """按证据 id 取回原文（支持 ``doc#3`` 与合并后的 ``doc#3-5``）。"""
        found: list[dict] = []
        missing: list[str] = []
        for raw in ids:
            chunk = self.index.get(raw)
            text = chunk["text"] if chunk else None
            paras = [chunk["para"], chunk["end_para"]] if chunk else None
            if chunk is None and "-" in raw.split("#", 1)[-1]:
                doc, span = raw.split("#", 1)
                parts = span.split("-")
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    lo, hi = int(parts[0]), int(parts[1])
                    pieces = [
                        c
                        for c in self.index.by_doc.get(doc, [])
                        if c["para"] >= lo and c["end_para"] <= hi
                    ]
                    if pieces:
                        text = "\n\n".join(c["text"] for c in pieces)
                        paras = [lo, hi]
                        chunk = pieces[0]
            if text is None:
                missing.append(raw)
                continue
            found.append(
                {
                    "id": raw,
                    "source": chunk["path"],
                    "paras": paras,
                    "text": snippet(text, max_chars) if max_chars else text,
                }
            )
        return {
            "schema": "mini_sag_wiki.get/1",
            "found": found,
            "missing": missing,
        }

    # --------------------------------------------------------------------- ask
    def ask(self, query: str, cfg: AskConfig | None = None) -> dict:
        cfg = cfg or AskConfig()
        if self.jev is None:
            raise RuntimeError("ask 需要 JEV；请先启动 jev.exe serve")
        started = time.perf_counter()

        query_terms = terms(query)
        if not query_terms:
            raise ValueError("查询里没有可用于检索的词项")

        frontier: dict[str, float] = {t: 1.0 for t in query_terms}
        origin: dict[str, str] = {t: "query" for t in query_terms}
        tried: set[str] = set(query_terms)
        expansion: list[str] = []
        seen: set[str] = set()
        chain_queue: list[tuple[str, str]] = []
        evidence: list[dict] = []
        clues: list[dict] = []
        clue_keys: set[tuple[str, str, str]] = set()
        trace: list[dict] = []
        rejected: list[dict] = []
        judged = 0
        requests = 0
        accepted_total = 0

        for round_no in range(1, cfg.rounds + 1):
            candidates: list[_Candidate] = []

            # (a) 段落线索：上一轮认可证据的相邻段落，直接排队
            pending, chain_queue = chain_queue, []
            for chunk_id, from_id in pending:
                if chunk_id in seen:
                    continue
                chunk = self.index.get(chunk_id)
                if chunk is None:
                    continue
                candidates.append(
                    _Candidate(chunk, 0.0, [], f"chain:{from_id}", from_id)
                )
            chained = {c.id for c in candidates}

            # (b) 关键词线索：BM25 粗筛
            hits = self.index.search(
                frontier, k=cfg.pool, exclude=seen | chained
            )
            for hit in hits:
                via_term = max(
                    hit["matched"], key=lambda t: frontier.get(t, 0.0), default=""
                )
                source = origin.get(via_term, "query")
                via = "seed" if source == "query" else "kw"
                candidates.append(
                    _Candidate(
                        hit["chunk"],
                        hit["score"],
                        hit["matched"],
                        f"{via}:{via_term}",
                        source,
                    )
                )
            candidates = candidates[: cfg.pool]
            if not candidates:
                break

            seen.update(c.id for c in candidates)

            # 记录线索边：谁把这段带出来的
            for cand in candidates:
                if cand.via.startswith("chain:"):
                    key = (cand.origin, "chain", cand.id)
                    if key not in clue_keys:
                        clue_keys.add(key)
                        clues.append(
                            {
                                "from": cand.origin,
                                "via": "chain",
                                "to": cand.id,
                                "round": round_no,
                            }
                        )
                elif cand.via.startswith("kw:") and cand.origin != "query":
                    key = (cand.origin, cand.via[3:], cand.id)
                    if key not in clue_keys:
                        clue_keys.add(key)
                        clues.append(
                            {
                                "from": cand.origin,
                                "via": cand.via[3:],
                                "to": cand.id,
                                "round": round_no,
                            }
                        )

            # 3) JEV 判断：这段原文对问题有没有用
            verdicts = self.jev.judge_passages(
                query, [c.chunk["text"] for c in candidates], workers=cfg.workers
            )
            judged += len(candidates)
            requests += len(candidates)

            accepted: list[_Candidate] = []
            for cand, verdict in zip(candidates, verdicts):
                cand_chunk = cand.chunk
                cand.p = verdict["p"]
                if verdict["p"] >= cfg.min_p:
                    accepted.append(cand)
                    evidence.append(
                        {
                            "id": cand.id,
                            "doc": cand_chunk["doc"],
                            "source": cand_chunk["path"],
                            "para": cand_chunk["para"],
                            "end_para": cand_chunk["end_para"],
                            "text": cand_chunk["text"],
                            "p": verdict["p"],
                            "jev": {
                                "useful": verdict["useful"],
                                "info": verdict["info"],
                            },
                            "bm25": cand.score,
                            "via": cand.via,
                            "round": round_no,
                        }
                    )
                elif cfg.debug:
                    rejected.append(
                        {
                            "id": cand.id,
                            "source": cand_chunk["path"],
                            "p": verdict["p"],
                            "bm25": cand.score,
                            "via": cand.via,
                        }
                    )

            accepted.sort(key=lambda c: (-c.p, -c.score))
            accepted_total += len(accepted)

            # 4) 从被认可的原文里挑新线索词
            new_terms: list[str] = []
            for cand in accepted:
                for term in extract_terms(
                    cand.chunk["text"],
                    lambda t: self.index.idf(t),
                    tried,
                    top=cfg.terms_per_round,
                    min_idf=cfg.min_idf,
                ):
                    tried.add(term)
                    origin[term] = cand.id
                    if term not in new_terms:
                        new_terms.append(term)
                        expansion.append(term)
                # 5) 段落线索：同原文相邻段落排队，下一轮判断
                for neighbour in self.index.neighbors(cand.id, cfg.chain):
                    if neighbour["id"] not in seen:
                        chain_queue.append((neighbour["id"], cand.id))

            trace.append(
                {
                    "round": round_no,
                    "candidates": len(candidates),
                    "accepted": len(accepted),
                    "new_terms": new_terms,
                    "top_p": round(
                        max((v["p"] for v in verdicts), default=0.0), 3
                    ),
                }
            )

            if len(evidence) >= cfg.target:
                break
            if not new_terms and not chain_queue:
                break

            frontier = {t: SEED_WEIGHT for t in query_terms}
            for term in new_terms:
                frontier[term] = EXPAND_WEIGHT

        # ------------------------------------------------------------ finalise
        merged = (
            merge_evidence(
                evidence,
                max_chars=cfg.merge_max_chars,
                max_parts=cfg.merge_max_parts,
            )
            if cfg.merge
            else evidence
        )
        merged.sort(key=lambda e: (-e["p"], -e["bm25"]))
        merged = merged[: cfg.max_evidence]

        adequacy = {"sufficient": 0.0, "direct": 0.0, "p": 0.0}
        if merged:
            requests += 1
            adequacy = self.jev.adequacy(query, [e["text"] for e in merged])

        for number, item in enumerate(merged, start=1):
            item["n"] = number

        accepted_ids = {
            part for item in merged for part in (item.get("parts") or [item["id"]])
        }
        for clue in clues:
            clue["hit"] = clue["to"] in accepted_ids

        warnings: list[str] = []
        missing = [t for t in query_terms if self.index.df(t) == 0]
        if missing and len(missing) == len(query_terms):
            warnings.append("query_not_in_index:" + ",".join(missing[:5]))
        if not merged:
            warnings.append("no_evidence_above_threshold")
        elif adequacy["p"] < 0.5:
            warnings.append("low_confidence")
        if len(merged) < cfg.target and len(trace) >= cfg.rounds:
            warnings.append("round_budget_exhausted")

        query_set = set(query_terms)
        evidence_out = []
        for item in merged:
            text = item["text"]
            evidence_out.append(
                {
                    "n": item["n"],
                    "id": item["id"],
                    "source": item["source"],
                    "paras": [item["para"], item["end_para"]],
                    "p": round(item["p"], 4),
                    "jev": {k: round(v, 4) for k, v in item["jev"].items()},
                    "bm25": round(item["bm25"], 3),
                    "via": item["via"],
                    "round": item["round"],
                    "parts": item["parts"],
                    "chars": len(text),
                    "text": focus_snippet(text, query_set, cfg.max_chars)
                    if cfg.max_chars
                    else text,
                }
            )

        result = {
            "schema": "mini_sag_wiki.ask/1",
            "query": query,
            "found": bool(evidence_out),
            "confidence": round(adequacy["p"], 4),
            "checks": {k: round(v, 4) for k, v in adequacy.items()},
            "rounds": len(trace),
            "keywords": {
                "query": query_terms,
                "expanded": expansion,
            },
            "unmatched_terms": missing,
            "evidence": evidence_out,
            "clues": clues,
            "trace": trace,
            "budget": {
                "candidates": judged,
                "judged": judged,
                "accepted": accepted_total,
                "jev_requests": requests,
                "evidence_chars": sum(e["chars"] for e in evidence_out),
            },
            "warnings": warnings,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        if cfg.debug:
            result["rejected"] = rejected
        if not merged:
            if judged == 0:
                result["hint"] = (
                    "索引里没有任何段落命中这些词：先用 `wiki search` 看看词表覆盖，"
                    "或换个说法/补充关键词"
                )
            else:
                result["hint"] = (
                    "BM25 有命中，但全部被 JEV 否决：换更完整的问句（例如把名词短语写成疑问句），"
                    "或用 --min-p 0.2 --debug 重新跑一遍看看边缘候选"
                )
        return result
