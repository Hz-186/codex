# codex Instructions

Use this file as the local operating guide for the current codebase. Prefer the code and the current CLAUDE.md over any older convention or remembered project shape.

## Before Any Analysis — Code Graph First (MANDATORY)
- **Always read `CODEGRAPH.md` at the repo root FIRST** before analyzing, modifying, or reviewing any part of this project. It is the maintained code map: the top-level and `codex-rs/` directory index, the multi-entry (tui / app-server / exec / cli-subcommand) architecture, the Session → `run_turn` engine chain and the tool dispatch chain, the config layering order, the core data flows, the pitfall list, and a jump table of high-frequency entry points.
- After locating the involved modules via `CODEGRAPH.md`, read the relevant code's overall call chain with a code-graph approach (symbol usage/reference tracing, value-flow tracing, or an exploration subagent) — entry → middle layers → persistence/output — before drawing conclusions or making edits. Do not start with blind repo-wide searches: this workspace has ~150 crates and an unfiltered `rg` will drown you.
- Always determine **which entry the task belongs to** (`codex-rs/tui`, `codex-rs/app-server`, `codex-rs/exec`, a `codex-rs/cli` subcommand, or the shared engine in `codex-rs/core`) before editing; the TUI no longer links `codex-core` directly — it drives an in-process app-server over the app-server protocol, so an engine-side change that has no protocol method will never reach the UI, and a TUI-side change that reaches for core types directly is rejected by the `verify_tui_core_boundary.py` CI check. Mind the easily-confused names: the `codex-cli/` npm wrapper vs the `codex-rs/cli/` Rust crate, `codex-core-api` vs `codex-api`, `codex-rs/tools/` (definitions) vs `codex-rs/core/src/tools/` (implementations).
- If project structure changes significantly (new top-level dirs, entry convergence, port/service changes), update `CODEGRAPH.md` in the same change.

## CodeGraph Index Sync (MANDATORY)
- The local CodeGraph index lives in `.codegraph/` (machine-local, git-ignored). Whenever ANY code change has been made (edit, create, delete, rename), run `codegraph sync` at the repo root before the turn ends so the index stays current. This is mandatory after every change batch — do not skip it or defer it to the user.
- If `codegraph` is not on PATH (expected at `D:\Users\hongze01.zhang\AppData\Local\codegraph\current\bin\codegraph.cmd` on this machine), report it and continue; never block the task on a missing index.

## Git Workflow (MANDATORY)
- **All work happens on the `study` branch.** If it does not exist locally, create it (`git checkout -b study`). Never commit directly to `main` or any other branch.
- **Push only to `origin`** (https://github.com/Hz-186/codex.git), e.g. `git push origin study`. **Never push to `upstream`** (the openai/codex source repo) or open PRs against it.
- Do not rebase/force-push shared branches without explicit user instruction.

## Core Stance
- Treat legacy code as liability, not as a compatibility target.
- Prefer deletion over shims, deprecated branches, wrapper APIs, and dual-track migration notes.
- If old and new implementations coexist, converge to one path unless an external contract forces compatibility.
- Remove dead tests, commented-out code, stale docs, and "move later" notes instead of preserving them.
- Reduce public surface area when a helper can be made private or internal.
- Keep refactors centered on the owning abstraction, not on adjacent compatibility layers.

## Current stack
- Rust (edition 2024) in a single Cargo workspace `codex-rs/` (147 explicit members, plus a few implicit ones pulled in by path dependencies); toolchain pinned to `1.95.0` in `codex-rs/rust-toolchain.toml`.
- Async runtime Tokio; HTTP via `reqwest`; model traffic over the OpenAI Responses API with SSE and WebSocket transports (`codex-rs/codex-api`, `codex-rs/codex-client`).
- Terminal UI built with ratatui in the `codex-tui` crate, with 1000+ insta snapshots.
- MCP via the `rmcp` crate (pinned `=3.2.0`) in `codex-rs/rmcp-client` + `codex-rs/codex-mcp`.
- Persistence: JSONL rollout session files (`codex-rs/rollout`), a SQLite state DB (`codex-rs/state`), behind storage-neutral interfaces (`codex-rs/thread-store`).
- Sandboxing per platform: Seatbelt on macOS, Landlock/seccomp + bubblewrap on Linux, restricted token / MXC on Windows (`codex-rs/sandboxing` and the platform crates).
- Dual build: Cargo remains the source of truth for crates and features, Bazel provides hermetic PR verification and release artifacts (`codex-rs/docs/bazel.md`).
- Node/pnpm side: `codex-cli/` (npm wrapper `@openai/codex`) and `sdk/typescript`; Python side: `sdk/python` (`openai-codex`) + `sdk/python-runtime`.

## Code Layout to Expect
- `codex-rs/` — the entire Rust engine and every binary. Core crates: `core/` (the `codex-core` engine: Session/Turn loop, tool routing, model client, context, rollout), `tui/`, `app-server/` + `app-server-protocol/`, `cli/`, `exec/`, `exec-server/`, `protocol/`, `config/` + `config-schema/`, `tools/`, `sandboxing/` and the Linux/Windows sandbox crates, `codex-mcp/` + `rmcp-client/`, and the persistence cluster (`rollout`, `state`, `thread-store`, `history`, `message-history`, `rollout-trace`).
- `codex-rs/ext/` — the extension mechanism: ~15 crates registering contributor objects (guardian review, skills, memories, goal, MCP, image generation, web search) into `ExtensionRegistry`.
- `codex-cli/` — the **npm wrapper** `@openai/codex` (`codex-cli/bin/codex.js`), not the Rust `codex-cli` crate in `codex-rs/cli/`. It only picks the platform package and spawns the native binary.
- `sdk/typescript/`, `sdk/python/`, `sdk/python-runtime/` — SDKs; the Python SDK's protocol types are generated from the app-server schema.
- `docs/` — mostly redirect stubs to developers.openai.com; real in-repo API docs live under `codex-rs/docs/` and `codex-rs/app-server/README.md`.
- `scripts/` (build/release automation, `codex_package/`), `tools/` (the `argument-comment-lint` Dylint lint), `bazel/` + `third_party/` + `patches/`, `.github/` (CI, release, and the Codex agent prompts/skills in `.codex/skills/`).
- Two directories share the name "cli" and two share "api": the npm `codex-cli/` vs the Rust `codex-rs/cli/`, and `codex-rs/core-api/` (thread-management facade over core) vs `codex-rs/codex-api/` (Responses API surface). Also note `codex-rs/windows-sandbox-rs/` is crate `codex-windows-sandbox`, and `codex-rs/utils/path-utils/` is crate `codex-utils-path`.

## 注释规范（强制）

本仓库的注释以「让没读过这段代码的人一遍看懂」为唯一目标，统一执行以下规则（本规范源自 ragflow study 分支的实践，风格范本见 ragflow 仓库 `rag/nlp/__init__.py`；本项目第一批注释完成后，在此登记本项目自己的范本文件）：

1. **函数开头注释只写三样东西**：
   - 一句话说明这个函数是干什么的（用大白话，可以加一个「—— 某某器/某某工」的短比喻）；
   - **每个传入参数的含义，以及它「长得什么样子」**：用代码块/缩进给出真实的数据结构示例（例如 Python 的 dict/list 示例），让读者不用跳去别处就能想象出数据的实际形态；
   - 返回值「长得什么样子」（同样给出真实结构示例）。
2. **步骤说明一律写在函数体内对应代码的旁边，且必须标注真实数据长相**：
   - 每一步做什么、为什么这么做，写成紧贴该步代码的行内/块内注释。**禁止**把一个函数的所有步骤集中堆在函数开头写成一大段「流程总览」。唯一例外：某一段逻辑本身技术含量很高、三言两语说不清，可以在那一小段代码上方多写几行把它讲透；
   - **行内必须带真实数据示例**：关键数据处理与流转步骤，必须直接在旁边用 `[]`（列表）、`{}`（字典/JSON 对象）等具体结构展示输入、产出数据的真实长相（例如 `输入: [{"id": "c1", ...}]`，`输出: [[{"chunk_id": "c1", ...}]]`），让读者一眼看透数据如何流动与变形。
3. **语言要求**：注释用中文，通俗易懂；禁止「这个/那个」式指代、黑话、不加解释的专有名词堆砌。原有英文注释翻译成中文；如果直译后仍然难懂，就改写成能让人看懂的版本。**只改注释，绝不改动任何代码逻辑**。
4. **批量改写必须分批提交**：一次要改的注释太多时，先列一个待改函数清单（list），然后分多次编辑，每次只替换一部分，保证每次改动可审、可回滚。
5. **改完必须自查**：每个文件改完后，检查是否还有「函数头大段步骤说明」残留、是否有未翻译的英文注释、关键步骤是否已带上 `[]` / `{}` 真实数据结构示例、是否有被误改的代码；必要时用子代理（subagent）复查，发现问题继续修，直到达标。

## Working Rules
- When reviewing documentation or code, inspect the full affected path and report all verifiable findings in one review; do not return after only a few findings and expose further issues in later rounds.
- When handling review comments, independently verify each substantive claim against the current code or tests before accepting, rejecting, or acting on it.
- Before editing, inspect the nearest code path that actually owns the behavior.
- Keep changes small and local unless the task is explicitly a broader refactor.

## Commands
Every `just` recipe runs with `codex-rs/` as its working directory (the `justfile` sets `working-directory`), so run these from the repo root. Sources: `justfile`, `docs/install.md`, `codex-rs/docs/bazel.md`. Prerequisites: `just`, `cargo-nextest`, `cargo-insta`, `dotslash`, `rg`, Python 3, `uv` (`just install` bootstraps the toolchain); Node ≥ 22 with pnpm `10.34.5` for the JS side; Bazel `9.0.0` for Bazel paths.

- Format: `just fmt` (five formatter groups in parallel: just, `cargo fmt`, buildifier via dotslash, Python SDK ruff, scripts ruff); check with `just fmt-check`.
- Lint/fix: `just fix -p <crate>` (wraps `cargo clippy --fix --tests`), `just clippy -p <crate>` for clippy only, `just argument-comment-lint` for the custom Dylint lint.
- Test: `just test -p <crate>` (wraps `cargo nextest run`; the repo forbids calling `cargo test` directly). Snapshots: `just test -p codex-tui`, then `cargo insta pending-snapshots -p codex-tui` / `cargo insta accept -p codex-tui`.
- Schemas: `just write-config-schema` (regenerates `codex-rs/core/config.schema.json`, required after any `ConfigToml` change), `just write-app-server-schema [--experimental]`, `just write-hooks-schema`.
- Bazel: `just bazel-lock-update` (required after any `Cargo.toml`/`Cargo.lock` change, commit `MODULE.bazel.lock`), `just bazel-lock-check`, `just build-for-release`.
- Run locally: `just codex <args>`, `just exec <args>`, `just log` (tails the state SQLite DB via `codex-cli --bin logs_client`).
- Gotcha: `just write-app-server-schema` is not a schema binary — it drives the `#[ignore]`d test `schema_fixtures_tests::write_schema_fixtures_from_env` through `app-server-protocol/scripts/write_schema_fixtures.py`.

## Validation Preference
- Run the narrowest relevant test, lint, or build command after a change; prefer a single test file over the suite.
- Rust smoke check for the touched crate: `cargo check -p <crate>` (e.g. `cargo check -p codex-core`), then `just test -p <crate>` for behavior.
- For comment-only changes (注释规范), tests are not required, but the file must still parse cleanly (`cargo check -p <crate>` where the change is Rust).
- For JS/TS changes: `pnpm run format` plus the package's own lint/test (e.g. `pnpm --filter @openai/codex-sdk lint`). For Python SDK changes: run its ruff/pytest via uv as CI does.
- Do not default to the full test suite.

---

# Rust/codex-rs

In the codex-rs folder where the rust code lives:

- Crate names are prefixed with `codex-`. For example, the `core` folder's crate is named `codex-core`
- When using format! and you can inline variables into {}, always do that.
- Install any commands the repo relies on (for example `just`, `rg`, or `cargo-insta`) if they aren't already available before running instructions here.
- Never add or modify any code related to `CODEX_SANDBOX_NETWORK_DISABLED_ENV_VAR` or `CODEX_SANDBOX_ENV_VAR`.
  - You operate in a sandbox where `CODEX_SANDBOX_NETWORK_DISABLED=1` will be set whenever you use the `shell` tool. Any existing code that uses `CODEX_SANDBOX_NETWORK_DISABLED_ENV_VAR` was authored with this fact in mind. It is often used to early exit out of tests that the author knew you would not be able to run given your sandbox limitations.
  - Similarly, when you spawn a process using Seatbelt (`/usr/bin/sandbox-exec`), `CODEX_SANDBOX=seatbelt` will be set on the child process. Integration tests that want to run Seatbelt themselves cannot be run under Seatbelt, so checks for `CODEX_SANDBOX=seatbelt` are also often used to early exit out of tests, as appropriate.
- Always collapse if statements per https://rust-lang.github.io/rust-clippy/master/index.html#collapsible_if
- Always inline format! args when possible per https://rust-lang.github.io/rust-clippy/master/index.html#uninlined_format_args
- Use method references over closures when possible per https://rust-lang.github.io/rust-clippy/master/index.html#redundant_closure_for_method_calls
- Avoid bool or ambiguous `Option` parameters that force callers to write hard-to-read code such as `foo(false)` or `bar(None)`. Prefer enums, named methods, newtypes, or other idiomatic Rust API shapes when they keep the callsite self-documenting.
- When you cannot make that API change and still need a small positional-literal callsite in Rust, follow the `argument_comment_lint` convention:
  - Use an exact `/*param_name*/` comment before opaque literal arguments such as `None`, booleans, and numeric literals when passing them by position.
  - A method's sole non-self argument is exempt when the method and parameter names match, such as `.enabled(false)` for `fn enabled(&self, enabled: bool)`.
  - Do not add these comments for string or char literals unless the comment adds real clarity; those literals are intentionally exempt from the lint.
  - The parameter name in the comment must exactly match the callee signature.
  - You can run `just argument-comment-lint` to run the lint check locally. This is powered by Bazel, so running it the first time can be slow if Bazel is not warmed up, though incremental invocations should take <15s. Most of the time, it is best to update the PR and let CI take responsibility for checking this (or run it asynchronously in the background after submitting the PR). Note CI checks all three platforms, which the local run does not.
- When possible, make `match` statements exhaustive and avoid wildcard arms.
- Newly added traits should include doc comments that explain their role and how implementations are expected to use them.
- Discourage both `#[async_trait]` and `#[allow(async_fn_in_trait)]` in Rust traits.
  - Prefer native RPITIT trait methods with explicit `Send` bounds on the returned future, as in `3c7f013f9735` / `#16630`.
  - Preferred trait shape:
    `fn foo(&self, ...) -> impl std::future::Future<Output = T> + Send;`
  - Implementations may still use `async fn foo(&self, ...) -> T` when they satisfy that contract.
  - Do not use `#[allow(async_fn_in_trait)]` as a shortcut around spelling the future contract explicitly.
- When writing tests, prefer comparing the equality of entire objects over fields one by one.
- Do not add tests for values that are statically defined.
- Do not add negative tests for logic that was removed.
- Do not add general product or user-facing documentation to the `docs/` folder. The official Codex documentation lives elsewhere. The exception is app-server API documentation, which is covered by the app-server guidance below.
- Prefer private modules and explicitly exported public crate API.
- If you change `ConfigToml` or nested config types, run `just write-config-schema` to update `codex-rs/core/config.schema.json`.
- When working with MCP tool calls, prefer using `codex-rs/codex-mcp/src/mcp_connection_manager.rs` to handle mutation of tools and tool calls. Aim to minimize the footprint of changes and leverage existing abstractions rather than plumbing code through multiple levels of function calls.
  - NOTE (study fork): this path is stale in the upstream text. The connection manager now lives at `codex-rs/codex-mcp/src/connection_manager.rs` (with a `connection_manager/` submodule directory); `mcp_connection_manager.rs` does not exist in this tree.
- Do not call `reset_client_session` unnecessarily; let the incremental check logic decide whether to reuse the previous request.
- If you change Rust dependencies (`Cargo.toml` or `Cargo.lock`), run `just bazel-lock-update` from the
  repo root to refresh `MODULE.bazel.lock`, and include that lockfile update in the same change. CI
  verifies lockfile drift.
- Bazel does not automatically make source-tree files available to compile-time Rust file access. If
  you add `include_str!`, `include_bytes!`, `sqlx::migrate!`, or similar build-time file or
  directory reads, update the crate's `BUILD.bazel` (`compile_data`, `build_script_data`, or test
  data) or Bazel may fail even when Cargo passes.
- Do not create small helper methods that are referenced only once.
- For tracing async work, instrument the function or method definition with
  `#[tracing::instrument(...)]` instead of attaching spans to futures with
  `.instrument(...)` at call sites. Before adding instrumentation, check whether the callee—or
  the implementation method it immediately delegates to—is already instrumented.
- Avoid large modules:
  - Prefer adding new modules instead of growing existing ones.
  - Target Rust modules under 500 LoC, excluding tests.
  - If a file exceeds roughly 800 LoC, add new functionality in a new module instead of extending
    the existing file unless there is a strong documented reason not to.
  - This rule applies especially to high-touch files that already attract unrelated changes, such
    as `codex-rs/tui/src/app.rs`, `codex-rs/tui/src/bottom_pane/chat_composer.rs`,
    `codex-rs/tui/src/bottom_pane/footer.rs`, `codex-rs/tui/src/chatwidget.rs`,
    `codex-rs/tui/src/bottom_pane/mod.rs`, and similarly central orchestration modules.
  - When extracting code from a large module, move the related tests and module/type docs toward
    the new implementation so the invariants stay close to the code that owns them.
  - Avoid adding new standalone methods to `codex-rs/tui/src/chatwidget.rs` unless the change is
    trivial; prefer new modules/files and keep `chatwidget.rs` focused on orchestration.
- When running Rust commands (e.g. `just fix` or `just test`) be patient with the command and never try to kill them using the PID. Rust lock can make the execution slow, this is expected.

Run `just fmt` (in the `codex-rs` directory) automatically after you have finished making code changes anywhere in this repository; do not ask for approval to run it. Additionally, run the tests:

1. Do not run `cargo test` directly. Use `just test` so test execution follows the repo defaults.
2. Run the test for the specific project that was changed. For example, if changes were made in `codex-rs/tui`, run `just test -p codex-tui`.
3. Once those pass, if any changes were made in common, core, or protocol, run the complete test suite with `just test`. Avoid `--all-features` for routine local runs because it expands the build matrix and can significantly increase `target/` disk usage; use it only when you specifically need full feature coverage. project-specific or individual tests can be run without asking the user, but do ask the user before running the complete test suite.

Before finalizing a large change to `codex-rs`, run `just fix -p <project>` (in `codex-rs` directory) to fix any linter issues in the code. Prefer scoping with `-p` to avoid slow workspace‑wide Clippy builds; only run `just fix` without `-p` if you changed shared crates. Do not re-run tests after running `fix` or `fmt`.

## The `codex-core` crate

Over time, the `codex-core` crate (defined in `codex-rs/core/`) has become bloated because it is the largest crate, so it is often easier to add something new to `codex-core` rather than refactor out the library code you need so your new code neither takes a dependency on, nor contributes to the size of, `codex-core`.

To that end: **resist adding code to codex-core**!

Particularly when introducing a new concept/feature/API, before adding to `codex-core`, consider whether:

- There is an existing crate other than `codex-core` that is an appropriate place for your new code to live.
- It is time to introduce a new crate to the Cargo workspace for your new functionality. Refactor existing code as necessary to make this happen.

Likewise, when reviewing code, do not hesitate to push back on PRs that would unnecessarily add code to `codex-core`.

## Code Review Rules

### Crate API surface

Keep crate API surfaces as small as possible. Avoid proliferating test-only helpers.

### Model visible context

Codex maintains a context (history of messages) that is sent to the model in inference requests.

1. No history rewrite - the context must be built up incrementally.
2. Avoid frequent changes to context that cause cache misses.
3. No unbounded items - everything injected in the model context must have a bounded size and a hard cap.
4. No items larger than 10K tokens.
5. Highlight new individual items that can cross >1k tokens as P0. These need an additional manual review.
6. All injected fragments must be defined as structs in `core/context` and implement ContextualUserFragment trait

### Breaking changes

Search for breaking changes in external integration surfaces:

- app-server APIs
- raw response item events (`rawResponseItem/*`), even while experimental
- CLI parameters
- configuration loading
- resuming sessions from existing rollouts

### Test authoring guidance

For agent changes prefer integration tests over unit tests. Integration tests are under `core/suite` and use `test_codex` to set up a test instance of codex.

Features that change the agent logic MUST add an integration test:

- Provide a list of major logic changes and user-facing behaviors that need to be tested.

If unit tests are needed, put them in a dedicated test file (\*\_tests.rs).
Avoid test-only functions in the main implementation.

Check whether there are existing helpers to make tests more streamlined and readable.

### Change size guidance (800 lines)

Unless the change is mechanical the total number of changed lines should not exceed 800 lines.
For complex logic changes the size should be under 500 lines.

If the change is larger, explore whether it can be split into reviewable stages and identify the smallest coherent stage to land first.
Base the staging suggestion on the actual diff, dependencies, and affected call sites.

## TUI style conventions

See `codex-rs/tui/styles.md`.

## TUI code conventions

- Use concise styling helpers from ratatui’s Stylize trait.
  - Basic spans: use "text".into()
  - Styled spans: use "text".red(), "text".green(), "text".magenta(), "text".dim(), etc.
  - Prefer these over constructing styles with `Span::styled` and `Style` directly.
  - Example: patch summary file lines
    - Desired: vec!["  └ ".into(), "M".red(), " ".dim(), "tui/src/app.rs".dim()]

### TUI Styling (ratatui)

- Prefer Stylize helpers: use "text".dim(), .bold(), .cyan(), .italic(), .underlined() instead of manual Style where possible.
- Prefer simple conversions: use "text".into() for spans and vec![…].into() for lines; when inference is ambiguous (e.g., Paragraph::new/Cell::from), use Line::from(spans) or Span::from(text).
- Computed styles: if the Style is computed at runtime, using `Span::styled` is OK (`Span::from(text).set_style(style)` is also acceptable).
- Avoid hardcoded white: do not use `.white()`; prefer the default foreground (no color).
- Chaining: combine helpers by chaining for readability (e.g., url.cyan().underlined()).
- Single items: prefer "text".into(); use Line::from(text) or Span::from(text) only when the target type isn’t obvious from context, or when using .into() would require extra type annotations.
- Building lines: use vec![…].into() to construct a Line when the target type is obvious and no extra type annotations are needed; otherwise use Line::from(vec![…]).
- Avoid churn: don’t refactor between equivalent forms (Span::styled ↔ set_style, Line::from ↔ .into()) without a clear readability or functional gain; follow file‑local conventions and do not introduce type annotations solely to satisfy .into().
- Compactness: prefer the form that stays on one line after rustfmt; if only one of Line::from(vec![…]) or vec![…].into() avoids wrapping, choose that. If both wrap, pick the one with fewer wrapped lines.

### Text wrapping

- Always use textwrap::wrap to wrap plain strings.
- If you have a ratatui Line and you want to wrap it, use the helpers in tui/src/wrapping.rs, e.g. word_wrap_lines / word_wrap_line.
- If you need to indent wrapped lines, use the initial_indent / subsequent_indent options from RtOptions if you can, rather than writing custom logic.
- If you have a list of lines and you need to prefix them all with some prefix (optionally different on the first vs subsequent lines), use the `prefix_lines` helper from line_utils.

## Tests

### Test module organization

- When adding a new test module, define its contents in a separate sibling file rather than inline in the implementation file.
- Use an explicit `#[path = "..._tests.rs"]` attribute so the test filename is descriptive and easy to locate:

  ```rust
  #[cfg(test)]
  #[path = "parser_tests.rs"]
  mod tests;
  ```

- This applies only when introducing a new test module. Do not move or rewrite existing inline `#[cfg(test)] mod tests { ... }` modules solely to follow this convention.

### Snapshot tests

This repo uses snapshot tests (via `insta`), especially in `codex-rs/tui`, to validate rendered output.

**Requirement:** any change that affects user-visible UI (including adding new UI) must include
corresponding `insta` snapshot coverage (add a new snapshot test if one doesn't exist yet, or
update the existing snapshot). Review and accept snapshot updates as part of the PR so UI impact
is easy to review and future diffs stay visual.

When UI or text output changes intentionally, update the snapshots as follows:

- Run tests to generate any updated snapshots:
  - `just test -p codex-tui`
- Check what’s pending:
  - `cargo insta pending-snapshots -p codex-tui`
- Review changes by reading the generated `*.snap.new` files directly in the repo, or preview a specific file:
  - `cargo insta show -p codex-tui path/to/file.snap.new`
- Only if you intend to accept all new snapshots in this crate, run:
  - `cargo insta accept -p codex-tui`

If you don’t have the tool:

- `cargo install --locked cargo-insta`

### Benchmarks

cargo benchmarks can be run with `just bench`, use the divan crate to write new ones.

Use `just bench-smoke` to dry-run the benchmark for a single iteration to ensure it works.

### Test assertions

- Tests should use pretty_assertions::assert_eq for clearer diffs. Import this at the top of the test module if it isn't already.
- Prefer deep equals comparisons whenever possible. Perform `assert_eq!()` on entire objects, rather than individual fields.
- Avoid mutating process environment in tests; prefer passing environment-derived flags or dependencies from above.

### Spawning workspace binaries in tests (Cargo vs Bazel)

- Prefer `codex_utils_cargo_bin::cargo_bin("...")` over `assert_cmd::Command::cargo_bin(...)` or `escargot` when tests need to spawn first-party binaries.
  - Under Bazel, binaries and resources may live under runfiles; use `codex_utils_cargo_bin::cargo_bin` to resolve absolute paths that remain stable after `chdir`.
- When locating fixture files or test resources under Bazel, avoid `env!("CARGO_MANIFEST_DIR")`. Prefer `codex_utils_cargo_bin::find_resource!` so paths resolve correctly under both Cargo and Bazel runfiles.

### Integration tests

#### codex_core integration testing

- Prefer the utilities in `core_test_support::responses` when writing end-to-end Codex tests.
- Use `TestCodexBuilder::build_with_auto_env()` by default to ensure that new tests work with
  foreign app/exec OSes. See $remote-tests for details.
- All `mount_sse*` helpers return a `ResponseMock`; hold onto it so you can assert against outbound `/responses` POST bodies.
- Use `ResponseMock::single_request()` when a test should only issue one POST, or `ResponseMock::requests()` to inspect every captured `ResponsesRequest`.
- `ResponsesRequest` exposes helpers (`body_json`, `input`, `function_call_output`, `custom_tool_call_output`, `call_output`, `header`, `path`, `query_param`) so assertions can target structured payloads instead of manual JSON digging.
- Build SSE payloads with the provided `ev_*` constructors and the `sse(...)`.
- Prefer `wait_for_event` over `wait_for_event_with_timeout`.
- Prefer `mount_sse_once` over `mount_sse_once_match` or `mount_sse_sequence`

- Typical pattern:

  ```rust
  let mock = responses::mount_sse_once(&server, responses::sse(vec![
      responses::ev_response_created("resp-1"),
      responses::ev_function_call(call_id, "shell", &serde_json::to_string(&args)?),
      responses::ev_completed("resp-1"),
  ])).await;

  codex.submit(Op::UserTurn { ... }).await?;

  // Assert request body if needed.
  let request = mock.single_request();
  // assert using request.function_call_output(call_id) or request.json_body() or other helpers.
  ```

#### app-server integration testing

- Tests should exercise app-server's public JSON-RPC API.
- Use similar server mocking as for core integration tests.
- Use `TestAppServer::builder().build()` and `TestAppServer::send_thread_start_request_with_auto_env()`
  by default to ensure that new tests work with foreign app/exec OSes. See `$remote-tests` for
  details.

## App-server API Development Best Practices

These guidelines apply to app-server protocol work in `codex-rs`, especially:

- `app-server-protocol/src/protocol/common.rs`
- `app-server-protocol/src/protocol/v2.rs`

### Core Rules

- All active API development should happen in app-server v2. Do not add new API surface area to v1.
- Follow payload naming consistently:
  `*Params` for request payloads, `*Response` for responses, and `*Notification` for notifications.
- Expose RPC methods as `<resource>/<method>` and keep `<resource>` singular (for example, `thread/read`, `app/list`).
- Always expose fields as camelCase on the wire with `#[serde(rename_all = "camelCase")]` unless a tagged union or explicit compatibility requirement needs a targeted rename.
- Always expose string enum values as camelCase on the wire with matching serde and TS `rename_all = "camelCase"` annotations unless an explicit compatibility requirement needs targeted renames.
- Exception: config RPC payloads are expected to use snake_case to mirror config.toml keys (see the config read/write/list APIs in `app-server-protocol/src/protocol/v2.rs`).
- Always set `#[ts(export_to = "v2/")]` on v2 request/response/notification types so generated TypeScript lands in the correct namespace.
- Never use `#[serde(skip_serializing_if = "Option::is_none")]` for v2 API payload fields.
  Exception: client->server requests that intentionally have no params may use:
  `params: #[ts(type = "undefined")] #[serde(skip_serializing_if = "Option::is_none")] Option<()>`.
- Keep Rust and TS wire renames aligned. If a field or variant uses `#[serde(rename = "...")]`, add matching `#[ts(rename = "...")]`.
- For discriminated unions, use explicit tagging in both serializers:
  `#[serde(tag = "type", ...)]` and `#[ts(tag = "type", ...)]`.
- Prefer plain `String` IDs at the API boundary (do UUID parsing/conversion internally if needed).
- Timestamps should be integer Unix seconds (`i64`) and named `*_at` (for example, `created_at`, `updated_at`, `resets_at`).
- For experimental API surface area:
  use `#[experimental("method/or/field")]`, derive `ExperimentalApi` when field-level gating is needed, and use `inspect_params: true` in `common.rs` when only some fields of a method are experimental.

### Client->server request payloads (`*Params`)

- Every optional field must be annotated with `#[ts(optional = nullable)]`. Do not use `#[ts(optional = nullable)]` outside client->server request payloads (`*Params`).
- Optional collection fields (for example `Vec`, `HashMap`) must use `Option<...>` + `#[ts(optional = nullable)]`. Do not use `#[serde(default)]` to model optional collections, and do not use `skip_serializing_if` on v2 payload fields.
- When you want omission to mean `false` for boolean fields, use `#[serde(default, skip_serializing_if = "std::ops::Not::not")] pub field: bool` over `Option<bool>`.
- For new list methods, implement cursor pagination by default:
  request fields `pub cursor: Option<String>` and `pub limit: Option<u32>`,
  response fields `pub data: Vec<...>` and `pub next_cursor: Option<String>`.

### Development Workflow

- Regenerate schema fixtures when API shapes change:
  `just write-app-server-schema`
  (and `just write-app-server-schema --experimental` when experimental API fixtures are affected).
- Validate with `just test -p codex-app-server-protocol`.
- Avoid boilerplate tests that only assert experimental field markers for individual
  request fields in `common.rs`; rely on schema generation/tests and behavioral coverage instead.

## Python Development Best Practices

### Ignore Python 2 compatibility

This project uses Python 3+. You should not use the `__future__` module.

If you need to worry about feature compatibility between different 3.xx point releases, check the
closest `pyproject.toml`'s `requires-python` field to see what minimum runtime version is supported.

## Platform Support

Tests and features must support Linux, macOS and Windows unless feature is explicitly OS-specific.

Codex supports running connected app-server and exec-server on different operating systems. See the
`$remote-tests` skill for details about integration testing these configurations.
