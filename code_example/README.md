# code_example — 把 mini_sag_wiki 当库用

一个不依赖 CLI 的独立脚本：**本地检索证据 → 交给 LLM 生成带出处的回答**。

```
ask_wiki.py
  ├─ 1. BM25Index.load(...)                     读索引（纯本地）
  ├─ 2. Wiki(index, Jev()).ask(question, cfg)   检索：粗筛 → JEV 判证据 → 扩词 → 串联
  ├─ 3. build_prompt(...)                       把 evidence 拼成 <evidence> 块
  ├─ 4. call_llm(...)                           POST {ANTHROPIC_BASE_URL}/v1/messages
  └─ 5. 打印回答 + 依据列表 + token 用量
```

检索部分不花任何 API 额度；只有第 4 步会调用模型。所以可以随时 `--dry-run` 看检索结果和提示词。

## 前提

- `jev.exe serve` 在跑（默认 `http://127.0.0.1:8017`）
- 索引已建好：`uv run mini_sag_wiki index`

## 环境变量

脚本只读**系统环境变量**里 `anthropic` 开头的那些，不读 `.env` 文件：

| 变量 | 必填 | 说明 |
|---|---|---|
| `ANTHROPIC_AUTH_TOKEN` | 是* | 作为 `Authorization: Bearer <token>` 发送（Claude Code 风格的网关常用这个） |
| `ANTHROPIC_API_KEY` | 否 | 没有 `AUTH_TOKEN` 时作为 `x-api-key` 发送 |
| `ANTHROPIC_BASE_URL` | 否 | 默认 `https://api.anthropic.com`；可指向任何 Anthropic 兼容网关 |
| `ANTHROPIC_MODEL` | 否 | 只用来提示；本示例固定用 `glm-5.3`，可用 `--model` 覆盖 |

\* 用 `--dry-run` 时不需要任何凭证。

查看当前机器上配置了哪些（不打印值）：

```powershell
Get-ChildItem Env: | Where-Object { $_.Name -match '^(?i)anthropic' } | Select-Object Name
```

## 运行

```powershell
# 完整流程：检索 + glm-5.3 生成
uv run python code_example/ask_wiki.py "how long is the rollback window"

# 只看检索结果和将要发送的提示词，不调模型
uv run python code_example/ask_wiki.py "退款政策是什么" --dry-run

# 调参
uv run python code_example/ask_wiki.py "..." --target 2 --evidence-chars 600 --rounds 4
uv run python code_example/ask_wiki.py "..." --model glm-5.3 --max-tokens 2048
```

装了依赖之后也可以直接 `python code_example/ask_wiki.py "..."`——脚本会把项目根加进 `sys.path`。

## 实际输出

```
问题：how long is the rollback window
检索：2 条证据，confidence=0.9167，rounds=2，kb 段落数=34

The rollback window is 30 minutes, counted from when a release reaches 100% traffic [2]. During
that window, any engineer on the release rotation can run `atlas rollback --to <release-id>` and
the previous release is restored from an immutable image in under 90 seconds [2]. After the window
closes, the rollback command refuses to run and you must use a forward fix or a new release
candidate instead [2].

依据：
  [1] atlas-faq-zh.md#0-1  p=0.9687  # Atlas 平台常见问题（中文速查） 回滚窗口为 30 分钟。…
  [2] atlas-deploy.md#3-3  p=0.9655  The rollback window is 30 minutes. For 30 minutes after…

(model=glm-5.3, input_tokens=375, output_tokens=138)
```

## 换成别的模型 / 厂商

脚本只依赖 **Messages API 的形状**（`POST /v1/messages`，`content` 数组）。只要网关兼容：

```powershell
$env:ANTHROPIC_BASE_URL = "https://your-gateway/api/anthropic"
$env:ANTHROPIC_AUTH_TOKEN = "..."     # 或 ANTHROPIC_API_KEY
uv run python code_example/ask_wiki.py "..." --model your-model
```

## 两个容易踩的坑

1. **GLM 会先返回 `thinking` 块**。脚本只拼接 `type == "text"` 的块；如果 `max_tokens` 太小
   （比如 32），额度会被思考过程吃完，`text` 就是空的——所以默认给了 1024。
2. **凭证别写进代码**。脚本只用环境变量，`--dry-run` 也不会打印任何 token。

## 改造建议

- 想换成流式输出：把 `call_llm` 里的 `urllib` 换成 SSE（`stream: true`），其他不变。
- 想加多轮对话：把 `SYSTEM_PROMPT` 固定，把 `messages` 变成历史数组，每轮重新检索一次证据。
- 想批量：把 `main()` 里的检索部分抽成函数，模型调用并发跑（检索是本地 CPU，JEV 建议串行）。
