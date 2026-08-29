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

**每个新目录 × 每种 agent 各需要一次人工信任确认。** 三种 CLI 的信任状态存在不同地方,且都是精确路径匹配、不继承父目录:

| agent | 落盘位置 |
|---|---|
| claude | `~/.claude.json` → `projects.<path>.hasTrustDialogAccepted` |
| codex | `~/.codex/config.toml` → `[projects."<path>"] trust_level` |
| agy | 未找到明确位置 |

编排器**不会**自动应答这些框 —— 它认出来就通知你并保住那个 pane。

## 用法

```bash
python3 orch.py <repo-root> "<任务描述>" --skip-perm
```

默认跑 `implement → review(双审) → fix → verify`。

| 参数 | 作用 |
|---|---|
| `--skip-perm` | 跳过权限确认。**按 agent 种类翻译**成各 CLI 自己的 flag |
| `--scan` | 打开勘查阶段(默认关) |
| `--report` | 打开总结阶段,写 `.orch/report.md`(默认关) |
| `--no-double` | 关掉 review 双审 |
| `--kind KIND` | 全阶段统一用某种 agent(调试/对照用) |
| `-- <agent-args>` | 原样透传给所有 agent。**异构模式下别用它传权限类 flag** |

最后那条是踩过的坑:`--dangerously-skip-permissions` 传给 codex 会让它 `error: unexpected argument` 然后启动超时。用 `--skip-perm` 让编排器按 kind 翻译。

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

## 已知边界

- **双审目前是纯开销**。实测两个 reviewer 提的是同样 4 个问题,只是措辞不同。原因可能是 review 的 7 条检查清单太具体,消掉了"不同视角"的空间。要让双审有意义,得给两个 reviewer 不同的清单 —— 那是另一个设计。findings 不做程序化去重(前 80 字符对不上,`文件:行` 又全指向同一处),由 fix 按语义归并。
- **scan 在小仓库是纯开销**。它给出的"只有一个函数、无测试框架、无构建配置"这类信息,implement 自己几秒就能看出来。大仓库里"目标涉及哪些文件、该跑哪条命令验证"才值得先勘查。
- **所有验证都在一个 3 行的玩具仓库上做的**。`fix → verify` 多轮回环只在单元测试里走过,真实场景 verify 每次一轮就过。`salvage()` 只在刻意造的超时场景触发过一次。
- **严格串行**。review 和 review2 完全独立,本可以并行。
- `--kind` 统一模式没怎么实测,异构才是主路径。

## 文件

- `orch.py` —— 编排器:阶段定义、prompt、控制流、补偿机制
- `herdr_api.py` —— Herdr CLI 的薄封装。区分 exit 2(用法错误,永不重试)和 exit 1(server 错误,按 code 决定)
