# SEE-1394 S1b MCP 工具集全量实测 + godot-mcp 实机拉起报告（Bachi）

- 日期：2026-10-11（UTC+8 约 22:15–22:30 采集）
- 执行机：105 本机（DESKTOP-INVD2QR / jerry / WSL2）
- 范围：§SPEC-015（MCP 工具集全量实测）+ §SPEC-016（godot-mcp 实机拉起与 read-only 工具验证）
- 背景：Owner 打回（O-004）指出 S1~S4 仅验证「存在性/一致性」，未实测「真实使用」。

## 结论摘要

| §SPEC | 判定 | 摘要 |
|---|---|---|
| §SPEC-015 MCP 全量实测 | **7/8 PASS，1 FAIL（F-003，非阻塞）** | hindsight / context7 / web_reader / github / understand_image / grepai(工具层连通，后端 404=F-001 维持) / godot-mcp（→§SPEC-016）PASS；**web_search FAIL**（搜索后端不可达，另含用户级 searxng server 同不可达） |
| §SPEC-016 godot-mcp 实机拉起 | **PASS** | 真实编辑器冷启动（Godot_v4.6.2-stable_win64 PID 15004）→ 两次 read-only 调用返回真实项目数据 → taskkill 关闭 → 零残留进程 |

**无阻塞项**。新登记：F-003（搜索后端不可达，处置建议）、O-007（workspace KOL 检出 .uid 副产物，处置建议）。O-006 编号由 S2b（Refacty）占用（Notification hook UNVERIFIED），本报告跳过。

## §SPEC-015 逐 server 真实调用证据

枚举依据（实际配置）：① multica runtime 注入的 agent mcp-config（本会话工具面：context7 / github / godot-mcp / grepai / hindsight / understand_image / web_reader / web_search；`multica agent get` 的 mcp_config 字段经 secret 边界 redact，以会话实际启用面为准）；② 用户级 `~/.claude.json`（searxng）；③ 插件 hindsight-memory。`claude mcp list`（exit 0）：`plugin:hindsight-memory ✔ Connected`；`searxng http://192.168.1.210:47322/sse ✘ Failed to connect`。

| # | MCP server | 工具 | 输入摘要 | 返回摘要 | 判定 |
|---|---|---|---|---|---|
| 1 | hindsight | agent_knowledge_recall | query「godot-mcp warmup SEE-1111」 | 返回 hold-to-warm 状态机 observation（COLD_EMPTY→WARMING→WARM/…/FAILED_EXIT，含 2026-08-06 修订记录） | **PASS** |
| 2 | context7 | resolve-library-id | libraryName「Godot Engine」 | /godotengine/godot-docs（44644 snippets，High reputation）+ 4.4/4.5/4.6/4.7 站点档 4 条 | **PASS** |
| 3 | web_reader | web_url_read | https://docs.godotengine.org/en/stable/（paragraph 1-3） | 「# Godot Docs – 4.7 branch … available in various languages and versions」 | **PASS** |
| 4 | web_search | web_search + instance_info | query「Godot 4.5 release notes features」（3 条） | 两次均 `error: cannot reach search backend: All connection attempts failed` | **FAIL → F-003** |
| 5 | github | search_repositories | query「godot-qa-toolkit in:name」（perPage 3） | 1 条：tadki/godot-qa-toolkit（pushed_at 2026-10-11T14:04:51Z，与今日验收 push 链吻合） | **PASS** |
| 6 | understand_image | understand_image | python3/zlib 现做 136B 纯红 64×64 PNG → `curl -X POST --data-binary 210:4890/upload`（exit 0）→ 问 dominant color | `Red`（模型对内容未知图像作答正确，端到端闭环） | **PASS** |
| 7 | grepai | grepai_search | query「player movement controller」（limit 3） | `search failed: Ollama returned status 404: model "nomic-embed-text" not found`（结构化业务错误，非传输错误） | 工具层连通 **PASS**；后端 404 = **F-001 维持** |
| 8 | godot-mcp | godot_project / godot_editor_read | 见 §SPEC-016 节 | 见 §SPEC-016 节 | **PASS**（§SPEC-016） |
| 9 | searxng（用户级配置） | — | `claude mcp list` + `curl 210:47322/sse` + `/dev/tcp` 探测 | `✘ Failed to connect`；curl http=000 exit=7；TCP Connection refused | **FAIL → F-003（同根因）** |

## §SPEC-016 godot-mcp 实机拉起证据

### 拉起链路（既有 launch 链路，未做任何修改）

```
$ tail ~/.multica/godot-mcp-launcher-port-6561.log
2026-10-11T22:15:13+0800 [godot-mcp-launcher] port 6561 free; proxy will lazy-spawn the editor on first tools/call.
[godot-mcp-launcher] stage=FORK_WIRED msg="godot-mcp served from owner fork"
  cli=/mnt/d/GodotProjects/king-of-likes/addons/godot_mcp/launch/../server/dist/cli.js quick_timeout_ms=90000
[godot-mcp-launcher] stage=LAUNCHER_EXEC msg="exec godot-mcp-proxy.mjs" port=6561
  (GODOT_PORT=6561 KOL_WORKTREE=.../workdir/KingOfLikes-Godot KOL_RUNTIME_ID=Bachi-43bcb5d88901)
```

进程证据：`proxy pid 86609（godot-mcp-proxy.mjs）→ fork cli.js pid 86954`；`held/Bachi-43bcb5d88901/pid` = 86609（hold-to-warm 结构按设计工作）。

### read-only 调用（真实项目数据）

```
调用 1: godot_project get_info
  → {"godot_version":"4.6.2-stable (official)","main_scene":"res://scenes/ui/title_screen/title_screen.tscn",
     "name":"KingOfLikes","path":"//wsl.localhost/Ubuntu/home/jerry/.../workdir/KingOfLikes-Godot/"}

调用 2: godot_editor_read get_state
  → {"current_scene":"res://scenes/ui/title_screen/title_screen.tscn","godot_version":"4.6.2-stable (official)",
     "is_playing":false,"main_screen":"2D","viewport_2d":{"center":{"x":1101.0,"y":811.75},"size":{"width":1700,"height":1241},"zoom":0.6667}}
```

### 编辑器进程证据与关闭

```
$ /mnt/c/Windows/System32/tasklist.exe | grep -i godot
Godot_v4.6.2-stable_win64    15004 Console    1    1,155,752 K     ← 真实编辑器进程（Windows 侧，proxy 经 WSL interop 拉起）
$ taskkill /PID 15004 /T       → ERROR: can only be terminated forcefully（首试非强制被拒，如实记录）
$ taskkill /F /PID 15004 /T    → SUCCESS: terminated
$ tasklist.exe | grep -i godot → 空（exit=1）；WSL 侧 pgrep 'Godot_v|win64' → 空   ← 零残留
```

### 副作用如实登记

- **O-002 预期累积**：本 run 在 `addons/godot_mcp/launch/held/` 新增会话目录 `Bachi-43bcb5d88901/`（含 pid 标记文件）——属 O-002 已知累积语义，不新登记。
- **O-007（新观察项）**：编辑器首次打开 workspace KOL 检出时，Godot 4.6 导入器为 430 个脚本生成 untracked `.uid` 副产物（`git status`：430 `?? *.uid` + 1 `M addons/godot_mcp`（子模块内 held/pid，即 O-002））。**HEAD/跟踪文件零变更**，主检出 `/mnt/d/GodotProjects/king-of-likes` 未被本次编辑器使用（proxy 经 KOL_WORKTREE 指向 workspace 检出）。处置建议：`.uid` 为 Godot 4.6 官方推荐的脚本稳定 ID，保留或纳入管理属 Owner 决策；不影响验收判定。
- 编辑器日志 `~/.multica/godot-editor/Bachi-43bcb5d88901.log` 尾部可见项目内容既有 parse error（`tests/regression/test_see798_vendor_regression.gd`）——项目既有内容，非本次引入，供下游注意。

## Findings Ledger（本报告新增/维持）

| ID | 内容 | 判定 | 处置建议 |
|---|---|---|---|
| **F-003**（新） | web_search MCP 调用 `cannot reach search backend`；用户级 searxng server（210:47322/sse）`Failed to connect`（TCP Connection refused，curl exit=7） | 非阻塞：web_reader 可用（直接抓取验证通过）；搜索能力当前唯一通道失效 | 210 侧恢复/启动 searxng 服务并核对端口 47322 与 runtime 注入的 web_search 后端配置；恢复后复测 web_search + `claude mcp list` |
| F-001（维持） | grepai 后端 404（210:11434 ollama 无 nomic-embed-text） | 工具层连通正常、后端 FAIL；S1 复核结论不变 | 210 上 `ollama pull nomic-embed-text` |
| O-002（维持） | held/ 会话目录累积（本 run +1） | 已知语义 | Owner 决定清理策略 |
| **O-007**（新） | workspace KOL 检出 430 个 untracked `.uid`（编辑器导入行为） | HEAD 未动；非代码变更 | Owner 决策保留/治理 |

## 只读合规性自证

- 三仓 HEAD 零变更（仅 `git fetch`/`git pull --ff-only` on godot-qa-toolkit shared/SEE-1394）；主检出 `/mnt/d/GodotProjects/king-of-likes` 未被编辑器打开。
- 105 配置零修改（凭据/known_hosts/sshd/端口均只读探查）。
- godot-mcp 仅调用 read-only 工具（get_info / get_state），无场景/节点写入；编辑器验证后强制关闭且零残留。
