#!/usr/bin/env python
"""独立示例：把 mini_sag_wiki 当**库**用，检索证据 → 交给 LLM 生成带出处的回答。

两段式，各管各的：

  1. 检索（本地）：BM25 粗筛 → JEV 判断证据 → 挑新关键词再搜 → 段落串联。
     这一步只产出**原文段落 + 概率**，不生成任何文字。
  2. 生成（可选，本示例演示）：把原文段落塞进 Anthropic 兼容的 Messages API，
     让模型只依据这些段落回答，并用 [n] 标注出处。

凭证与端点全部来自系统环境变量里 ``anthropic`` 开头的那几个：

  ANTHROPIC_AUTH_TOKEN   必填（除非只用 --dry-run），作为 ``Authorization: Bearer``
  ANTHROPIC_API_KEY      可选，没有 AUTH_TOKEN 时作为 ``x-api-key`` 发送
  ANTHROPIC_BASE_URL     默认 https://api.anthropic.com，可指向任何 Anthropic 兼容网关
  ANTHROPIC_MODEL        仅用于提示信息；本示例固定用 glm-5.3（可用 --model 覆盖）

用法：

  uv run python code_example/ask_wiki.py "how long is the rollback window"
  uv run python code_example/ask_wiki.py "退款政策是什么" --dry-run
  python code_example/ask_wiki.py "..."          # 装了依赖后也可以直接跑

前提：``jev.exe serve`` 在跑（默认 127.0.0.1:8017），并且已经 ``mini_sag_wiki index``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 允许 `python code_example/ask_wiki.py` 直接运行，不必先安装包
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mini_sag_wiki.bm25 import BM25Index, IndexMissing
from mini_sag_wiki.jev import Jev, JevError
from mini_sag_wiki.pipeline import AskConfig, Wiki

DEFAULT_MODEL = "glm-5.3"
DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

SYSTEM_PROMPT = """你是一个严谨的知识库助手。只能依据用户给出的 <evidence> 里的原文回答。

规则：
1. 每个事实性陈述后面用 [n] 标注它来自哪一条证据。
2. 证据不足以回答时，直接说「知识库中没有找到相关原文」，并说明还缺什么信息；不要用常识补全。
3. 证据之间互相矛盾时，把两种说法都列出来并分别标注出处。
4. 用提问者的语言回答，2-4 句，不要复述整个证据块。"""


# --------------------------------------------------------------------- 配置
def read_env(override_model: str | None = None) -> dict:
    """读取环境变量；只读取，不打印任何密钥内容。"""
    base = (os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    return {
        "base_url": base,
        "auth_token": os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip(),
        "api_key": os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        "model": override_model or DEFAULT_MODEL,
        "env_model": os.environ.get("ANTHROPIC_MODEL", ""),
    }


def endpoint_for(base_url: str) -> str:
    """兼容 base_url 是否已经带了 /v1。"""
    return base_url + ("/messages" if base_url.endswith("/v1") else "/v1/messages")


# --------------------------------------------------------------------- 提示词
def build_prompt(question: str, evidence: list[dict], max_chars: int) -> str:
    lines = [f"问题：{question}", "", "<evidence>"]
    for item in evidence:
        start, end = item["paras"]
        text = item["text"] if max_chars <= 0 else item["text"][:max_chars]
        lines.append(f"[{item['n']}] {item['source']}#{start}-{end}  (p={item['p']})")
        lines.append(text.strip())
        lines.append("")
    lines.append("</evidence>")
    lines.append("")
    lines.append("请只依据上面 <evidence> 里的原文回答，并用 [n] 标注出处。")
    return "\n".join(lines)


# --------------------------------------------------------------------- LLM
def call_llm(config: dict, prompt: str, *, max_tokens: int, timeout: float) -> dict:
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": ANTHROPIC_VERSION,
    }
    if config["auth_token"]:
        headers["Authorization"] = f"Bearer {config['auth_token']}"
    elif config["api_key"]:
        headers["x-api-key"] = config["api_key"]

    body = {
        "model": config["model"],
        "max_tokens": max_tokens,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(
        endpoint_for(config["base_url"]),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"LLM 返回 HTTP {error.code}: {detail}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"连不上 {config['base_url']}: {error.reason}（检查 ANTHROPIC_BASE_URL / 网络）"
        ) from None

    # 有些模型会先返回 thinking 块，只取 text 块
    text = "".join(
        block.get("text", "")
        for block in payload.get("content", [])
        if block.get("type") == "text"
    ).strip()
    return {
        "text": text,
        "model": payload.get("model"),
        "usage": payload.get("usage"),
        "stop_reason": payload.get("stop_reason"),
    }


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="mini_sag_wiki 库调用示例：检索 + LLM 生成带出处的回答"
    )
    parser.add_argument("question", help="要问的问题（建议写成完整疑问句）")
    parser.add_argument("--kb", default="data", help="知识库目录（默认 data）")
    parser.add_argument("--index-dir", default=None, help="索引目录（默认 <kb>/.wiki）")
    parser.add_argument("--jev-url", default=None, help="JEV 地址（默认 127.0.0.1:8017）")
    parser.add_argument("--target", type=int, default=4, help="最多用几条证据（默认 4）")
    parser.add_argument("--rounds", type=int, default=3, help="最多检索几轮（默认 3）")
    parser.add_argument("--min-p", type=float, default=0.5, help="JEV 认可阈值（默认 0.5）")
    parser.add_argument(
        "--evidence-chars",
        type=int,
        default=1200,
        help="每条证据喂给模型时的字符上限，0 = 不限（默认 1200）",
    )
    parser.add_argument("--model", default=None, help=f"默认 {DEFAULT_MODEL}")
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--dry-run", action="store_true", help="只检索并打印将要发送的提示词，不调用模型"
    )
    parser.add_argument("--show-retrieval", action="store_true", help="同时打印检索原始 JSON")
    args = parser.parse_args(argv)

    # ---- 1) 检索：纯本地，不需要任何 API key
    kb = Path(args.kb)
    index_path = Path(args.index_dir) / "bm25.json" if args.index_dir else kb / ".wiki" / "bm25.json"
    try:
        index = BM25Index.load(index_path)
    except IndexMissing as error:
        print(f"[x] {error}\n    先运行：uv run mini_sag_wiki index --kb {kb}", file=sys.stderr)
        return 4

    wiki = Wiki(index, Jev(url=args.jev_url))
    try:
        result = wiki.ask(
            args.question,
            AskConfig(
                target=args.target,
                rounds=args.rounds,
                min_p=args.min_p,
                max_chars=0,  # 先拿全文，喂模型时再截断
            ),
        )
    except JevError as error:
        print(f"[x] JEV 不可用：{error}", file=sys.stderr)
        if error.hint:
            print(f"    {error.hint}", file=sys.stderr)
        return 3

    if args.show_retrieval:
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)

    print(f"问题：{args.question}")
    print(
        f"检索：{len(result['evidence'])} 条证据，confidence={result['confidence']}，"
        f"rounds={result['rounds']}，kb 段落数={index.stats()['chunks']}"
    )

    if not result["found"]:
        print("\n知识库里没有找到相关原文。")
        if result.get("hint"):
            print(f"提示：{result['hint']}")
        if result["warnings"]:
            print(f"warnings: {', '.join(result['warnings'])}")
        return 0

    # ---- 2) 生成：把原文交给 LLM
    prompt = build_prompt(args.question, result["evidence"], args.evidence_chars)
    config = read_env(args.model)

    if not (config["auth_token"] or config["api_key"]):
        print(
            "\n[x] 没有找到凭证：请设置 ANTHROPIC_AUTH_TOKEN 或 ANTHROPIC_API_KEY。",
            file=sys.stderr,
        )
        print("    只想看检索结果的话，加 --dry-run 即可跳过模型调用。", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"\n--- 将要发送给 {config['model']} 的提示词 ---\n{prompt}")
        return 0

    try:
        answer = call_llm(
            config, prompt, max_tokens=args.max_tokens, timeout=args.timeout
        )
    except RuntimeError as error:
        print(f"\n[x] {error}", file=sys.stderr)
        return 3

    print()
    print(answer["text"] or "（模型没有返回文本内容）")

    print("\n依据：")
    for item in result["evidence"]:
        start, end = item["paras"]
        preview = item["text"].replace("\n", " ")[:90]
        print(f"  [{item['n']}] {item['source']}#{start}-{end}  p={item['p']}  {preview}…")

    usage = answer.get("usage") or {}
    print(
        f"\n(model={answer.get('model') or config['model']}, "
        f"input_tokens={usage.get('input_tokens')}, output_tokens={usage.get('output_tokens')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
