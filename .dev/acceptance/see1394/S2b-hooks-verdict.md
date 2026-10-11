# SEE-1394 S2b hooks 实际触发验证报告（Refacty，2026-10-11）

范围：§SPEC-017。枚举 `~/.claude/settings.json` 全部 hook 绑定并逐条给出**实际触发**证据。证据来源三类：① 本 run 会话内真实收到的 hook 注入上下文引用；② hook 落盘日志（`~/.claude/hook-fire.log` / repo 级 `./.claude/hook-fire.log`）与副作用工件（`~/.claude/hooks/unified/state/.hook-fired-*`）；③ 受控 `claude -p` 最小会话（独立临时目录 `/tmp/see1394-s2b-hooks`）触发副作用取证。全程只读，未改任何配置与 hook 脚本。

## 0. 绑定枚举（~/.claude/settings.json）

settings.json `hooks` 段共 **9 个事件**，每个事件 `matcher:"*"` 绑定同一条命令 `hook-wrapper.sh <Event> unified-hook`（exit=0 直读）：

| # | 事件 | 绑定命令 |
|---|---|---|
| 1 | SessionStart | `~/.claude/hooks/hook-wrapper.sh SessionStart unified-hook` |
| 2 | UserPromptSubmit | 同上 `UserPromptSubmit unified-hook` |
| 3 | PreToolUse | 同上 `PreToolUse unified-hook` |
| 4 | PostToolUse | 同上 `PostToolUse unified-hook` |
| 5 | Stop | 同上 `Stop unified-hook` |
| 6 | SubagentStop | 同上 `SubagentStop unified-hook` |
| 7 | SessionEnd | 同上 `SessionEnd unified-hook` |
| 8 | Notification | 同上 `Notification unified-hook` |
| 9 | PreCompact | 同上 `PreCompact unified-hook` |

引擎拓扑（只读取证）：`hook-wrapper.sh` 每次触发先写一行 firelog（`[<ts>] [<Event>] alias=unified-hook`，路径由 `scripts/lib/hook-fire-path.lib.sh` 解析——repo 级 `.claude/hook-fire.log` 优先，否则全局 `~/.claude/hook-fire.log`），再 `exec dispatcher.sh → unified-hook.sh → engine.js`。规则共 21 条：`rules-v2.json` 13 条（deny/context 类：mention-confirm-check、recall-reminder、grepai-search-reminder、child-issue-guard、各 agent skill-check/reminder 等）+ `rules.d/runscript-mounts.json` 8 条（run_script 类：repo-checkout-mount（SessionStart）、push-guard-mount / mention-check-mount / atlas-taskflow-exit-reminder（PreToolUse）、gdformat-gdlint-mount / atlas-taskflow-mark-a/b（PostToolUse）、auto-pr-stop-mount（Stop））。Notification / SessionEnd / SubagentStop / PreCompact 无规则绑定，触发后仅留 firelog 行（引擎 `action=none`）。

## 1. 逐绑定触发证据

| # | 事件 → 脚本 | 触发证据（可复核） | 结论 |
|---|---|---|---|
| 1 | SessionStart → repo-checkout-mount → `repo-checkout.sh` | ① 本 run 会话内真实收到的注入上下文原文：「SessionStart hook additional context: 已成功将本 issue 的 working branch checkout 到 shared/SEE-1394。如需进行代码工作或探索，请 cd 到 'godot-qa-toolkit'…」（该内容即 repo-checkout.sh 的输出通道）；② 引擎行 `[2026-10-11T14:15:05Z] event=SessionStart rules=21 fired=repo-checkout-mount action=run_script … result exit=0`（全局 firelog）；repo 级 firelog `[14:15:31Z] [SessionStart]`（本 run 起始）；③ 受控 `claude -p`（temp 目录）同样在 `[14:20:26Z] fired=repo-checkout-mount action=run_script` | PASS |
| 2 | UserPromptSubmit → unified-hook（无规则，仅引擎通道；插件侧另有 hindsight 注入） | ① 本 run 第二 turn 的 user message 携带真实注入上下文：`<hindsight_memories>` 块（「Relevant memories from past conversations … Current time - 2026-10-11 14:15 UTC」，9 条历史记忆，标注 UserPromptSubmit hook additional context）——注入来源为 hindsight-memory 插件 hook，但该事件绑定的 settings.json 侧同样触发：② repo 级 firelog `[14:15:35Z] [UserPromptSubmit]`（本 run 触发 turn）；③ 受控会话每次 prompt 均有对应行（14:19:01Z / 14:21:19Z / 14:21:38Z） | PASS |
| 3 | PreToolUse → unified-hook（13 条 deny/context 规则 + push-guard/mention-check 脚本） | ① 本 run 第二 turn 首个 Bash 调用真实收到注入：「PreToolUse:Bash hook additional context: [recall-reminder] 必须激活 /task-explore 技能探索任务 / 必须使用 hindsight 工具召回『owner-order』页面并遵守规则」（本 run 实际照此执行）；② **deny 类真实生效**：本 run 第一 turn 发布汇报评论时被 mention-confirm-check 拦截，收到原文「**错误：发布 comment 已被拦截，必须进行自查**。检测到本条评论包含 @mention…」，补自查声明后放行；副作用工件 `~/.claude/hooks/unified/state/.hook-fired-mention-confirm-check-Refacty-01a12b1f-baab-7cb2-8fe5-b2140df42947`（mtime 21:26 本地=13:26 UTC，与拦截时点一致，run ID 与本 run 对应）；③ repo 级 firelog 本 run 内 PreToolUse 成对出现于每次工具调用前（14:15:57 / 14:15:58 / 14:16:11 / 14:16:26 / 14:17:20 / 14:17:40 / 14:17:53…）；全局历史累计 1983 次 | PASS |
| 4 | PostToolUse → unified-hook（grepai-search-reminder 等 context 规则 + gdformat-gdlint 等脚本） | ① 本 run 内每次 Bash 结果附带真实注入：「PostToolUse:Bash hook additional context: [grepai-search-reminder] 提示：检测到你正在使用 grep 类查找工具…」（本报告编写期间至少 4 次，最近一次在 Notification/SubagentStop 取证命令后）；② repo 级 firelog 本 run PostToolUse 与 PreToolUse 严格配对（见 #3 时间戳）；③ 受控会话 `[14:21:34Z] [PostToolUse]`（子代理 tool 调用后） | PASS |
| 5 | Stop → auto-pr-stop-mount → `auto-pr-on-stop.sh` | ① repo 级 firelog：本 run 第一 turn 结束时 `[2026-10-11T13:27:49Z] [Stop]`（紧邻 `[13:27:49Z] [SessionEnd]`，turn 收尾链真实发生）；② 受控 `claude -p` 会话每次结束均有 Stop（14:19:15Z、14:21:37Z、14:21:52Z）；③ 全局历史累计 37 次 | PASS |
| 6 | SubagentStop → unified-hook（无规则，firelog 见证） | ① 受控取证：temp 目录 `claude -p "Use the Agent tool with subagent_type general-purpose … MAINDONE." --model haiku`，stdout=MAINDONE exit=0；全局 firelog 当场出现 `[2026-10-11T14:21:38Z] [SubagentStop]`（位于子代理 turn 的 Stop 之后、主 turn 续行之前，时序符合子代理生命周期）；② 另有历史实证 `[2026-09-20T14:49:36Z] [SubagentStop] rules=21 fired=- action=none exit=0` | PASS |
| 7 | SessionEnd → unified-hook（无规则，firelog 见证） | ① repo 级 firelog `[2026-10-11T13:27:49Z] [SessionEnd]`（本 run 第一 turn 会话收尾）；② 受控会话 14:19:15Z / 14:21:53Z 各一条，引擎行 `event=SessionEnd … exit=0`；③ 全局历史累计 90 次 | PASS |
| 8 | Notification → unified-hook（无规则） | 全局 firelog（1.9MB，含 9/20 以来的全部历史）**0 次 Notification 触发**。触发条件为交互式权限请求/空闲提醒，`claude -p` 非交互模式不产生该事件（受控实验未触发）；settings.json 绑定本身存在且引擎通道与其余事件同构。**在「只读、不得修改配置」约束内无法构造真实触发场景** | UNVERIFIED（非阻塞，见 §3） |
| 9 | PreCompact → unified-hook（无规则，firelog 见证） | 全局 firelog 历史实证：`[2026-09-20T14:47:29Z] [PreCompact] rules=21 fired=- action=none … result exit=0`（真实上下文压缩触发的引擎全链执行）。受控复现需构造超长上下文会话（代价大且非确定性），按判据允许以「hook 落盘日志」作证，采用历史条目 | PASS（历史实证） |

## 2. 机制级佐证

- firelog 事件分布（本机实测统计）：全局 1983×PreToolUse / 1164×SessionStart / 651×result / 453×PostToolUse / 90×SessionEnd / 37×Stop / 15×UserPromptSubmit / 1×SubagentStop / 1×PreCompact / 0×Notification；repo 级（本 issue workdir）：33×PreToolUse / 28×PostToolUse / 3×UserPromptSubmit / 3×SessionStart / 2×Stop / 2×SessionEnd——分布与事件语义吻合（工具调用高频、会话级事件低频），非伪造分布。
- 副作用工件：`~/.claude/hooks/unified/state/` 含本 run 维度 fired-marker（`.hook-fired-recall-reminder-Refacty-01a12b1f-baab-…`、`.hook-fired-mention-confirm-check-Refacty-01a12b1f-baab-…` 等），mtime 与本 run 各 turn 时点一致；今日同目录还可见 Atlas/Bachi/Harti/Revy 各 run 的 marker，全 agent 面一致。
- 受控会话完整生命周期链：`SessionStart(14:21:18) → UserPromptSubmit(14:21:19) → PreToolUse(14:21:34) → PostToolUse(14:21:34) → Stop(14:21:37) → SubagentStop(14:21:38) → Stop(14:21:52) → SessionEnd(14:21:53)`，一次 `claude -p` 跑通 7 类事件的端到端引擎执行，全部 exit=0。

## 3. Findings

- **Notification 绑定 UNVERIFIED**（非阻塞）：触发条件仅限交互式会话的权限请求/空闲提醒；headless `claude -p` 不产生该事件；本机 firelog 全历史 0 次说明日常 headless 运行模式从不触发它。处置建议：① 接受按设计闲置（无规则绑定，触发与否仅差一行日志）；② 如需严格闭合，由 Owner 交互式会话做一次权限请求场景触发（不属于只读验收范围）。登记为 **O-006**。
- **O-007 观察项（插件侧，非 settings.json 绑定）**：受控 `claude -p` 子代理会话结束时 stderr 出现 `SessionEnd hook [python3 … session_end.py] failed: Hook cancelled`——为 hindsight-memory **插件** hook（`CLAUDE_PLUGIN_ROOT/scripts/session_end.py`）的一次失败提示，settings.json 侧 unified-hook SessionEnd 同会话 exit=0 正常。非阻塞；建议 Owner 决定是否排查插件 hook 在 headless 场景的取消路径。
- 既有 F-001/F-002/O-002/O-003/O-004 与 §SPEC-017 判据无交集。

## 4. 判定汇总

| §SPEC-017 绑定 | 判定 |
|---|---|
| SessionStart | PASS（注入原文 + 引擎 fired=repo-checkout-mount + 受控复现） |
| UserPromptSubmit | PASS（hindsight_memories 注入原文 + firelog + 受控复现） |
| PreToolUse | PASS（recall-reminder 注入原文 + mention-confirm-check 真实拦截 + 副作用 marker） |
| PostToolUse | PASS（grepai-search-reminder 注入原文 ×4 + 严格配对 firelog） |
| Stop | PASS（firelog 时点 + 受控复现） |
| SubagentStop | PASS（受控会话当场取证 14:21:38Z + 历史实证） |
| SessionEnd | PASS（firelog + 受控复现 exit=0） |
| Notification | UNVERIFIED → O-006（非阻塞，处置建议见 §3） |
| PreCompact | PASS（历史 firelog 实证 2026-09-20T14:47:29Z） |

S2b 收口：9 绑定中 8 PASS + 1 UNVERIFIED（O-006 非阻塞）。可流转 S3b（Harti 硬化）。
