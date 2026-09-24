# 为什么 MWM 在 agentic workspace 和 coding 上不见效：原因、证据与改法

日期：2026-09-24。基线：GitHub `main` f6e51ff（本目录即该提交的全新克隆）。
对照：RRSI 论文（arXiv 2609.24972v1，Google，2026-09-21）、MetaWorld 原仓库
`metaworld/`、OfficeQA v3/v4/v5 三次成对实验、本地未提交的 v6 分支。

写法说明：每条原因先给结论，再给证据（文件:行号，或可复算的数字），最后
标注对抗验证的结果。7 条初始假设经 14 个独立验证代理逐条反驳，3 条被驳倒，
2 条被大幅修正，另有 4 条原因是查漏时补上的，排序按解释力。被驳倒的假设
也保留在文末，避免下一轮重复走弯路。

## 0. 一句话结论

MWM 输在两层，上层比下层重：

1. **交付层**：MWM 唯一真正到达做题 agent 的东西是散文简报。能承载"过程"
   的工具层和子代理层在所有已记录的运行里从未被 actor 调用过一次。而
   agentic 和 coding 任务的失败形态是执行保真度问题（循环、跳步、选错值、
   忽略已送达的规则），原仓库自己的裁定是"文字交付无法治"。RRSI 改的是
   harness 本身：完成前审计、错误恢复提示、循环控制，这些机制不依赖模型
   是否读了某段话。
2. **选择层**：框架和原仓库都没有按成绩选择候选的环节，任何通过结构检查的
   候选直接上线。更根本的是，单遍在线协议（每题一次、每 5 题学一次）没有
   足够的统计功效去做选择：同一 harness 在同 50 题上两次基线相差 5 题，
   RRSI 的噪声带则来自每轮 89 到 120 题各跑两遍。缺的不是"更丰富的反馈"，
   而是一个有功效的评估 harness。

## 1. RRSI 是怎么让 harness 变好的

RRSI = Regularized Recursive Self-Improvement，把 harness 演化看成在有限
evolve 集上的自适应经验优化，然后给"提案"和"选择"两侧各加正则项。

| 侧 | 机制 | 做法 | 我们的框架里有没有 |
|---|---|---|---|
| 提案 | 退火的编辑预算（L0 类比） | 每轮最多 b_t 条可独立归因的编辑，b 从 4 降到 1（式 4） | 无。learner 一次会话可改任意多文件 |
| 提案 | 证据感知的归因 | 记录每条编辑的组件、假设、diff、ΔS、ΔC、是否被采纳，供下一轮参考 | 无。原仓库的预测账本判定器从未运行，2026-08-21 删除 |
| 提案 | 结构化探索 | 进展停滞时把部分预算留给从未动过的组件类型 | 无 |
| 选择 | 泄漏筛查 | 评估前由 critic 读 diff，拒绝含任务名、实体、答案的编辑 | 仅 prompt 层禁止，无机械检查 |
| 选择 | 噪声下限（式 5） | 重复评估未演化的基线得到 δ，候选分数须 ≥ S* − δ | 无 |
| 选择 | 成本规则（式 7，L2 类比） | ΔC ≤ β0 + β1·ΔS，额外推理成本必须由可测增益支付 | 无 |
| 选择 | 结构剪枝（L1 类比） | n_prune 轮内没有正增益的组件列为删除目标 | 无 |
| 选择 | 最终选择 | 通过全部非补偿性条件的候选里取分数最高者，否则保留现役 | 无。candidate_ready 即采纳 |

RRSI 的结果（论文表 1 到表 4、图 3、图 4）：evolve 集上 +1.1 到 +6.0，
6 个从未见过的 held-out 集全部为正，最大 +4.7；去掉两侧正则后 evolve 集
反而更高（92.8）但 OOD 掉到 40.3，与未演化的 39.7 无异，token 从每次
1.56M 涨到 3.80M。选择侧只用两个标量：任务奖励 r∈[0,1] 和策略 token 数。
提案侧则用分析器从轨迹写出的跨轮失败反馈，不是只看答案。

## 2. 主要原因（按解释力排序）

### R1 交付缺口：能载"过程"的层从未到达 actor，散文层被少量消费且后期被截断

- 工具层：v5 最终世界模型自己的记录写着 block 4 时 wm 列出 "admitted tools:
  (none)" 并拒绝调用，50 个 episode 里没有任何一次 admitted tool 被尝试。
  `officeqa-codex50-full-mwm-v5-work/evolution/10/sessions/evolve-2vckxnbs/adapter/memory/tools-channel.md:4-7`。
  根因是移植时的规则 `schema.get('name')==directory`
  （`harnessadapter/architectures/mwm/mwm_core/capabilities.py:23-34`），
  加上执行沙箱只挂载 /adapter、看不到语料。
- 子代理层：原仓库 system-review 事故 #4"子代理在 host 容器不可运行，对
  host 从未生效"；banking 四方案 13 个 session 只产出 1 个子代理且被 meta
  自行退役（`metaworld/claude-doc/banking-conclusions.md` 第四节第 11 条）。
- 散文层：GitHub 提交 f6e51ff 的说明：wm search 对 100 次 host 查询里的
  91 次回答 "no matches"，42 题里有 30 题 host 从此不再咨询世界模型。v5 里
  Codex 从第 36 题起把 AGENTS.md 截断在 32,768 字节，open questions、目录
  和 API 说明全部丢失。
- 原仓库把这件事列为"实验主旨层面的问题"：block 2 的 7 条轨迹只有 2 条
  真实读取了世界模型文件（`metaworld/claude-doc/system-review.md` D1）。

验证结果：查漏补充，排位第一。上游于其余所有原因：选择、归因、成本都作用
在 learner 写出来的东西上，而写出来的东西大部分没被消费。

### R2 执行保真度下限：agentic 和 coding 的失败是执行漂移，散文治不了

- v5 最终 journal："repeat-loop 是主导的残余失败形态"，block 10 五题里两题
  超过 90% 的请求在重发相同命令；UID0029 364 次请求，UID0237 272 次 wm
  调用，后者是 20 个不同调用各重复约 13 次，因为参数形状写错后一直收到错误
  串（`evolution/10/.../history/meta/journal.md:137`；调用统计来自
  `runs/adapted/044`）。
- "submit early""不要重发失败的查询"这类规则在 t=4 起就在简报里，之后仍
  出现 6 次纪律性空提交（`adapter/memory/submission-discipline.md:7`）。
- 原仓库对 banking 和 coding 为零的最终裁定：97 题双输 55 题，实读样本的
  失败在审计漏行、接受用户端假成功、长对话跳步，"执行保真度问题，文字交付
  无法治"（`metaworld/claude-doc/auto-research-log.md:1245-1252`）。
- 对照 RRSI 表 6：被采纳的编辑是"完成前的有界验证审计""对反复出现的
  workdir 错误的有界恢复提示"，都是 harness 机制，不是知识。

验证结果：查漏补充，排位第二。它和 R1 合起来说明：agentic 任务不是输在
知识通道上。

### R3 运行故障淹没学习效果，且 Codex 压缩阈值配置错误仍在 GitHub 主线

| v5 | 错题 | 传输失败 | 失控 | 有答案但错 |
|---|---|---|---|---|
| adapted | 30 | 13 | 6 | 11 |
| baseline | 33 | 7 | 6 | 20 |

- 两边都完成的 27 题里 adapted 19 对 14，只有一边对的 6 比 1。adapted 的
  13 次传输失败集中在第 26 到 48 题一段，9 次在第 1 个请求、零 token 时死亡，
  是一次提供方故障窗口，不是 adapter 引起的负载。
- Codex 被配置成 `model_context_window` 1,048,576、自动压缩阈值 800,000，
  而两个 arm 里所有长 episode 的实际上下文都在约 245K 处被提供方切断，压缩
  永远不会先于断流触发。v5 `acting.py:291`；GitHub 主线仍是
  `tasks/OfficeQA/host/acting.py:38,364`（注释称这是 Fireworks 声明的
  context_length）。同文件另一个 profile 用的是 196,608 / 160,000。
- GitHub 记录的 2026-09-21 运行：22/50 对 22/50，10 轮学习只有第 1、5、8 轮
  被采纳，其余 6 轮耗尽 learner 输出预算，1 轮超出输入上下文
  （`tasks/OfficeQA/README.md:24-40`）。那次的零结果主要是 learner 侧基础
  设施故障。
- 原仓库一周内记录 14 起 harness 事故，其中一起让全部 SWE 数据作废
  （`metaworld/claude-doc/system-review.md` 第一节）。

验证结果：查漏补充。它是测量混杂而非设计原因，但比任何单条设计原因都更能
解释观测到的差距。

### R4 没有按成绩选择或回滚，而且现有协议没有做选择的统计功效

- 采纳判据：learner 进程正常退出、报告 success、至少一次 broker 文件调用
  成功、受保护文件未变、结构校验通过，即 `candidate_ready`
  （`harnessadapter/evolution/session.py:239-242`）。README:55 明说
  "not that performance improved. Nothing is promoted automatically"；
  OfficeQA 控制器对任何 candidate_ready 直接替换现役
  （`tasks/OfficeQA/host/learn.py:106-113`）。
- 原仓库：证伪合同的判定函数 `evaluate()` 整个 v6 代读的是旧文件，从未
  运行，28 个世界模型累计 937 条预测全部停在 pending，2026-08-21 决定不
  重建（`metaworld/mwm/contracts.py:11-18`）；session.py 的立场是"分数
  本身就是度量"（`metaworld/mwm/session.py:7-11`）；准入门 137 场只触发 4
  次、全是格式问题，改为只提建议（`gates.py` check() 文档串）。原仓库唯一
  的回滚是越界读写触发的 `git reset --hard`，与质量无关。
- 功效：同一 native harness 在同 50 题上，v4 基线 22 对、v5 基线 17 对；
  1% 口径 30 对 21，11 题翻转，精确检验 p=0.012。以每题翻转率约 20%、
  n=50、k=1 估算，成对分差的标准差约 6 分，按 RRSI 式 5 校准的 δ 会在
  6 到 12 分，任何候选都过不了门。RRSI 的 δ 是 0.4 到 2.0 分，来自每轮
  89 到 120 题各跑 2 遍（表 5）。
- 注意：之前用来估噪声的 bw_orig/bw_rep 是另一套 Python 检索基线，与
  v3 到 v5 的 50 题只有 18 题重合，不能作为本 harness 的噪声带。

验证结果：保留，措辞修正。"没有选择"改为"没有按成绩的选择或回滚"；并且
它在 R1 到 R3 修好之前不会成为瓶颈。

### R5 归因交给 LLM 单样本更新：不是错规则持久，而是一遇反例就改

- 使用记录是有的：每次 wm recall 都进轨迹，clbench_recall 有 uptake 日志。
  缺的是"使用 × 判分"的连接，而这一半在 2026-09-06 被有意删除：
  "outcome rates of the episodes an entry's scope matched … was removed with
  every other wrapper-computed statistic"（`mwm_core/staleness.py:5-10`）。
- 实际观察到的是噪声追逐：v5 的版本规则在 1 个反例上拆成年度/月度两条
  （最终记忆 `bulletin-revision-latest` 标 "1/1 verified"，其 open 行承认只
  有一个正例）；v4 的"去最新一期核对"默认值在首次误用后的下一场会话即被
  降级；banking 审查记录连续 9 个 session 改写同一条卡片规则。
- 两个曾被当作"错规则持久"的例子经核对不成立：Q29 没有调用任何 lesson，
  Q36 基线给出了完全相同的错答案。Q17 的规则在误导时有 4/4 支持，按结果
  计数的追踪器只会把它升级。

验证结果：方向反转后保留。它指向 RRSI 式的噪声带和最小支持数，而不是
一个使用账本就够。

### R6 learner 模型低于原设计的能力下限

- v5 的 learner 是 GLM-5.3-Flash（`learning.py:26`），所有反馈和修订都由它
  写。原仓库记录的每一次获胜用的 meta-agent 是 Opus 5、gpt-5.6-sol 或
  kimi-k3。设计文档的不变量："实验设计固定一个强 meta-agent，把 host 作为
  变量，不反过来"（`metaworld/claude-doc/meta-world-model-design-hypotheses.md:150`），
  并引用 Continual Harness 的测量：meta-agent 弱于阈值时自我改进净负。
- 用同一 flash 模型做 learner 的 banking 实验测到"过度遵从"：四句本意
  正确的指令都被执行到字面极端并造成失分
  （`metaworld/claude-doc/harness-design-experience.md:29-52`）。

验证结果：查漏补充，中等权重，未做换模型消融。

### R7 场景条件：信息不对称、失败可归因、有活的可执行通道

- 原仓库的推论：适合 MetaWorld 的域要同时满足 host 无法廉价自查的环境
  知识、失败可归因为可成文机制、可执行件有真实调用通道；banking 三条全
  不满足，coding 满足第三条但不满足第一条（`auto-research-log.md:1250-1252`）。
- coding 唯一有效的成对结果：flipt 36/85 对 34/85，qutebrowser 56/79 对
  58/79，净效应 +2/−2（`auto-research-log.md:1298-1305`）。此前 flipt v2.1
  的 89.4% 对 45.7% 发生在 08-15，08-17 用户裁定 SWE 全部数据因泄漏作废
  （host 经 go proxy 拉取了本仓库的未来版本，`system-review.md` 事故 #1），
  不能引用。
- banking 是版本相关的：v2.2 brief 37/97、v3.1 econ 41/97（+11.4），v5.1
  31/97（±1）。
- 我最初的"只在单一持久环境上赢"不成立：SWE-bench Pro 正是因为同一仓库
  被 56 到 96 题重访才被选中，OfficeQA 也是同一份 697 文件语料；DABstep 在
  从未见过的题型上 +49.7。

验证结果：驳倒后改写。区分度是三条件，不是"是否重复"。

### R8 本地 v6 的 lesson 重放是出处检查，不是规则检验

- `postmortem.replay()` 只做两种检查：语料某行含某子串；learner 自己写的
  候选字符串与已放出参考答案比对。单元测试直接把 candidate 设为参考答案并
  断言 verified（`tests/test_postmortem.py:106-114`）；唯一一次真实重放
  （v6c block 1）的两条 answer check 都原样写着参考答案串。没有任何代码
  执行 lesson 陈述的规则。
- 阶梯 S1 写死了"月度行用首印值"（`postmortem.py:17`）。这条规则来自
  UID0155 的一个参考答案，而那题在 v5 里没有 actor 运行（example 记录
  首行 "NO ACTOR RUN — transport_rejected"）。OfficeQA 论文附录 E.5 给
  agent 的指令是"多份文件报同一指标时用最新修订，除非题目指定日期或文件"，
  参考答案本身在这题上违反了它；databricks/officeqa issue #47（2026-06-23
  开放、维护者未回复）把 UID0155 列入"后期公告修订了数字、答案应更新"，
  并指出 UID0213（CPI 年均还是 12 月值）和 UID0237（比值、百分比还是
  十亿美元）题意不清。这三题都在我们"六个格子全错"的 16 题里。
- 原仓库 banking 审查第 4 条记录了工具准入门的同一模式：test.py 只回放
  与当前读数一致的题，"自证通过"。

验证结果：保留，措辞修正："永远保持 verified"是从代码推出的结构性结论，
目前只有一次重放记录。

### 被驳倒的两条假设（保留以免重蹈）

- **"准入判据把过程性知识挡在门外"**：不成立。v5 的记忆和简报里就有
  submit-early、loop-breaker、搜索纪律等过程规则，contract 也允许 tools 和
  agents；banking 里手写的纪律结构（清单、闸门、收尾审计）得分约 0，知识
  通道反而 +11.4。真正的缺口是过程没有可执行、可强制的载体，已并入 R1。
- **"成本失控"**：不成立。v5 adapted 全程输入 194.1M token、1,987 次请求，
  低于 baseline 的 320.1M、3,271 次；第 44 题的 38.3M token 里 98.4% 是缓存
  命中，结束于传输失败。v6 也没有"移除上限"，v5 协议本来就没有上限，v6
  增加的是重试。原仓库每题 100 次 wm 调用的上限是失控守卫，不是成本控制。

## 3. 改法（按实施顺序）

### S0 先把测量修对（R3）

- Codex 上下文窗口和压缩阈值改成实测上限之下（同文件另一 profile 的
  196,608 / 160,000 是现成模板），让压缩先于断流发生。
- 保留 v6 的重试和两臂同步推进；传输失败的题对两臂对称剔除或重试后再计分。
- harness 级循环守卫：第三次完全相同的命令或 wm 调用直接拒绝并提示换角度；
  终止时提交已有的最佳候选。

### S1 让"过程"有可执行的载体（R1、R2）

- 修工具服务：按 `_module` 分发而不是目录名等于 schema 名；工具沙箱挂载
  语料只读；wm 参数用类型化 schema 和别名，未知键返回用法提示，避免
  20 个调用重复 13 次那种接口循环。
- 把 RRSI 表 6 那类机制做成 adapter 的工具和钩子而不是散文：完成前审计
  （核对题目要求的每个量都有值和单位）、错误恢复提示、提交纪律。
- 简报按题检索注入，总字节预算硬性执行，API 说明放在简报之前。

### S2 建一个有功效的评估与选择环节，只用答案（R4、R5）

- 两级验证。可执行的 lesson（取数位置加公式）由控制器自己推导计算，再用
  reward.py 判分：没有采样噪声，一题反例即拒收，这一级零成本。行为类改动
  才走统计选择。
- 统计选择按 RRSI 选择侧实现：候选和现役在累计已放出题集上各跑 k≥2 遍；
  δ 用同一 harness 重复跑未演化基线校准（不是别的检索基线）；候选须满足
  S ≥ S* − δ、ΔC ≤ β0 + β1·ΔS；否则保留现役。功效不够的早期块只准入
  第一级可验证的 lesson。
- 泄漏筛查机械化：lesson 文本含题号或参考答案串即拒收；answer check 改为
  推导，不接受候选字符串。
- 稀疏与剪枝：每块最多 b_t 条 lesson 变更，随块数退火；n 块内未被 recall
  或引用的 lesson 列为删除目标。
- 恢复"使用 × 判分"的连接，但作为 RRSI 剪枝和最小支持数的输入，不作为
  自动裁决；一条规则至少两个独立题目支持才进简报。

### S3 learner 与协议（R6、R7）

- meta-agent 用强模型；证据给摘要面板加原始轨迹可读，而不是 30,000 字符
  分页硬读。
- 若目标是可比较的结论，改用 RRSI 的划分：在 evolve 集上多轮演化（例如
  OfficeQA Pro 133 题中未用的 83 题，或 Full 的 113 题），每轮 k=2，
  held-out 的 50 题只在最后测一次。单遍在线流可以继续作为提案来源，但
  采纳决定放到有功效的评估上。
- 对 issue #47 列出的题（UID0155、0213、0237 等）标 UNRESOLVED，不从它们
  造规则。

## 4. 对"只根据答案迭代经验"的直接回答

答案信号足够做选择：RRSI 的选择侧只用每题的标量奖励和 token 成本，
OfficeQA 的评分脚本正好给出前者。但它有两个前提，现在都不满足：一是
功效，单遍在线、每题一次、每 5 题学一次的协议在 n=50 时噪声带 6 到 12 分，
必须用机械推导重放和 k≥2 的累计重放补上；二是信号纯度，标量奖励分不清
"规则错了"和"流断了"，传输失败要先剔除。提案侧则不必只看答案：RRSI 自己
也用分析器从轨迹写失败反馈。所以正确的分工是：提案可以用轨迹和答案，
采纳只认答案加成本，而且要有功效。

## 附：验证方法

7 条初始假设各由两个独立代理反驳（一个核对引用是否属实，一个找替代解释），
再由一个查漏代理补充遗漏并排序。保留：R4 前身、R8。修正后保留：R5、R7。
驳倒：准入判据说、成本失控说、"只在单一持久环境上赢"说的二分法部分。
查漏补充：R1、R2、R3、R6。原始判定记录在会话的工作流输出里。
