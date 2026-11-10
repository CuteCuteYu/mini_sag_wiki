"""纯词法文本工具：BM25 分词、按段落切块、摘要截断。

这里没有任何实体抽取、没有模型调用：所谓「关键词」就是原文里真实出现过的词，
只是按 IDF 从常见到罕见排了个序。
"""

from __future__ import annotations

import re

# 中日韩统一表意文字、扩展 A、兼容表意文字、假名、谚文
_CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7AF),
)

_CJK_CLASS = "\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3040-\u30FF\uAC00-\uD7AF"

# 拉丁词 / 数字 / 连续 CJK 片段
_TOKEN_RE = re.compile(
    rf"[A-Za-z][A-Za-z0-9_+#.'\-]*"
    rf"|\d+(?:[.,]\d+)*%?"
    rf"|[{_CJK_CLASS}]+"
)

EN_STOPWORDS = frozenset(
    """a an and are as at be been being but by can could did do does doing done for from
    had has have having he her here hers him his how i if in into is it its itself just
    may me might more most must my no nor not of off on once only or other our out over
    own same she should so some such than that the their them then there these they this
    those through to too under until up very was we were what when where which while who
    whom why will with would you your
    happen become seem want know say get give take go come try keep put use make
    first second next last also still even much many one two
    """.split()
)

# 单字虚词：出现在 CJK 单字 token 位置时直接丢掉
CJK_STOP_CHARS = frozenset(
    "的了和是在有与及或个我你他她它这那就都而也很被把对为以于上下中来说能会要不没还但如所其之者吗呢吧啊哦嗯"
    "一二三四五六七八九十"
)

# 高频双字组合：作为检索线索毫无区分度
CJK_STOP_BIGRAMS = frozenset(
    """的话 我们 你们 他们 她们 这个 那个 这些 那些 什么 怎么 可以 因为 所以 如果
    但是 而且 已经 一个 一些 没有 不是 就是 这样 那样 时候 现在 以及 并且 或者 还是
    只是 由于 通过 进行 需要 使用 相关 内容 问题 方法 情况 方面 目前 可能 应该 一定
    所有 任何 每个 其他 自己 如何 这里 那里 因此 然后 同时 例如 比如 包括""".split()
)


def is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def _stem(word: str) -> str:
    """极简英文词干化：让 returns / returned / returning 落到同一个词上。"""
    if len(word) < 4 or not word.isalpha():
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    if word.endswith("ing") and len(word) > 6:
        base = word[:-3]
        if len(base) >= 3 and base[-1] == base[-2]:
            base = base[:-1]
        return base
    if word.endswith("ed") and len(word) > 5:
        base = word[:-2]
        if len(base) >= 3 and base[-1] == base[-2]:
            base = base[:-1]
        return base
    return word


def tokenize(text: str) -> list[str]:
    """把文本切成 BM25 用的词项。

    拉丁文按词（带轻量词干化），CJK 按二元组（bigram）——二元组对中文的召回和精度
    都比单字好，且不需要词典或分词模型。
    """
    out: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        if is_cjk(token[0]):
            size = len(token)
            if size == 1:
                if token not in CJK_STOP_CHARS:
                    out.append(token)
                continue
            for i in range(size - 1):
                bigram = token[i : i + 2]
                if bigram in CJK_STOP_BIGRAMS:
                    continue
                if bigram[0] in CJK_STOP_CHARS and bigram[1] in CJK_STOP_CHARS:
                    continue
                out.append(bigram)
            continue
        word = token.lower().strip(".-'")
        if not word:
            continue
        if word.isdigit():
            out.append(word)
            continue
        if len(word) < 2:
            continue
        stemmed = _stem(word)
        if word in EN_STOPWORDS or stemmed in EN_STOPWORDS:
            continue
        out.append(stemmed)
    return out


def terms(text: str) -> list[str]:
    """去重后的词项，保持首次出现顺序。"""
    return list(dict.fromkeys(tokenize(text)))


def split_paragraphs(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def split_sentences(text: str) -> list[str]:
    # 中文断句标点；英文句号后跟空格 + 大写字母；换行
    parts = re.split(r"(?<=[。！？!?；;])|(?<=\.)\s+(?=[A-Z0-9`\"(\[])|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def chunk_text(text: str, min_chars: int = 140, max_chars: int = 1200) -> list[dict]:
    """按原文段落切块；过短的段落向后合并，过长的段落按句子拆开。

    返回 [{"para": 起始段号, "end_para": 结束段号, "text": 原文}]，段号从 0 开始，
    便于把证据指回「原文第几段」。
    """
    paragraphs = split_paragraphs(text)
    blocks: list[tuple[int, str]] = []
    for index, paragraph in enumerate(paragraphs):
        if len(paragraph) <= max_chars:
            blocks.append((index, paragraph))
        else:
            for sentence in split_sentences(paragraph):
                while len(sentence) > max_chars:  # 极端长句，硬切
                    blocks.append((index, sentence[:max_chars]))
                    sentence = sentence[max_chars:]
                if sentence:
                    blocks.append((index, sentence))

    chunks: list[dict] = []
    buffer: list[str] = []
    start: int | None = None
    end = 0
    size = 0

    def flush() -> None:
        nonlocal buffer, start, end, size
        if buffer and start is not None:
            chunks.append(
                {
                    "para": start,
                    "end_para": end,
                    "text": "\n\n".join(buffer).strip(),
                }
            )
        buffer, start, end, size = [], None, 0, 0

    for index, block in blocks:
        if start is None:
            start = index
        buffer.append(block)
        end = index
        size += len(block)
        if size >= min_chars:
            flush()
    flush()
    return chunks


def snippet(text: str, limit: int) -> str:
    """把一段原文压到 limit 字符以内，尽量断在句子边界上。"""
    flat = re.sub(r"\s+", " ", text).strip()
    if limit <= 0 or len(flat) <= limit:
        return flat
    cut = flat[:limit]
    for sep in ("。", "！", "？", "；", ". ", "! ", "? ", "; ", ", "):
        pos = cut.rfind(sep)
        if pos > limit * 0.5:
            return cut[: pos + len(sep)].rstrip() + " …"
    return cut.rstrip() + " …"


def focus_snippet(text: str, query_terms: set[str], limit: int) -> str:
    """截断时围绕「命中查询词最多的那句」取窗口，别把答案截掉。"""
    flat = re.sub(r"\s+", " ", text).strip()
    if limit <= 0 or len(flat) <= limit:
        return flat
    sentences = split_sentences(flat)
    if len(sentences) <= 1 or not query_terms:
        return snippet(flat, limit)

    best, best_hits = 0, -1
    for index, sentence in enumerate(sentences):
        hits = len(set(tokenize(sentence)) & query_terms)
        if hits > best_hits:
            best, best_hits = index, hits

    chosen = [best]
    total = len(sentences[best])
    forward, backward = best + 1, best - 1
    while True:
        grew = False
        if forward < len(sentences) and total + len(sentences[forward]) + 1 <= limit:
            chosen.append(forward)
            total += len(sentences[forward]) + 1
            forward += 1
            grew = True
        if backward >= 0 and total + len(sentences[backward]) + 1 <= limit:
            chosen.insert(0, backward)
            total += len(sentences[backward]) + 1
            backward -= 1
            grew = True
        if not grew:
            break

    chosen.sort()
    body = " ".join(sentences[i] for i in chosen)
    if len(body) > limit:
        return snippet(body, limit)
    head = "" if chosen[0] == 0 else "… "
    tail = "" if chosen[-1] == len(sentences) - 1 else " …"
    return head + body + tail
