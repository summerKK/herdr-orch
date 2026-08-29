#!/usr/bin/env python3
"""实现 → 评审 → 修 → 验证 流水线编排器。

两条硬性设计原则:

1. 结果走文件,不走 agent read。
   agent TUI 跑在 alternate screen 上,超出窗口高度的行被覆盖而非滚入
   scrollback,herdr 无法访问。read 的截断是静默的 —— 你会拿到看起来
   完整的文本,只是开头没了。所以 prompt 只发一句短指令指向任务文件,
   agent 把结果写进 results/<stage>.json。

2. 完成信号 = 结果文件可解析,不是 agent_status。
   idle 只表示"在等输入";unknown 明确表示 herdr 不确定。两者都不代表
   任务做完或做对。--wait 仅用于省掉轮询开销,不作真值来源。

注 1 的准确表述(herdr 0.8.2 实测修正):
   0.8.0 起 herdr 会对 idle 的 alternate-screen agent 自动滚轮收割历史,
   所以屏幕读取能超出视口高度 —— w9:p8 视口 53 行,实测读到 131 行。
   截断也不再是静默的,PaneReadResult.truncated 是 required 字段。
   但这个通道的深度有界(max_offset_from_bottom),且依赖 agent 处于
   idle、上报滚轮、未被手动滚动。对编排器来说"有界且条件不可控"和
   "读不到"是同一类风险:不能当契约。所以阶段产出仍走 results/*.json,
   短观测才用 read_or_file。
"""
import json
import sys
import time
from pathlib import Path

import herdr_api as h

POLL_SEC = 3
# 补 enter 前至少等这么久。反向判据(见 nudge_submit)会在 agent 刚受理
# prompt、还没打出输出的窗口里也判"该补",等一等让它露出活动迹象。
NUDGE_WAIT = 45
# 全部阶段,按执行顺序。scan/report 是可选的两头 —— 见 ADVISORY_STAGES。
STAGES = ["scan", "implement", "review", "fix", "verify", "report"]

# 这两个阶段不参与成败判定:
#   scan   给 implement 当参考。扫不出来就照常实现,不该因此中止。
#   report 只做人读的总结。它失败不能把已经验证通过的流水线判成失败。
# 其余阶段的 status 才是流水线的真值。
ADVISORY_STAGES = {"scan", "report"}

# 每个阶段派给哪种 agent。异构的意义是让每个 agent 的短板落在它不负责的
# 维度上 —— codex 细心但写代码过度防御,所以只让它评审和验证(评审不写代码,
# 缺点不成立);agy 快、代码能力弱,所以只让它扫仓库和写总结。
STAGE_KIND = {
    "scan": "agy",
    "implement": "claude",
    "review": "codex",
    "fix": "claude",
    "verify": "codex",
    "report": "agy",
}

# review 的副审。双审的意义是两个独立视角,不是投票 —— 见 double_review()。
# agy 代码能力弱于 codex,但它探索面广(实测会主动翻 git log、找相邻文件),
# 容易注意到 codex 按清单逐条走时不会去看的地方。
REVIEW_SECOND_KIND = "agy"

# 每种 kind 的必备启动参数。都是实测踩出来的:
#
# claude: ~/.mcp.json 全局继承,不切断则每个新 agent 都弹一次 "New MCP
#         server found",而那个框选"仅本次"不落盘 —— 流水线跑不完。
# codex:  --no-alt-screen 跑 inline 模式保留 scrollback,打捞路径不再受
#         alternate-screen 收割深度限制。-a never 关掉命令审批框。
# agy:    暂无必备参数。但它的 herdr 检测清单(2026.06.24.1)没有信任框
#         规则,认不出就兜底成 idle —— 见 TRUST_PATTERNS。
KIND_ARGS = {
    "claude": ["--strict-mcp-config"],
    "codex": ["--no-alt-screen", "-a", "never"],
    "agy": [],
}

# 本来想用 codex 的 -s read-only 把"评审不改代码"变成机制约束,实测行不通:
# read-only 是整个工作区级别的,把 .orch/results/ 一起挡掉了 ——
#   Called 写入评审结果
#   └ EPERM: operation not permitted, open '.../.orch/results/review.json'
# 而契约要求每个阶段写自己的结果 JSON。codex 会报 done 但文件永远不出现,
# 编排器白等到超时 —— 比不加沙箱更糟。--add-dir 也救不了:实测 read-only 下
# 它不生效,codex 同样回 done 而文件没写出来。
# 所以统一给 workspace-write,"不改代码"退回 prompt 约定。
SANDBOX_ARGS = {"codex": ["-s", "workspace-write"]}

# "跳过权限确认"每种 CLI 一个写法,完全不通用 —— 实测把 claude 的
# --dangerously-skip-permissions 传给 codex,codex 直接
# "error: unexpected argument",agent_start 超时。所以这个意图必须由编排器
# 按 kind 翻译,不能让调用方把某一种 CLI 的 flag 透传给所有 kind。
# codex 的 -a never 已在 KIND_ARGS 里(关命令审批),这里不重复。
SKIP_PERM_ARGS = {
    "claude": ["--dangerously-skip-permissions"],
    "agy": ["--dangerously-skip-permissions"],
    "codex": [],
}

# 各 kind 的信任框文案。herdr 对 codex 有 trust_directory 规则,对 agy 没有,
# 所以 agy 卡在信任框时会被误报成 idle→done,流水线要等到超时才发现。
# 编排器自己兜一层:认出来就当 blocked 通知人。这是补检测缺口,不是自动应答。
TRUST_PATTERNS = (
    "Do you trust the contents of this",   # codex
    "trust this folder",                   # agy
    "Is this a project you trust",         # claude
)


# 每个阶段的指令。review 写得比别的细,是因为实测过:原来只有一句
# "只评审,不要改代码。列出可执行的问题",codex 连续三跑 findings 全空
# —— 它把"能跑通"当成"没问题"。不给检查清单,评审就退化成冒烟测试。
STAGE_TASK = {
    "scan": """摸清这个仓库的现状,给后面实现的人当地图。不要改任何代码。

只回答跟目标相关的部分,别通篇罗列:
1. 目标涉及哪些文件?各自现在是什么样(行数、关键函数/类)?
2. 项目用什么测试方式?有 pytest / unittest / 裸脚本吗?看得出约定就照它。
3. 有构建/lint 配置吗(Makefile、pyproject、package.json、justfile)?
   实现完该跑哪条命令验证?
4. 现有代码的风格约定:命名、类型注解、docstring、错误处理的既有做法。
5. 目标有没有已经被实现的部分?有没有明显的坑(路径依赖、全局状态)?

findings 里每条写一个结论,越具体越好 —— 后面的人只看你这份 JSON,
看不到你的屏幕。找不到的东西就明说找不到,不要猜。""",
    "implement": "实现目标。改动真实代码。",
    "review": """只评审,不要改代码。

逐条过下面的清单,每条都要给结论。发现问题就写进 findings,
格式 "文件:行 — 问题 — 建议改法"。

1. 需求覆盖:目标里每个要求都实现了吗?有没有漏的分支或函数?
2. 测试是否真的在测:断言会因为实现错误而失败吗?
   全是恒真断言(assert True 之类)等于没测。
3. 测试用例够不够:边界值(0、负数、空、极大)、类型混用、
   浮点精度(不能用 == 比较浮点)、异常路径,各覆盖了吗?
4. 断言失败时能定位吗?裸 assert 不带消息,失败了看不出实际值。
5. 测试文件有 import 副作用吗?模块级直接跑断言,被 import 就会执行。
6. `python -O` 会把 assert 整体优化掉 —— 依赖裸 assert 的测试会静默通过。
7. 实现本身:命名、错误处理、有没有过度防御或不必要的抽象。

注意:"跑通了"不等于"没问题"。测试通过只说明第 2 条没炸,
不代表 3-6 条成立。清单里任何一条不满足,都要写进 findings。
如果确实全部满足,findings 留空并在 summary 里说清逐条都查过了。""",
    "fix": "修掉评审列出的问题。只动相关代码。",
    # 双审时替换上面那句 —— findings 来自两个独立 reviewer,会有同义重复。
    # 编排器不做程序化去重(措辞不同、行号又都指向同一处,两种键都失效),
    # 所以把"按语义合并"这件事明确交给 fix。
    "fix_double": """修掉评审列出的问题。只动相关代码。

findings 来自两个独立评审的 agent,每条前面的 [kind] 是来源。
两人可能用不同措辞提同一个问题 —— 先按"问题本身"归并,再动手改。
在 summary 里说清实际归并出几个问题、各自怎么修的。
只有一方提到的问题也要处理,不要因为另一方没提就当它不存在。""",
    "verify": """跑构建和测试,报告是否真的通过。

必须实际执行命令,不能只看代码推断。至少做到:
1. 语法/构建检查(没有构建系统就用 py_compile 之类)。
2. 跑测试,记下退出码和输出。
3. 数一下断言条数,和测试文件里实际写的对得上吗?
4. 上一阶段说修了什么,逐条确认真的改了。

任何一步失败或结果与上一阶段的说法不符,status 填 failed
并把实际输出写进 findings。不要替它圆场。""",
    "report": """把这条流水线做了什么写成一份人能读的总结。不要改代码。

前面每个阶段的产出都在 .orch/results/*.json,自己读。
写成 Markdown 存到 .orch/report.md,包含:

1. 做了什么 —— 一段话说清最终改动,别复述阶段流水。
2. 评审提了什么、怎么修的 —— 逐条对应,让人看得出问题真被解决了。
3. 验证结果 —— 跑了哪些命令、结论是什么。
4. 遗留问题 —— 没修的、绕过的、需要人决定的。没有就明说没有。

写给一个没看过这次运行的同事。不要吹,失败和妥协照实写。
写完 report.md 再按契约写 results/report.json,summary 一句话概括,
findings 放遗留问题(没有就空数组)。""",
}


class Run:
    def __init__(self, root: Path, task: str, kind=None, agent_args=None,
                 skip_perm=False):
        """kind=None 时按 STAGE_KIND 异构派活;给了具体 kind 就全阶段用它
        (调试和对照用)。

        skip_perm 是"跳过权限确认"这个意图,由 SKIP_PERM_ARGS 按 kind 翻译成
        各 CLI 自己的 flag。agent_args 是原样透传的额外参数 —— 异构模式下要
        当心,一种 CLI 的 flag 传给另一种会让它启动失败。
        """
        self.root = root
        self.dir = root / ".orch"
        self.tasks = self.dir / "tasks"
        self.results = self.dir / "results"
        self.kind = kind
        self.agent_args = agent_args or []
        self.skip_perm = skip_perm
        for d in (self.tasks, self.results):
            d.mkdir(parents=True, exist_ok=True)
        (self.tasks / "goal.md").write_text(task, encoding="utf-8")
        self.state_path = self.dir / "state.json"
        self.state = {"stage": None, "history": [], "agent": None}

    # ---------- 状态 ----------
    def save(self):
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def log(self, msg):
        print(f"[orch] {msg}", flush=True)
        self.state["history"].append({"t": time.strftime("%H:%M:%S"), "msg": msg})
        self.save()

    # ---------- kind 派活 ----------
    def stage_kind(self, stage: str) -> str:
        """本阶段用哪种 agent。self.kind 非空表示调用方强制统一,优先。"""
        return self.kind or STAGE_KIND.get(stage, "claude")

    def stage_args(self, kind: str, stage: str) -> list:
        args = list(KIND_ARGS.get(kind, []))
        args += SANDBOX_ARGS.get(kind, [])
        if self.skip_perm:
            args += SKIP_PERM_ARGS.get(kind, [])
        return args + self.agent_args

    # 屏幕上出现这些就说明 agent 真的在干活,别去打扰它。
    # 都是各家 TUI 的活动行前缀/文案,实测抓的。
    BUSY_MARKS = ("•", "└", "⎿", "▸", "●", "Working", "Explored", "Thought for",
                  "esc to interrupt", "Ran ")

    def nudge_submit(self, stage: str, name: str, pane: str, sent: str) -> bool:
        """agent 卡在"输入没提交"上?补一个 enter。

        实测(codex + MCP 启动期):agent_start 返回后 herdr 判 idle 用的是
        osc_title_idle —— 终端标题变了就算就绪,比"TUI 能受理输入"宽松。
        codex 那会儿还在启动 MCP,回车被吞掉,文本留在 › 输入行,状态 idle。
        编排器无法区分"在思考"和"输入没提交",会白等到超时。

        判据是"反向"的 —— 屏幕上没有活动迹象就补,而不是"看到 prompt 文本
        才补"。原来那个正向判据实测会漏:codex 会把输入行重绘掉,屏幕只剩
        状态栏一行,文本却仍在输入缓冲里 —— 对那个 pane 直接送 enter,状态
        立刻 idle→working。所以"看不见文本"不等于"没有待提交的文本",
        正向判据在这种情况下该补而不补,白等到超时。

        安全性靠两点,不靠"认出自己的文本":
          1. 只在 collect() 拿不到结果、且 herdr 报 idle/done 时才调用 ——
             真在干活的 agent 是 working,不会走到这里。
          2. BUSY_MARKS 兜一层:屏幕上有活动行就不碰。
        enter 本身对空输入行是无害的(codex/claude 都只是换行或忽略),
        而且调用方有次数上限。仍然不碰任何对话框 —— 审批和信任框只能人点,
        那些情况 herdr 报的是 blocked,走不到这里。
        """
        try:
            text = h.agent_read(pane, 40)
        except Exception:
            # 读不到屏幕时保守起见仍然补 —— 走到这里已经说明没结果且 idle
            text = ""
        tail = [l.strip() for l in text.splitlines() if l.strip()][-4:]
        busy = next((m for m in self.BUSY_MARKS
                     for l in tail if l.startswith(m) or m in l), None)
        if busy:
            return False
        # 信任/审批框另有 trust_blocked() 和 blocked 分支处理,这里再挡一道
        if any(p in text for p in TRUST_PATTERNS):
            return False
        self.log(f"{stage}: 疑似输入未提交(屏幕无活动迹象),补 enter")
        try:
            h.agent_send_keys(name, "enter")
            return True
        except Exception as e:
            self.log(f"补 enter 失败 {type(e).__name__}: {e}")
            return False

    def trust_blocked(self, pane: str) -> str | None:
        """屏幕上有信任框吗?返回命中的文案,没有则 None。

        herdr 对 agy 认不出信任框(检测清单缺规则),会把它兜底报成 idle。
        实测:agy 卡在框上时 agent_status 一路 idle→done,流水线要等到
        超时才发现。这里主动扫一遍,把误报的 idle 纠正成 blocked。
        """
        try:
            text, _ = h.read_raw(pane, 60)
        except Exception:
            return None
        return next((p for p in TRUST_PATTERNS if p in text), None)

    # ---------- 契约 ----------
    def brief(self, stage: str, prev: dict | None, slot: str = None) -> Path:
        """把完整指令写成文件。prompt 里只发路径 —— 长文本过 bracketed-paste
        又慢又容易被 TUI 吃掉字符。

        slot 是产出名,默认等于 stage。双审时两个 reviewer 同为 review 阶段
        (共用 STAGE_TASK 的清单)但要写不同文件,靠 slot 区分。
        """
        slot = slot or stage
        out = self.results / f"{slot}.json"
        p = self.tasks / f"{slot}.md"
        body = [f"# 阶段: {stage}\n", "## 目标\n", (self.tasks / "goal.md").read_text(encoding="utf-8"), ""]
        if prev:
            # scan 的产出是仓库现状,不是"上一阶段干了什么" —— 标题别误导
            head = "仓库现状(scan 阶段的勘查结果)" if stage == "implement" \
                else "上一阶段产出"
            body += [f"## {head}\n", "```json",
                     json.dumps(prev, ensure_ascii=False, indent=2), "```", ""]
        body += [
            "## 你要做的事\n",
            # 双审的 fix 要额外做语义归并 —— 见 STAGE_TASK["fix_double"]
            STAGE_TASK["fix_double"] if stage == "fix" and (prev or {}).get("double")
            else STAGE_TASK[stage],
            "",
            "## 输出契约(必须遵守)\n",
            f"完成后把结果写到 `{out}`,JSON 格式:",
            "```json",
            json.dumps({
                "status": "ok | failed | blocked",
                "summary": "一两句话。review/verify 说清查了什么,不要只说通过",
                "findings": ["每条一个具体问题:文件:行 — 问题 — 建议改法"],
                "files_changed": ["相对路径"],
            }, ensure_ascii=False, indent=2),
            "```",
            "写完文件后,回复里只说一句 done。不要把完整结果贴在终端 ——",
            "编排器读文件,不读你的屏幕输出。",
        ]
        p.write_text("\n".join(body), encoding="utf-8")
        return p

    def collect(self, stage: str) -> dict | None:
        """唯一的完成判据:文件存在且能解析且有 status。"""
        f = self.results / f"{stage}.json"
        if not f.exists():
            return None
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None          # 可能正写到一半,下轮再看
        return d if "status" in d else None

    # ---------- 驱动 ----------
    def cleanup(self, pane, stage):
        """关掉本阶段的 pane。skill 只禁止关"不是自己建的" —— 这个是我们
        tab_create 出来的,不收就每轮漏 4 个,几轮下来 workspace 就满了。
        失败不致命:阶段结果已经拿到了。"""
        try:
            h.call("pane", "close", pane)
        except Exception as e:
            self.log(f"{stage}: pane {pane} 未能关闭 ({type(e).__name__}),需手动清理")

    def run_stage(self, stage: str, prev: dict | None, timeout_sec=1800,
                  slot: str = None, kind: str = None) -> dict:
        """跑一个阶段。

        slot 是产出名(默认 = stage),kind 是强制指定的 agent 种类
        (默认按 STAGE_KIND 派)。双审就靠这两个参数:同一个 review 阶段、
        同一份清单,两个 kind 各写自己的 slot。
        """
        slot = slot or stage
        name = f"orch_{slot}"
        self.state["stage"] = slot
        self.state["agent"] = name
        self.save()

        brief = self.brief(stage, prev, slot)
        stale = self.results / f"{slot}.json"
        stale.unlink(missing_ok=True)   # 别把上一轮的结果当本轮的

        pane = h.tab_create(self.ws, str(self.root), f"orch:{slot}")
        self.state["pane"] = pane
        keep = False   # blocked 时置 True:pane 要留给人手动应答
        try:
            # 名字全局唯一且只在 agent 退出后才释放。上一轮崩掉/被 kill 时 agent
            # 还活着占着名字,下一轮同名就 agent_name_taken。带上 pane 后缀去重。
            # 必须小写:herdr 只收 [a-z][a-z0-9_-]{0,31},而 pane id 是 w9:pM 这种。
            name = f"{name}_{pane.replace(':', '')}".lower()
            self.state["agent"] = name
            kind = kind or self.stage_kind(stage)
            self.state["kind"] = kind
            h.agent_start(name, kind, pane, self.stage_args(kind, stage))
            self.log(f"{slot}: {kind} 起在 {pane}")

            # herdr 对 agy 认不出信任框,会报成 idle —— 先自己扫一遍,
            # 免得白等到超时才发现 agent 从没开始干活。
            hit = self.trust_blocked(pane)
            if hit:
                h.notify(f"orch: {slot} 需要确认",
                         f"{kind} 在 {pane} 等信任确认,点完重跑本阶段")
                self.log(f"{slot}: {kind} 卡在信任框(命中「{hit}」),"
                         f"herdr 报的是 {h.agent_status(name)}。去 {pane} 手动应答")
                keep = True
                return {"status": "blocked", "summary": f"{kind} 等信任确认",
                        "findings": []}

            sent = f"读 {brief} 并按其中的输出契约执行。"
            try:
                h.agent_prompt(name, sent,
                               wait=True, timeout_ms=timeout_sec * 1000)
            except h.HerdrServerError as e:
                # agent_blocked 在 prompt 阶段就可能抛 —— 比如 claude 在陌生
                # 目录里弹的信任确认框。这类框只能人来点(skill 明令不许自动
                # 回答),所以叫人,不重试。
                if e.code != "agent_blocked":
                    raise
                # 常见原因:claude 在陌生目录弹的信任框。这是刻意保留的人工闸门 ——
                # 每个 worktree 一次,点过就落盘。不自动应答。
                h.notify(f"orch: {slot} 需要确认",
                         f"去 {pane} 手动应答,然后重跑本阶段")
                self.log(f"{slot}: prompt 时 blocked。屏幕:\n{h.agent_read(pane, 40)}")
                keep = True   # 留着 pane 给人点,别在 finally 里关掉
                return {"status": "blocked", "summary": "agent 启动即 blocked,等人工确认",
                        "findings": []}

            # --wait 返回不代表做完了。独立轮询文件。
            deadline = time.time() + timeout_sec
            nudges, last_nudge = 0, time.time()
            while time.time() < deadline:
                got = self.collect(slot)
                if got:
                    self.log(f"{slot}: {got['status']} — {got.get('summary','')[:80]}")
                    return got

                st = h.agent_status(name)
                # idle/done 且拿不到结果 —— 可能回车被启动流程吃掉了。
                # done 必须一起算:实测卡在输入行时 herdr 报的正是 done
                # (它和 idle 是同一个底层状态,只差"有没有被看见过")。
                #
                # 判据在 nudge_submit 里是反向的(没有活动迹象就补),所以这里
                # 必须节流:agent 刚受理 prompt、还没打出第一行输出时也是
                # idle+无活动,那一瞬间补键是无害的但没必要。等 NUDGE_WAIT
                # 再补,给它足够时间露出活动迹象。上限 3 次,免得死循环。
                if st in ("idle", "done") and nudges < 3:
                    if time.time() - last_nudge >= NUDGE_WAIT:
                        if self.nudge_submit(stage, name, pane, sent):
                            nudges += 1
                        last_nudge = time.time()
                        time.sleep(POLL_SEC)
                        continue
                if st == "blocked":
                    # skill 明确要求不自动回答 approval 对话框。叫人。
                    # 这里是抓屏诊断,不是取产出 —— 用 agent_read 正合适。
                    h.notify(f"orch: {slot} 卡住", "需要人工确认")
                    self.log(f"{slot}: blocked,已通知。屏幕:\n{h.agent_read(pane, 40)}")
                    keep = True   # 同上:等人工确认的 pane 必须留着
                    return {"status": "blocked", "summary": "等人工确认", "findings": []}
                if st is None:
                    # agent 退出了 name 就被清空 —— 但文件可能刚写完
                    time.sleep(POLL_SEC)
                    got = self.collect(slot)
                    if got:
                        return got
                    return self.salvage(slot, name, pane, "agent 消失且无结果")
                time.sleep(POLL_SEC)

            return self.salvage(slot, name, pane, f"{slot} 超时")
        finally:
            if not keep:
                self.cleanup(pane, stage)

    def salvage(self, stage, name, pane, why) -> dict:
        """没拿到结果文件时,把 agent 实际说了什么捞出来存档。

        这是 read_or_file 的正当用途:诊断信息宁可残缺也比没有好,
        而且它会自己判断可靠性并在必要时退化到文件,再不行才交残屏。
        产出本身仍然只认 results/*.json —— 打捞不改判定,只写日志。
        """
        try:
            text, src = h.read_or_file(
                name, pane, f"阶段 {stage}", self.dir, timeout_ms=120000)
        except Exception as e:
            self.log(f"{stage}: 打捞失败 {type(e).__name__}: {e}")
            text, src = "", "none"
        if text:
            (self.dir / f"salvage-{stage}.md").write_text(text, encoding="utf-8")
            self.log(f"{stage}: 已打捞 {len(text)} 字符(来源 {src})"
                     f" -> .orch/salvage-{stage}.md")
        return {"status": "failed", "summary": why,
                "findings": [], "salvage_source": src if text else "none"}

    def advisory(self, stage: str, prev: dict | None) -> dict | None:
        """跑一个不参与成败判定的阶段。失败只记日志,不影响返回给下游的东西。

        scan/report 的 agent 是 agy —— 它的 herdr 检测清单缺信任框规则,
        比别的 kind 更容易卡住。这类阶段不值得为它中止整条流水线。
        返回它的产出(供下游参考),失败则返回 None。
        """
        got = self.run_stage(stage, prev)
        if got["status"] == "ok":
            return got
        self.log(f"{stage}: {got['status']}({got.get('summary','')[:50]}) —— "
                 f"不影响流水线,继续")
        return None

    def double_review(self, prev: dict) -> dict:
        """两个 kind 各自独立评审同一份实现,两份 findings 都交给 fix。

        独立是关键:两个 reviewer 拿同样的输入(implement 的产出),谁也看不到
        对方的结论。如果把 codex 的 findings 喂给 agy,它大概率只会附议 ——
        那就退化成一个 reviewer 加一次复读。

        不做程序化去重。实测过两种键都不成立:
          前 80 字符 —— codex 和 agy 提同一个问题时措辞不同,一条都合不掉
                       (实测 4+4 条同义意见"去重"出 8 条)。
          文件:行    —— 8 条全指向 test_calc.py:3,按位置合会把 4 个不同
                       问题压成 1 条,比不合更糟。
        语义相同而字面不同,这是模型判断,不是字符串匹配能做的事。所以两份
        原样标源送给 fix,并在 prompt 里明说可能重复、按问题去重后再改。

        合并策略仍是并集(不因为只有一方提到就丢):评审提的是"值得看一眼的
        地方",假阳性代价是 fix 多花点时间,漏报代价是问题留在代码里。

        status 取较严的那个:任一方 failed 就 failed。副审自己跑挂了
        (没 findings 的 failed)不算,那是它自己的问题,不该判实现有罪。
        """
        primary = self.run_stage("review", prev, slot="review")
        second_kind = REVIEW_SECOND_KIND
        secondary = self.run_stage("review", prev, slot="review2",
                                   kind=second_kind)

        pk = self.stage_kind("review")
        merged = [f"[{who}] {f}"
                  for who, r in ((pk, primary), (second_kind, secondary))
                  for f in (r.get("findings") or [])]

        # 副审没跑成(failed 但没 findings)只记日志,不影响判定
        if secondary["status"] != "ok" and not secondary.get("findings"):
            self.log(f"review2({second_kind}) 没跑成:"
                     f"{secondary.get('summary','')[:60]} —— 只用主审结果")
            status = primary["status"]
        else:
            status = "failed" if "failed" in (primary["status"],
                                             secondary["status"]) else "ok"

        n1 = len(primary.get("findings") or [])
        n2 = len(secondary.get("findings") or [])
        self.log(f"双审: {pk} 提 {n1} 条, {second_kind} 提 {n2} 条 "
                 f"—— 两份都给 fix,由它按语义合并")
        return {
            "status": status,
            "summary": f"[{pk}] {primary.get('summary','')} "
                       f"|| [{second_kind}] {secondary.get('summary','')}",
            "findings": merged,
            "double": True,          # 让 fix 的 prompt 知道要去重
            "files_changed": [],
        }

    def main(self, max_fix_rounds=2, with_scan=False, with_report=False,
             double=True):
        self.ws = h.call("workspace", "list")["workspaces"][0]["workspace_id"]
        self.log(f"workspace={self.ws} root={self.root}")

        # scan 默认关。实测在小仓库里是纯开销 —— e2e 那次它给的 5 条
        # (只有 add、无测试框架、无构建配置、snake_case、无全局状态)
        # implement 自己几秒就能看出来。大仓库里"目标涉及哪些文件、
        # 该跑哪条验证命令"才值得先勘查,那时用 --scan 打开。
        scan = self.advisory("scan", None) if with_scan else None

        prev = self.run_stage("implement", scan)
        if prev["status"] != "ok":
            return self.finish(prev, "implement")

        prev = self.double_review(prev) if double \
            else self.run_stage("review", prev)
        # review 的 failed 有两种含义,不能一概中止:
        #   带 findings —— "代码有问题",这正是我们要 fix 的输入,继续走。
        #   不带 findings —— 评审自己没跑成(超时/打捞/blocked),没东西可修,中止。
        # 加强 review prompt 后 codex 会如实报 failed + findings,原来那条
        # `status != ok 就 return` 会把整条修复链堵死。
        if prev["status"] != "ok" and not prev.get("findings"):
            return self.finish(prev, "review")

        # 修→验证 循环:验证不过就带着失败信息再修一轮
        for rnd in range(max_fix_rounds):
            if not prev.get("findings"):
                self.log("评审无问题,跳过修复")
            else:
                prev = self.run_stage("fix", prev)
                if prev["status"] != "ok":
                    return self.finish(prev, "fix")

            v = self.run_stage("verify", prev)
            if v["status"] == "ok" and not v.get("findings"):
                return self.finish(v, "verify", with_report)
            self.log(f"验证未过(第 {rnd+1} 轮),回到修复")
            prev = v
        return self.finish(prev, "verify", with_report)

    def finish(self, result, stage, with_report=False):
        # report 只在流水线真通过后才跑 —— 失败时该看的是日志和 salvage,
        # 不是一份漂亮的总结。它自己失败也不改判定。
        if with_report and result["status"] == "ok":
            self.advisory("report", result)
        self.state["stage"] = f"done:{stage}"
        self.save()
        ok = result["status"] == "ok"
        h.notify("orch 完成" if ok else "orch 未通过", result.get("summary", "")[:120],
                 sound="done" if ok else "request")
        print(json.dumps({"final_stage": stage, **result}, ensure_ascii=False, indent=2))
        return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: orch.py <repo-root> <任务描述> [--kind KIND] [--skip-perm] "
              "[--scan] [--report] [--no-double] [-- <agent-args>]")
        print(f"  阶段: {' → '.join(STAGES)}  (review 默认双审)")
        print(f"  不给 --kind 则按阶段派活: {STAGE_KIND}")
        print("  给了 --kind 则全阶段统一用它(调试/对照用)")
        print("  --skip-perm 按 kind 翻译成各 CLI 的跳过权限 flag;"
              "异构模式下别用 -- 透传这类 flag")
        print(f"  --scan 打开勘查阶段(默认关,小仓库里是纯开销)")
        print(f"  --report 打开总结阶段(默认关,写 .orch/report.md)"
              f" —— {'/'.join(sorted(ADVISORY_STAGES))} 均不参与成败判定")
        print(f"  --no-double 关掉 review 双审"
              f"(默认 {STAGE_KIND['review']} + {REVIEW_SECOND_KIND} 各审一遍)")
        sys.exit(2)
    argv = sys.argv[1:]
    kind, extra = None, []   # None = 按 STAGE_KIND 异构
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    if "--kind" in argv:
        i = argv.index("--kind")
        kind = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    flags = {f: f in argv for f in
             ("--skip-perm", "--scan", "--report", "--no-double")}
    argv = [a for a in argv if a not in flags]
    sys.exit(Run(Path(argv[0]).resolve(), argv[1], kind, extra,
                 flags["--skip-perm"]).main(
                     with_scan=flags["--scan"],
                     with_report=flags["--report"],
                     double=not flags["--no-double"]))
