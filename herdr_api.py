"""herdr CLI 的薄封装。只做三件事:调用、解析、区分错误。

字段路径全部来自 `herdr api schema`,不是猜的:
  worktree create -> .result.workspace / .result.tab / .result.root_pane / .result.worktree
  tab create      -> .result.tab / .result.root_pane
  agent start     -> .result.agent
  pane split      -> .result.pane
  wait-output --raw -> .result.read (PaneReadResult, 含 truncated)

读取分两层,别混:
  read_raw / read_or_file  取产出。能拿到 truncated,不可靠时退化到文件。
  agent_read               抓屏给人看。诊断 blocked 用。

实测于 herdr 0.8.2 (client+server, protocol 20)。
"""
import json
import subprocess
import time
from pathlib import Path


class HerdrUsageError(Exception):
    """exit 2 = 我们自己的 CLI 用法 bug。永远不该重试。"""


class HerdrServerError(Exception):
    """exit 1 = server 侧 JSON 错误。code 决定能否重试。"""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# 这些 code 是瞬时的,重试有意义
RETRYABLE = {"agent_prompt_stalled", "timeout", "agent_not_ready"}


def _run(args, timeout):
    p = subprocess.run(
        ["herdr", *[str(a) for a in args]],
        capture_output=True, text=True, timeout=timeout,
    )
    if p.returncode == 2:
        raise HerdrUsageError(f"herdr {' '.join(map(str, args))}\n{p.stdout}{p.stderr}")
    if p.returncode != 0:
        # server 错误是 stderr 上的 JSON
        try:
            err = json.loads(p.stderr or p.stdout)["error"]
        except Exception:
            raise HerdrServerError("unparseable", (p.stderr or p.stdout).strip()[:500])
        raise HerdrServerError(err.get("code", "unknown"), err.get("message", ""))
    return p.stdout


def call(*args, timeout=180):
    """跑一条返回 JSON 的 herdr 命令,取 .result。"""
    out = _run(args, timeout)
    return json.loads(out)["result"] if out.strip() else {}


def call_text(*args, timeout=60):
    """给只打印纯文本的命令用。`agent read` / `pane read` 没有 --json,
    塞进 call() 会 JSONDecodeError。"""
    return _run(args, timeout)


def call_retry(*args, tries=3, backoff=2.0, **kw):
    """只重试 RETRYABLE 的 code。用法错误立刻抛。"""
    last = None
    for i in range(tries):
        try:
            return call(*args, **kw)
        except HerdrServerError as e:
            if e.code not in RETRYABLE:
                raise
            last = e
            time.sleep(backoff * (i + 1))
    raise last


# ---------- 拓扑 ----------

def worktree_create(branch, base=None, cwd=None, label=None):
    """建 git worktree + 开一个 workspace。返回 (workspace_id, root_pane_id, path)。"""
    a = ["worktree", "create", "--branch", branch, "--no-focus"]
    if base:
        a += ["--base", base]
    if cwd:
        a += ["--cwd", cwd]
    if label:
        a += ["--label", label]
    r = call(*a)
    return (r["workspace"]["workspace_id"],
            r["root_pane"]["pane_id"],
            r["worktree"]["path"])


def tab_create(workspace_id, cwd, label):
    """新 tab。并行 worker 用这个,不要反复 split。返回 root pane id。"""
    r = call("tab", "create", "--workspace", workspace_id,
             "--cwd", cwd, "--label", label, "--no-focus")
    return r["root_pane"]["pane_id"]


# ---------- agent ----------

def agent_start(name, kind, pane_id, extra_args=None, timeout_ms=60000):
    a = ["agent", "start", name, "--kind", kind,
         "--pane", pane_id, "--timeout", timeout_ms]
    if extra_args:
        a += ["--", *extra_args]
    # 新建的 pane 里 shell 可能还没到提示符,herdr 就报 agent_pane_busy。
    # 这是竞态,不是永久状态 —— 退避重试。pane 里真跑着东西时会耗完重试次数
    # 后抛出,不会静默吞掉。
    for i in range(5):
        try:
            call(*a, timeout=timeout_ms / 1000 + 30)
            break
        except HerdrServerError as e:
            # agent_not_ready 表示启动时就 blocked,但 name 仍可用 — 不当致命错
            if e.code == "agent_not_ready":
                break
            if e.code != "agent_pane_busy" or i == 4:
                raise
            time.sleep(1.0 * (i + 1))
    return name


def agent_get(name):
    return call("agent", "get", name)["agent"]


def agent_status(name):
    """返回 idle/working/blocked/done/unknown,agent 消失则 None。"""
    try:
        return agent_get(name)["agent_status"]
    except HerdrServerError as e:
        if e.code == "agent_not_found":
            return None
        raise


def agent_prompt(name, text, wait=True, timeout_ms=900000):
    a = ["agent", "prompt", name, text]
    if wait:
        a += ["--wait", "--timeout", timeout_ms]
    return call_retry(*a, timeout=timeout_ms / 1000 + 60)


def agent_send_keys(name, *keys):
    """送逻辑键。herdr 会先校验全部键名再写字节,写坏不了。

    编排器只用它补一个 enter —— 提交我们自己刚送进去的 prompt。
    不要拿它去回答审批/信任对话框:那些必须人来点。
    """
    return call("agent", "send-keys", name, *keys)


def agent_read(name, lines=200):
    """纯文本抓屏,给人看的。拿不到 truncated 标记 —— CLI 的 read 不吐 JSON。
    要判断完整性用 read_raw()。"""
    return call_text("agent", "read", name,
                     "--source", "recent-unwrapped", "--lines", lines)


def read_raw(pane_id, lines=2000, timeout_ms=4000):
    """唯一能拿到 truncated 标记的读法。

    schema 里 PaneReadResult.truncated 是 required 字段,但 `agent read` /
    `pane read` 只打印纯文本,没有 --json/--raw。只有 wait-output --raw 会
    吐完整的 PaneReadResult(在 .result.read 下)。--regex "." 匹配任意非空行,
    等价于"立刻读一次" —— wait-output 会先搜当前快照,已存在的输出直接命中。

    返回 (text, truncated)。空白屏幕匹配不到,按 timeout 处理成 ("", True):
    读不到东西时不能谎报"完整"。
    """
    try:
        r = call("pane", "wait-output", pane_id, "--regex", ".",
                 "--source", "recent-unwrapped", "--lines", lines,
                 "--timeout", timeout_ms, "--raw",
                 timeout=timeout_ms / 1000 + 15)
    except HerdrServerError as e:
        if e.code == "timeout":
            return "", True
        raise
    d = r.get("read", {})
    return d.get("text", ""), d.get("truncated", True)


def read_depth(pane_id):
    """(viewport_rows, max_offset_from_bottom)。

    max_offset 是当前可回溯的历史深度。0.8.0 起 herdr 会在 idle 的
    alternate-screen agent 上自动滚轮收割历史,所以这个值会随收割变化 ——
    working 中的 pane 通常是 0。用它判断"还值不值得加大 lines"。
    """
    sc = call("pane", "current", "--pane", pane_id)["pane"]["scroll"]
    return sc["viewport_rows"], sc["max_offset_from_bottom"]


def agent_wait(name, until=("idle", "done"), timeout_ms=900000):
    a = ["agent", "wait", name]
    for u in until:
        a += ["--until", u]
    a += ["--timeout", timeout_ms]
    return call(*a, timeout=timeout_ms / 1000 + 30)


def read_or_file(name, pane_id, ask, tmpdir, lines=2000, timeout_ms=900000):
    """读 agent 的产出。先直读,不可靠才退化到文件。

    顺序照 skill 的要求来 —— 文件 fallback 不进初始 prompt,只在第一次读
    不可靠时才追加一句。这样常规情况省一次往返。

    1. 等 idle/done。working 中的 pane 历史深度是 0,读了也是残的。
    2. read_raw 直读,拿 truncated。
    3. truncated 或 空 才判定不可靠。
    4. 追问一句"写文件只回路径",然后直读那个文件。

    返回 (text, source),source ∈ {"screen", "file"},方便调用方记账。
    """
    agent_wait(name, timeout_ms=timeout_ms)
    text, truncated = read_raw(pane_id, lines)
    if text.strip() and not truncated:
        return text, "screen"

    out = Path(tmpdir) / f"{name}.md"
    agent_prompt(
        name,
        f"把你刚才关于「{ask}」的完整回复原样写入 {out},"
        f"然后只回复这个路径,不要重复内容。",
        wait=True, timeout_ms=timeout_ms,
    )
    if out.exists():
        return out.read_text(encoding="utf-8"), "file"
    # 文件也没有 —— 把残缺的屏幕内容交出去,但标明来源
    return text, "screen-truncated"


def notify(title, body="", sound="request"):
    call("notification", "show", title, "--body", body, "--sound", sound)
