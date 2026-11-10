"""面向 AI 的 CLI。

约定（对调用方友好优先）：
  * stdout 上永远只有一个 JSON 对象，日志/错误提示走 stderr；
  * 每条命令都有稳定的 ``schema`` 字段（``mini_sag_wiki.<cmd>/1``），字段只增不改；
  * 不交互、不分页、不输出颜色，失败也是结构化 JSON；
  * 退出码：0 正常，2 参数/输入错误，3 JEV 不可用，4 索引缺失。
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import sys
from pathlib import Path

from . import __version__
from .bm25 import BM25Index, IndexMissing
from .corpus import build_chunks, discover
from .jev import Jev, JevError
from .pipeline import AskConfig, Wiki

DEFAULT_KB = "data"
DEFAULT_INDEX_DIR = None  # None = <kb>/.wiki
INDEX_FILE = "bm25.json"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_JEV = 3
EXIT_INDEX = 4

HELP = {
    "schema": "mini_sag_wiki.help/1",
    "name": "mini_sag_wiki",
    "alias": ["wiki"],
    "version": __version__,
    "about": "只用原文段落建 BM25 索引，用 JEV 判断证据，再从证据里挑新关键词继续搜。",
    "flow": [
        "index：只对知识库原文建 BM25 关键词索引",
        "search：BM25 粗筛（不花 JEV）",
        "ask：BM25 粗筛 -> JEV 判断有用-> 从证据里挑新词 -> 再搜 -> 段落串联",
        "get：按证据 id 取回原文",
    ],
    "commands": [
        {
            "name": "ask",
            "usage": "mini_sag_wiki ask QUERY [--rounds 3] [--pool 12] [--target 4] [--min-p 0.5] [--max-chars 600] [--debug]",
            "returns": "mini_sag_wiki.ask/1",
            "summary": "完整检索：返回证据段落、JEV 概率、关键词线索链、覆盖度自查。",
        },
        {
            "name": "search",
            "usage": "mini_sag_wiki search QUERY [-k 10]",
            "returns": "mini_sag_wiki.search/1",
            "summary": "只做 BM25 粗筛，用于快速探查词表覆盖。",
        },
        {
            "name": "get",
            "usage": "mini_sag_wiki get ID [ID ...] [--max-chars 0]",
            "returns": "mini_sag_wiki.get/1",
            "summary": "按证据 id（如 atlas-deploy#3-4）取回未被截断的原文。",
        },
        {
            "name": "index",
            "usage": "mini_sag_wiki index [--min-chars 140] [--max-chars 1200]",
            "returns": "mini_sag_wiki.index/1",
            "summary": "重建 BM25 索引（只索引原文，不抽实体）。",
        },
        {
            "name": "ingest",
            "usage": "mini_sag_wiki ingest PATH... [--index] [--name NAME  # 读 stdin 时用 -]",
            "returns": "mini_sag_wiki.ingest/1",
            "summary": "把原始文件收进知识库。",
        },
        {
            "name": "status",
            "usage": "mini_sag_wiki status",
            "returns": "mini_sag_wiki.status/1",
            "summary": "知识库、索引新鲜度、JEV 服务状态。",
        },
        {
            "name": "doctor",
            "usage": "mini_sag_wiki doctor",
            "returns": "mini_sag_wiki.doctor/1",
            "summary": "自检：索引是否可读、jev 是否在跑。",
        },
        {
            "name": "help",
            "usage": "mini_sag_wiki help",
            "returns": "mini_sag_wiki.help/1",
            "summary": "输出本清单。",
        },
    ],
    "env": {
        "MINI_SAG_WIKI_JEV_URL": "jev 服务地址，默认 http://127.0.0.1:8017",
        "MINI_SAG_WIKI_JEV_MODEL": "模型名，默认 jev-latest",
        "MINI_SAG_WIKI_JEV_API_KEY": "可选，Bearer token",
    },
}


# --------------------------------------------------------------------- helpers
def _index_path(args) -> Path:
    """索引跟着知识库走：默认放在 <kb>/.wiki/bm25.json（点目录不会被当原文扫到）。"""
    if getattr(args, "index_dir", None):
        return Path(args.index_dir) / INDEX_FILE
    return Path(args.kb) / ".wiki" / INDEX_FILE


def _load_index(args) -> BM25Index:
    return BM25Index.load(_index_path(args))


def _jev(args) -> Jev:
    return Jev(url=getattr(args, "jev_url", None), model=getattr(args, "model", None))


def _emit(payload: dict, args) -> None:
    fmt = getattr(args, "format", "json")
    if fmt == "text":
        print(_render_text(payload))
        return
    pretty = getattr(args, "pretty", False)
    if pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), default=str
            )
        )


def _render_text(payload: dict) -> str:
    schema = payload.get("schema", "")
    if schema == "mini_sag_wiki.ask/1":
        lines = [
            f"query: {payload['query']}",
            f"found: {payload['found']}  confidence: {payload['confidence']:.3f}  "
            f"rounds: {len(payload['trace'])}  evidence: {len(payload['evidence'])}",
        ]
        for item in payload["evidence"]:
            start, end = item["paras"]
            lines.append("")
            lines.append(
                f"[{item['n']}] {item['source']}#{start}-{end}  "
                f"p={item['p']:.2f}  via={item['via']}"
            )
            lines.append(item["text"])
        if payload.get("clues"):
            lines.append("")
            lines.append(
                "clues: "
                + "  ".join(
                    f"{c['from']} -{c['via']}-> {c['to']}"
                    for c in payload["clues"][:10]
                )
            )
        if payload.get("warnings"):
            lines.append("warnings: " + ", ".join(payload["warnings"]))
        return "\n".join(lines)

    if schema == "mini_sag_wiki.search/1":
        lines = [f"query: {payload['query']}  terms: {' '.join(payload['terms'])}"]
        for hit in payload["hits"]:
            lines.append(
                f"{hit['score']:8.3f}  {hit['source']}#{hit['paras'][0]}-{hit['paras'][1]}  "
                f"({','.join(hit['matched'][:4])})"
            )
            lines.append(f"         {hit['text']}")
        return "\n".join(lines)

    return json.dumps(payload, ensure_ascii=False, indent=2)


# -------------------------------------------------------------------- commands
def cmd_help(args):
    return EXIT_OK, HELP


def cmd_status(args):
    kb = Path(args.kb)
    files = discover(kb)
    payload = {
        "schema": "mini_sag_wiki.status/1",
        "version": __version__,
        "kb": {"dir": str(kb.resolve()), "exists": kb.exists(), "files": len(files)},
        "index": {"path": str(_index_path(args)), "exists": False},
        "jev": _jev(args).ping(),
    }
    try:
        index = _load_index(args)
    except IndexMissing:
        payload["index"]["hint"] = "运行 mini_sag_wiki index 建立索引"
        return EXIT_OK, payload
    payload["index"] = {
        **index.stats(),
        "path": str(_index_path(args)),
        "exists": True,
        "stale": index.is_stale(kb, discover),
    }
    return EXIT_OK, payload


def cmd_index(args):
    kb = Path(args.kb)
    if not kb.exists():
        raise FileNotFoundError(f"知识库目录不存在: {kb}")
    chunks, sources = build_chunks(
        kb,
        min_chars=getattr(args, "min_chars", 140),
        max_chars=getattr(args, "max_chars", 1200),
    )
    if not chunks:
        raise ValueError(f"{kb} 里没有可索引的文本文件")
    index = BM25Index.build(
        chunks,
        kb=str(kb.resolve()),
        sources=sources,
        k1=getattr(args, "k1", 1.2),
        b=getattr(args, "b", 0.75),
    )
    index.save(_index_path(args))
    return EXIT_OK, {
        "schema": "mini_sag_wiki.index/1",
        "ok": True,
        **index.stats(),
        "path": str(_index_path(args)),
    }


def _store(kb: Path, name: str, data: bytes) -> dict:
    """把一个文件写进知识库；同名不同内容存成 name-<hash>.ext，避免覆盖。"""
    digest = hashlib.sha256(data).hexdigest()[:8]
    target = kb / name
    status = "added"
    if target.exists():
        if target.read_bytes() == data:
            status = "unchanged"
        else:
            target = kb / f"{Path(name).stem}-{digest}{Path(name).suffix}"
            if target.exists() and target.read_bytes() == data:
                status = "unchanged"
            else:
                status = "added"
    if status == "added":
        target.write_bytes(data)
    return {
        "name": target.relative_to(kb).as_posix(),
        "status": status,
        "bytes": len(data),
    }


def cmd_ingest(args):
    kb = Path(args.kb)
    kb.mkdir(parents=True, exist_ok=True)
    results = []
    for source in args.paths:
        if source == "-":
            if not args.name:
                raise ValueError("从 stdin 读取时必须用 --name 指定文件名")
            results.append(_store(kb, args.name, sys.stdin.buffer.read()))
            continue

        # 自己展开通配符：PowerShell 不会给原生命令展开 glob
        matches = sorted(glob.glob(source))
        if not matches:
            if Path(source).is_file():
                matches = [source]
            else:
                raise FileNotFoundError(f"文件不存在: {source}")
        for match in matches:
            path = Path(match)
            if not path.is_file():
                continue
            results.append(_store(kb, args.name or path.name, path.read_bytes()))

    payload = {
        "schema": "mini_sag_wiki.ingest/1",
        "kb": str(kb.resolve()),
        "files": results,
    }
    if args.index:
        code, indexed = cmd_index(args)
        payload["index"] = indexed
        return code, payload
    payload["hint"] = "运行 mini_sag_wiki index 让新文件进入索引"
    return EXIT_OK, payload


def cmd_search(args):
    wiki = Wiki(_load_index(args))
    payload = wiki.search(args.query, k=args.k, max_chars=args.max_chars)
    return EXIT_OK, payload


def cmd_get(args):
    wiki = Wiki(_load_index(args))
    return EXIT_OK, wiki.get(args.ids, max_chars=args.max_chars)


def cmd_ask(args):
    jev = _jev(args)
    wiki = Wiki(_load_index(args), jev)
    cfg = AskConfig(
        pool=args.pool,
        target=args.target,
        rounds=args.rounds,
        min_p=args.min_p,
        terms_per_round=args.terms,
        min_idf=args.min_idf,
        chain=args.chain,
        max_chars=args.max_chars,
        merge=not args.no_merge,
        max_evidence=args.max_evidence,
        workers=args.workers,
        debug=args.debug,
    )
    payload = wiki.ask(args.query, cfg)
    payload["jev"] = {"url": jev.url, "model": jev.model}
    return EXIT_OK, payload


def cmd_doctor(args):
    checks = []
    kb = Path(args.kb)
    checks.append(
        {
            "check": "kb",
            "ok": kb.exists(),
            "detail": f"{kb.resolve()} ({len(discover(kb))} files)",
        }
    )
    try:
        index = _load_index(args)
        checks.append(
            {
                "check": "index",
                "ok": True,
                "detail": f"{index.stats()['chunks']} chunks, built {index.stats()['built_at']}",
            }
        )
        stale = index.is_stale(kb, discover)
        checks.append(
            {
                "check": "index_fresh",
                "ok": not stale,
                "detail": "stale" if stale else "fresh",
            }
        )
    except IndexMissing as error:
        checks.append({"check": "index", "ok": False, "detail": str(error)})

    ping = _jev(args).ping()
    checks.append(
        {
            "check": "jev",
            "ok": bool(ping.get("ok")),
            "detail": ping if not ping.get("ok") else f"{ping.get('model')} in {ping.get('ms')} ms",
        }
    )
    ok = all(item["ok"] for item in checks if item["check"] != "index_fresh")
    payload = {
        "schema": "mini_sag_wiki.doctor/1",
        "ok": ok,
        "checks": checks,
        "jev": ping,
    }
    if not ping.get("ok"):
        return EXIT_JEV, payload
    if not any(c["check"] == "index" and c["ok"] for c in checks):
        return EXIT_INDEX, payload
    return EXIT_OK, payload


# ---------------------------------------------------------------------- parser
def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--kb", default=DEFAULT_KB, help=f"知识库目录（默认 {DEFAULT_KB}）")
    parser.add_argument(
        "--index-dir",
        default=DEFAULT_INDEX_DIR,
        help="索引目录（默认 <kb>/.wiki）",
    )
    parser.add_argument(
        "--format", choices=["json", "text"], default="json", help="默认 json"
    )
    parser.add_argument("--pretty", action="store_true", help="JSON 缩进")


def _add_jev(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--jev-url", default=None, help="默认 http://127.0.0.1:8017")
    parser.add_argument("--model", default=None, help="默认 jev-latest")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini_sag_wiki", description=HELP["about"], add_help=True
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    p = subparsers.add_parser("help", help="输出命令清单（JSON）")
    _add_common(p)
    p.set_defaults(func=cmd_help)

    p = subparsers.add_parser("status", help="知识库/索引/JEV 状态")
    _add_common(p)
    _add_jev(p)
    p.set_defaults(func=cmd_status)

    p = subparsers.add_parser("index", help="重建 BM25 索引")
    _add_common(p)
    p.add_argument("--min-chars", type=int, default=140)
    p.add_argument("--max-chars", type=int, default=1200)
    p.add_argument("--k1", type=float, default=1.2)
    p.add_argument("--b", type=float, default=0.75)
    p.set_defaults(func=cmd_index)

    p = subparsers.add_parser("ingest", help="把原文收进知识库")
    _add_common(p)
    p.add_argument("paths", nargs="+", help="文件路径（支持通配符），用 - 从 stdin 读")
    p.add_argument("--name", default=None, help="落盘文件名（stdin 时必填）")
    p.add_argument("--index", action="store_true", help="顺便重建索引")
    p.set_defaults(func=cmd_ingest)

    p = subparsers.add_parser("search", help="只做 BM25 粗筛")
    _add_common(p)
    p.add_argument("query")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--max-chars", type=int, default=300)
    p.set_defaults(func=cmd_search)

    p = subparsers.add_parser("get", help="按证据 id 取回原文")
    _add_common(p)
    p.add_argument("ids", nargs="+")
    p.add_argument("--max-chars", type=int, default=0)
    p.set_defaults(func=cmd_get)

    p = subparsers.add_parser("ask", help="完整检索（BM25 + JEV + 扩词 + 串联）")
    _add_common(p)
    _add_jev(p)
    p.add_argument("query")
    p.add_argument("--rounds", type=int, default=3, help="最多几轮检索")
    p.add_argument("--pool", type=int, default=12, help="每轮送 JEV 的候选段数")
    p.add_argument("--target", type=int, default=4, help="收集到几条证据就停")
    p.add_argument("--max-evidence", type=int, default=8)
    p.add_argument("--min-p", type=float, default=0.5, help="JEV 认可阈值")
    p.add_argument("--terms", type=int, default=4, help="每条证据挑几个新线索词")
    p.add_argument("--min-idf", type=float, default=1.2, help="线索词的最低 IDF")
    p.add_argument("--chain", type=int, default=1, help="上下各串几段")
    p.add_argument("--max-chars", type=int, default=600, help="单条证据字符上限，0=全文")
    p.add_argument("--no-merge", action="store_true", help="不合并相邻证据段")
    p.add_argument("--workers", type=int, default=4, help="并发问 JEV 的线程数")
    p.add_argument("--debug", action="store_true", help="附带被拒候选")
    p.set_defaults(func=cmd_ask)

    p = subparsers.add_parser("doctor", help="环境自检")
    _add_common(p)
    _add_jev(p)
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass

    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        argv = ["help"]
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        args.func = cmd_help
        _emit(HELP, args)
        return EXIT_OK

    try:
        code, payload = args.func(args)
    except JevError as error:
        _emit({"schema": "mini_sag_wiki.error/1", "ok": False, "error": error.as_dict()}, args)
        print(f"wiki: {error}", file=sys.stderr)
        return EXIT_JEV
    except IndexMissing as error:
        _emit(
            {
                "schema": "mini_sag_wiki.error/1",
                "ok": False,
                "error": {
                    "code": "index_missing",
                    "message": str(error),
                    "hint": "先运行 `mini_sag_wiki index` 建立索引",
                },
            },
            args,
        )
        print(f"wiki: {error}", file=sys.stderr)
        return EXIT_INDEX
    except (ValueError, FileNotFoundError, OSError) as error:
        _emit(
            {
                "schema": "mini_sag_wiki.error/1",
                "ok": False,
                "error": {
                    "code": "bad_request",
                    "message": str(error),
                },
            },
            args,
        )
        print(f"wiki: {error}", file=sys.stderr)
        return EXIT_USAGE

    _emit(payload, args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
