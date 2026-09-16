# codex 的上下文压缩：从「暴力摘要」到「按需取回」

> 本文基于仓库 `study` 分支（HEAD `4f8c63fa0f`，约等于上游 `508a006d7a`）的真实代码写成，
> 所有结论都标了 `文件:行号`。凡是我读不到、只能靠推断的地方（主要是服务端行为），都会明确写出来，
> 不当作事实陈述。
>
> 配套可运行代码：[`context_compaction_demo.py`](./context_compaction_demo.py)（纯标准库，`python context_compaction_demo.py` 直接跑）

---

## 目录

1. [先给结论：是的，但只对了一半](#1-先给结论是的但只对了一半)
2. [看代码前先把名词认全](#2-看代码前先把名词认全)
3. [老办法：把历史压成一段摘要](#3-老办法把历史压成一段摘要)
4. [新办法：换一个窗口，需要时再回去查](#4-新办法换一个窗口需要时再回去查)
5. [三条路怎么选：一行判断决定命运](#5-三条路怎么选一行判断决定命运)
6. [新旧两条路的正面对比](#6-新旧两条路的正面对比)
7. [完整生命周期：一次跨窗口的任务长什么样](#7-完整生命周期一次跨窗口的任务长什么样)
8. [可运行 demo：把整套机制跑一遍](#8-可运行-demo把整套机制跑一遍)
9. [怎么载入，怎么确认真的生效](#9-怎么载入怎么确认真的生效)
10. [边界：哪些是本地做的，哪些是服务端做的](#10-边界哪些是本地做的哪些是服务端做的)
11. [存疑与风险（写文档时没能证实的部分）](#11-存疑与风险写文档时没能证实的部分)
12. [关键文件索引](#12-关键文件索引)

---

## 1. 先给结论：是的，但只对了一半

用户的问题是：**codex 现在的上下文压缩，是不是已经从「暴力压缩、丢原文」改成「按需加载」了？**

准确回答分三层：

**第一层：确实存在一套「按需取回」的新机制，而且它的设计思路正是「不丢原文，需要时再查」。**

这套机制叫 **token budget / context window（token 预算 / 上下文窗口）**。它在压缩时的行为非常反直觉：

- 它**不调用任何模型**去写摘要；
- 它把当前窗口里的对话**整段丢掉**；
- 然后（默认情况下）只重新装回两样东西：固定的初始上下文，以及一条带「窗口编号」的开发者消息
  （打开 `Feature::RetainClientDeveloperMessages` 时还会额外按预算装回客户端自撰的 developer 消息，
  这个开关默认关闭，详见 §6 表格）；
- 事后如果模型需要旧内容，用工具按编号**取回来**。

关键代码只有一句 —— `codex-rs/core/src/compact_token_budget.rs:73`：

```rust
sess.start_new_context_window(step_context, world_state).await;
```

对比一下老办法：老办法要先花一次模型调用换来一段摘要文本，然后把摘要装回历史。
新办法这段摘要**根本不存在** —— 在 `codex-rs/core/src/session/mod.rs:4487` 那里，
写进记录的摘要字段被硬编码成空字符串：

```rust
CompactedHistoryMetadata {
    message: String::new(),   // ← 没有摘要。这就是「不丢原文」的最直接证据
    ...
}
```

**第二层：但它不是默认行为，老办法仍然在跑。**

`Feature::TokenBudget` 这个功能开关在 `codex-rs/features/src/lib.rs:1622-1627` 里的定义是：

```rust
FeatureSpec {
    id: Feature::TokenBudget,
    key: "token_budget",
    stage: Stage::UnderDevelopment,   // 还在开发阶段
    default_enabled: false,           // 默认关闭
},
```

默认关闭、且还在开发阶段。所以今天绝大多数会话走的仍然是**摘要式压缩**（本地摘要或远程压缩），
那两条路依然会丢弃原始上下文。

**第三层：就算开了新机制，「取回原文」的能力也不在本地。**

这一点最容易误解。原始对话确实**完整保存在你磁盘上**（rollout JSONL 文件是只追加的：
不删改已有内容，冷文件会整体重编码成 zstd 压缩格式 `.jsonl.zst`），
但**模型没有任何工具能读它**。模型能读的，是**服务端**保存的另一份索引 ——
也就是 codex 后端在你每次请求时顺手收录进去的那一份。

所以更精确的说法是：

> 压缩后原文没有被删掉，但**换了一个存放位置**，从「模型的视野里」搬到了「需要凭编号去服务端取回的仓库里」。
> 这不是「无损」，而是「有损只在模型没去取的时候才发生」。

---

## 2. 看代码前先把名词认全

下面这些词在本文里会反复出现，先一次说清。

| 名词 | 说白了是什么 |
|---|---|
| **上下文 / context** | 每次请求模型时，你发给它的那一整包内容。模型没有记忆，它「记得」的一切都是这一包里写的。 |
| **窗口 / context window** | 一份上下文的最大容量上限，以 token 计。可以理解成「模型的工作台有多大」。 |
| **token** | 模型计量的最小单位，大致可以理解成「词块」。英文约 4 个字符 1 个 token，中文约 1~2 个汉字 1 个 token。 |
| **压缩 / compaction** | 上下文快装满时，把它变小一点，好让对话能继续下去。 |
| **摘要式压缩** | 压缩的一种做法：让模型把前面聊的内容概括成一段话，然后用这段话替换掉原来的全文。原文就此从模型视野里消失。 |
| **窗口编号 / window id** | 系统给每个「窗口」发的一个身份证号。本文里它是一个 UUID（形如 `0199c1f0-...`），实际实现用的是 UUIDv7 —— 意思是「按生成时间排序的 UUID」，所以编号本身能反映先后顺序。 |
| **条目编号 / item id** | 窗口里**每一条**内容（每条用户消息、每条模型回复、每条工具输出）各有一个编号。这就是提示词里那个 `[id: ...]`。 |
| **取件码** | 上面两个编号合起来，就是本文反复说的「取件码」。有了它，就能精确取回某一条原文。 |
| **notes（笔记）** | 一套让模型自己写工作笔记的工具。笔记存在服务端，**能跨窗口存活**。 |
| **history（历史）** | 一套只读工具，让模型凭取件码把旧窗口的内容查回来。 |
| **ingest（收录）** | 服务端把内容存进它的历史索引这个动作。客户端只负责在请求里说一句「请收录」，实际收录由服务端做。 |
| **rollout 文件** | 本地磁盘上那份「这一轮对话发生过什么的流水账」，JSONL 格式（每行一个 JSON）。 |
| **feature flag（功能开关）** | 一个名字 + 开/关状态。用来让还没做完的功能默认不生效。 |
| **provider（提供商）** | 你连的是哪家模型服务（OpenAI 官方、Azure、还是别家的兼容接口）。 |

---

## 3. 老办法：把历史压成一段摘要

### 3.1 两条摘要路线

摘要式压缩有**两条**实现，走哪条不看功能开关，只看 `provider` 支不支持远程压缩。
判断点在 `codex-rs/model-provider/src/provider.rs:353-366`：

```rust
fn capabilities(&self) -> ProviderCapabilities {
    let remote_compaction = if self.info.is_openai()
        || is_azure_responses_provider(&self.info.name, self.info.base_url.as_deref())
    {
        RemoteCompactionSupport::V2          // 是 OpenAI 或 Azure → 走远程
    } else {
        RemoteCompactionSupport::Unsupported // 其他兼容接口 → 走本地
    };
    ProviderCapabilities { remote_compaction, ..ProviderCapabilities::default() }
}
```

- **本地摘要**（`codex-rs/core/src/compact.rs`）：客户端自己发一条「请写交接摘要」的消息给模型，
  把模型回复的那条助手消息当作摘要。
- **远程 V2**（`codex-rs/core/src/compact_remote_v2.rs`）：客户端不加任何提示词，
  只是往请求末尾塞一个控制标记，**让服务端来做压缩**，服务端返回一段密文
  （密文 = 加密后的内容，客户端解不开，只能原样传回去）。

### 3.2 本地摘要用的提示词原文

真源在 `codex-rs/prompts/templates/compact/prompt.md`，全文是：

```text
You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work.
```

模型回复的摘要会被套上一段固定前缀（`codex-rs/prompts/templates/compact/summary_prefix.md`，单行）：

```text
Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:
```

这段前缀还有实际用途：`codex-rs/core/src/compact.rs:592` 的 `is_summary_message()` 靠
「内容是不是以这段前缀开头」来判断「这条消息是压缩摘要」，从而在下次压缩时把旧摘要排除掉。

### 3.3 压缩之后，历史里还剩什么

替换历史由 `build_compacted_history` 拼出来（`codex-rs/core/src/compact.rs:664-675`，
真正的拼接逻辑在它调用的内层函数 `build_compacted_history_with_limit`，`:677-754`），规则是：

1. 从**最新往旧**收集用户消息，累计不超过 **20,000 token**
   （常量 `COMPACT_USER_MESSAGE_MAX_TOKENS`，`codex-rs/core/src/compact.rs:60`）；
   如果某条消息加进去会超，就把这条**截断**后放进去，然后停止收集。
2. 把收集到的消息**反转回原来顺序**。
3. 最后追加一条消息，内容是「固定前缀 + 模型写的摘要」。

**别的全都丢掉了**：所有模型回复正文、所有推理过程、所有工具调用与工具输出。

这不是推测，是测试里逐字断言过的。看
`codex-rs/core/tests/suite/snapshots/all__suite__compact__manual_compact_with_history_shapes.snap`：

```text
## Window 3 ...
-- request 3 (turn) --
00:message/user:      first manual turn              ← 保留下来的老用户消息
01:message/user:      <COMPACTION_SUMMARY>
                      FIRST_MANUAL_SUMMARY            ← 摘要（前缀 + 模型输出）
02:message/developer: <PERMISSIONS_INSTRUCTIONS>      ← 下一回合重新注入的初始上下文
03:message/user:      <ENVIRONMENT_CONTEXT>
04:message/user:      second manual turn              ← 新用户消息
```

注意里面**没有任何 assistant 消息**。同一目录的测试也断言了这点
（`codex-rs/core/tests/suite/compact.rs:670-672`）：

```rust
assert_eq!(assistant_count, 0, "assistant history should be cleared");
```

**这就是「暴力压缩、损失原始上下文」的准确含义**：模型回复全文没了，工具输出全文没了，
只剩「用户说过的话」+「一段模型自己概括的话」。概括必然丢细节 —— 一段 828 行的代码分析，
摘要可能只剩一句「已读 compact.rs」。

### 3.4 远程 V2 有什么不同

远程 V2 把「让模型写摘要」换成了「让服务端处理」。客户端在请求末尾塞的控制标记是
`codex-rs/core/src/compact_remote_v2_attempt.rs:78`：

```rust
input.push(ResponseItem::CompactionTrigger {});
```

服务端返回一条密文（`ResponseItem::Compaction { encrypted_content }`，
密文 = 加密后的内容，客户端解不开）。
客户端不会把整段历史原样装回去，而是按一套保留规则挑出可保留的部分，
再连同这条密文一起装回历史。保留规则写在
`is_retained_for_remote_compaction_v2`（`codex-rs/core/src/compact_remote_v2.rs:536-580`）：

- `AgentMessage`（子代理之间的消息）→ 保留，但排除两类：子代理的进度消息
  （正文以 `Message Type: MESSAGE` 开头）和最终答复（`Message Type: FINAL_ANSWER`），
  且单条不超过 `MAX_RETAINED_AGENT_MESSAGE_TOKENS`（`:76`）；
- role 为 `user` 且能解析成用户消息（`TurnItem::UserMessage`）**或钩子注入的提示消息
  （`TurnItem::HookPrompt`）** → 保留；
- role 为 `developer` 且是「客户端自撰」（`is_client_authored_developer_message`）
  且 `retain_client_developer_messages` 开关打开 → 保留；
- 其余一律不保留。

挑完之后还有总量上限 **64,000 token**
（`RETAINED_MESSAGE_TOKEN_BUDGET`，`codex-rs/core/src/compact_remote_v2.rs:75`）。

**注意这条路径上没有任何「笔记」参与** —— 保留的是上面这几类历史条目，
不是笔记。笔记（notes）只在换窗口式的 token budget 路径里起作用。

它和本地摘要最大的差别是：**原始内容被服务端完整看到过**，
而且返回的密文里理论上带着服务端认为该保留的信息。但密文解不开，
「原文到底在不在里面、保留多久」在本仓库里查不到，属于服务端行为。

**两条摘要路线的共同点：客户端保存的替换历史里，原始助手消息和工具输出都被丢掉了。**

---

## 4. 新办法：换一个窗口，需要时再回去查

### 4.1 核心思路一句话

> 与其花力气把历史「压小」，不如承认历史就是装不下；
> 那就**开一个新的空窗口**，然后把「去哪里找旧内容」这件事告诉模型。

这套机制由五个零件组成，缺一不可。

### 4.2 零件一：窗口编号（three IDs）

每个窗口有三个编号，定义在 `codex-rs/core/src/state/auto_compact_window.rs:4-9`：

```rust
pub(crate) struct AutoCompactWindowIds {
    pub(crate) first_window_id: Uuid,           // 第一个窗口的编号，永远不变
    pub(crate) previous_window_id: Option<Uuid>, // 刚刚被丢掉的窗口的编号
    pub(crate) window_id: Uuid,                 // 当前窗口的编号
}
```

换窗口时的轮转逻辑在 `codex-rs/core/src/state/auto_compact_window.rs:77-85`：

```rust
pub(super) fn advance(&mut self) -> (u64, AutoCompactWindowIds) {
    self.window_number = self.window_number.saturating_add(1);
    self.ids.previous_window_id = Some(self.ids.window_id);  // 记住刚丢的那个
    self.ids.window_id = Uuid::now_v7();                      // 发一个新号
    self.new_context_window_requested = false;
    self.token_budget_reminder_delivered = false;             // 两个"只发一次"的开关复位
    self.auto_compact_fallback_delivered = false;
    (self.window_number, self.ids)
}
```

三行就说完：`first` 不动，`previous` 变成旧的 `current`，`current` 换成新号。
所以**只要模型看到 `Previous context window id` 这一行，它就知道「我被换过窗口了」**。

### 4.3 零件二：告诉模型的窗口消息

这条消息渲染出来的样子（实现见 `codex-rs/core/src/context/token_budget_context.rs:60-75`）：

```text
<context_window>
Agent name: /root
First context window id: 0199c1f0-1111-7000-8000-000000000001
Current context window id: 0199c1f0-3333-7000-8000-000000000003
Previous context window id: 0199c1f0-2222-7000-8000-000000000002
</context_window>
```

这是**开发者消息**（role = `developer`），而且要求独立成一条消息
（`requires_separate_message()` 返回 `true`）。

### 4.4 零件三：笔记工具（notes）

让模型自己写工作笔记，存在服务端，跨窗口存活。五个动作
（`codex-rs/ext/history-notes/src/tools.rs:71-97`）：

| 工具名 | 干什么 |
|---|---|
| `notes.list_files_by_prefix` | 列出有哪些笔记文件 |
| `notes.read_file` | 读某个笔记文件 |
| `notes.search_contents` | 在笔记里搜索 |
| `notes.append_to_file` | 追加写（模型最常用） |
| `notes.write_file` | 整篇覆盖写 |

笔记路径是**虚拟路径**，不是磁盘路径。单个文件有硬上限 100 万字节（UTF-8）。

### 4.5 零件四：历史检索工具（history）

这是一套**只读**工具，让模型凭取件码把旧内容查回来，四个动作
（`codex-rs/ext/history-notes/src/tools.rs:71-97`）：

| 工具名 | 对应的服务端接口 | 干什么 |
|---|---|---|
| `history.list_windows` | `alpha/history/v2/list_windows` | 列出都有哪些窗口，各有多少条内容 |
| `history.list_items` | `alpha/history/v2/list_items` | 列出某个窗口里的条目（每条只给开头一小段，避免一次撑爆上下文） |
| `history.read_item` | `alpha/history/v2/read_item` | **按「窗口编号 + 条目编号」精确读回原文**，还能指定读哪一段 |
| `history.search_contents` | `alpha/history/v2/search_contents` | 在全部历史里做字面搜索，用来在**不知道编号时**先定位 |

`read_item` 要求两个编号都传，从它的参数说明就能看出设计意图
（`codex-rs/ext/history-notes/src/tools.rs:166-176`）：

```json
"read_item": {
  "required": ["item_id", "window_id"],
  "properties": {
    "item_id": {
      "description": "The short item ID is the suffix shown in the target item's trailing `[id: ...]` marker, printed after that item's content."
    },
    "window_id": { "type": "string" },
    "offset_chars": { ... },
    "limit_chars": { ... }
  }
}
```

**这四条工具就是「按需加载」四个字的全部实现。**
它们把「把原文塞进上下文」换成了「把取件码塞进上下文，需要时再凭码取原文」。

### 4.6 零件五：换窗口工具（new_context）

除了系统自动换窗口，模型也可以**主动要求**换。工具名叫 `new_context`
（注意：文件名是 `new_context_window*`，工具名却是 `new_context`，容易看岔）。
调用后工具返回这句话（`codex-rs/core/src/tools/handlers/new_context_window.rs:13`）：

```rust
pub(crate) const NEW_CONTEXT_WINDOW_MESSAGE: &str =
    "A new context window will start without summarizing conversation history.";
```

这句话本身就是对机制的准确概括：**开新窗口，但不总结历史**。

它只在 `Feature::TokenBudget` 打开时才注册（`codex-rs/core/src/tools/spec_plan.rs:1207-1210`）：

```rust
if features.enabled(Feature::TokenBudget) {
    registry.add_with_exposure(NewContextWindowHandler, ToolExposure::DirectModelOnly);
    registry.add(GetContextRemainingHandler);
}
```

注意同一段代码还注册了 `get_context_remaining` —— 让模型能主动问「我还剩多少 token」。

### 4.7 换窗口到底做了什么

`codex-rs/core/src/session/mod.rs:4446-4497`（简化后；`world_state` 是这一回合的
「世界状态」快照，装着系统指令、环境信息这类需要重新注入的固定内容）：

```rust
pub(crate) async fn start_new_context_window(
    &self,
    step_context: &StepContext,
    world_state: Arc<WorldState>,
) -> u64 {
    // 0. 默认情况下这里是空的；只有打开 RetainClientDeveloperMessages 开关时，
    //    才会按预算挑出「客户端自撰的 developer 消息」装回去
    let retained_client_developer_messages = ...;   // 见 :4452-4468

    // 1. 轮转窗口编号
    let window = {
        let mut state = self.state.lock().await;
        state.start_new_context_window()
    };
    let (window_number, window_ids) = window;

    // 2. 重新生成初始上下文（系统指令、权限说明、环境信息……）
    let context_items = self
        .build_initial_context_with_world_state(step_context, world_state.as_ref())
        .await
        .into_iter()
        .map(ResponseItemEnvelope::new)
        .chain(retained_client_developer_messages)   // 默认是空迭代器
        .collect();

    // 3. 用这些内容整体替换掉旧历史
    self.replace_compacted_history(
        context_items,
        Some(turn_context_item),
        Some(world_state),
        CompactedHistoryMetadata {
            message: String::new(),   // ← 摘要为空
            window_number,
            window_ids,
            compaction_response_id: None,
            compaction_model_hash: None,
        },
    )
    .await;
    self.recompute_token_usage(turn_context).await;
    window_number
}
```

「整体替换」发生在 `codex-rs/core/src/context_manager/history.rs:509-525`：

```rust
pub(crate) fn replace_compacted(&mut self, items: Vec<ResponseItemEnvelope>) {
    ...
    self.items = Arc::new(items);        // ← 旧的那一整个 Vec 被换掉
    self.history_version = self.history_version.saturating_add(1);
    self.world_state_baseline = None;
}
```

内存里的旧历史就此消失。**但磁盘上的没消失** —— 见第 10 节。

### 4.8 什么时候提醒模型「赶紧写笔记」

压缩之前会先给模型一个**写笔记的机会窗口**，
由 `codex-rs/core/src/session/token_budget.rs:161-224` 的 `maybe_record` 实现，
分两级：

**第一级：快到阈值了 → 提醒它提前记笔记**

判断条件是「剩余 token ≤ 提醒阈值，且本窗口还没提醒过」。生产环境用的提醒文案原文
（`codex-rs/models-manager/models.json`，所有带 `token_budget` 配置的模型文案一致）：

```text
<context_window_reminder>
Your current context window is nearly exhausted; only {n_remaining} tokens remain. Before starting a new context window, save concise progress notes with the `notes` tool with the goal, decisions, progress, learnings, next steps, and the window ID and item ID of every relevant user request still being solved, as well as important actions/tool calls for future reference. Note that every non-assistant item, such as user, developer, tool response, has an item id `[id: ...]` that is immediately after its item content. You should write or append notes in a way to best help you recover in a new context window. It is also a good idea to clean up your old notes if they become obsolete or irrelevant. Future context windows will not automatically include the current conversation. After saving your state, call `functions.new_context` to continue in a fresh context window.
</context_window_reminder>
```

这段文案里有三处关键信息值得单独点出来：

1. 明确说「**未来窗口不会自动包含当前对话**」—— 坦率告知会丢。
2. 明确要求「记下每个还在处理的用户请求的 **window ID 和 item ID**」—— 这就是取件码的由来。
3. 明确说「每个非 assistant 条目后面都跟着一个 `[id: ...]`」—— 告诉模型取件码长什么样。

**第二级：主额度刚好用光 → 只许写笔记，不许干活**

这就是 `auto_compact_fallback_prompt`（兜底 = 最后一道保险），文案是：

```text
<context_window_reminder>
The current context window is exhausted. Do not continue the task or give a final answer in this window.
The next window will not automatically include this conversation. Make exactly one write or append call to
`notes` now to save a concise checkpoint ...
</context_window_reminder>
```

**它的触发条件是「剩余恰好等于 0」**，也就是主额度刚好耗尽的那一刻 ——
不是「扣掉缓冲之后变成负数」。这一点在代码里写得很直白，
`codex-rs/core/src/session/token_budget.rs:201`：

```rust
if !allow_auto_compact_fallback || base_window_tokens_remaining != 0 {
    return;
}
```

之所以不存在「负数剩余」，是因为剩余值被钳制在 0 以上
（`tokens_remaining`，`codex-rs/core/src/session/context_window.rs:21-23`）：

```rust
fn tokens_remaining(limit: Option<i64>, used: i64) -> Option<i64> {
    limit.map(|limit| limit.saturating_sub(used).max(0))
}
```

那生产环境这个 `auto_compact_fallback_buffer_tokens`（**16384 token**）管什么用？
它管的是**推迟「强制换窗口」**，不是触发兜底提示
（`codex-rs/core/src/session/context_window.rs:101-109`）：

```rust
// 换窗口线 = 主额度 + 缓冲
let buffered_auto_compact_limit = auto_compact_scope_limit
    .map(|limit| limit.saturating_add(auto_compact_fallback_buffer_tokens));

// 已用量越过「主额度 + 缓冲」，或碰到模型完整上下文窗口这个硬上限，才判定必须换窗
let full_context_window_limit_reached =
    full_context_window_limit.is_some_and(|limit| active_context_tokens >= limit);
let token_limit_reached = buffered_auto_compact_limit
    .is_some_and(|limit| auto_compact_scope_tokens >= limit)
    || full_context_window_limit_reached;
```

字段名就叫 `token_limit_reached`（`codex-rs/core/src/session/context_window.rs:18`）。
提醒阈值是 **6144 token**，缓冲是 **16384 token**
（`codex-rs/models-manager/models.json`）。

于是完整时序是三段：

```
剩余 ≤ 提醒阈值(6144)              →  注入提醒："赶紧写笔记"
剩余 == 0（主额度刚好耗尽）         →  注入兜底提示："别干活了，只写一次笔记"
已用量 ≥ 主额度 + 缓冲(16384)       →  强制换新窗口
（注意：模型完整上下文窗口这个硬上限不吃缓冲，一碰到就立刻换窗）
```

中间那段（剩余已经是 0、但还没到换窗口线）的作用是：
**给模型留出真正把笔记写完的空间**。

这段时序是端到端测试逐字钉死的（`codex-rs/core/tests/suite/token_budget.rs`）：

- `token_budget_auto_compact_fallback_uses_buffer_until_new_context`（`:1411-1509`）：
  主额度 9000、缓冲 4000，用量 9500 时出现兜底提示，但**不换窗**，回合继续；
- `token_budget_auto_compact_fallback_rolls_over_after_buffer`（`:1512-1571`）：
  用量涨到 13500 ≥ 9000 + 4000 = 13000，这时才真正换窗。

这两条提醒都由 `claim_token_budget_reminder()` / `claim_auto_compact_fallback()`
保证**每个窗口只发一次**，换窗口时在 `advance()` 里复位。

### 4.9 阈值本身怎么算

上面说的「剩余」「提醒」「换窗口线」全都建立在同一个**主额度**上
（代码里叫 `auto_compact_scope_limit`）。它由两段代码拼出来，分别看。

第一段，已用量怎么取（`codex-rs/core/src/session/context_window.rs:57`）：

```rust
let active_context_tokens = sess.get_total_token_usage().await;   // 已用多少
```

第二段，默认阈值 = 上下文窗口的 90%，并且用户配置只能把它「收紧」、不能放宽
（`codex-rs/protocol/src/openai_models.rs:521-532` 的 `auto_compact_token_limit`）：

```rust
let context_limit = self
    .resolved_context_window()
    .map(|context_window| (context_window * 9) / 10);
let config_limit = self.auto_compact_token_limit;
if let Some(context_limit) = context_limit {
    return Some(
        config_limit.map_or(context_limit, |limit| std::cmp::min(limit, context_limit)),
    );
}
config_limit
```

两个细节：

- **默认阈值是窗口容量的 90%**。这大概是为了留 10% 余量、避免刚好卡在边界上被服务端拒绝
  —— 代码里没有写这句话，这是从数值反推的**推断**，不是注释原文。
- **已用量是「混合记账」**：服务端上次返回的真实用量 + 本地对之后新增内容的粗略估算
  （`codex-rs/core/src/context_manager/history.rs:685-702`）。本地估算函数自己都注明是
  「coarse lower bound, not a tokenizer-accurate count」
  （粗略下界，不是精确分词，见 `:444-445`）。
  下界的意思是**倾向于少算**已用量 → 剩余被高估 → 提醒和换窗口只会**延后**触发，
  而不是提前。真实风险是反过来：跑过头、被服务端以「超出上下文窗口」拒掉。

### 4.10 模型怎么知道该用笔记？靠 guidance

除了「快满了」的临时提醒，还有一段**常驻指引**，由配置 `guidance_message` 提供，
渲染成 `<context_window_guidance>` 消息。生产环境文案（`codex-rs/models-manager/models.json`）的关键段落：

```text
If Previous context window id is present in `<context_window>`, it means a context reset occurred
and this is a new window. After a reset, read the checkpoint and use the read-only `history` tool
to recover any missing details. When a window ID and item ID are known, prefer `read_item` directly;
when they are missing or uncertain, use `list_items`, or `search_contents` to locate the item first.
```

这段几乎就是一份操作手册，把整个「按需取回」的流程讲清楚了：

1. 看到 `Previous` 编号 → 说明换过窗口了；
2. 先读笔记里的检查点；
3. 要细节 → 用 `history` 工具；
4. **有编号就直接 `read_item`；没编号就先 `list_items` 或 `search_contents` 定位**。

最后还有一条约束：

```text
Treat notes and history as internal bookkeeping. Do not mention them in user-facing messages.
```

要求模型把这一切当内部记账，不要向用户提起。

---

## 5. 三条路怎么选：一行判断决定命运

所有自动压缩最终都汇到同一个分发函数 `run_auto_compact`
（`codex-rs/core/src/session/turn.rs:1405-1463`），主干是：

```rust
async fn run_auto_compact(...) -> CodexResult<()> {
    let turn_context = &step_context.turn;

    // 第一判断：token budget 开关
    if turn_context.config.features.enabled(Feature::TokenBudget) {
        crate::compact_token_budget::run_inline_auto_compact_task(
            Arc::clone(sess), step_context, initial_context_injection,
        ).await?;
        return Ok(());                    // ← 直接返回，根本不看 provider
    }

    // 第二判断：provider 支不支持远程压缩
    match turn_context.provider.capabilities().remote_compaction {
        RemoteCompactionSupport::V2 => {
            run_inline_remote_auto_compact_task_v2(...).await?;   // 远程 V2
        }
        RemoteCompactionSupport::Unsupported => {
            run_inline_auto_compact_task(...).await?;             // 本地摘要
        }
    }
    Ok(())
}
```

**优先级从高到低：token budget（换窗口）→ 远程 V2（服务端摘要）→ 本地摘要**。

手动压缩走的是另一条入口（`Op::Compact` → `CompactTask`），但**分发顺序完全一样**
（`codex-rs/core/src/tasks/compact.rs:36-68`）：

```rust
if ctx.config.features.enabled(Feature::TokenBudget) {
    crate::compact_token_budget::run_manual_compact_task(session, ctx).await?;
    return Ok(None);
}
let result = match ctx.provider.capabilities().remote_compaction {
    RemoteCompactionSupport::V2 => crate::compact_remote_v2::run_remote_compact_task(...).await,
    RemoteCompactionSupport::Unsupported => crate::compact::run_compact_task(...).await,
};
```

### 触发时机：6 个自动调用点 + 手动入口

`run_auto_compact` 在 `turn.rs` 里一共有 **6 个自动调用点**：
`:344`、`:613`、`:724`、`:1256`、`:1338`、`:1386`。
它们按场景归成下面这张表 ——「模型主动调 `new_context`」不是独立的调用点，
它是 `:600-601` 那个 `should_roll_over` 判断里的一个 or 条件，
和「回合中途超限」共用同一个调用点。表里前 6 行正好就是这 6 个调用点；
后 2 行是另外两条入口：Guardian 审查会话内部走的是 `Op::Compact`（手动那条路），
最后一行则是用户亲自敲的 `/compact`。

| 场景 | 位置 | 什么条件 |
|---|---|---|
| 回合开始前，Guardian 注入证据就把预算撑爆 | `turn.rs:323-353`（调用在 `:344`） | Guardian（自动安全审查子代理）会话 且 注入时收到 `ContextWindowExceeded` 且 **没开** `Feature::TokenBudget`（`:334-338`）；reason = `ContextLimit`，phase = `PreTurn` |
| 回合开始前，token 超限 | `turn.rs::run_pre_sampling_compact`（`:1239-1268`，调用在 `:1256`） | `token_limit_reached`（已用量越过「主额度 + 缓冲」） |
| 回合开始前，换模型导致兼容性哈希（模型给请求格式算的指纹）变化 | `turn.rs::maybe_run_previous_model_inline_compact`（调用在 `:1338`） | 前后两个哈希都存在且不同；reason = `CompHashChanged` |
| 回合开始前，换到更小窗口的模型 | 同上（调用在 `:1386`） | 旧模型窗口更大、新模型已越限；reason = `ModelDownshift` |
| 回合中途，超限或模型主动要求（含模型调 `new_context`） | `turn.rs:600-601` 的 `should_roll_over` | 还需要继续跑 且（`token_limit_reached` 或 有 `new_context` 请求） |
| 采样（发一次模型请求）报「超出上下文窗口」 | `turn.rs:706-739` 的错误恢复分支 | Guardian 场景 且 每步最多一次 且 **没开** `Feature::TokenBudget`（`:709`） |
| Guardian 审查会话内部换窗 | `guardian/review_session.rs:383-391` | Guardian 场景 且 `Feature::TokenBudget` 打开 且 `token_limit_reached` |
| 用户敲 `/compact` | `Op::Compact` → `CompactTask` | 用户主动 |

关于「Guardian 审查会话内部换窗」这一行需要补一句注：它和 §11.7 说的
「Guardian 强制关闭 token budget」在常规路径下**是互相排斥的**。
reviewer 的配置由 `build_guardian_review_session_config`
（`codex-rs/core/src/guardian/review.rs:194`）构造，其中
`inherit_token_budget` 恒为 false（`codex-rs/ext/guardian-reviewer/src/settings.rs:57`），
`disabled_features` 里就含 `Feature::TokenBudget`
（同文件 `:64-75`，在 `codex-rs/core/src/guardian/reviewer_config.rs:94-107` 执行 disable）。
所以 `review_session.rs:383` 这个分支只有在 disable **失败**时
（`reviewer_config.rs:101-106` 的 warn 路径）才可能进得去。

要强调的是：**只有回合中途那次换窗口**才会把「初始上下文插到最后一条真实用户消息之上」
（`InitialContextInjection::BeforeLastUserMessage`）。回合开始前和手动压缩用的是
`DoNotInject`，意思是「什么都别插，等下一个正常回合自己重新注入」。
原因写在 `codex-rs/core/src/compact.rs:62-77` 的注释里：模型被训练成
「回合中途压缩后，摘要应该落在历史最末尾」，所以中途那次要特殊处理。

---

## 6. 新旧两条路的正面对比

| 维度 | 摘要式（本地 / 远程 V2） | 换窗口式（token budget） |
|---|---|---|
| **压缩时调模型吗** | 调（本地：发提示词；远程：发控制标记） | **不调** |
| **产出摘要吗** | 产出一段摘要文本 / 一段密文 | **不产出**（`message: String::new()`） |
| **原文去哪了** | 客户端这份历史里被替换掉 | 客户端这份历史里被替换掉 |
| **原文在服务端吗** | 远程 V2：在（但是密文，客户端看不到）；本地摘要：**不在** | 在（被 `history_ingest_requested` 收录进可检索索引） |
| **原文在本地磁盘吗** | 在（rollout JSONL 只追加、不删改内容） | 在（同上） |
| **模型能读回原文吗** | **不能**。只能看摘要 | **能**。用 `history.read_item` 凭编号精确取回 |
| **替换历史里保留什么** | 本地：最近用户消息（≤20k token）+ 摘要<br>远程：用户消息 / hook prompt（钩子注入的提示消息）/ Agent 消息，外加开关打开时客户端自撰的 developer 消息（合计 ≤64k token）+ 密文 | 初始上下文 + 窗口编号消息（+ 开关打开时按预算装回的客户端自撰 developer 消息） |
| **触发前会提醒模型吗** | 不会 | **会**。两级提醒：阈值提醒（剩余 ≤6144）+ 兜底提醒（剩余 == 0 时的最后一道保险），给模型时间写笔记 |
| **默认开启吗** | 是 | **否**（`default_enabled: false`，还在开发阶段） |
| **谁来决定走哪条** | provider 是不是 OpenAI/Azure | `Feature::TokenBudget` 开关 |

一句话概括差别：

> 老办法把「原文」换成「一段概括」，指望这段概括够用；
> 新办法把「原文」换成「一张取件码」，需要的时候再拿码去取。

---

## 7. 完整生命周期：一次跨窗口的任务长什么样

把第 4 节的零件串起来，这就是一次跨越两个窗口的任务的完整时间线：

```
【窗口 1 诞生】
  window_id = A（同时也是 first_window_id）
  上下文 = 初始上下文 + <context_window>(Current = A)
       │
       │  正常干活：用户消息、模型回复、工具输出不断追加
       │  每次请求都带 history_ingest_requested: true
       │  → 服务端把这些内容按 (A, item_id) 收录进它的索引
       ▼
【剩余 token 降到 6144】
  注入 <context_window_reminder>："赶紧写笔记，记下 window id 和 item id"
  模型调用 notes.append_to_file(...)，写入：
      目标 / 进度 / 下一步 / [id: itm_1] [id: itm_4] ...
       │
       │  模型继续干活（提醒给了它写笔记的时间）
       ▼
【剩余 token 降到 0（主额度刚好耗尽）】
  注入 auto_compact_fallback_prompt："别干活了，只写一次笔记"
  模型写最后一次笔记
       │
       │  注意：这时**还不换窗口**。剩余只钳到 0，不会变负数；
       │  换窗口线是「已用量 ≥ 主额度 + 缓冲(16384)」，要再涨一段才到
       ▼
【已用量越过 主额度 + 16384（缓冲用完）→ 强制换窗口】
  advance():  previous_window_id = A, window_id = B（新号）
  内存历史被整体替换为：初始上下文 + <context_window>(Current = B, Previous = A)
  磁盘追加一行 {"type":"compacted", "window_id": B, "previous_window_id": A, ...}
       │
       ▼
【窗口 2 里的模型看到 Previous = A】
  按 guidance 的指示：
    1) 先读笔记 notes.read_file("/root/notes/progress.md")
    2) 笔记里有取件码 [id: itm_3]
    3) 用 history.read_item(window_id = A, item_id = "itm_3") 取回原文
    4) 如果笔记没记编号 → history.search_contents("关键词") 先定位
```

这里面最关键的一步是 **「写笔记」**。它不是锦上添花，而是**整套机制能否成立的前提**：

- 服务端索引里有原文，但那是一大堆没有语义的条目，模型不可能靠遍历找出哪条重要；
- **是模型自己写的笔记，充当了「摘要」这个角色** —— 只不过这份摘要由模型主动维护、存在服务端、
  而且附带精确的取件码。

所以从信息论角度看，新办法并不是「无损」，而是把「生成摘要」这件事
**从「压缩时一次性生成」改成了「工作中持续维护」**。
好处是摘要的质量由模型在**信息最全的时候**决定，而且可以随时修订；
代价是**如果模型忘了写，就真的丢了**。

---

## 8. 可运行 demo：把整套机制跑一遍

配套文件 [`context_compaction_demo.py`](./context_compaction_demo.py) 用纯标准库
模拟了上面整条链路，直接运行：

```bash
python study/context_compaction_demo.py
```

它用三层结构对应真实实现：

| demo 里的类 | 对应的真实东西 | 真实代码位置 |
|---|---|---|
| `HistoryStore` | 服务端历史索引 `alpha/history/v2/*` | `codex-rs/ext/history-notes/src/backend.rs` |
| `NotesStore` | 服务端笔记 `alpha/notes/v2/*`（含单文件 100 万 UTF-8 字节上限） | 同上 |
| `Session` | 客户端会话（窗口编号 / 记账 / 提醒 / 兜底 / 换窗口判定） | `core/src/session/mod.rs`、`core/src/state/auto_compact_window.rs`、`core/src/session/context_window.rs` |

demo 里那组数字（窗口 300 / 提醒阈值 60 / 缓冲 40）是特意挑的：
它能让三级时序——**提醒 → 兜底 → 强制换窗**——在几十行输出里全部真实触发一遍。

关键片段 —— 换窗口那一步（对应 `start_new_context_window`）：

```python
def start_new_context_window(self, reuse_prefix: str) -> None:
    """把整个窗口里的一切丢掉，只重新注入「初始上下文」。

    这一步**不调用任何模型**、**不生成任何摘要** —— 这正是它和「摘要式压缩」
    最本质的区别。
    """
    self.window_number += 1
    self.previous_window_id = self.window_id   # previous 记住刚被丢掉的窗口
    self.window_id = str(uuid.uuid4())         # 发一个新号
    self.reminder_sent = False                 # 两个"只发一次"的开关复位
    self.fallback_sent = False

    # 历史清空，只剩初始上下文 + 一条窗口编号消息
    self.items = [
        Item(self.next_item_id(), "developer", reuse_prefix),
        Item(self.next_item_id(), "developer", self.context_window_fragment()),
    ]
```

跑出来的实际输出（节选真实运行结果；窗口编号每次运行都不同，下面用 `...` 省略）：

**第 2 步 —— 历史增长，每次请求都被收录：**

```text
  + 加入一条 user      后，已用   47 token，剩余  253
  + 加入一条 assistant 后，已用   72 token，剩余  228
  + 加入一条 tool      后，已用  144 token，剩余  156
  + 加入一条 user      后，已用  159 token，剩余  141
  + 加入一条 assistant 后，已用  204 token，剩余   96
  + 加入一条 tool      后，已用  246 token，剩余   54

服务端历史索引里现在有： [{'window_id': 'c95e9ac6-...', 'item_count': 7}]
```

**第 3 步 —— 剩余 54 ≤ 阈值 60，提醒触发；再问一次不重复提醒：**

```text
已用 246，剩余 54，提醒阈值 60 → 该提醒了

注入了提醒：
<context_window_reminder>
你的上下文窗口快满了（只剩 54 tokens）。在开新窗口之前，先用 notes 工具写下：目标、已做的决定、进度、下一步，以及你正在处理的每个用户请求对应的 window id 和 item id。新窗口不会自动带上当前对话。写完调用 new_context。
</context_window_reminder>

再问一次会不会又提醒一遍？答案是 None
（每个窗口只提醒一次，靠 reminder_sent 这个开关挡住；换窗口时才会复位）
```

**第 4.5 步 —— 剩余降到 0，兜底提示触发，但还没到换窗口线：**

```text
  + 又干了一步后，已用 303 token，剩余 0
剩余 == 0（主额度 300 刚好用光）→ 注入兜底提示（兜底 = 最后一道保险，只许写笔记、不许再干活）：
<context_window_reminder>
当前窗口已经用尽。不要继续任务、不要给最终答复。现在只做一件事：往 notes 里写一次检查点，然后调用 new_context。
</context_window_reminder>

但这时**还不换窗口**：token_limit_reached = False，因为已用 303 < 主额度 300 + 缓冲 40 = 340
（这段缓冲就是留给模型把兜底提示要求的最后一份笔记写完的空间）
```

**第 5 步 —— 缓冲用完，强制换窗口，模型视野里只剩两条：**

```text
  + 加入一条 tool      后，已用  320 token，剩余    0
  + 加入一条 assistant 后，已用  348 token，剩余    0

已用 348 >= 主额度 300 + 缓冲 40 → token_limit_reached = True → 强制换窗口
模型也在这时调用了 new_context，工具返回：A new context window will start without summarizing conversation history.

旧窗口 c95e9ac6-... 里的 13 条内容已被丢弃
新窗口编号：a444ed0c-...

新窗口里此刻的全部内容（就是模型的全部视野）：
  [0] developer  | <PERMISSIONS_INSTRUCTIONS> 允许读取工作区、允许运行测试
  [1] developer  | <context_window> / Agent name: /root / First context window id: c95e9ac6-b
```

**第 6 步 —— 靠取件码把原文取回来：**

```text
模型先列出旧窗口里都有什么：
    {'item_id': 'itm_0', 'role': 'developer', 'preview': '<PERMISSIONS_INSTRUCTIONS> 允许读取工作区、允许运行测试'}
    {'item_id': 'itm_1', 'role': 'user', 'preview': '帮我把 summary_prefix 这段文案翻译成中文，另外梳理一下上下文压缩的触发点都有哪些。'}
    {'item_id': 'itm_3', 'role': 'tool', 'preview': '已读取 codex-rs/core/src/compact.rs，共 828 行。关键函数：run_compact_task、run_inline_auto_compact_task、build_compacted_history。已读取 '}
    ...

模型再按 item_id 精确读回它需要的那一条原文（笔记里记的是 itm_3）：
    {'item_id': 'itm_3', 'window_id': 'c95e9ac6-...', 'content': '已读取 codex-rs/core/src/compact.rs，共 828 行。关键函数：run_compact_task、run_inline_auto_compact_task、build_compacted_history。已读取 codex-rs/core/src/session/turn.rs，找到 run_pre_sampling_compact、run_auto_compact、run_sampling_request 三个关键函数。', 'truncated': False}
```

**最后 —— demo 用一段对照实验证明「丢了 vs 没丢」：**

```text
实证：在新窗口的模型视野里搜索旧内容关键词 ——
   搜索 '828 行' → 找不到（已不在模型视野内）
   搜索 '草稿'   → 找不到（已不在模型视野内）
   搜索 '三种策略' → 找不到（已不在模型视野内）

但它在服务端仓库里仍然完好：
   搜索 '828 行' → 找到了 1 条
   搜索 '草稿'   → 找到了 1 条
   搜索 '三种策略' → 找到了 1 条
```

这段对照就是整套机制的本质：**内容不在模型眼前，但也没消失；能不能拿回来，取决于有没有去取。**

---

## 9. 怎么载入，怎么确认真的生效

### 9.1 硬性前置条件（不满足就直接是「关了」的状态）

`Feature::TokenBudget` 本身只是个开关，但要让「按需取回」这套真正能用，
还需要下面条件**同时**成立。这些条件写在
`codex-rs/ext/history-notes/src/extension.rs:45-63`：

```rust
fn update_config(&self, thread_store: &ExtensionData, config: &Config) {
    if config.token_budget.as_ref()
            .is_some_and(|token_budget| token_budget.use_history_notes_extension)   // 条件 1
        && config.model_provider.is_openai()                                        // 条件 2
        && self.auth_manager.current_auth_uses_codex_backend()                       // 条件 3
    {
        thread_store.insert(HistoryNotesExtensionConfig { backend: ... });
    } else {
        thread_store.remove::<HistoryNotesExtensionConfig>();   // 不满足 → 工具直接不注册
    }
}
```

逐条解释：

| 条件 | 含义 | 为什么必须 |
|---|---|---|
| `use_history_notes_extension = true` | 你要显式打开「笔记 + 历史」这套工具 | 默认关。不开就没工具，只有空的窗口 |
| `provider.is_openai()` | provider 是 OpenAI 官方 | `alpha/history/v2/*` 是 codex 后端专有接口，别的兼容接口没有 |
| `current_auth_uses_codex_backend()` | 当前登录方式是 codex 后端鉴权 | 同上，接口要求这种鉴权 |

还有一条启动期硬校验（`codex-rs/core/src/session/mod.rs:720-729`）：
如果打开了 `use_history_notes_extension`，但所选模型**不支持 experimental context**，
会话会直接**启动失败**并报错：

```rust
"features.token_budget.use_history_notes_extension is not supported by model `{model}`;
 disable it or select a model that supports experimental context"
```

也就是说：**不是所有模型都能用这套机制**，模型本身要声明支持。

### 9.2 三条启用途径

**途径 A：手动写配置（最可控）**

在 `config.toml` 里写：

```toml
[features.token_budget]
enabled = true
use_history_notes_extension = true

# 以下是可选项，不写就用模型自带的默认值
reminder_threshold_tokens = 6144          # 剩余多少 token 时提醒写笔记
auto_compact_fallback_buffer_tokens = 16384  # 主额度用光后再宽限多少 token
reminder_message_template = "..."          # 提醒文案，必须含 {n_remaining}
guidance_message = "..."                   # 常驻指引文案
auto_compact_fallback_prompt = "..."       # 兜底文案
```

这段配置能写成表结构，是因为 `FeatureToml` 被定义成「布尔值或配置对象」两种形态
（`codex-rs/features/src/lib.rs:878-884`）：

```rust
#[serde(untagged)]
pub enum FeatureToml<T> {
    Enabled(bool),
    Config(T),
}
```

解析时 `Config` 分支会调用它的 `enabled()` 方法取值，
最终统一塞进功能表（`codex-rs/features/src/lib.rs:836-837`）：

```rust
if let Some(enabled) = self.token_budget.as_ref().and_then(FeatureToml::enabled) {
    entries.insert(Feature::TokenBudget.key().to_string(), enabled);
}
```

字段约束（来自 `codex-rs/core/config.schema.json` 的 `TokenBudgetConfigToml` 定义，
以及 `codex-rs/core/src/config/mod.rs:1212-1219` 的运行时校验）：

- `reminder_threshold_tokens`：整数，最小 1；
- `reminder_message_template`：1~2000 字符，模板里用 `{n_remaining}` 占位；
- `guidance_message`：最多 2000 字符；
- `auto_compact_fallback_prompt`：最多 2000 字符；
- `auto_compact_fallback_buffer_tokens`：整数，最小 1；
- **`auto_compact_fallback_prompt` 一旦设置，`auto_compact_fallback_buffer_tokens` 就必填** ——
  只写提示词不给缓冲会被直接判为配置错误（同文件 `:1212-1217`）。

**途径 B：让模型自己声明（你什么都不用配）**

如果服务端下发的模型信息里带了 `model_messages.token_budget.enabled = true`，
且你**没有**显式配置过 token budget，客户端会自动打开
（`codex-rs/core/src/session/token_budget.rs:122-159` 的 `apply_model_defaults`）。
仓库里打包的 `models-manager/models.json` 共 9 个模型，情况是：
**只有 5 个带 `token_budget` 配置块**（`gpt-6-astra`、`gpt-5.6-sol/terra/luna`、
`gpt-daybreak-blue-latest`），**另外 4 个连块都没有**；
而这 5 个里**只有 `gpt-6-astra` 显式写了 `enabled: false, use_history_notes_extension: false`**，
其余 4 个根本没写这两个字段，是靠 `#[serde(default)]` 默认成 `false` 的
（字段定义见 `codex-rs/protocol/src/openai_models.rs:606-618` 的 `ModelTokenBudgetConfig`）。
所以「默认关闭」这个结论成立，但理由不是「大家都写了 false」，
而是「写了 false 或干脆不写，两种都落到 false」。
运行时从服务端拉取的服务端模型目录（`models_cache`）可能不一样，因此
「生产环境实际默认值」无法从本仓库确定。

**途径 C：experimental context 自动激活（条件最苛刻）**

打开 `context_management`（一套更实验性的上下文管理开关）且满足一整套条件时，
会自动顺带打开 token budget 并强制打开 `use_history_notes_extension`
（`codex-rs/core/src/session/token_budget.rs:13-58`）。条件是：

- `context_management` 开关打开；
- 模型声明 `supports_experimental_context`；
- provider 支持 codex 后端路由、要求 OpenAI 鉴权、且没用自定义 key / bearer / AWS；
- **登录方式是 ChatGPT，且套餐是 Plus / Pro / ProLite 之一**。

```rust
fn experimental_context_is_eligible(auth_mode: AuthMode, plan_type: Option<PlanType>) -> bool {
    auth_mode == AuthMode::Chatgpt
        && matches!(plan_type, Some(PlanType::Plus | PlanType::Pro | PlanType::ProLite))
}
```

### 9.3 怎么确认真的生效了（四个可观测的检查点）

配置写完不代表在跑。按下面顺序验证，每一层都能独立确认：

**检查点 1：功能开关确实开了**

看会话日志里有没有出现 `new_context` / `get_context_remaining` 这两个工具。
它们只在 `Feature::TokenBudget` 打开时注册（`core/src/tools/spec_plan.rs:1207-1210`），
所以「模型看到的工具列表里有没有这两个」是最直接的开关证据。

**检查点 2：窗口消息确实注入了**

看请求体里有没有 `<context_window>` 这段开发者消息。它的注入前提是
「token budget 开着 **且** 模型有明确的上下文窗口大小」
（`codex-rs/core/src/session/world_state.rs:93-98`）——
如果模型的窗口大小未知，这一节**完全不渲染**，此时 `get_context_remaining`
会返回 `"You have unknown tokens left in this context window."`。

**检查点 3：检索工具确实注册了**

看请求体里 tools 列表有没有 `history.list_windows` / `history.list_items` /
`history.read_item` / `history.search_contents` 这四个，以及 `notes.*` 五个。
**如果这九个工具没出现，说明 9.1 的三个条件没同时满足** ——
最常见的原因是用的是第三方兼容接口而不是 OpenAI 官方。

有现成的端到端测试可以直接参考，它就是这么断言的
（`codex-rs/app-server/tests/suite/v2/history_notes_extension.rs:163-180`）。

**检查点 4：服务端真的收录了内容**

看请求头 `x-codex-turn-metadata` 里的 `history_ingest_requested` 字段是不是 `true`
（装配点 `codex-rs/core/src/session/session.rs:713-718`）：

```rust
history_ingest_requested: turn_context.config.token_budget.as_ref()
    .is_some_and(|config| config.use_history_notes_extension).then_some(true),
```

这个字段是 `true`，才意味着服务端会把这轮内容收进可按编号检索的索引。
**它是「按需取回能成立」的真正开关。**

### 9.4 「保证准确」的实际含义

用户问「怎么载入能保证是准确的」。诚实的回答是：**能保证的部分和不能保证的部分要分开看。**

**能保证的：**

- 只要 9.1 的三个条件满足且 `history_ingest_requested = true`，
  内容就会进服务端索引，模型凭编号能取回原文（不是摘要，是原文）。
- 服务端索引里的原文是**只读、最终一致**的。官方文档化的行为：
  未知窗口返回「没有匹配」（history 工具的描述，`ext/history-notes/src/tools.rs:27`）；
  笔记**单文件**上限 100 万 UTF-8 字节（notes 工具的描述，同文件 `:28`）——
  注意这是笔记文件的限制，history 索引那边没有「单文件」这个概念。

**不能保证的（这是这套机制的固有弱点）：**

1. **模型有没有写笔记、笔记写得对不对，无法保证。**
   笔记是模型自己生成的。取件码记漏了，就得多花一轮用 `search_contents` 去猜。
2. **「最终一致」意味着刚发生的内容可能几秒内还查不到**；
   以及 **`[id: ...]` 这个标记由谁打印，在本仓库里找不到生成代码**。
   这两条分别是第 11 节的第 2 条和第 1 条，理由和取证过程都写在那边，这里不重复。
3. **一旦换个 provider 或换用非 codex 后端鉴权，检索能力立刻整体消失。**
   此时窗口照换不误（因为 `Feature::TokenBudget` 是另一套判断），
   但模型**再也没有任何手段把旧内容取回来**。这是最危险的组合。

所以如果要用在生产上，**先按 9.3 的四个检查点逐层确认，尤其是检查点 3 和 4**，
不要只看「配置写进去了」就认为好了。

---

## 10. 边界：哪些是本地做的，哪些是服务端做的

这一节回答「原文到底还在不在」，因为它决定了第 9.4 节那些风险的实际严重程度。

### 10.1 本地磁盘：原文**完整保留**，只追加、不删改

rollout 文件以**追加模式**打开（`codex-rs/rollout/src/recorder.rs:1734-1742`）：

```rust
let mut file = std::fs::OpenOptions::new()
    .read(true)
    .append(true)          // ← 只追加
    .create(true)
    .open(path)?;
```

压缩发生时，客户端往这个文件**追加一行**压缩记录，而不是重写文件
（`codex-rs/core/src/session/mod.rs:3981-4059`）：

```rust
let mut compacted_item = CompactedItem {
    message: metadata.message,
    replacement_history: Some(items.clone()),   // 替换后的整套历史，整份抄进这一行记录
    window_number: Some(metadata.window_number),
    first_window_id: ...,
    previous_window_id: ...,
    window_id: ...,
    ...
};
let mut rollout_items = vec![RolloutItem::Compacted(compacted_item)];
...
self.persist_rollout_items(&rollout_items).await;
```

文件路径规则（`codex-rs/rollout/src/recorder.rs:1701-1722` +
`codex-rs/rollout/src/rollout_file_name.rs:62-74`）：

```text
$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<时间戳>-<thread_id>.jsonl
```

每行是一个 JSON，形态大致是（`ordinal` = 这行在文件里的序号，用来排序；
`replacement_history` = 压缩后替换进去的那整套历史）：

```json
{"timestamp":"...","ordinal":3,"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"..."}]}}
{"timestamp":"...","ordinal":5,"type":"compacted","payload":{"message":"","replacement_history":[...],"window_number":2,"first_window_id":"...","previous_window_id":"...","window_id":"..."}}
```

**所以：磁盘上压缩前的那些行一直在。** 唯一的「删除」是用户显式删线程，
以及冷文件被整体重编码成 zstd 压缩格式 `.jsonl.zst`
（无损压缩，读的时候透明解压，不是内容裁剪）。

### 10.2 内存里：旧历史**确实被丢掉了**

`replace_compacted` 把 `self.items` 整个换掉（第 4.7 节），而且结构体里**没有**
「压缩前 items」这个字段可以找回。压缩记录（落盘 = 写进磁盘上的 rollout 文件）里
确实留了两样东西：

- Guardian 审查模式下的**压缩前非上下文条目副本**（有容量上限：每类 128 条 / 4 MiB，
  见 `codex-rs/guardian-context/src/history.rs:21-24`），
  但那份副本**不会进模型的请求**；
- `retained_context` 字段（`codex-rs/history/src/lib.rs:190-207`，落盘见
  `codex-rs/core/src/session/mod.rs:4033`）：有界地保留「已验证答复」和用户消息
  （`codex-rs/history/src/retained_context.rs:139-152`），
  用于回滚和授权还原。

这两样都和「模型能不能读到旧原文」无关 —— 它们不会出现在模型视野里。

### 10.3 服务端：这才是「按需取回」真正依靠的地方

**客户端没有任何上传代码。** 全仓搜 `alpha/history` 只在 `ext/history-notes/` 出现，
没有 uploader。客户端做的只有一件事：在请求元数据里声明 `history_ingest_requested = true`，
**由服务端自己把这轮请求的内容收进去**。

### 10.4 三份存储的关系图

```
                          ┌─────────────────────────────────────┐
                          │  服务端历史索引（可按编号检索）        │
   压缩后模型凭编号 ──────▶ │  索引键 = (window_id, item_id)       │  ◀── 模型能读的唯一一份
   取回原文                 │  由 history_ingest_requested 触发收录 │
                          └─────────────────────────────────────┘
                                          ▲
                                          │ 客户端只发"请收录"的声明
                                          │
   ┌──────────────────┐      ┌────────────┴──────────────┐
   │ 内存中的历史       │      │  rollout JSONL 文件        │
   │ 压缩后整体被替换   │      │  压缩前后全部保留、只追加   │
   │ ✗ 模型读不到       │      │ ✗ 模型也读不到（没工具）    │
   └──────────────────┘      └───────────────────────────┘
        左框：原文已被替换掉，不在内存里了
        右框：原文仍在磁盘上，但模型没有工具去读
        两者模型都够不着；模型唯一能读的是上面那份服务端索引
```

**三份存储里，模型只能读服务端那一份。** 这是理解整套机制的关键，
也是为什么第 9.1 节那三个条件如此重要 —— 它们决定的就是「服务端那份到底有没有」。

---

## 11. 存疑与风险（写文档时没能证实的部分）

按 AGENTS.md 的要求，不确定的地方必须明确标出来，不能当事实讲。

1. **`[id: ...]` 标记的生成方无法证实。**
   全仓（`.rs` 文件）搜不到这个字面量的生成点，它只出现在工具参数描述和模型文案里。
   最合理的推断是**服务端在收录时插入**，但本仓库无法证明。
   这直接影响「模型能不能真的用 item id 精准取回」这个能力评估。

2. **服务端 `alpha/history/v2/*` 和 `alpha/notes/v2/*` 的实现完全不可见。**
   数据保留多久、是否真的存全文、跨会话可见范围 —— 这些都是 closed backend 行为。
   仓库里能确认的只有工具描述里文档化的部分：只读、最终一致、未知窗口返回无匹配。

3. **`history_ingest_requested` 到底让服务端做了什么，只有命名证据。**
   字段名、装配位置、以及它和检索工具同时受 `use_history_notes_extension` 控制 ——
   这些都强烈指向「服务端据此收录」，但没有服务端代码可以确认。

4. **打包的模型目录里 token budget 默认是关的，但生产可能不同。**
   `codex-rs/models-manager/models.json` 共 9 个模型，只有 5 个带 `token_budget`
   配置块，其中只有 `gpt-6-astra` 显式写了
   `"enabled": false, "use_history_notes_extension": false`；
   另外 4 个（`gpt-5.6-sol/terra/luna`、`gpt-daybreak-blue-latest`）没写这两个字段，
   靠 `#[serde(default)]` 落到 false（`codex-rs/protocol/src/openai_models.rs:606-618`）；
   剩下 4 个模型连 `token_budget` 块都没有。
   所以「默认关闭」成立，但措辞应该是「不写或写 false，两种都等于 false」。
   运行时从服务端拉取的模型目录（`models_cache`）可能不一样，因此
   「生产环境实际默认值」无法从本仓库确定。

5. **token 数字是估算混合值，不是精确值。**
   本地估算函数自己标注为
   `coarse lower bound, not a tokenizer-accurate count`
   （`codex-rs/core/src/context_manager/history.rs:444-445`，
   就在 `estimate_token_count` 上方；`:816-822` 是另一个相关函数
   `estimate_item_token_count` 的注释，不是这句话的出处）。
   所以提醒里显示的「还剩多少 token」与实际 API 计数存在偏差。
   还要注意「下界」的方向：它倾向于**少算**已用量，也就是剩余被高估、
   提醒和换窗口只会延后，不会提前。

6. **`previous_window_id` 只保留相邻一个。**
   `advance()` 只覆盖上一枚编号。更早的窗口编号一旦离开
   current/previous，就不再出现在模型可见文本里了 ——
   模型要找回更早的窗口，只能靠 `history.list_windows`（服务端接口，本仓库看不到分页细节）。

7. **同一个线程树里不同 agent 可以走不同策略。**
   Guardian（自动安全审查子代理）会**强制关闭** token budget，
   让审查仍然走摘要式压缩（`codex-rs/core/src/guardian/reviewer_config.rs:46`）。
   这是刻意的：摘要式压缩才能保留被审查的动作和证据原文 ——
   这句话的代码依据在 `codex-rs/core/src/session/turn.rs:716-718` 的注释
   （"Only summarizing compaction preserves the action and evidence;
   token-budget resets must fail closed and retire the reviewer."）。
   反过来，§5 表格里「Guardian 审查会话内部换窗」那一行，
   只有在 disable 失败时才可能生效，两处互为边界情况。
   所以「这个会话走哪条路」不能一概而论。

8. **`<context_window>` 是否每个回合都重发，我的结论是「只在全量注入时新生成一次」。**
   依据是 `render_diff` 的差分逻辑（`render_diff` = 把新快照和上一版快照做差分，
   只把变化的部分渲染出去）加上测试注释，不是端到端可观测证据。

9. **这套机制的设计意图可以从提交信息确认。**
   `git log -1 daa48072f4`（`Add history and notes tools for token-budget sessions (#39827)`）写着：
   > Token-budget sessions need a way to recover prior conversation context and preserve
   > working state across context-window transitions.

   这是「按需取回」设计意图的官方表述，可作为本文立论的一手依据。

---

## 12. 关键文件索引

| 关注点 | 文件 |
|---|---|
| **压缩策略分发（三条路的岔口）** | `codex-rs/core/src/session/turn.rs:1405-1463`（`run_auto_compact`）<br>`codex-rs/core/src/tasks/compact.rs:36-68`（手动） |
| 触发时机（6 个自动调用点 + 手动入口） | `codex-rs/core/src/session/turn.rs` 的 `:344`、`:613`、`:724`、`:1256`、`:1338`、`:1386`（调用点）<br>`:323-353`（Guardian 回合开始前）、`:600-640`（回合中途）、`:706-739`（采样报超窗） |
| **换窗口式压缩主体** | `codex-rs/core/src/compact_token_budget.rs` |
| 换窗口的实际动作 | `codex-rs/core/src/session/mod.rs:4446-4497`（`start_new_context_window`） |
| 内存历史替换 | `codex-rs/core/src/context_manager/history.rs:509-525`（`replace_compacted`） |
| 窗口编号与轮转 | `codex-rs/core/src/state/auto_compact_window.rs`（`advance` 在 `:77-85`） |
| 阈值计算 / 换窗口判定 | `codex-rs/core/src/session/context_window.rs:52-121`（`token_limit_reached` 在 `:18`） |
| 默认阈值 = 窗口 90% | `codex-rs/protocol/src/openai_models.rs:521-532` |
| 提醒注入时机 | `codex-rs/core/src/session/token_budget.rs:161-224`（`maybe_record`） |
| 启动激活逻辑 | `codex-rs/core/src/session/token_budget.rs:13-159` |
| **注入模型的窗口消息** | `codex-rs/core/src/context/token_budget_context.rs` |
| 提示词标签常量 | `codex-rs/protocol/src/protocol.rs:137-140` |
| **检索工具（按需取回）** | `codex-rs/ext/history-notes/src/tools.rs` |
| 工具门禁（三个条件） | `codex-rs/ext/history-notes/src/extension.rs:45-63` |
| 服务端接口调用 | `codex-rs/ext/history-notes/src/backend.rs:29-92` |
| `history_ingest_requested` 装配 | `codex-rs/core/src/session/session.rs:713-718` |
| `new_context` / `get_context_remaining` 注册 | `codex-rs/core/src/tools/spec_plan.rs:1207-1210` |
| `new_context` 处理器 | `codex-rs/core/src/tools/handlers/new_context_window.rs` |
| **本地摘要压缩** | `codex-rs/core/src/compact.rs` |
| 摘要提示词原文 | `codex-rs/prompts/templates/compact/prompt.md`、`summary_prefix.md` |
| 摘要片段类型 | `codex-rs/core/src/context/compaction_summary.rs` |
| **远程 V2 压缩** | `codex-rs/core/src/compact_remote_v2.rs`、`compact_remote_v2_attempt.rs` |
| 远程压缩前的输出截断 | `codex-rs/core/src/compact_remote_history.rs` |
| provider 能力判定 | `codex-rs/model-provider/src/provider.rs:353-366` |
| 功能开关定义与默认值 | `codex-rs/features/src/lib.rs:1622-1633`、`:878-884` |
| 配置结构定义 | `codex-rs/features/src/feature_configs.rs:317-350` |
| 配置项约束（真实 schema） | `codex-rs/core/config.schema.json`（搜 `TokenBudgetConfigToml`） |
| 生产环境文案（提醒/指引/兜底） | `codex-rs/models-manager/models.json`（搜 `token_budget`） |
| rollout 追加写 | `codex-rs/rollout/src/recorder.rs:1734-1742` |
| 压缩记录落盘（落盘 = 写进磁盘上的 rollout 文件） | `codex-rs/core/src/session/mod.rs:3981-4059` |
| rollout 路径规则 | `codex-rs/rollout/src/rollout_file_name.rs:62-74` |
| 压缩事件类型 | `codex-rs/protocol/src/items.rs:484-503`（`ContextCompactionItem`） |
| 压缩记录里的保留上下文 | `codex-rs/history/src/lib.rs:190-207`、`codex-rs/history/src/retained_context.rs:139-152` |
| **三级时序的端到端证据** | `codex-rs/core/tests/suite/token_budget.rs:1411-1571`（提醒 → 兜底 → 换窗） |
| **行为规范（测试即文档）** | `codex-rs/core/tests/suite/token_budget.rs`<br>`codex-rs/core/tests/suite/compact.rs`<br>`codex-rs/core/tests/suite/compact_remote.rs`<br>`codex-rs/app-server/tests/suite/v2/history_notes_extension.rs` |
| 请求体快照（最直观） | `codex-rs/core/tests/suite/snapshots/all__suite__token_budget__token_budget_new_context_window_tool_full_context.snap` |

---

## 附：一句话总结

codex 当前**同时存在**两代上下文压缩：

- **摘要式**（默认生效）：压缩时让模型写一段概括，替换掉原始历史。
  模型回复正文、工具输出全部从模型视野里消失，只剩用户说过的话 + 一段摘要。
- **换窗口式**（`Feature::TokenBudget`，默认关闭，开发中）：完全不生成摘要，
  把整个窗口丢掉，只留初始上下文和一张「取件码」，模型需要时用
  `history.read_item` 凭编号去服务端把原文取回来。

所以「是否改成按需加载了」的答案是：**机制已经存在并且完整，但还不是默认路径**；
而且它的「可取回」依赖服务端索引，**换一个 provider 就会整体失效** ——
旧窗口内容会真的变成取不回来的东西。
