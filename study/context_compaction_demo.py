"""
这个文件是 codex「上下文窗口 + 按需取回」式压缩机制的最小可运行模拟。

它不连接任何网络、不调用任何模型，只用标准库把下面这条链路完整跑一遍：

    1. 会话开始，拿到第 1 个上下文窗口的编号（window id）
    2. 每一轮请求都告诉服务端"请把这次请求的内容收录进历史索引"
    3. 窗口快满时（剩余 <= 提醒阈值），客户端往上下文里塞一条提醒，告诉模型"赶紧写笔记"
    4. 模型写笔记（笔记本身也是存在服务端的，能跨窗口存活）
    5. 主额度刚好耗尽时（剩余 == 0），再注入一条「兜底提示」：别干活了，只写一次笔记
    6. 但换窗口不会马上发生：要等已用量越过「主额度 + 缓冲额度」，
       这段缓冲就是留给模型把笔记真正写完的时间
    7. 窗口换新（历史被清空，只重新注入初始上下文 + 窗口编号）
    8. 模型从新的窗口里，靠 window id + item id 把旧窗口的原文按需取回

对照的 Rust 代码位置（都能在仓库里找到）：
    - 换新窗口        codex-rs/core/src/session/mod.rs::start_new_context_window
    - 窗口编号轮转    codex-rs/core/src/state/auto_compact_window.rs::advance
    - 阈值与提醒      codex-rs/core/src/session/token_budget.rs::maybe_record
    - 换窗口的判定    codex-rs/core/src/session/context_window.rs::context_window_token_status_with_config
    - 窗口编号注入    codex-rs/core/src/context/token_budget_context.rs
    - history 工具    codex-rs/ext/history-notes/src/tools.rs
    - 各家后端接口    codex-rs/ext/history-notes/src/backend.rs
"""

import uuid
from dataclasses import dataclass, field


# =============================================================================
# 第一层：服务端的「历史仓库」—— 对应 alpha/history/v2/*
# =============================================================================
class HistoryStore:
    """服务端保存的"标准化历史"。

    它就是一个大字典，用 (窗口编号, 条目编号) 这两个 key 定位一条原文：

        {
          ("0199c1f0-...-a1", "itm_7"): {          # (window_id, item_id) 两个 key
              "window_id": "0199c1f0-...-a1",
              "item_id":   "itm_7",
              "agent":     "/root",
              "role":      "user",
              "content":   "把 summary_prefix 这段文案翻译成中文",
          },
          ...
        }

    模型拿到的两个编号就是查这张表的「取件码」。
    """

    def __init__(self) -> None:
        # (window_id, item_id) -> 一条记录的字典；就是上面注释里的长相
        self._items: dict[tuple[str, str], dict] = {}
        # window_id -> 该窗口里所有 item_id 的顺序清单，例如 ["itm_1", "itm_2"]
        self._windows: dict[str, list[str]] = {}

    # --- 收录：模拟服务端在收到 history_ingest_requested=true 后做的事 ---
    def ingest(self, window_id: str, agent: str, items: list[dict]) -> None:
        """把一次请求携带的内容收录进来。

        items 的实际长相（列表里每个元素是一个字典）：
            [
              {"item_id": "itm_1", "role": "user",      "content": "你好"},
              {"item_id": "itm_2", "role": "assistant", "content": "你好，有什么可以帮你"},
            ]
        """
        self._windows.setdefault(window_id, [])
        for it in items:
            key = (window_id, it["item_id"])
            self._items[key] = {
                "window_id": window_id,
                "item_id": it["item_id"],
                "agent": agent,
                "role": it["role"],
                "content": it["content"],
            }
            if it["item_id"] not in self._windows[window_id]:
                self._windows[window_id].append(it["item_id"])

    # --- history.list_windows ---
    def list_windows(self) -> list[dict]:
        """列出所有窗口。

        返回值的实际长相（列表套字典）：
            [{"window_id": "0199c1f0-...-a1", "item_count": 14},
             {"window_id": "0199c1f0-...-b2", "item_count": 3}]
        """
        return [
            {"window_id": wid, "item_count": len(itm_ids)}
            for wid, itm_ids in sorted(self._windows.items())
        ]

    # --- history.list_items ---
    def list_items(self, window_id: str, max_chars_per_item: int = 120) -> list[dict]:
        """列出某个窗口里的全部条目，但每条只给开头一小段，避免一次撑爆上下文。

        返回值的实际长相：
            [{"item_id": "itm_1", "role": "user", "preview": "你好"},
             {"item_id": "itm_2", "role": "assistant", "preview": "你好，有什么可以帮你"}]
        """
        ids = self._windows.get(window_id)
        if ids is None:
            # 未知窗口 → 返回空列表（对应 Rust 文档里的 "no matches for an unknown window"）
            return []
        out = []
        for iid in ids:
            rec = self._items[(window_id, iid)]
            out.append(
                {
                    "item_id": iid,
                    "role": rec["role"],
                    "preview": rec["content"][:max_chars_per_item],
                }
            )
        return out

    # --- history.read_item ---
    def read_item(
        self, window_id: str, item_id: str, offset_chars: int = 0, limit_chars: int = 10_000
    ) -> dict:
        """按两个编号精确读回原文，还可以只读其中一段（分页）。

        返回值的实际长相（读到了）：
            {"item_id": "itm_7", "window_id": "0199c1f0-...-a1",
             "content": "把 summary_prefix 这段文案翻译成中文",
             "truncated": False}
        读不到时：
            {"item_id": "itm_7", "window_id": "...", "content": None, "truncated": False}
        """
        rec = self._items.get((window_id, item_id))
        if rec is None:
            return {"item_id": item_id, "window_id": window_id, "content": None, "truncated": False}
        text = rec["content"]
        chunk = text[offset_chars : offset_chars + limit_chars]
        return {
            "item_id": item_id,
            "window_id": window_id,
            "content": chunk,
            "truncated": offset_chars + limit_chars < len(text),
        }

    # --- history.search_contents ---
    def search_contents(self, query: str) -> list[dict]:
        """在全部历史里做字面子串搜索，用来在「不知道编号」时先定位。

        返回值的实际长相：
            [{"window_id": "0199c1f0-...-a1", "item_id": "itm_7",
              "snippet": "把 summary_prefix 这段文案翻译成中文"}]
        """
        return [
            {"window_id": wid, "item_id": iid, "snippet": rec["content"][:80]}
            for (wid, iid), rec in self._items.items()
            if query in rec["content"]
        ]


# =============================================================================
# 第二层：服务端的「笔记本」—— 对应 alpha/notes/v2/*
# =============================================================================
class NotesStore:
    """模型自己写的笔记。路径是虚拟路径，不是磁盘路径。

    内部就是「路径 -> 文本」的字典：

        {"/root/notes/progress.md": "目标：改注释\\n进度：已完成 3 个文件\\n下一步：..."}
    """

    # 仓库里文档化的硬上限（tools.rs:28 的 NOTES_DESCRIPTION）：
    # "Every file must remain at or below 1,000,000 UTF-8 bytes"
    MAX_BYTES_PER_FILE = 1_000_000

    def __init__(self) -> None:
        self._files: dict[str, str] = {}

    @staticmethod
    def _truncate_to_bytes(text: str, max_bytes: int) -> str:
        """按 UTF-8 字节数截断，而不是按字符数。

        为什么不能直接写 text[:max_bytes]：中文一个字占 3 个字节，
        按「字符」切的话 100 万字符会变成 300 万字节，远超真实上限。
        真实限制是按字节算的，所以这里先编码、按字节切、再解回字符串
        （切到半个汉字时用 errors="ignore" 丢掉残字节）。
        """
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        return encoded[:max_bytes].decode("utf-8", errors="ignore")

    def write_file(self, path: str, text: str) -> None:
        """整篇覆盖写。"""
        self._files[path] = self._truncate_to_bytes(text, self.MAX_BYTES_PER_FILE)

    def append_to_file(self, path: str, text: str) -> None:
        """追加写 —— 模型最常用的就是这一种。"""
        old = self._files.get(path, "")
        self._files[path] = self._truncate_to_bytes(old + text, self.MAX_BYTES_PER_FILE)

    def read_file(self, path: str) -> str:
        return self._files.get(path, "")

    def list_files_by_prefix(self, prefix: str = "") -> list[dict]:
        """返回值的实际长相：
        [{"path": "/root/notes/progress.md", "bytes": 96}]
        """
        return [
            {"path": p, "bytes": len(t.encode("utf-8"))}
            for p, t in sorted(self._files.items())
            if p.startswith(prefix)
        ]


# =============================================================================
# 第三层：客户端会话 —— 窗口编号、token 记账、提醒/兜底、换窗口判定
# =============================================================================
@dataclass
class Item:
    """上下文里的一条内容。注意每条都带一个 item_id，这就是"取件码"的一半。"""

    item_id: str
    role: str  # "user" / "assistant" / "developer" / "tool"
    content: str


def approx_tokens(text: str) -> int:
    """粗估一段文本会占多少 token。

    仓库里真实的估算是 `approx_tokens_from_byte_count`
    （`codex-rs/utils/string/src/truncate.rs:80-84`），规则是
    「按 UTF-8 字节数算、向上取整、约 4 个字节 1 个 token」：

        (字节数 + 3) // 4

    这里照抄这条规则，**不是**按字符数算 —— 中文一个字占 3 个字节，
    两种算法能差出 3 倍。简化之处在于：真实实现算的是「模型可见字节数」
    （不含传输用的 id、元数据、外层 JSON 转义），这里直接算内容本身的字节数。

    另外，真实实现的注释里自己写明这只是 "coarse lower bound"
    （粗糙下界）：它倾向于少算，不是精确分词。
    """
    return (len(text.encode("utf-8")) + 3) // 4


@dataclass
class Session:
    """一个会话（thread）在「上下文窗口」这套机制下的全部状态。"""

    # --- 硬性预算，来自模型信息或 config.toml ---
    window_token_limit: int = 300  # 主额度：这个窗口最多能装多少 token
    reminder_threshold: int = 60  # 剩余 token 少于这个数就提醒模型写笔记
    # 缓冲额度：主额度用光之后，再宽限这么多 token 才强制换窗口。
    # 注意它**不是**兜底提示的触发条件 —— 兜底提示在「剩余 == 0」时就发了。
    fallback_buffer: int = 40

    # --- 运行时状态 ---
    window_number: int = 1
    items: list[Item] = field(default_factory=list)

    # --- 三个窗口编号。注意：真实实现里是 UUIDv7（按时间有序的 UUID） ---
    first_window_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    window_id: str = ""
    previous_window_id: str | None = None

    # --- 「每个窗口只提醒一次」的两个开关，换窗口时会被复位 ---
    reminder_sent: bool = False
    fallback_sent: bool = False

    # --- 依赖的外部系统 ---
    history: HistoryStore = field(default_factory=HistoryStore)
    notes: NotesStore = field(default_factory=NotesStore)
    agent_name: str = "/root"

    # --- 记账用 ---
    _item_seq: int = 0

    def __post_init__(self) -> None:
        if not self.window_id:
            self.window_id = self.first_window_id

    def next_item_id(self) -> str:
        """发一个新的条目编号。

        这就是 guidance 文案里说的那个出现在内容后面的 `[id: ...]`，
        也是 history.read_item 要求的两个「取件码」之一。
        """
        self._item_seq += 1
        return f"itm_{self._item_seq}"

    # -------------------------------------------------------------------------
    # 记账
    # -------------------------------------------------------------------------
    def used_tokens(self) -> int:
        """当前窗口已经吃掉了多少 token。

        真实实现是「服务端最近一次返回的 usage + 本地对之后新增内容的估算」两段相加，
        这里简化成对全部内容求和。
        """
        return sum(approx_tokens(i.content) for i in self.items)

    def remaining_tokens(self) -> int:
        """还剩多少 token 可用。

        真实实现叫 `tokens_remaining`（`codex-rs/core/src/session/context_window.rs:21-23`），
        写法是 `limit.saturating_sub(used).max(0)` —— 也就是**被钳制在 0 以上**，
        永远不会出现负数。所以「剩余 -16384」这种说法是不存在的，
        主额度用光的标志就是「剩余恰好等于 0」。
        """
        return max(0, self.window_token_limit - self.used_tokens())

    def token_limit_reached(self) -> bool:
        """是否已经到了「必须换窗口」的地步（对应真实代码里的 token_limit_reached）。

        真实判定在 `codex-rs/core/src/session/context_window.rs:101-109`：

            buffered_auto_compact_limit = 主额度 + auto_compact_fallback_buffer_tokens
            token_limit_reached = 已用量 >= buffered_auto_compact_limit
                                 || 已用量 >= 模型完整上下文窗口（这个硬上限不吃缓冲）

        这里的 window_token_limit 扮演「主额度」，所以换窗口线是
        `window_token_limit + fallback_buffer`。

        关键点：**换窗口线比「剩余归零」晚 fallback_buffer 那么多**。
        中间这一段（剩余已经是 0，但还没到换窗口线）就是留给模型
        「把兜底提示要求的最后一份笔记真正写完」的空间。
        """
        return self.used_tokens() >= self.window_token_limit + self.fallback_buffer

    # -------------------------------------------------------------------------
    # 窗口编号注入 —— 对应 TokenBudgetContext
    # -------------------------------------------------------------------------
    def context_window_fragment(self) -> str:
        """渲染成一条 developer 消息。真实实现渲染出的文本就是下面这个格式：

            <context_window>
            Agent name: /root
            First context window id: 0199c1f0-...
            Current context window id: 0199c1f0-...
            Previous context window id: 0199c1f0-...
            </context_window>

        注意 Previous 只在「换过窗口」之后才出现 —— 模型看到它就知道
        "我被换过窗口了，旧内容要用 history 工具去取"。
        """
        lines = [
            "<context_window>",
            f"Agent name: {self.agent_name}",
            f"First context window id: {self.first_window_id}",
            f"Current context window id: {self.window_id}",
        ]
        if self.previous_window_id is not None:
            lines.append(f"Previous context window id: {self.previous_window_id}")
        lines.append("</context_window>")
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # 发出一次请求
    # -------------------------------------------------------------------------
    def send_request(self, new_items: list[tuple[str, str]]) -> list[dict]:
        """模拟「把当前上下文发给模型」这一次调用。

        参数 new_items 的实际长相（列表套二元组，二元组是 (角色, 内容)）：
            [("user", "帮我改一下注释"), ("tool", "已修改 3 个文件")]

        返回：这次请求实际发出去的条目列表（就是模型的「视野」）。
        """
        for role, content in new_items:
            self.items.append(Item(self.next_item_id(), role, content))

        # 关键动作：如果服务端收录开关打开，就把这次请求的内容全部上报收录。
        # 真实实现是请求元数据里的 history_ingest_requested: true，
        # 服务端据此把内容按 window_id 建索引，之后 history 工具才读得到。
        self.history.ingest(
            self.window_id,
            self.agent_name,
            [{"item_id": i.item_id, "role": i.role, "content": i.content} for i in self.items],
        )

        # 模型的视野 = 当前窗口里的全部条目
        return [{"item_id": i.item_id, "role": i.role, "content": i.content} for i in self.items]

    # -------------------------------------------------------------------------
    # 阈值判定 + 提醒注入 —— 对应 token_budget::maybe_record
    # -------------------------------------------------------------------------
    def maybe_remind(self) -> str | None:
        """每次采样（发一次模型请求）结束后调用一次。

        返回被塞进上下文的提醒文本；没有需要提醒的事就返回 None。

        真实实现是 `maybe_record`（`codex-rs/core/src/session/token_budget.rs:161-224`），
        两段判断，语义分别是：

          1. 剩余 <= reminder_threshold 且本窗口还没提醒过  → 注入「赶紧写笔记」提醒；
          2. 剩余 == 0 且本窗口还没注入过兜底提示，并且**这一轮还没到强制换窗口**
             → 注入「别干活了，只写一次笔记」兜底提示。
             条件是那句 `if !allow_auto_compact_fallback || base_window_tokens_remaining != 0`
             （同文件 `:201`）——「剩余为 0」是硬条件，跟缓冲额度无关。
        """
        remaining = self.remaining_tokens()

        # --- 第 1 步：快到阈值了，提醒它提前写笔记 ---
        if remaining <= self.reminder_threshold and not self.reminder_sent:
            self.reminder_sent = True
            return (
                "<context_window_reminder>\n"
                f"你的上下文窗口快满了（只剩 {remaining} tokens）。"
                "在开新窗口之前，先用 notes 工具写下：目标、已做的决定、进度、下一步，"
                "以及你正在处理的每个用户请求对应的 window id 和 item id。"
                "新窗口不会自动带上当前对话。写完调用 new_context。\n"
                "</context_window_reminder>"
            )

        # --- 第 2 步：主额度刚好用光（剩余 == 0），最后一次机会，只许写笔记 ---
        # 注意这里多一个「还没到强制换窗口」的条件，和真实实现的
        # allow_auto_compact_fallback 一致：真要换窗口的那一步不会再发兜底提示。
        if remaining == 0 and not self.fallback_sent and not self.token_limit_reached():
            self.fallback_sent = True
            return (
                "<context_window_reminder>\n"
                "当前窗口已经用尽。不要继续任务、不要给最终答复。"
                "现在只做一件事：往 notes 里写一次检查点，然后调用 new_context。\n"
                "</context_window_reminder>"
            )

        return None

    # -------------------------------------------------------------------------
    # 换新窗口 —— 对应 start_new_context_window
    # -------------------------------------------------------------------------
    def start_new_context_window(self, reuse_prefix: str) -> None:
        """把整个窗口里的一切丢掉，只重新注入「初始上下文」。

        参数 reuse_prefix 是每次换窗口都要重新给模型的那部分内容（系统指令、环境信息等）。
        真实实现里这些片段由 build_initial_context_with_world_state 重新生成。

        这一步**不调用任何模型**、**不生成任何摘要** —— 这正是它和「摘要式压缩」
        最本质的区别：摘要式压缩要花一次模型调用换来一段概括文本，这里连这段文本都没有。
        """
        self.window_number += 1
        # previous 记住「刚刚被丢掉的窗口」，first 永远不动
        self.previous_window_id = self.window_id
        self.window_id = str(uuid.uuid4())
        # 两个「只发一次」的开关复位，新窗口会重新有机会收到提醒
        self.reminder_sent = False
        self.fallback_sent = False

        # 历史清空，只剩初始上下文 + 一条窗口编号消息
        self.items = [
            Item(self.next_item_id(), "developer", reuse_prefix),
            Item(self.next_item_id(), "developer", self.context_window_fragment()),
        ]

    # -------------------------------------------------------------------------
    # 模型侧：调用 notes / history
    # -------------------------------------------------------------------------
    def call_notes_append(self, path: str, text: str) -> str:
        self.notes.append_to_file(path, text)
        return f"appended {len(text.encode('utf-8'))} bytes to {path}"

    def call_history_read_item(self, window_id: str, item_id: str) -> dict:
        return self.history.read_item(window_id, item_id)

    def call_history_search(self, query: str) -> list[dict]:
        return self.history.search_contents(query)


# =============================================================================
# 演示：让两个"窗口"之间的故事完整跑一遍
# =============================================================================
def main() -> None:
    def title(text: str) -> None:
        print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")

    session = Session(
        window_token_limit=300,
        reminder_threshold=60,
        fallback_buffer=40,
    )
    # 每次换窗口都要重新注入的固定内容，这里用一段占位文本代表
    reuse_prefix = "<PERMISSIONS_INSTRUCTIONS> 允许读取工作区、允许运行测试"

    # -------------------------------------------------------------------------
    title("第 1 步：第 1 个窗口诞生")
    session.items.append(Item("itm_0", "developer", reuse_prefix))
    print("当前窗口编号 :", session.window_id)
    print("这个窗口是第 :", session.window_number, "个")
    print("此时注入的窗口消息长这样：")
    print(session.context_window_fragment())

    # -------------------------------------------------------------------------
    title("第 2 步：正常干活，历史不断增长，每次请求都被服务端收录")
    turns = [
        ("user", "帮我把 summary_prefix 这段文案翻译成中文，另外梳理一下上下文压缩的触发点都有哪些。"),
        ("assistant", "好的，我先看代码，先定位 compact.rs 和 session/turn.rs 里跟压缩有关的函数。"),
        ("tool", "已读取 codex-rs/core/src/compact.rs，共 828 行。关键函数：run_compact_task、"
                 "run_inline_auto_compact_task、build_compacted_history。已读取 "
                 "codex-rs/core/src/session/turn.rs，找到 run_pre_sampling_compact、"
                 "run_auto_compact、run_sampling_request 三个关键函数。"),
        ("user", "顺便把三种压缩策略的区别整理成一张表。"),
        ("assistant", "三种策略分别是本地摘要压缩、远程 v2 压缩、以及 token budget 换窗口。"
                      "前两种都会生成摘要，第三种完全不生成摘要，直接开新窗口。"),
        ("tool", "已生成对比表草稿，共 5 列 8 行，列分别是：策略名、选路条件、是否调用模型、"
                 "替换历史里保留什么、是否丢失原始上下文。"),
    ]
    for role, content in turns:
        session.send_request([(role, content)])
        print(f"  + 加入一条 {role:<9} 后，已用 {session.used_tokens():>4} token，"
              f"剩余 {session.remaining_tokens():>4}")
    print("\n服务端历史索引里现在有：", session.history.list_windows())

    # -------------------------------------------------------------------------
    title("第 3 步：剩余 token 掉到阈值以下，客户端注入「赶紧写笔记」提醒")
    print(f"已用 {session.used_tokens()}，剩余 {session.remaining_tokens()}，"
          f"提醒阈值 {session.reminder_threshold} → 该提醒了")
    reminder = session.maybe_remind()
    print("\n注入了提醒：")
    print(reminder)
    print("\n再问一次会不会又提醒一遍？答案是", session.maybe_remind())
    print("（每个窗口只提醒一次，靠 reminder_sent 这个开关挡住；换窗口时才会复位）")

    # -------------------------------------------------------------------------
    title("第 4 步：模型收到提醒后，一边继续干活，一边把关键信息写进笔记")
    for role, content in [
        ("assistant", "快满了，我先写笔记再继续。"),
        ("user", "那你先把触发场景也列一下。"),
        ("assistant", "好，我逐个核对后写进笔记。"),
    ]:
        session.send_request([(role, content)])
        print(f"  + 加入一条 {role:<9} 后，已用 {session.used_tokens():>4} token，"
              f"剩余 {session.remaining_tokens():>4}")

    print("\n模型往笔记里追加内容（笔记存在服务端，能跨窗口存活）：")
    print(session.call_notes_append(
        "/root/notes/progress.md",
        "目标：翻译 summary_prefix 文案 + 整理压缩触发点 + 出对比表\n"
        f"当前窗口编号：{session.window_id}\n"
        "待办用户请求：[id: itm_1] 翻译并梳理触发点；[id: itm_4] 出三种策略对比表\n"
        "已完成：compact.rs / turn.rs 已读，共 828 行；三种策略已定位；对比表草稿 5 列 8 行\n"
        "触发场景已核对：回合前超限、回合中途超限、兼容性哈希变化、模型降级、手动 /compact\n"
        "下一步：把草稿补完，尤其是「替换历史里保留什么」这一列\n",
    ))
    print("\n当次笔记的完整内容：")
    print(session.notes.read_file("/root/notes/progress.md"))

    # -------------------------------------------------------------------------
    title("第 4.5 步：主额度刚好耗尽 —— 兜底提示触发，但换窗口还没到")
    session.send_request(
        [("assistant", "触发点我核对完了：回合前超限、回合中途超限、换模型、手动 /compact 这四类。")]
    )
    print(f"  + 又干了一步后，已用 {session.used_tokens()} token，"
          f"剩余 {session.remaining_tokens()}")
    print(f"剩余 == 0（主额度 {session.window_token_limit} 刚好用光）→ 注入兜底提示"
          f"（兜底 = 最后一道保险，只许写笔记、不许再干活）：")
    print(session.maybe_remind())
    print(f"\n但这时**还不换窗口**：token_limit_reached = {session.token_limit_reached()}，"
          f"因为已用 {session.used_tokens()} < 主额度 {session.window_token_limit}"
          f" + 缓冲 {session.fallback_buffer} = "
          f"{session.window_token_limit + session.fallback_buffer}")
    print("（这段缓冲就是留给模型把兜底提示要求的最后一份笔记写完的空间）")

    # -------------------------------------------------------------------------
    title("第 5 步：缓冲也用完 —— 客户端判定必须换窗口，历史被清空，没有摘要")
    for role, content in [
        ("tool", "笔记已追加：/root/notes/progress.md，现在共 "
                 f"{len(session.notes.read_file('/root/notes/progress.md').encode('utf-8'))} 字节。"),
        ("assistant", "最后一轮笔记写完，现在调用 new_context 换到新窗口，旧窗口的原文靠取件码回去取。"),
    ]:
        session.send_request([(role, content)])
        print(f"  + 加入一条 {role:<9} 后，已用 {session.used_tokens():>4} token，"
              f"剩余 {session.remaining_tokens():>4}")
    print(f"\n已用 {session.used_tokens()} >= 主额度 {session.window_token_limit}"
          f" + 缓冲 {session.fallback_buffer} → token_limit_reached = "
          f"{session.token_limit_reached()} → 强制换窗口")
    old_window_id = session.window_id
    old_items_count = len(session.items)
    print(f"模型也在这时调用了 new_context，工具返回："
          f"A new context window will start without summarizing conversation history.")
    session.start_new_context_window(reuse_prefix)
    print(f"\n旧窗口 {old_window_id} 里的 {old_items_count} 条内容已被丢弃")
    print(f"新窗口编号：{session.window_id}")
    print("\n新窗口里此刻的全部内容（就是模型的全部视野）：")
    for i, item in enumerate(session.items):
        print(f"  [{i}] {item.role:<10} | {item.content[:70].replace(chr(10), ' / ')}")

    # -------------------------------------------------------------------------
    title("第 6 步：模型看到 Previous 编号，意识到窗口换过了，按需取回细节")
    print("新窗口里注入的消息长这样：")
    print(session.context_window_fragment())
    print()
    print("模型先列出旧窗口里都有什么：")
    for row in session.history.list_items(old_window_id):
        print("   ", row)
    print("\n模型再按 item_id 精确读回它需要的那一条原文（笔记里记的是 itm_3）：")
    recalled = session.call_history_read_item(old_window_id, "itm_3")
    print("   ", recalled)

    # -------------------------------------------------------------------------
    title("第 7 步：如果模型当时忘了编号，还可以用 search_contents 先定位")
    hits = session.call_history_search("翻译成中文")
    print("搜索 \"翻译成中文\" 的结果：")
    for h in hits:
        print("   ", h)

    # -------------------------------------------------------------------------
    title("对照：如果当时没有 notes 也没有 history，会怎样？")
    print("新窗口里就只剩这段固定前缀和窗口编号消息。")
    print("上面那些 user / assistant / tool 内容——在没有 notes 和 history 的情况下——")
    print("对模型来说就等于从未存在过。")
    text_blob = "\n".join(i.content for i in session.items)
    print("\n实证：在新窗口的模型视野里搜索旧内容关键词 ——")
    for probe in ["828 行", "草稿", "三种策略"]:
        print(f"   搜索 {probe!r} → {'找到了' if probe in text_blob else '找不到（已不在模型视野内）'}")
    print("\n但它在服务端仓库里仍然完好：")
    for probe in ["828 行", "草稿", "三种策略"]:
        hit = session.history.search_contents(probe)
        print(f"   搜索 {probe!r} → {'找到了 ' + str(len(hit)) + ' 条' if hit else '找不到'}")


if __name__ == "__main__":
    main()
