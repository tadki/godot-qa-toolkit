# SEE-1394 S3b 补测轮硬化报告（§SPEC-015/016/017）

> 硬化对象：S1b（`.dev/acceptance/see1394/S1b-mcp-verdict.md`，commit `230e825`）与 S2b（`.dev/acceptance/see1394/S2b-hooks-verdict.md`，commit `6698d4d`）的新证据。
> 执行者：Harti（105 本机，DESKTOP-INVD2QR / jerry / WSL2），2026-10-11 14:36–15:05Z。
> 方法：对抗性预设「默认上游证据无效」——去找复现不了的调用、判不死的后端、关闭不干净的残留；全部机器命令/真实调用证据，无 headless/自报充当 PASS。
> 只读合规：三仓 HEAD 零变更（落仓报告除外）、105 配置零修改、godot-mcp 仅 read-only 调用、编辑器验证后关闭零残留、未触碰 SSH 私钥。

## 1. 硬化计划

| 判据项 | 判据类型 | 目标 | 达标阈值 |
|---|---|---|---|
| B1 hindsight 独立复跑 | MCP 真实调用 | get_page / recall | 返回真实记忆内容 |
| B2 context7 独立复跑 | MCP 真实调用 | resolve-library-id「Godot Engine」 | 命中 Godot 文档库 |
| B3 github 独立复跑 | MCP 真实调用 | search_repositories | 命中 tadki/godot-qa-toolkit |
| B4 understand_image 独立复跑（对抗性变体） | MCP 真实调用 | 自制**纯蓝** PNG 端到端 | 模型答 "Blue"（S1b 用红色答 "Red"；变体证明模型真实读图） |
| B5 grepai 复现（F-001）+ 4893 网关探测 | MCP 真实调用 + curl | grepai_search + 210:4893 | 复现 404；网关状态登记 |
| B6 web_search 复现（F-003）+ 4892 网关探测 | MCP 真实调用 + curl | web_search + 210:4892/47322 | 复现或证伪（Owner 14:33Z 指正：后端已非 47322） |
| B7 godot-mcp 冷启动复现（§SPEC-016 核心） | 实机断言 | 既有 launch 链路（launcher→proxy→cli.js→真实编辑器） | 真实编辑器拉起 + read-only 返回真实项目数据 + 关闭零残留 |
| B8 S2b firelog 复核（§SPEC-017） | 机器命令 | 全局/repo firelog、unified state markers | 绑定枚举一致、Notification=0（O-006）、关键时点可复核 |
| B9 O-008 .uid 副产物复核 | 机器命令 | 编辑器冷启动后 workdir KOL 检出 | 独立复现计数（S1b 时点 430） |

## 2. 判定执行结果

### B1–B4 MCP 真实调用独立复跑 — 全 PASS（与 S1b 一致）

| # | server / 工具 | 本 run 实测（独立执行） | 判定 |
|---|---|---|---|
| B1 | hindsight：get_page(owner-order) 全页返回 + recall×2（owner-order 规则页、daemon 注册值 query） | 完整 standing-rules 页面 + 带 scores 的实时记忆条目返回 | PASS |
| B2 | context7：resolve-library-id「Godot Engine」 | 返回 5 个 Godot 文档库条目（/websites/godotengine_en_4_4~4_7 等，snippets 268257/68737/…）——**与 S1b 快照不同**，证明实时索引非回放 | PASS |
| B3 | github：search_repositories「godot-qa-toolkit in:name」 | 命中 tadki/godot-qa-toolkit，`pushed_at=2026-10-11T14:30:04Z`（= S1b 报告 commit 推送时点，链路吻合） | PASS |
| B4 | understand_image（**对抗性变体**） | python3/zlib 自制 168B 纯**蓝** 64×64 PNG → `curl -X POST 210:4890/upload`（exit 0，返回 /tmp/understand-image-uploads/75b2ba10….png）→ 问 dominant color → 答 **"Blue"**。S1b 用红色问出 "Red"；两方向一致证明模型真实读图、非固定应答 | PASS |

### B5 grepai 复现（F-001）+ 网关探测 — 复现成立，处置建议按 Owner 指正更新

- `grepai_search("player movement controller")` → `search failed: Ollama returned status 404: {"error":"model \"nomic-embed-text\" not found, try pulling it first"}`——与 S1b/S1 逐字一致，**F-001 维持**。
- 端口探测：`210:4893`（Owner 指认的 grepai 后端网关）→ HTTP **404** on `/`（服务存活，路由层应答）；`210:11434/api/tags` → `{"models":[]}`（ollama API 层无模型）。404 错误文本显式指名 ollama，与「4893 网关前置 210:11434 ollama」拓扑吻合。
- 处置建议更新：在 **210** 的 ollama（11434，经 4893 网关暴露）上 `ollama pull nomic-embed-text` 后即可修复；S1 已纠正版处置不变，仅补网关拓扑佐证。非阻塞，待 Owner。

### B6 web_search 复现（F-003）+ 网关探测 — 失败复现成立，F-003 归因按 Owner 指正修正

- `web_search("Godot 4.5 release notes features")` → `error: cannot reach search backend: All connection attempts failed`——与 S1b 逐字一致，失败现象**复现成立**。
- 端口探测：`210:47322/sse`（S1b 登记的旧后端）→ http=000 / exit=7（Connection refused，维持）；`210:4892`（Owner 14:33Z 指认的 LiteLLM Gateway web_search 后端）→ HTTP **404** on `/`（网关存活）。
- **硬化修正**：S1b 的 F-003 处置建议（「恢复 searxng 47322」）已过时——真实后端是 4892 网关且网关本身存活；故障点在 runtime 注入的 web_search MCP server 配置仍指向不可达端点（或 4892 上搜索路由未就绪）。修正后处置建议：**核对 runtime 注入的 web_search 后端配置指向 4892 的正确路由**，属配置面修复，105 侧无缺陷。非阻塞维持（web_reader 可用作退路）。

### B7 godot-mcp 冷启动复现（§SPEC-016 硬化核心）— PASS（含两次拉起全程证据）

**冷启动链路时序（既有 launch 链路，零修改；driver 仅经 stdio 驱动 launcher）：**

```
14:49:22Z  launcher: port 6569 free; proxy lazy-spawn on first tools/call
           stage=FORK_WIRED cli=.../addons/godot_mcp/launch/../server/dist/cli.js quick_timeout_ms=90000
           stage=LAUNCHER_EXEC exec godot-mcp-proxy.mjs (GODOT_PORT=6569
             KOL_WORKTREE=<本 run workdir>/KingOfLikes-Godot KOL_RUNTIME_ID=Harti-df889eca1620)
14:49:23Z  proxy: NPX_SPAWN cmd=/usr/bin/node pid=204064；initialize/tools/list 立即应答
14:49:31Z  first tools/call → ENSURE_EDITOR_BEGIN → PREPARE_SH/CONFIGURE_SH/START_SH 全 rc=0
14:49:36Z  start-godot-editor: schtasks task=kol-editor-Harti-df889eca1620 → EDITOR_LAUNCH_TRIGGERED
14:50:27Z  MILESTONE_PLUGIN_INIT elapsed=54.6s
14:50:57Z  MILESTONE_SERVER_LISTENING elapsed=85.1s
```

- **编辑器进程证据**：`Godot_v4.6.2-stable_win64 PID 23664`（tasklist 实测，~1.30GB RSS）——真实编辑器（WSL interop 经 schtasks 拉起），非 headless。
- **tools/list**：23 个工具（godot_scene / godot_node_read / godot_node_edit / godot_editor_read / godot_editor_edit / godot_project / …），真实 fork 面。
- **read-only 调用返回真实项目数据（isError=false）**：

```
CALL godot_project/get_info →
  {"godot_version":"4.6.2-stable (official)","main_scene":"res://scenes/ui/title_screen/title_screen.tscn",
   "name":"KingOfLikes","path":"//wsl.localhost/Ubuntu/home/jerry/.../see-1394-df889eca1620/workdir/KingOfLikes-Godot/"}
  + workdir_snapshot {"runtime_id":"Harti-df889eca1620","workdir_hash":"df889eca1620","hash_source":"slot"}

CALL godot_editor_read/get_state →
  {"current_scene":"res://scenes/ui/title_screen/title_screen.tscn","godot_version":"4.6.2-stable (official)",
   "is_playing":false,"main_screen":"2D","open_scenes":["res://scenes/ui/title_screen/title_screen.tscn"],
   "viewport_2d":{"center":{"x":1101.0,"y":811.75},"size":{"width":1700,"height":1241},"zoom":0.6667}}
```

- **对抗性对照（server 判别力佐证）**：首两次调用参数形状错误（`method` 而非 `action`）→ server 如实返回 `isError:true` 并列出合法 action 清单——工具面有真实输入校验，非回声机。
- **关闭与零残留**：`taskkill /F /PID 23664 /T` → SUCCESS；复检 `tasklist|grep -i godot` 空（rc=1）、WSL `pgrep Godot_v` 空、`6569 free`、schtask `kol-editor-Harti-df889eca1620` 已不存在（ERROR: cannot find the file specified）——**零残留**。
- **如实登记**：① 首次 tools/call 因 driver 侧 120s 超时早于编辑器 warmup（~124s）退出一次，warm 重连后调用成功——链路无失败，属探测脚本参数问题；② 本次冷启动在我 workdir 检出生成 430 个 untracked `.uid`（见 B9）。

### B8 S2b firelog 复核（§SPEC-017）— 全部确认

| 断言 | 本 run 独立复核结果 |
|---|---|
| 9 绑定枚举 | 本 run 以 python 直读 settings.json 复枚举：9 事件 × `hook-wrapper.sh <Event> unified-hook`，与 S2b 一致 |
| firelog 分布 | 全局：PreToolUse 1984 / SessionStart 1173 / PostToolUse 454 / SessionEnd 98 / Stop 40 / UserPromptSubmit 18 / SubagentStop 2 / PreCompact 1 / **Notification 0**。S2b 时点 1983→1984 单调增长，吻合 |
| O-006（Notification 0 触发） | 全历史含本 run 仍为 **0**——维持 UNVERIFIED 语义，非阻塞 |
| PreCompact 历史实证 | `[2026-09-20T14:47:29Z] [PreCompact] alias=unified-hook` 原文在案，逐字一致 |
| SubagentStop 受控时点 | `[14:21:37Z] [Stop]` → `[14:21:38Z] [SubagentStop]` 相邻在案，与 S2b 引用一致 |
| deny 规则真实生效 | `~/.claude/hooks/unified/state/.hook-fired-mention-confirm-check-Harti-01a12b26-618e-…`（mtime 21:41 本地 = 13:41Z）——**本 hardener 自己 S3 run 的评论拦截时点**，第一方 deny 证据独立于 S2b |
| 本 turn 实时注入 | 本 run 每turn真实收到 PreToolUse `[recall-reminder]` / PostToolUse `[grepai-search-reminder]` / UserPromptSubmit `<hindsight_memories>` 注入（会话内可复核） |

### B9 O-008 .uid 副产物复核 — 1:1 独立复现

- 本次冷启动（我的 workdir 检出，launch 前 `git status --porcelain`=0 行、top-level .uid 90 个全 tracked）后：`430 ?? *.uid` + `1 M addons/godot_mcp`（held/pid 标记）——**与 S1b 计数 430 完全一致**，机制（编辑器 4.6 导入器生成）与规模双重独立复现。
- HEAD 前==后==`e357d1b`（零提交）；跟踪文件零变更。
- **处置状态观察**：Bachi workdir 副本中的同 430 个 .uid 已被 `git add` 进 index（未 commit，其会话仍在跑）——O-008 处置正在上游演进中；本 hardener 对自己副本不做任何 git 操作，保留原样交 Owner 决策（.gitignore / commit / 清理三选一）。

## 3. Findings（本步新增观察项，均非阻塞）

| ID | 内容 | 处置建议 |
|---|---|---|
| **O-009**（新） | launcher 名字解析路径不含 `Harti`（legacy agent 表仅 Atlas/Refacty/Fronti/Revy/Archi/Bachi；报错 `Unknown agent name 'Harti'`）——直调名字路径的会话无法经名字解析获得端口 | 平台侧 runtime 注入走动态池/runtime_id 路径可正常工作；若需闭合，vendored 侧（只读红线）由 Owner/维护者决定补名或平台侧显式传端口。本次硬化用文档化显式端口入口（KOL_MCP_PORT），链路其余环节未变 |
| **O-002 精化** | held/ 目录并非无界累积：正常 shutdown 的 runtime 会自清（本 run 的 `Harti-df889eca1620` 释放后即消失；今晨的 `Atlas-c36447ba0908` 亦已被清）；现存 `Atlas-4573646d372e`（proxy 已死=stale 待 reaper）与 `Bachi-43bcb5d88901`（proxy 存活 pid 194872=在用）。V6-A4 计数漂移语义随 reaper 周期波动 | 维持 Owner 决策；处置时按「stale 可清、在用勿动」区分 |
| **F-003 修正** | 见 B6：后端非 47322（Owner 指正确认），网关 4892 存活；故障在 MCP server 配置指向或 4892 搜索路由未就绪 | 配置面核对/修复（Owner） |
| **F-001 维持+拓扑补证** | 见 B5：404 复现；4893 网关存活、210:11434 ollama 无模型 | 210 上 `ollama pull nomic-embed-text`（经 4893 网关即生效） |
| **O-006 维持** | Notification 全历史 0 触发复核成立 | 同 S2b：接受按设计闲置或 Owner 交互式补测 |
| **O-008 维持+复现** | 430 .uid 1:1 复现；处置在上游（Bachi 会话）演进中 | Owner 三选一（.gitignore / commit / 清理），对已 checkout 副本逐份处理 |

**无 blocker。** 未发现「复现不了的调用 / 判不死的后端 / 关闭不干净的残留」类反例。

## 4. 硬化结论

**PASS** — S1b/S2b 证据经对抗性独立复跑全部站得住：① §SPEC-015 五项 MCP 真实调用复跑一致（understand_image 用反色变体证明真实读图）；② F-001/F-003 失败现象逐字复现，归因按 Owner 14:33Z 指正修正（4892/4893 网关拓扑实证）；③ §SPEC-016 冷启动链路（launcher→proxy→cli.js→真实编辑器）独立复现：真实编辑器 PID、23 工具面、两个 read-only 调用返回真实项目数据（runtime_id/workdir_hash 自证指向本检出）、关闭零残留；④ §SPEC-017 firelog 全部断言可复核，Notification=0（O-006）维持；⑤ O-008 的 430 .uid 计数 1:1 复现。S1b/S2b 可信，可流转 S4b（Revy QA）。

## 5. 硬化产物

| # | File | Description |
|---|------|-------------|
| 1 | `.dev/acceptance/see1394/S3b-hardening.md`（本文件） | S3b 补测轮硬化报告 |

硬化用 driver 脚本（s3b-editor-probe.mjs）与探针 PNG 均为临时产物，已 `rm` 清理；hindsight `get_page(owner-order)` 已按 owner-order §1.1 条 1 执行召回并遵守。
