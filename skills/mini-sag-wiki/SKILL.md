---
name: mini-sag-wiki
description: '用本地知识库回答问题：BM25 粗筛候选段落 + JEV 判断哪段原文有用 + 从证据里挑新关键词再搜，返回带出处的原文与置信度。Use this skill whenever the user asks something that should be answered from their own documents, notes, wiki or internal rules（知识库、文档、资料、笔记、手册、内部规定），mentions mini_sag_wiki / sag wiki / 查一下知识库, wants to search or cite local files, or wants to add and index new documents. Prefer it over guessing or web search whenever the answer needs a citation from local files.'
argument-hint: '要问的问题，或要收进知识库的文件路径'
---

# mini_sag_wiki：用本地原文回答问题

这是一个**检索**工具，不是生成模型：它只返回原文段落和「这段对问题有没有用」的概率。
**答案由你来组织，出处必须来自返回的 `evidence`。**

```
问题 → BM25 粗筛 → JEV 判断证据 → 从证据里挑罕见词 → 再搜 → 段落串联 → JSON(证据+线索+置信度)
```

## 何时使用

- 用户问「文档/知识库里怎么说的」「XX 的规定是什么」「帮我查一下我们自己的资料」
- 回答需要指明出处（文件名 + 段落号）
- 用户要把新文件收进知识库（ingest）或重建索引

不要用：闲聊、纯代码问题、需要联网实时信息的问题。

## 0. 先定位项目并自检（每次会话做一次）

```bash
uv run mini_sag_wiki doctor          # 别名：uv run wiki doctor
```

- **项目根** = 含 `pyproject.toml` 和 `data/` 的目录。当前目录不是它时用：
  `uv --directory <项目路径> run mini_sag_wiki <cmd>`
- 首次使用先 `uv sync`（没有 `.venv` 时）。
- `doctor` 返回 JSON，看 `checks[].ok`：
  - `jev.ok = false`（退出码 3）→ 让用户启动 JEV 服务：在 OpenJev 目录执行 `.\jev.exe serve`
    （默认 `http://127.0.0.1:8017`）。JEV 本体不在本仓库，来自
    https://github.com/GPT-AGI/OpenJev——用户没有的话先告诉他去那里下载。
    换地址用 `--jev-url` 或 `MINI_SAG_WIKI_JEV_URL`。
    **不要跳过 JEV 拿纯 BM25 结果当答案**——那样没有「这段到底有没有用」的判断。
  - `index.ok = false`（退出码 4）→ `uv run mini_sag_wiki index`
  - `index_fresh = stale` → 知识库改过了，重建索引

## 1. 提问：用完整问句

```bash
uv run mini_sag_wiki ask "how long is the rollback window"
uv run mini_sag_wiki ask "退款政策是什么" --format text
```

**最重要的一条经验：把问题写成完整的疑问句，不要只丢名词短语。**

| 查询 | 结果 |
|---|---|
| `When does a plan downgrade take effect?` | 命中，p=0.97 |
| `downgrade from Team to Starter` | 全被 JEV 否决（p<0.1），`found=false` |

小模型对「名词短语 + 专有名词」的判断很保守；补上 what/when/how 之类的疑问结构就能救回来。

其它约定：

- 默认输出 JSON（schema `mini_sag_wiki.ask/1`）；给人看时加 `--format text`，`--pretty` 缩进。
- 上下文紧张时加 `--max-chars 400`（单条证据字符上限，`0` = 全文）。

## 2. 读结果

| 字段 | 含义 | 怎么用 |
|---|---|---|
| `found` | 有没有拿到证据 | `false` → 别编答案，见第 3 节 |
| `confidence` | JEV 对「这些证据够不够回答」的概率 | < 0.5 时在回答里说明证据不足 |
| `checks.sufficient` / `checks.direct` | 覆盖度 / 直接程度 | 都低说明只沾边 |
| `evidence[].text` | **原文**（围绕命中句截断） | 唯一可引用的事实来源 |
| `evidence[].p` | 这段对问题有用 的概率 | 越高越该优先引用 |
| `evidence[].jev` | 两句原始判断（`useful` / `info`） | 需要解释可信度时看 |
| `evidence[].source` + `paras` | 文件名 + 段落号 | 出处写成 `source#para` |
| `evidence[].via` | `seed:词` / `kw:词` / `chain:段落id` | 说明这条是怎么被找到的 |
| `clues` | 检索走过的路（`hit=false` 是死路） | 向用户解释「还找了哪」时才用 |
| `warnings` | `low_confidence` / `no_evidence_above_threshold` / `round_budget_exhausted` / `query_not_in_index:<词>` | 决定是否重试 |
| `hint` | 只在 `found=false` 时出现 | 直接照它给的建议做 |

`evidence[].text` 是**截断过的**（围绕命中句取窗口）。需要完整上下文再 `get`。

## 3. 证据不足时的处理顺序

1. 看 `warnings` 和 `hint`：
   - `query_not_in_index:xxx` → 词表里没有这个词，换说法（同义词/全称缩写）。
     可以先 `mini_sag_wiki search "<关键词>"` 确认词表覆盖（不花 JEV）。
   - `hint` 说「BM25 有命中但全部被 JEV 否决」→ 换更完整的问句，或放宽阈值。
2. 放宽重试（一次一项，别全开）：
   ```bash
   uv run mini_sag_wiki ask "<更完整的问句>" --min-p 0.3 --debug
   uv run mini_sag_wiki ask "<问句>" --rounds 4 --pool 16
   ```
   `--debug` 会带上 `rejected` 数组（被拒候选及其 p），用来说明「差一点命中」还是「真的没有」。
3. 还不行就明确告诉用户：**知识库里没有找到相关原文**，并复述用过的关键词。
   绝不根据常识补一个「应该是……」的答案。

## 4. 需要上下文时取回原文

```bash
uv run mini_sag_wiki get "atlas-deploy#3-4" --max-chars 0
```

支持 `doc#3`（单段）和 `doc#3-5`（合并段），一次可传多个 id。

## 5. 组织回答

```markdown
<直接用 1-3 句回答，只使用 evidence 里的事实>

依据：
- [1] atlas-deploy.md#3：rollback window 是 30 分钟，超时后只能走 forward fix。
- [2] atlas-faq-zh.md#0-1：回滚 90 秒内还原上一版本镜像。

<confidence < 0.5 或 warnings 非空时补一句>
注：知识库只覆盖了部分信息（confidence 0.42），建议补充确认。
```

规则：

- **每一句事实都要能指回某条 `evidence`**，格式 `[n] source#para`。
- 不同 `evidence` 冲突时，把冲突讲出来（列出各自的 `p` 和出处），不要替用户选。
- 不要复述 `clues` 给用户看，除非用户问「你还查了哪些地方」。
- 不要凭空扩写：`evidence` 里没有的数字、日期、条款都不要写。

## 6. 维护知识库

```bash
uv run mini_sag_wiki ingest D:\notes\*.md --index      # 收文件并重建索引
Get-Content note.md | uv run mini_sag_wiki ingest - --name note.md --index   # 从 stdin
uv run mini_sag_wiki index                            # 只重建索引
uv run mini_sag_wiki status                           # 文件数 / 段落数 / 是否过期
```

- `ingest` 会按内容去重：同名不同内容会存成 `name-<hash>.md`，返回 `status: added|unchanged`。
- 索引存在 `<知识库>/.wiki/bm25.json`，是纯派生数据，随时可以删掉重建。
- 改完文档一定要重建索引，否则 `status.index.stale = true`。

## 7. 退出码

| 码 | 含义 | 处理 |
|---|---|---|
| 0 | 正常（注意：`found=false` 也是 0） | 看 JSON 内容判断结果 |
| 2 | 参数/输入错误 | 修参数；查询里没有可检索词项也会是 2 |
| 3 | JEV 不可用 / 报错 | 让用户启动 `jev.exe serve` |
| 4 | 索引缺失 | `mini_sag_wiki index` |

失败时 stdout 仍是 JSON：`{"schema":"mini_sag_wiki.error/1","error":{"code":...,"message":...,"hint":...}}`。

## 8. 常用参数

| 参数 | 默认 | 用途 |
|---|---|---|
| `--format text` | json | 人读输出 |
| `--max-chars` | 600 | 单条证据字符上限，`0` = 全文 |
| `--min-p` | 0.5 | JEV 认可阈值；漏召回调低到 0.3，噪声多调高到 0.6 |
| `--rounds` / `--pool` | 3 / 12 | 轮数 / 每轮送审候选数 |
| `--target` | 4 | 收够几条证据就停 |
| `--terms` / `--min-idf` | 4 / 1.2 | 每条证据挑几个新线索词 / 线索词最低 IDF |
| `--debug` | 关 | 附带被拒候选 |
| `--kb` / `--index-dir` | `data` / `<kb>/.wiki` | 换知识库 |

完整命令、字段、环境变量见 [references/cli-reference.md](./references/cli-reference.md)。

如果用户想在**自己的 Python 代码里**调用而不是走命令行，直接用
`from mini_sag_wiki.pipeline import Wiki, AskConfig`；仓库里的 `code_example/ask_wiki.py`
是一个完整示例（检索 + 调 LLM 生成带出处的回答）。
