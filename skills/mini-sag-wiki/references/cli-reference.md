# mini_sag_wiki CLI 参考

## 入口

| 方式 | 说明 |
|---|---|
| `uv run mini_sag_wiki <cmd>` | 正式入口（`[project.scripts]`） |
| `uv run wiki <cmd>` | 同一入口的短别名 |
| `uv run python main.py <cmd>` | 不装包也能用（cwd 需在项目根） |
| `uv run python -m mini_sag_wiki <cmd>` | 同上 |

不在项目根时：`uv --directory <项目路径> run mini_sag_wiki <cmd>`。

## 全局选项（每个子命令都接受）

| 选项 | 默认 | 说明 |
|---|---|---|
| `--kb` | `data` | 知识库目录（原文所在处） |
| `--index-dir` | `<kb>/.wiki` | 索引目录，文件名为 `bm25.json` |
| `--format` | `json` | `json` \| `text` |
| `--pretty` | 关 | JSON 缩进 2 空格 |
| `--jev-url` | `http://127.0.0.1:8017` | JEV 地址（`status`/`doctor`/`ask`） |
| `--model` | `jev-latest` | 模型名（`status`/`doctor`/`ask`） |

## 命令

### ask — 完整检索

```
mini_sag_wiki ask QUERY [选项]
```

| 选项 | 默认 | 说明 |
|---|---|---|
| `--rounds` | 3 | 最多几轮「粗筛 → JEV → 扩词 → 再搜」 |
| `--pool` | 12 | 每轮送 JEV 的候选段数（先放段落线索，再放 BM25 命中） |
| `--target` | 4 | 收够几条证据后提前结束 |
| `--max-evidence` | 8 | 最终最多返回几条（按 `p` 降序） |
| `--min-p` | 0.5 | JEV 认可阈值，`p ≥ min_p` 才算证据 |
| `--terms` | 4 | 每条被认可的原文里最多挑几个新线索词 |
| `--min-idf` | 1.2 | 线索词的最低 IDF，过滤过于常见的词 |
| `--chain` | 1 | 沿同篇原文上下各串几段 |
| `--max-chars` | 600 | 单条证据字符上限，`0` = 全文 |
| `--no-merge` | 关 | 不合并同一原文里相邻的证据段 |
| `--workers` | 4 | 并发问 JEV 的线程数 |
| `--debug` | 关 | 输出里附带 `rejected`（被拒候选及概率） |

### search — 只做 BM25

```
mini_sag_wiki search QUERY [-k 10] [--max-chars 300]
```

不调用 JEV，所以不需要服务在线。用来快速确认「词表里有没有这个词」。

### get — 按 id 取回原文

```
mini_sag_wiki get ID [ID ...] [--max-chars 0]
```

`ID` 形如 `atlas-deploy#3` 或合并后的 `atlas-deploy#3-5`。`--max-chars 0` 返回全文。

### index — 重建索引

```
mini_sag_wiki index [--min-chars 140] [--max-chars 1200] [--k1 1.2] [--b 0.75]
```

- 只索引 `--kb` 下的原文（`.md .markdown .txt .text .rst .org .log .csv .json .yaml .yml`）。
- 以点开头的目录（如 `.wiki`）不会被当成原文。
- 段落短于 `--min-chars` 会向后合并，长于 `--max-chars` 会按句子拆开。

### ingest — 收原文

```
mini_sag_wiki ingest PATH... [--name NAME] [--index]
```

- `-` 表示从 stdin 读（此时必须给 `--name`）。
- 内容相同 → `unchanged`；同名不同内容 → 存成 `name-<sha8>.md` 并视为 `added`。
- `--index` 会顺便重建索引。

### status / doctor / help

- `status`：知识库文件数、索引统计、`index.stale`、JEV 探活。
- `doctor`：逐项自检；JEV 不通 → 退出码 3，索引缺失 → 退出码 4。
- `help`：命令清单（JSON），适合 Agent 用来自我发现能力。

## 退出码

| 码 | 场景 |
|---|---|
| 0 | 正常（`found=false` 也是 0） |
| 2 | 参数错误、查询没有可检索词项、文件不存在 |
| 3 | JEV 连不上或返回错误 |
| 4 | 索引不存在 / 版本不匹配 |

## 输出格式

所有命令 stdout 只有一个 JSON 对象，首字段是 `schema`：

| schema | 命令 |
|---|---|
| `mini_sag_wiki.ask/1` | ask |
| `mini_sag_wiki.search/1` | search |
| `mini_sag_wiki.get/1` | get |
| `mini_sag_wiki.index/1` | index |
| `mini_sag_wiki.ingest/1` | ingest |
| `mini_sag_wiki.status/1` | status |
| `mini_sag_wiki.doctor/1` | doctor |
| `mini_sag_wiki.help/1` | help |
| `mini_sag_wiki.error/1` | 任意命令出错 |

### `mini_sag_wiki.ask/1`

```jsonc
{
  "schema": "mini_sag_wiki.ask/1",
  "query": "原始查询",
  "found": true,                 // 是否有证据通过 JEV
  "confidence": 0.8824,          // JEV 对「够不够回答」的联合概率
  "checks": {"sufficient": 0.91, "direct": 0.85, "p": 0.88},
  "rounds": 2,                   // 实际跑了几轮
  "keywords": {
    "query": ["error", "budget"],// 查询分词（英文已词干化）
    "expanded": ["burn", "freeze"] // 从证据里挑出的新线索词
  },
  "unmatched_terms": [],         // 索引里不存在的查询词
  "evidence": [
    {
      "n": 1,                    // 引用角标
      "id": "atlas-incidents#4", // 证据 id，可用于 get
      "source": "atlas-incidents.md",
      "paras": [4, 4],           // 原文段落号（0 基）
      "p": 0.9462,               // 「这段对问题有用」的概率
      "jev": {"useful": 0.96, "info": 0.93},
      "bm25": 6.688,             // 粗筛分数
      "via": "seed:error",       // seed:查询词 / kw:线索词 / chain:段落id
      "round": 1,
      "parts": ["atlas-incidents#4"], // 合并前的原始段 id
      "chars": 363,              // 原文长度
      "text": "..."              // 围绕命中句截断的原文
    }
  ],
  "clues": [                     // 线索边：谁把谁带出来的
    {"from": "atlas-incidents#4", "via": "freeze", "to": "atlas-deploy#6", "round": 2, "hit": false}
  ],
  "trace": [                     // 每轮统计
    {"round": 1, "candidates": 3, "accepted": 2, "new_terms": ["burn"], "top_p": 0.946}
  ],
  "budget": {"candidates": 10, "judged": 10, "accepted": 2, "jev_requests": 11, "evidence_chars": 652},
  "warnings": [],                // low_confidence / no_evidence_above_threshold /
                                 // round_budget_exhausted / query_not_in_index:<词>
  "hint": "仅在 found=false 时出现",
  "jev": {"url": "http://127.0.0.1:8017", "model": "jev-latest"},
  "elapsed_ms": 597.5,
  "rejected": []                 // 仅 --debug
}
```

### `mini_sag_wiki.search/1`

`{schema, query, terms[], missing_terms[], hits[{id, source, paras, score, matched[], text}]}`

### `mini_sag_wiki.get/1`

`{schema, found[{id, source, paras, text}], missing[]}`

### `mini_sag_wiki.status/1`

`{schema, version, kb{dir, exists, files}, index{chunks, documents, terms, avg_chunk_terms, built_at, kb, params, path, exists, stale}, jev{ok, url, model, status, ms}}`

### `mini_sag_wiki.doctor/1`

`{schema, ok, checks[{check, ok, detail}], jev{...}}`，`check` 取值：`kb` / `index` / `index_fresh` / `jev`。

### `mini_sag_wiki.error/1`

```json
{"schema":"mini_sag_wiki.error/1","ok":false,
 "error":{"code":"jev_unavailable","message":"...","hint":"..."}}
```

`code` 取值：`jev_unavailable` / `jev_unreachable` / `jev_http_error` / `jev_bad_response` / `index_missing` / `bad_request`。

## 环境变量

| 变量 | 默认 |
|---|---|
| `MINI_SAG_WIKI_JEV_URL` | `http://127.0.0.1:8017` |
| `MINI_SAG_WIKI_JEV_MODEL` | `jev-latest` |
| `MINI_SAG_WIKI_JEV_API_KEY` | 空（设置了就发 `Authorization: Bearer`） |

## 目录布局

```
<kb>/                     # 原文
<kb>/.wiki/bm25.json      # 索引（派生产物，可删除重建）
```

索引是纯 JSON：`meta`（构建时间、源文件清单）+ `chunks`（每段原文与词频表）+ `params`。
装了 BM25 以外的东西吗？没有——所以任何时候删掉它都不会丢数据。

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 退出码 3，`jev_unavailable` | JEV 没起：在 OpenJev 目录 `.\jev.exe serve`（本体来自 https://github.com/GPT-AGI/OpenJev） |
| 退出码 4 | 索引不存在：`mini_sag_wiki index` |
| `found=false` + `query_not_in_index` | 查询词不在语料里，换同义词 |
| `found=false` + `hint` 说全部被否决 | 名词短语换成完整问句，或 `--min-p 0.3 --debug` |
| `status.index.stale = true` | 原文改过但没重建索引 |
| 结果太长 | `--max-chars 300 --max-evidence 4` |
| 中文专有名词切得很碎 | 中文按二元组切词，属已知限制；换个说法或多给上下文 |

## 当库用

```python
from pathlib import Path
from mini_sag_wiki.bm25 import BM25Index
from mini_sag_wiki.jev import Jev
from mini_sag_wiki.pipeline import AskConfig, Wiki

wiki = Wiki(BM25Index.load(Path("data/.wiki/bm25.json")), Jev())
result = wiki.ask("how long is the rollback window", AskConfig(target=3))
```

完整示例（检索 + LLM 生成）：`code_example/ask_wiki.py`。
