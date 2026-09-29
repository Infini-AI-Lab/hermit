# 卡片架构与 evolver 设计 v1.2：只准入"送达后被证明有效"的改动

日期 2026-09-24。版本：v1 = evolver + host 模块；v1.1 = 提升为架构；v1.2 = 架构合同经
3 个视角对抗审查后的修订（账本移出 adapter、控制器代码移到 evolver、v1 不带工具、
补齐未定义处）。目标仓库：本目录（GitHub main 克隆，分支
analysis/mwm-agentic-coding-diagnosis；上游已推进到 b85763a，新增 lemma_board 架构）。
前置报告：docs/analysis/2026-09-24-mwm-agentic-coding-diagnosis.md（原因 R1–R8、
改法 S0–S3）。对照论文：RRSI（Regularized Recursive Self-Improvement，arXiv 2609.24972）。

评审过程：初稿以"声明式 recipe 重放"为核心，交给 9 个独立代理（3 个写替代方案，
6 个按框架接入、RRSI 忠实度、统计、钻空子、OfficeQA 可行性、简单性挑错），核心机制
被推翻（§14）。v1.1 把交付与验证合同提升为架构；再经框架接入、上游先例一致性、
简单性三个视角审查，得到本版。每条框架接入都核对过 文件:行号。

## 0. 一句话

**新架构 `cards` 规定 actor 拿到什么：一段受保护的固定前言，加上从已准入卡片编译、
硬字节预算的简报，编译代码本身受保护。evolver `cards` 让 learner 只提卡片，并带着
筛查、判定、组装的代码；host 只负责把 actor 重新跑一遍和判分。通过验证的卡才进
简报；每张卡记账在 run 目录；无效的卡不会改变已记分的题，其命中记录是下一版
剪枝的输入。**

术语：架构（architecture）= 框架里 adapter 的起始结构，规定 actor 收到的上下文、
MCP 工具、角色，以及哪些文件可以演化；evolver = 每 5 题之后运行一次的学习算法；
adapter = 交付给做题 agent 的可演化包；actor = 做题的 Codex 会话；learner = 运行
evolver 提案的模型会话；host = tasks/OfficeQA/host 里的控制器。

## 1. 为什么要在 architectures 这一层做，以及三层各管什么

框架把"actor 拿到什么"归架构管：adapter.json 声明上下文渲染器、工具和角色，
hosts/ 把它们变成 AGENTS.md 和 MCP 服务器（docs/guide/architectures.md、
deployment.md）。诊断里排第一第二的原因 R1（交付缺口）和 R2（执行保真度）都发生在
这一层。v1 把交付逻辑放进 OfficeQA 的 host 模块，有两个毛病：只有 OfficeQA 能用；
简报预算不是受保护的代码，任何 host 都可能不执行。

上游同日新增的 lemma_board 架构（提交 b85763a）是样板：架构 = 一份 ADAPTER.md 加
六个 board_* 工具；状态放在 adapter 之外、由环境变量命名的目录里（board.py:1-9、
EVOLVER.md:8-10）；**验证放在 adapter 之外**由启动器做（README.md:31 "The verifier
is the launcher's job"）；adapter 里只保留状态格式作为与验证器的合同。本设计沿用
同一分工，并按审查意见把控制器代码放在 evolver 目录（先例：
harnessadapter/evolvers/mwm/maintenance/gates.py 在 evolver 侧，导入框架自己的
mwm_core 副本）：

| 层 | 拥有什么 | 本设计里是什么 |
|---|---|---|
| 架构 `cards` | actor 进程里执行的东西：表示、渲染、预算；与验证器的格式合同 | cards/ 卡片文件；受保护的 PREAMBLE.md；受保护的 cards_core/{schema.py, render.py}（解析、关键词匹配、编译与 CARDS_MAX）；EVOLVER.md 写明账本与状态格式是合同 |
| evolver `cards` | 提案怎么产生，以及块级算法 | PROMPT.md + evolver.toml；block.py（筛查、判定、组装、账本读写、前瞻记录），host 从这里导入 |
| host（OfficeQA） | 跑 actor、判分 | 约 60 行：一个 `run(adapter_dir, question) → {strict, status, out_tokens}` 回调，包 arm.actor 与 records.grader；报告 |

架构层解决得更多的部分：预算成为受保护代码，任何 host（Codex、Claude Code、
CLBench 的 database_system.py:128-129）都经过 Adapter.context()（loader.py:154-161），
所以都受它约束；前言受保护，learner 无法绕过逐卡筛查夹带内容；卡片是结构化文件而
不是一份自由散文；账本格式是合同，别的 venue 复用。架构层解决不了的部分：验证仍
然要 host 跑 actor，lemma_board 也是如此。

**工具通道**：v1 不带 MCP 工具（adapter.json `tools: []`，与 prompt_only 相同）。
理由：v1 规模下（每块 ≤ 2 个提案，10 块，每卡 ≤ 600 字节）简报不会溢出，一个
`card_read` 工具没有上下文里没有的内容；注册一个 required 的 MCP 服务器只增加启动
和超时的失败模式（acting.py:401-405）；CLBench 的 host 只分发名为 wm 的工具
（database_system.py:111-114），"任何 host 都能用"对工具通道不成立。工具是这个
架构的扩展点，准入规则：只有当它携带上下文里拿不到的内容（真实溢出，或
RRSI 表 6 那类"完成前审计" `check_answer`）时才加，并用同一套成对重跑验证它的效果
（候选 = 开工具 对 关工具）。

## 2. 架构 `cards`

```
harnessadapter/architectures/cards/
  adapter.json        contract harnessadapter.python.v1；id "cards"；
                      context = {"entrypoint": "interface.py:context"}；tools = []；
                      evolver_readme = EVOLVER.md
  interface.py        context() → cards_core.render.compile(ROOT)   （无资源参数；纯函数）
  cards_core/         受保护：__init__.py、schema.py（卡片解析校验、关键词匹配）、
                      render.py（编译与预算 CARDS_MAX = 16,384）
  PREAMBLE.md         受保护：告诉 actor 这些卡片是什么、怎么用（固定文本，v1 不演化）
  cards/              learner 可写；起始只有 README.md 和 _template.md
  EVOLVER.md          三段：what may change（只有 cards/）；what must stay（卡片格式、
                      账本与状态格式是与 host 验证器的合同；渲染器与前言不动）；
                      evidence（goal 里的 pool / right / 账本摘要；判断标准是成对重跑）
  README.md           人读：验证是 host 的工作；状态机；host 怎么调用 evolvers/cards/block.py
```

- **渲染是 cards/ 的纯函数**：`compile(root)` 读 cards/ 里带 `admitted:` 字段的卡
  （只有控制器组装时才写这个字段，learner 的提案没有），按 (admitted 块升序, support
  数降序, id) 排序，取编译后字节 ≤ CARDS_MAX 的最长前缀（"Reading A"：成员在组装时
  确定，溢出只可能淘汰最新的卡，并记入账本）。前言 PREAMBLE.md 拼在最前，计入预算。
  `harnessadapter deploy` 与运行时渲染完全一致（render_context.py:13 无资源调用；
  渲染器签名不带 `_resources`）。不做按题排序（推迟，§13）。
- **受保护**：evolver.toml 里 `protected = ["adapter.json", "EVOLVER.md", "interface.py",
  "cards_core", "PREAMBLE.md"]`。框架按路径前缀拒写（workspace.py:114-121）并拒收改动
  过的候选（session.py:126,233,242）。渲染在 actor 进程里执行 adapter 自己的副本
  （loader.py:102-104,137-143）；块级算法从 evolver 包导入；两者不相干，不再需要
  "用框架副本"的说明。
- **部署**：`harnessadapter deploy cards --host codex` 只写 AGENTS.md，不注册 MCP
  （hosts/codex.py:63-64,73-75 仅在 tools 非空时注册）。paths.py:7 的
  BUILTIN_ARCHITECTURES 加 "cards"（lemma_board 同一处）；paths.py:8 的 BUILTIN_EVOLVERS
  也加（仅 launch.yaml 路径校验用，launch.py:30）。docs/guide/architectures.md 末尾
  加一节 "## Cards (experimental)"，照 lemma_board 那节的五项写。
- **测试**：tests/test_cards.py 照 tests/test_lemma_board.py 与 test_architectures.py：
  resolve_architecture("cards")；Adapter(CARDS).context() 含前言且 ≤ CARDS_MAX，
  对预置 cards/ 的排序与溢出；codex_prepare 断言 server 为 None、AGENTS.md 含前言。
  块级算法的测试在 tests/test_cards_block.py：筛查规则、judge 的零假设用例、组装、
  账本 schema。

## 3. 三个对象

### 3.1 卡片 `cards/<id>.md`

一张卡 = 一次原子编辑 = RRSI 的一条 atomic edit。learner 写四个字段，控制器组装时
追加一个：

```
id: C007
scope: fiscal year, annual, revised, later bulletin      # 1–8 个关键词，见 §4.3 匹配规则
claim: |                                                  # ≤ 600 字节，actor 读的就是这段
  When a question asks for an annual or fiscal-year figure and the same row is
  printed in more than one bulletin, use the LATEST bulletin that prints the row
  (later printings carry revisions, often marked "r"). Do not use the first printing
  unless the question names a specific bulletin or date.
support: [q006, q013]                                     # ≥ 2 道已放出、现役答错的题
admitted: 3                                               # 控制器写：准入的块号
```

没有 recipe、没有 alternative、没有 status 字段。learner 对已准入卡片的修改一律丢弃并
记账（v1 不支持改卡；要改就提新卡）。

### 3.2 账本与状态：`run/cards/`（host 拥有，adapter 之外）

照 lemma_board：可写状态在 adapter 之外。OfficeQA 下是 `run/cards/ledger.jsonl` 和
`run/cards/state.json`；learner 通过 goal 文本看到摘要，完整账本由 host 复制一份到
证据目录（run/evidence/ledger.jsonl，只读，session.py:81 随证据进会话）。
**任何时候都不写正在服务的 adapter**：arm.py:91 与 :121-122 在每次 actor 运行前后
比对 adapter 全量哈希，同块题目并发共用同一 adapter（arm.py:297-300），写入会让
在飞的题抛 "Actor mutated its read-only adapter"。

账本每行（RRSI 式 10：t、组件 ℓ = id、假设 h = claim_sha、diff d = file_sha、
ΔS = runs、ΔC = tokens_ratio、a = admitted）：

```
{"t": 3, "id": "C007", "action": "add", "claim_sha": "...", "file_sha": "...",
 "scope": [...], "support": ["q006","q013"], "keep": ["q002","q011"],
 "runs": [{"q": "q006", "role": "support", "k": 1, "adapter_sha": "...",
           "strict": 1, "status": "completed", "out_tokens": 8123, "redraw": false}, ...],
 "tokens_ratio": 1.4, "admitted": true, "reason": "support 4/4, keep 4/4, cost 1.4",
 "session": "evolve-2vckxnbs", "session_commit": "..."}
```

state.json：

```
{"claimed": {"q006": "C007", "q013": "C007"},
 "rendered": ["C007", "C012"], "overflow": [],
 "hits": {"C007": [["q019", 1], ["q023", 0]]},        # 前瞻命中记录：(题号, strict)
 "rejected_sha": ["..."], "not_card_induced": []}
```

`tokens_ratio` 的分母是同题原运行的 out_tokens，不论原运行状态。

### 3.3 编译的上下文

```
<PREAMBLE.md 全文>
## C007 — applies when: fiscal year, annual, revised, later bulletin
<claim>
## C012 — applies when: ...
```

CARDS_MAX = 16,384 字节，按 render.compile() 的输出（嵌套标题之前）计量。两条交付
路径的差别：`harnessadapter deploy` 会把上下文包进标记块并把标题降两级
（hosts/context.py:26,34-39）；OfficeQA 的 host 直接写 caller 渲染的文本
（acting.py:116-121）。标记块约 300 字节，在预算之外。host 把每题 AGENTS.md 的字节数
记进 summary.json（arm.py:127-132），不设第二个常量。

数字的来源：Codex 默认把 AGENTS.md 截断在 32,768 字节（配置键 `project_doc_max_bytes`）；
v5 的 MWM 包在第 36 题达到 37,451 字节，wm 的 API 说明从第 32,365 字节开始，整段被
截掉（R1）。prompt-only 的固定开销不到 1 KB。

## 4. 算法（一页）

### 4.1 常量

| 名称 | 值 | 在哪里 | 含义 |
|---|---|---|---|
| CARDS_MAX | 16,384 B | 架构 cards_core（受保护） | 编译上下文上限；影响 actor 进程 |
| b | 2 | evolver block.py 参数 | 每块最多提案数；一张卡一个候选 |
| k | 2 | 同上 | 每道验证题重跑次数 |
| SUPPORT_MIN | 2 | 同上 | 一张卡至少针对 2 道现役答错的题 |
| KEEP | 2 | 同上 | 控制器抽的对照题数 |
| COST_MAX | 3.0 | 同上 | 验证运行平均输出 token 不得超过原运行的 3 倍 |

协议参数（b、k、KEEP、COST_MAX）不进 adapter：P2 改参数不该改 adapter 指纹。

### 4.2 集合

- pool = 已放出且 现役答错（completed 且 strict=0）或 失控（timeout / tool_cap /
  request_cap / loop）的题，减去 state.claimed 里的题，减去 issue #47 的三题。
  传输失败的题永不入池。
- right = 已放出且答对的题，减去 issue #47 的题。

### 4.3 scope 关键词匹配（cards_core/schema.py，learner、host、测试共用）

题干与关键词都 casefold、压缩空白、去标点；关键词作为整词或整短语匹配；至少 1 个
关键词命中即"命中"；关键词长度 ≥ 4 且不在停用词表里，否则筛查拒收；排序平局按
support 数降序再按 id。

### 4.4 每块 t

标 [arch] 的在架构 cards_core，[evo] 的在 evolvers/cards/block.py，[host] 的在
OfficeQA host。

```
1  [evo]  goal := 本块记录列表（records.render）+ 状态文本：pool（题号、题干前 600 字、
          给出的答案、参考答案）、right 题号、b、账本摘要（每个提案的 id、动作、
          support/keep 结果、是否准入、原因；每张现役卡的命中 对/错 计数）
2  [host] learner 会话 := 框架 evolve（evolver=cards，强模型，--feedback gold）
          learner 在 candidate/cards/ 下写 ≤ b 个提案；受保护路径写不了
3  [evo]  收获 ⇔ result.status == "candidate_ready"（session.py:239-242 已含进程成功、
          未改受保护文件、结构校验）；否则账本记 session_rejected（附 status、unchanged），
          H_{t+1} = H_t
4  [evo]  筛查（新文件，按 learner 的文件顺序，只看前 b 个，其余记 over_budget）：
     [arch] schema 合法（schema.py）；|support| ≥ 2、都在 pool 里、互不相同
     泄漏：不得含 UID\d{4} | q\d{3} | (19|20)\d{2}_\d{2} | treasury_bulletin_\S+
           | 大于 400 且 ≥ 3 位有效数字的非年份数字 | 与任一已放出题干共享 ≥ 6 个连续词
     去重：sha256(normalize(scope+claim)) 不在 state.rejected_sha 里
     对已准入卡的修改：丢弃并记 edit_discarded
5  验证（每个通过筛查的提案）：
     [evo]  keep := 从 right 里抽 KEEP 道：优先 scope 命中的，其次最近的；绝不选它的 support
     [evo]  trial := copytree(H_t) 到 run/verify/b{t:03d}/{id}/adapter/ + 这张卡（写 admitted:t）
            （目录名不匹配 learn.py:54 的 adapter_b[0-9][0-9][0-9] glob）
     [host] 对 support ∪ keep 里每道题、j = 1..k：run(trial, q) 以 run/verify/b{t:03d}/{id}/k{j}/
            为根调用 arm.actor（每根目录一题一次，arm.py:83-84）
            状态表：completed → 判分；timeout | tool_cap | request_cap | loop |
            invalid_final | failed → strict 0（actor 行为）；transport_lost（arm.actor 内部
            已重试 3 次，arm.py:56-77）| launch_error → 对称剔除，不再重抽
            out_tokens = summary["usage"]["output_tokens"]（两种通道相同，arm.py:101-104,116）
     [evo]  support_ok := support 的 2×|support| 次运行里失误 ≤ 1
            keep_ok    := 没有任何 keep 题两次都错
            cost_ok    := 验证运行平均 out_tokens ≤ COST_MAX × 同题原运行的平均
            admitted   := support_ok ∧ keep_ok ∧ cost_ok
6  [evo]  组装（只在有准入时；否则 H_{t+1} = H_t，adapter 一个字节不变，即 RRSI 算法 2 第 16 行）：
     adapter_b{t:03d} := shutil.copytree(H_t, ignore=__pycache__/*.pyc)（保留 .git；
     seed 在 Learner 构造时 versioning.ensure_repo 一次）；复制本块全部准入卡（写 admitted:t）；
     versioning.commit_all(adapter_b{t:03d}, f"cards: block {t}")；
     [arch] rendered/overflow 由 compile() 的前缀规则得出，写回 state.json；淘汰记账
     state.claimed += 准入卡的 support
     [host] learn.py 的 adopt 回调：跳过 learn.py:108-114 的整目录复制，改为接收组装好的
            路径（或 None），并同样设置 self.adapter、self.revisions（arm.py:297 与
            learn.py:54-55 的续跑都靠它）
7  [evo]  账本：每个提案一行（含会话级拒收）；同步一份到 run/evidence/ledger.jsonl
8  [evo]  前瞻序贯记录：每块结束、learner 会话之前，从本块 batch 一次性写
          state.hits[card] += (q, strict)（对每张 rendered 卡，scope 命中题干的题）；
          绝不在 arm.py:91–121 之间写任何东西
9  [host] 报告：每块现役版本的 strict；最终成对 strict / 1% 及其 ±12 分的 95% 半宽；
          每张卡的 support / keep / 命中结果；每题 AGENTS.md 字节；验证 episode 数与费用
```

## 5. 验证原语：为什么是"actor 成对重跑"

### 5.1 它验证的是什么

一张卡准入的精确含义：**在为它而写的题上，送达之后有效，且没有拖累控制器抽的对照
题。** 不是"泛化到新题"。泛化只能由两样东西说明：第 8 步的前瞻序贯记录
（prequential，"先预测后揭晓"：每道新题先用现役简报做，判分后才归入统计），以及
可选的 P2 协议（§11）。

### 5.2 为什么不用初稿的 recipe 重放

- **learner 手里有参考答案**（records.py:62-73 的 gold 模式，:84-89 把"Correct answer"
  写进轨迹）。语料里有 31/50 个参考答案原样出现（"6244" 出现 342 次），learner 可以
  grep 到等于答案的单元格再倒推 recipe。重放证明的是"这套指针能算出已知答案"，是 v6
  出处检查（R8）往上挪了一层。
- **重放看不见"送达了但 actor 没用"**。v5 adapted 的 q6 是"推翻了自己的 latest-vintage
  记忆"，q17 是"latest-bulletin 规则把它引到 OCR 错位的表"。规则已送达、actor 用错，
  正是 R1、R2；recipe 重放对它们完全无感。
- **覆盖面小**：v5 adapted 30 道错题里可以 recipe 化的只有 7 道；50 题内同一约定出现
  两次的只有两组。上限约 3–4 题。
- **语料不配合**：8,102 张表（8.5%）无表头，65.7% 多级表头，列号跨版本漂移。

actor 重跑一个原语把这些都覆盖了：它测的就是 actor 拿到这段话之后会不会做对。

### 5.3 判据的统计性质

翻转率来自 rows.json 里两臂都完成的题（同一原生 harness，不同日期）：错变对
9/35 = 0.26，对变错 9/37 = 0.24。

| 情形 | 计算 | 概率 |
|---|---|---|
| 无效卡通过 support 判据（2 题 × 2 次，≤ 1 次失误） | P(Bin(4, 0.25) ≥ 3) | 0.051 |
| 无效卡通过 keep 判据（2 题，没有一题两次都错） | (1 − 0.24²)² | 0.88 |
| 无效卡整体通过 | 乘积 | 0.045 |
| 有效卡通过（单次成功率 0.7 / 0.8 / 0.9） | 同上 | 0.60 / 0.75 / 0.87 |

一次 50 题的运行约有 15–20 个提案，期望有 1 张无效卡混进来，在线上无害（不改变已
记分的题）；它的命中记录是 v1.1 删除验证的输入。

### 5.4 状态的处理

传输失败是基础设施问题，arm.actor 内部已重试 3 次，仍失败则在验证里对称剔除，
永不记 0。超时、工具上限、请求上限、循环、无效终答、失败是 actor 行为：记 0。因此
pool 里允许失控题做 support：一张"不要重发相同命令"的过程卡，可以被同一套验证检验，
而不是像 v5 那样写在简报里被无视（R2）。这与部署合同"经验 5"的措辞有出入，见 §15。

## 6. 筛查与剪枝

- **泄漏筛查**（对渲染出来的 scope 和 claim 做；support 等字段不进简报）：题号
  （UID\d{4}、q\d{3}）、公告文件名、大于 400 且 ≥ 3 位有效数字的非年份数字、与任一
  已放出题干共享 ≥ 6 个连续词、超过 600 字节。为什么这么严：v5 的 learner 把 36 个
  参考答案串写进 memory/answer-format.md，把 "6,911 − 667 = 6,244" 写进
  bulletin-revision-latest.md。100、365、52 这类小常数仍合法。这是 RRSI 泄漏 critic 的
  机械替代。
- **去重**：哈希等于 state.rejected_sha 里任一条目 → 直接拒，不跑 actor，记 collision。
- **剪枝**：v1 只做两件便宜的事：命中记录（state.hits）和溢出（被动剪枝）。删除验证
  （learner 提 `<id>.remove`，用同一判据反向验证：命中的错题在没有这张卡时重跑要变
  对，对照题保持对；不通过则记 not_card_induced）推迟到 v1.1，理由：≤ 20 张卡、期望
  1 张无效卡、效果上限 3–4 题，这条路径在 P1.5′ 里几乎不会触发，却占了三分之一的规范。
  不用 actor 自报"我用了 C017"作为使用信号（R2）。

## 7. evolver `cards`

```
harnessadapter/evolvers/cards/
  evolver.toml   prompt = "PROMPT.md"
                 protected = ["adapter.json","EVOLVER.md","interface.py","cards_core","PREAMBLE.md"]
                 [execution] backend = "codex"，network = false
  PROMPT.md      只写流程：读 EVOLVER.md；只从 pool 造卡；每张卡回答"actor 在哪一步选错了"
                 并给一句让它选对的规则，写明适用与不适用；support ≥ 2 道 pool 里的题且是
                 这条规则真正决定的题；≤ b 个提案；不写 cards/ 以外的文件、题号、答案、
                 文件名；第 0 块不开会话；写 report
  block.py       筛查、judge、组装、账本读写、前瞻记录：纯函数，参数为 run 目录状态与
                 adapter 路径；host 导入
```

输入全部通过 `--goal` 文本（definition.py:51-53 拼接 PROMPT.md + "Read adapter/EVOLVER.md"
+ "Update goal:" + goal）和证据目录里的 ledger.jsonl。learner 用强模型（gpt-5.6-sol 或
Opus 级），不用 flash（R6）。actor 仍是 flash。

## 8. 框架接入

| 位置 | 改动 | 机制核对处 |
|---|---|---|
| harnessadapter/architectures/cards/（新） | §2 的目录，tools = [] | loader.py:26-79 清单校验；:150-161 渲染器；hosts/codex.py:63-80 仅在有工具时注册 MCP |
| harnessadapter/paths.py:7 与 :8 | BUILTIN_ARCHITECTURES 与 BUILTIN_EVOLVERS 各加 "cards" | lemma_board 改了 :7；launch.py:30 校验两者 |
| docs/guide/architectures.md 末尾 | "## Cards (experimental)" 一节 | lemma_board 的同类条目（origin/main:docs/guide/architectures.md:68-76） |
| harnessadapter/evolvers/cards/（新） | evolver.toml + PROMPT.md + block.py | definition.py:16,21 接受 protected；session.py:122-123 写 control/protected.json；workspace.py:50-53,95-96,114-121 拒写；session.py:126,233,242 拒收 |
| tasks/OfficeQA/host/cards.py（新，约 60 行） | `run` 回调（包 arm.actor + records.grader）；调用 block.py；报告 | arm.py:59-77,83-84；records.py:54-59 |
| learn.py:17-19 | 加 "cards" 臂 | arm.py:32 ARMS |
| learn.py:96-121 | session() 接受 adopt 回调，有回调时跳过 :108-114 的整目录复制 | :54-55 续跑 glob |
| learn.py:50-52 | cards 臂：seed 复制后 versioning.ensure_repo(seed) | versioning.py:33-45 |
| learn.py:123-129 / arm.py:269-270 | bootstrap 不开会话，直接写 bootstrap.json | — |
| arm.py:318-320 | goal = block.status_text(run) + listing；前瞻记录在 :316 之后、会话之前 | learn.py:64 传 --goal |
| arm.py:127-132 | summary 记 AGENTS.md 字节数 | acting.py:339-340 已做 sha 校验 |
| records.py:115-116 | readme_text() 加一段描述 ledger.jsonl | — |
| tests/test_cards.py、tests/test_cards_block.py（新） | 照 test_lemma_board.py / test_architectures.py | — |
| 不动 | session.py、workspace.py、definition.py、hosts/*、loader.py、reward.py、MWM、prompt_only、lemma_board | 框架指纹排除 evolvers/ 与 architectures/（session.py:116,229） |

S0 前置修复（两臂相同，evolver 之外；没有它 evolver 的效果会被运行故障淹没，R3）：

| 位置 | 改动 |
|---|---|
| acting.py:38 | FLASH_CONTEXT_WINDOW 1,048,576 → 196,608 |
| acting.py:364 | 增加 `model_auto_compact_token_limit = 160000`（Codex 配置键；v5 的 acting.py:280 用过，GitHub 版删掉了，只剩 :360 的注释） |
| acting.py build_settings | 可选：提高 `project_doc_max_bytes`（默认 32,768）作双保险；简报预算仍保留 |
| acting.py:615-629,648-650,683-685；arm.py:125 | 循环守卫：pump_events 里计数完全相同的 (command \| mcp args) 条目，第 3 次像 cap_hit 一样停止，状态 loop |
| arm.py:326-329 | 已有：transport_lost 不计分 |

Codex exec 运行中不能被插话，所以"拒绝第 3 次调用"实现上是停止，不是拒绝。

## 9. 与 RRSI 的对照

| RRSI 机制 | 本设计 | 说明 |
|---|---|---|
| 稀疏可归因编辑（式 4、式 9） | 保留：一卡一候选，b=2 常数 | 退火为打包编辑的归因服务；一卡一候选归因已精确，且 support 池随运行增长（v5 按块末：2,4,5,9,10,12,13,13,14,17），递减预算反而不匹配。有意的偏离 |
| 证据账本（式 10–11） | 保留 | 通过 goal 文本回灌；哈希去重 |
| 结构化探索（式 12–13） | 删 | 只有一种组件类型 |
| 泄漏筛查 | 保留，机械化 | critic 换成 5 条规则 |
| 噪声下限（式 5） | 保留在编辑层 | keep 题 = 这张卡可能拖累的题上的非补偿性下限；整体 δ 只在 P2 |
| 成本规则（式 7） | 保留为失控守卫 | β1=0、β0=2.0 |
| 带内规则（式 15–17） | 删 | 表 5 未列权重；单张散文卡在 OfficeQA 上永远落在带内 |
| 结构剪枝（式 14） | v1 记命中、溢出；删除验证 v1.1 | 命中记录就是式 11 的 g_t 输入 |
| 领域守卫 | 保留 | transport 对称剔除；失控记 0 |
| 无合格候选则保留现役（算法 2 第 16 行） | 保留 | 组装只在有准入时发生 |
| k 次试验 | 保留 | k=2 |
| 分析器写的失败反馈 F_t | learner 自己在会话里做 | 强模型读原始记录 |
| 多候选 m_t | 每个提案就是一个候选 | |

## 10. 对 R1–R8

| 原因 | 本设计怎么处理 |
|---|---|
| R1 交付缺口 | 唯一通道是编译上下文，预算与前言在受保护代码里；工具是有准入规则的扩展点 |
| R2 执行保真度 | 验证的就是 actor 拿到卡片后会不会做对；失控题可做 support；循环守卫是 host 机制 |
| R3 运行故障 | S0 先修；验证里传输失败对称剔除 |
| R4 无成绩选择、无功效 | 每张卡有成对重跑；k=2；零假设通过率 0.045；不做整体 δ 门，改为前瞻记录和可选 P2 |
| R5 单样本改规则 | support ≥ 2 且都是现役答错的题；pool 跨块累计；一 support 的卡等第二道 |
| R6 learner 太弱 | 强模型 learner |
| R7 场景条件 | OfficeQA 满足三条件；架构与 venue 无关；账本格式是可复用的合同 |
| R8 出处检查冒充验证 | 没有任何 learner 写的东西被控制器"执行"来自证 |

## 11. 协议与成本

P1.5′（建议先跑）：stateless 对 cards；50 题在线，每 5 题一块（T=10）；两臂都用 S0；
--feedback gold；learner 强模型。

| 项目 | 数量 | 费用（v5 费率：输入 0.15、缓存 0.03、输出 0.50 美元/百万；v5 每题 0.15–0.23 美元） |
|---|---|---|
| 主运行 | 2 臂 × 50 题 | 16–19 |
| 验证 | ≤ 2 提案 × (2 support + 2 keep) × k=2 = 16 episode/块，全程 100–130 | 15–30 |
| learner | 10 场 | 登录通道 0；API 通道 10–30 |
| 可选：第二次 stateless | 50 题，得到翻转率 | 8–12 |
| 合计 | | 45–90 |

墙钟：每块 16 个验证 episode 在并发 8 下 15–30 分钟，全程多 3–5 小时。

报告不做"≥ v5"的断言：v5 是 S0 之前的（13 对 7 次传输失败），且同一原生 harness 在
v4→v5 之间基线从 22 掉到 17。它能检验的命题：强 learner 写的卡能不能过送达验证；
准入卡命中的后续题是否比其他题答得更好；现役版本逐块分数是否不退。知识卡在 50 题
上的效果上限约 3–4 题（6–8 分）。

P2（可选，能给出可比较结论）：evolve 集 = Pro 133 题里未用的 83 题（或取 40）；
T=8；每轮一个候选 = H_t 加本轮准入的卡；k=2；δ = H_0 重复 ≥ 3 次的成对差标准差；
式 5 下限与式 7 成本规则；held-out 50 题最后各测一次 k=3。约 1,820 个 episode，
250–400 美元，1–2 天。建议等 P1.5′ 证明卡片确实能被准入之后再决定。

## 12. MWM 与 prompt-only 怎么办

`cards` 是第三个架构，与 MWM、prompt-only 并列。它替代 MWM 承担 agentic / QA 场景的
adapter 角色；MWM 保留作对照臂和非平稳环境的方案。v1 实验只跑 stateless 对 cards。

## 13. 有意不做或推迟的事

- 不做假设账本、证伪合同、age-based staleness、子代理；
- learner 不写上下文、不写代码给控制器跑、不做裁决；不打包多张卡成一个候选；
- v1 无 MCP 工具（§1 的准入规则）；不按题排序（deploy 渲染 = 运行渲染更重要）；
- 前言不演化（筛查只覆盖卡片，前言可演化就有夹带通道）；
- 删除验证推迟到 v1.1（§6）；
- 不让工具语义可演化（lemma_board 允许，但那意味着 learner 写的代码进 actor 的 MCP
  服务器；等验证原语覆盖到代码改动再说）。

## 14. 被推翻与被改动的地方（留档）

| 机制 | 判决 | 理由 |
|---|---|---|
| 声明式 recipe 由控制器重放（v0） | 删 | §5.2 |
| alternative_cells 对比重放（v0） | 删 | 任何一个错单元格都能满足"竞争读法判错" |
| "0 反例"一票否决（v0） | 删 | 反例只在 learner 自选的 support 上数 |
| evolver 生命周期 + 改 definition.py（v0） | 删 | basic 型 evolver + protected + --goal 够用 |
| 在线统计门（δ、式 17）（v0） | 删 | n=40,k=2 时 δ≈4–6 分；单张散文卡测不出 |
| 退火预算 3→1（v0） | 改为常数 2 | §9 |
| actor 在 trace 里引用卡片 id（v0） | 删 | 散文遵从通道（R2） |
| 交付与验证逻辑放在 OfficeQA host 模块（v1） | 改为架构 | §1 |
| 账本放 adapter 里、每题写一次（v1.1） | 改为 run 目录、每块写一次 | arm.py:91,121-122 的只读校验；lemma_board 先例；components.md:41 |
| 筛查/判定/组装放架构受保护核心（v1.1） | 移到 evolvers/cards/block.py | 先例 gates.py 在 evolver 侧；不把 actor 从不执行的代码部署到每个工作区 |
| cards_list / card_read 工具（v1.1） | v1 不带 | 无上下文里没有的内容；required MCP 增加失败模式；CLBench 只分发 wm |
| 按题排序（v1.1） | 推迟 | 框架无 task 资源；v1 不会溢出；deploy 渲染应等于运行渲染 |
| 删除验证（v1.1） | 推迟到 v1.1 | 几乎不触发却占三分之一规范 |
| 常量 b/k/KEEP/COST_MAX 放 adapter（v1.1） | 移为 block.py 参数 | 协议参数不该改 adapter 指纹 |

## 15. 待你决定（附建议）

1. **learner 模型与通道。** 建议 gpt-5.6-sol 走 ChatGPT 登录（边际 0 元）；API 则每次
   10–30 美元。
2. **要不要跑第二次 stateless 基线。** 建议跑：8–12 美元换来实测翻转率。
3. **失控题能否做 support。** 建议能；需要改部署合同"经验 5"那一段。
4. **issue #47 的三题。** 建议从 support/keep 池剔除，计分保留但单列。
5. **删除验证是否进 v1。** 建议不进（§6）；命中记录先攒着。
6. **P2 现在预算还是等。** 建议等 P1.5′ 的账本显示卡片确实被准入。
