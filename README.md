# herdr-orch

用 [Herdr](https://herdr.dev) 把多个编码 agent 编排成一条流水线:实现 → 评审 → 修 → 验证。

每个阶段起一个**全新的 agent**,跑在自己的 tab 里,拿自己的上下文。阶段之间不直接通讯 —— 上一阶段把结果写成 JSON,编排器读出来塞进下一阶段的任务文件。

异构派活是核心:让每个 agent 的短板落在它不负责的维度上。

| 阶段 | agent | 为什么是它 |
|---|---|---|
| scan (可选) | agy | 快,而且这活儿不写代码,代码能力差距不体现 |
| implement | claude | 要写对且不过度防御 |
| review | codex + agy | 细心是评审的核心美德;评审不写代码,所以"过度防御"这个缺点在这里不成立 |
| fix | claude | 同 implement |
| verify | codex | 跑测试报结果,细心 > 手快 |
| report (可选) | agy | 拟人化文档 |

## 前置条件

- Herdr 0.8.2+,且当前进程跑在 Herdr 管理的 pane 里(`HERDR_ENV=1`)
- 用到的 agent CLI 已安装并登录:`claude` / `codex` / `agy`
- Python 3.10+(用了 `X | None` 语法)

**每个新目录 × 每种 agent 各需要一次人工信任确认**,这是目录级的、点一次就落盘。三种 CLI 的信任状态存在不同地方,且都是精确路径匹配、不继承父目录:

| agent | 落盘位置 |
|---|---|
| claude | `~/.claude.json` → `projects.<path>.hasTrustDialogAccepted` |
| codex | `~/.codex/config.toml` → `[projects."<path>"] trust_level` |
| agy | 未找到明确位置 |

编排器**不会**自动应答这些框 —— 它认出来就通知你、保住那个 pane,并把接回信息写进 `state.json`。点完之后 `--resume` 接回去,不必重跑本阶段。

注意目录信任框和**逐次操作确认**是两套机制,别混。上面那张表说的是前者:一个目录点一次、落盘、之后不再问。后者是 claude 每改一个文件、每跑一条命令都要点一次 —— 那个由 `--skip-perm` / `--accept-edits` 管(见用法),不给的话流水线走不完。

## 用法

```bash
python3 orch.py <repo-root> "<任务描述>" --skip-perm
```

默认跑 `implement → review(双审) → fix → verify`。

| 参数 | 作用 |
|---|---|
| `--skip-perm` | 跳过权限确认。**按 agent 种类翻译**成各 CLI 自己的 flag |
| `--accept-edits` | 中间档:只自动批准文件编辑,命令审批和信任框仍留给人。两档同给以 `--skip-perm` 为准 |
| `--resume` | blocked 人工应答完之后接回原 agent 继续等结果,已跑完的阶段复用盘上结果 |
| `--scan` | 打开勘查阶段(默认关) |
| `--report` | 打开总结阶段,写 `.orch/report.md`(默认关) |
| `--no-double` | 关掉 review 双审 |
| `--kind KIND` | 全阶段统一用某种 agent(调试/对照用) |
| `-- <agent-args>` | 原样透传给所有 agent。**异构模式下别用它传权限类 flag** |

最后那条是踩过的坑:`--dangerously-skip-permissions` 传给 codex 会让它 `error: unexpected argument` 然后启动超时。用 `--skip-perm` 让编排器按 kind 翻译。

**权限这三档必须选一档,不给会丢工作。** 实测(Desmos,10k 行真实项目):不给任何一档,implement 阶段第一次改文件就弹「Do you want to make this edit to dft.ts?」,编排器按设计返回 blocked 退出 —— 屏幕上已经是一份正确的 diff,而磁盘上零字节,一个阶段的思考白扔。

而 `--skip-perm` 在**嵌套 agent 环境里可能整条不可用**:它对 claude/agy 翻译成 `--dangerously-skip-permissions`,如果编排器本身跑在某个 agent harness 里,上层权限策略会把这条命令直接拦掉(实测被 Claude Code 的自动模式分类器拒绝)。这种场景下用 `--accept-edits`。

`--accept-edits` 给 claude 的是 `--permission-mode acceptEdits` **加一份验证命令白名单**。白名单不是可选的:`acceptEdits` 只覆盖文件编辑,不覆盖 Bash。实测同一次 Desmos 跑,fix 的编辑全部免确认应用了(642 行落盘),但它接着要跑 `npx vitest run` 自验时照样 blocked。implement/fix 改完必然跑测试,verify 阶段本身只跑命令 —— 只给 `acceptEdits` 等于把 blocked 从"第一次编辑"推迟到"第一次跑命令"。

白名单(`VERIFY_CMDS`)是刻意窄的:测试/构建/lint/包管理器和只读 git。`git push`、`git reset`、`rm`、`curl` 这类不在里面,仍然会停下来问人。

`agy` 没有对应档位(见已知边界),`codex` 靠 `KIND_ARGS` 里的 `-a never` 关命令审批、写文件本身不弹框,两者都不需要这个 flag。

## 产出

全部在 `<repo-root>/.orch/`:

```
.orch/
  tasks/goal.md            原始任务
  tasks/<stage>.md         每阶段的完整指令(含检查清单和输出契约)
  results/<stage>.json     每阶段的产出 —— 唯一的完成判据
  results/review2.json     副审的产出
  state.json               运行状态和日志
  salvage-<stage>.md       阶段失败时打捞的屏幕内容
  report.md                --report 时生成
```

阶段产出统一契约:

```json
{
  "status": "ok | failed | blocked",
  "summary": "一两句话",
  "findings": ["文件:行 — 问题 — 建议改法"],
  "files_changed": ["相对路径"]
}
```

## blocked 不是终点

人工闸门是刻意保留的,但**撞上闸门不该让一个阶段的工作作废**。

Desmos 实测:implement 思考两分钟、屏幕上已经是一份正确的 diff,卡在「Do you want to make this edit to dft.ts?」上。编排器返回 blocked 退出,而 `git status` 显示那个文件零字节 —— 编辑还在框里,那份实现无从取回。

关键事实是 pane 和 agent 都**还活着**(`keep=True` 跳过了 cleanup,实测 `agent_status` 仍是 `blocked`),人点完框之后 agent 会自己接着干完并写出结果文件。所以缺的只是接回去的路径:

```bash
# 撞上闸门 -> 去那个 pane 点掉 -> 接回来
python3 orch.py <repo-root> "<任务>" --accept-edits
python3 orch.py <repo-root> "<任务>" --accept-edits --resume
```

`--resume` 做两件事,都不新建 agent:

- **接回 blocked 的那个阶段** —— 不 `tab_create`、不重发 prompt、不删结果文件,直接回到轮询。
- **复用已跑完的阶段** —— 盘上有 `results/<slot>.json` 就直接拿。不复用的话 resume 会把 implement/review 全部重跑,等于没修。

一个阶段弹多个框是常态(claude 每改一个文件问一次),所以仍 blocked 时会**重新记一次**接回信息,可以反复 resume。这里踩过一个坑:每一跑的 `self.state` 是全新骨架,`save()` 一写就把上一跑的 `blocked` 覆盖掉 —— 不重记的话第二次 resume 就接不回来,恰好在最常见的场景下失效。

`agent` 已经不在了(pane 被关、进程挂了)就作废接回信息、退回重跑,并在日志里说清原因。

## 三条设计原则

这三条不是设计时想清楚的,是踩出来的。

### 1. 结果只走文件,不读屏

agent TUI 跑在 alternate screen 上。Herdr 0.8.0 起会对 idle 的 agent 自动滚轮收割历史,所以屏幕读取**能**超出视口高度(实测视口 53 行读到 131 行),`PaneReadResult.truncated` 也是 required 字段,截断不再静默。

但这个通道深度有界(`max_offset_from_bottom`),且依赖 agent 处于 idle、上报滚轮事件、未被手动滚动。对编排器来说"有界且条件不可控"和"读不到"是同一类风险:**不能当契约**。

所以 prompt 只发一句指向任务文件的短指令,阶段产出走 `results/*.json`。屏幕只用于诊断和打捞。

### 2. 完成信号 = 结果文件可解析,不是 `agent_status`

`idle` 只表示"在等输入",`unknown` 表示 Herdr 无法分类,两者都不代表任务做完或做对。`--wait` 只用来省轮询开销,不作真值来源。

这条原则挡住了四起真实的"agent 报 done 但什么都没交付":

- agy 卡在信任框 —— Herdr 的 agy 检测清单缺 trust 规则,兜底报成 `idle → done`
- codex 被 `-s read-only` 挡住写入 —— `EPERM` 但它仍报 done
- codex 在 `--add-dir` 下静默失败
- codex 的 prompt 没提交 —— 回车被 MCP 启动流程吃掉

如果用 `agent_status` 当判据,这四起全会变成静默错误(review 拿到空产出去评审空气)。因为只认 JSON 文件,它们全部表现为"超时"这个可诊断的失败。

### 3. 辅助阶段不参与成败判定

`scan` 和 `report` 在 `ADVISORY_STAGES` 里。scan 扫不出来不该中止实现;report 失败不能把已验证通过的流水线判成失败。

## 两个补偿机制

Herdr 和各家 CLI 的交界处有些时序问题,编排器自己兜。

**`trust_blocked()`** —— Herdr 对 codex 有 `trust_directory` 检测规则,对 agy 没有(检测清单老两个月),agy 卡在信任框时会被误报成 `idle → done`。编排器主动扫屏幕上的信任框文案,认出来就当 blocked 通知人。这是补检测缺口,不是自动应答。

**`nudge_submit()`** —— codex 启动 MCP 期间,Herdr 判 idle 用的是 `osc_title_idle`(终端标题变了就算就绪),比"TUI 能受理输入"宽松。这时送的 prompt 回车会被吞掉,文本留在输入行,状态 `idle`。编排器无法区分"在思考"和"输入没提交",会白等到超时。

判据是**反向**的 —— 屏幕上没有活动迹象就补 enter,而不是"看到 prompt 文本才补"。因为 codex 会把输入行重绘掉:屏幕只剩状态栏一行,文本却仍在输入缓冲里(对那个 pane 直接送 enter,状态立刻 `idle → working`)。正向判据在这种情况下该补而不补。

安全性靠三层,不靠"认出自己的文本":只在拿不到结果且状态是 idle/done 时才调用(真在干活的是 `working`)、`BUSY_MARKS` 检测活动行、`TRUST_PATTERNS` 再挡一道对话框。有 45 秒节流和 3 次上限。

第二层曾经是**完全失效**的,Desmos 实测抓到:原来只扫屏幕尾部 4 行,而 herdr pane 底部固定挂着状态栏(Session/Model/分支/manual mode)恰好占满 4 行 —— 扫到的永远是状态栏,活动行在它上面。实测 claude implement 阶段,活动行 `✶ Nebulizing… (2m 0s · still thinking with high effort)` 在距底 8 行,命中 `None`;而且当时 `BUSY_MARKS` 里也没有任何 thinking 文案,两道都漏。正在高强度思考的 agent 会被判成"无活动迹象"而被补 enter。

现在扫 `NUDGE_SCAN_LINES`(14)行,并把各家的 thinking 文案补进了 `BUSY_MARKS`。claude 的旋转词是随机的(Nebulizing/Pondering/…)靠不住,所以认它后面那截固定的进度括号(`still thinking`、`tokens`)。

这个 bug 在玩具仓库上永远不会暴露:触发还要求 `agent_status ∈ (idle, done)` 同时成立,小任务的 agent 很快进入真 idle,只有长任务才会在 working/idle 边界上撞到。

## 已知边界

- **双审目前是纯开销**。实测两个 reviewer 提的是同样 4 个问题,只是措辞不同。原因可能是 review 的 7 条检查清单太具体,消掉了"不同视角"的空间。要让双审有意义,得给两个 reviewer 不同的清单 —— 那是另一个设计。findings 不做程序化去重(前 80 字符对不上,`文件:行` 又全指向同一处),由 fix 按语义归并 —— 只在**两个来源都真的提了意见**时才这样(否则 `double` 不置位,fix 拿普通 prompt;实测副审被打断、0 条 findings 时,fix 收到归并指令后自己发现"6 条全来自 codex,无跨源重复",指令空转)。

  Desmos 那次没能给这条结论提供新证据:副审 agy 报了 `ok` 且 0 findings,但它中途被命令审批打断过,无法区分"审完认为没问题"和"没审到那些点"。而主审 codex 提的 6 条里有一条是真问题且我自己核对时漏掉的(见下),所以 review 本身不是冗余 —— 冗余的可能只是"第二个 reviewer 用同一份清单"。
- **scan 在小仓库是纯开销**。它给出的"只有一个函数、无测试框架、无构建配置"这类信息,implement 自己几秒就能看出来。大仓库里"目标涉及哪些文件、该跑哪条命令验证"才值得先勘查。
- **`--resume` 靠 `state.json` 里的 `blocked` 表接回**,按 slot 存,一次跑里多个阶段先后 blocked 互不覆盖(实测过 review2 被 fix 覆盖那种情况)。但记录只在同一个 `.orch/` 下有效,换 root 或删掉 `.orch/` 就接不回了。
- **`--accept-edits` 对 agy 无效**。agy 只有 `--dangerously-skip-permissions` 一档全开,没有"只放开编辑"这一级。实测:review2 的 agy 卡在 `git diff` 的命令审批框上,双审在这一档下必然退化成单审。启动时会警告,要无人值守跑完就用 `--skip-perm`。
- **单阶段最坏耗时曾是声称值的两倍**。`agent_prompt(wait=True)` 和之后的文件轮询原本各拿一份完整 `timeout_sec`。已修成共用一个 deadline(wait 拿剩余预算的 80%),实测 `timeout_sec=120` 时阶段实耗 120s,旧行为 240s。但仍**没有全局预算上限** —— 5 阶段串行只是各自不再翻倍。
- **`salvage()` 仍未在真实项目上验证**,只在刻意造的超时场景触发过一次。`fix → verify` 的多轮回环也只在单元测试里走过 —— Desmos 那次 verify 一轮就过。

  完整链路本身跑通了:`implement(claude) → review(codex)+review2(agy) → fix(claude) → verify(codex)`,`final_stage=verify status=ok`。verify 独立跑了 vitest/lint/build 并数了断言条数核对文件内容,没有替 fix 圆场。产出质量另经独立核对(未采信 agent 自报):175 测试连跑 5 次全过,用另写的 oracle 复验数值语义 14/14。
- **严格串行**。review 和 review2 完全独立,本可以并行。
- **workspace 靠 label 猜**。原来硬取 `workspaces[0]`,实测在 Desmos 上取到的是 herdr-orch 自己的 workspace —— `tab_create` 带 cwd 所以功能不出错,但 pane 全建到了无关 workspace 里。现在按 `root` 的目录名匹配 label,匹配不上退回第一个并记日志。目录名和 label 不一致时仍会落到兜底路径。
- `--kind` 统一模式没怎么实测,异构才是主路径。

## 文件

- `orch.py` —— 编排器:阶段定义、prompt、控制流、补偿机制
- `herdr_api.py` —— Herdr CLI 的薄封装。区分 exit 2(用法错误,永不重试)和 exit 1(server 错误,按 code 决定)
