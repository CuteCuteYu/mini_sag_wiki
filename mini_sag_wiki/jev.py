"""JEV 客户端：只问 yes/no，只拿回概率。

线协议与 TypeSafe Jev 一致：``POST /v1/systemone``，body 里 ``model`` + ``state`` +
``questions``，每个 noul 问题回来一个 0~1 的 ``P(yes)``。一次请求可以带多个问题，
而 ``state`` 只读一次——所以「一段原文 + 两个判断」比「一段原文 + 一个判断」贵不了多少。

三个实测结论决定了这里的写法：

1. **问题必须写进 instruction，不能只放 state**。把 ``{"question", "passage"}`` 一起塞进
   state 时，jevos-v3 几乎对任何片段都回答 0.8 以上（把「相关」当成「格式正确」）；
   改成 ``state=原文片段``、问题写进指令后，无关片段会稳定落到 0.1 以下。
2. **问法比权重重要**。同一批样本上，``"Is the passage related to this question?"``
   这种问法会把「讲到 downgrade 的段落」判成 0.02，换成``"Does the passage help answer it?"``
   /``"Does the passage contain information that answers ...?"`` 后两类样本分得干干净净。
   两句一起问再取平均，10 个标定样本上相关段 ≥ 0.93、无关段 ≤ 0.26。
3. **指令用英文写**。jevos 的发布说明标注以英语为主；实测即使原文是中文，
   英文指令 + 中文 state 仍然能正确区分（相关 0.94 / 无关 0.02）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_URL = "http://127.0.0.1:8017"
DEFAULT_MODEL = "jev-latest"

# —— 判断「这段原文有用吗」的两问；{question} 会替换成原问题，state 只放原文
Q_USEFUL = (
    "The passage is evidence for this question: {question} "
    "Does the passage help answer it?"
)
Q_INFO = (
    "Does the passage contain information that answers the following question? "
    "{question}"
)

# —— 最终覆盖度自查
Q_SUFFICIENT = (
    "Question: {question} Do the passages contain enough information to answer it?"
)
Q_DIRECT = (
    "Question: {question} Do the passages answer it directly, rather than only "
    "mentioning related topics?"
)

USEFUL_WEIGHT = 0.5
INFO_WEIGHT = 0.5


class JevError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.status = status

    def as_dict(self) -> dict:
        payload: dict = {"code": self.code, "message": str(self)}
        if self.status is not None:
            payload["status"] = self.status
        if self.hint:
            payload["hint"] = self.hint
        return payload


class Jev:
    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        *,
        timeout: float = 60.0,
        api_key: str | None = None,
    ) -> None:
        self.url = (
            url or os.environ.get("MINI_SAG_WIKI_JEV_URL") or DEFAULT_URL
        ).rstrip("/")
        self.model = (
            model or os.environ.get("MINI_SAG_WIKI_JEV_MODEL") or DEFAULT_MODEL
        )
        self.timeout = timeout
        self.api_key = api_key or os.environ.get("MINI_SAG_WIKI_JEV_API_KEY")

    # ------------------------------------------------------------------ plumbing
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url + path, data=data, headers=self._headers(), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise JevError(
                "jev_http_error",
                f"jev 返回 HTTP {error.code}: {detail[:500]}",
                status=error.code,
                hint="检查 state 是否为空、问题文本是否超长（>8000 字符）",
            ) from None
        except urllib.error.URLError as error:
            raise JevError(
                "jev_unavailable",
                f"连不上 jev ({self.url}): {error.reason}",
                hint="先启动服务：在 jev 目录运行 `jev.exe serve`，或用 MINI_SAG_WIKI_JEV_URL 指定地址",
            ) from None
        except (TimeoutError, OSError) as error:
            raise JevError(
                "jev_unreachable",
                f"请求 jev 失败 ({self.url}): {error}",
                hint="确认 jev.exe serve 仍在运行",
            ) from None
        except json.JSONDecodeError as error:
            raise JevError(
                "jev_bad_response", f"jev 返回了非 JSON 内容: {error}"
            ) from None

    # ------------------------------------------------------------------- public
    def health(self) -> dict:
        return self._request("GET", "/health")

    def ping(self) -> dict:
        """轻量探活：返回 {ok, model, ms}，不会抛异常。"""
        import time

        started = time.perf_counter()
        try:
            payload = self.health()
        except JevError as error:
            return {"ok": False, "url": self.url, "error": error.as_dict()}
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        return {
            "ok": True,
            "url": self.url,
            "model": payload.get("model"),
            "status": payload.get("status"),
            "ms": elapsed,
        }

    def decide(self, state, questions: dict[str, str]) -> dict[str, float]:
        """一次请求问多个 yes/no 问题，返回 {问题名: P(yes)}。"""
        if not questions:
            return {}
        body = {
            "model": self.model,
            "state": state,
            "questions": {
                name: {"type": "noul", "instructions": text}
                for name, text in questions.items()
            },
        }
        payload = self._request("POST", "/v1/systemone", body)
        answers = payload.get("answers") or {}
        return {
            name: float(answers.get(name, {}).get("noul", 0.0))
            for name in questions
        }

    # ------------------------------------------------------------------ judging
    def judge_passage(self, question: str, passage: str) -> dict:
        """判断一段原文对问题是否有用。P 是两问的平均，保留原始两问便于解释。"""
        scores = self.decide(
            passage,
            {
                "useful": Q_USEFUL.format(question=question),
                "info": Q_INFO.format(question=question),
            },
        )
        useful = scores.get("useful", 0.0)
        info = scores.get("info", 0.0)
        return {
            "useful": round(useful, 6),
            "info": round(info, 6),
            "p": round(USEFUL_WEIGHT * useful + INFO_WEIGHT * info, 6),
        }

    def judge_passages(
        self, question: str, passages: list[str], workers: int = 4
    ) -> list[dict]:
        """并发判断多段原文（每段一个请求，两问共享同一次读）。"""
        if not passages:
            return []
        if len(passages) == 1 or workers <= 1:
            return [self.judge_passage(question, text) for text in passages]
        with ThreadPoolExecutor(max_workers=min(workers, len(passages))) as pool:
            futures = [
                pool.submit(self.judge_passage, question, text) for text in passages
            ]
            return [future.result() for future in futures]

    def adequacy(self, question: str, passages: list[str]) -> dict:
        """最终自查：现有证据够不够回答问题。"""
        if not passages:
            return {"sufficient": 0.0, "direct": 0.0, "p": 0.0}
        scores = self.decide(
            passages,
            {
                "sufficient": Q_SUFFICIENT.format(question=question),
                "direct": Q_DIRECT.format(question=question),
            },
        )
        sufficient = scores.get("sufficient", 0.0)
        direct = scores.get("direct", 0.0)
        return {
            "sufficient": round(sufficient, 6),
            "direct": round(direct, 6),
            "p": round(0.5 * sufficient + 0.5 * direct, 6),
        }
