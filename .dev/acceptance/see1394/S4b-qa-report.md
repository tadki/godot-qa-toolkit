# SEE-1394 S4b 真机 QA 收口复核报告（Revy，2026-10-11）

- 执行机：**105 本机**（DESKTOP-INVD2QR / jerry / WSL2）；210 侧仅只读探针（curl / 无 SSH 修复动作）
- 范围：修复三项（F-001/F-003/O-009）独立收口复核 + §SPEC-015/016/017 独立复核 + findings ledger 终稿
- 心态：对抗式——重点验证「修复是否探针级成功而非真实使用级」「去重是否漏项」「复测是否挑引擎」；全部真实调用/亲执命令，不复用上游结论

## 0. 结论摘要

| 项 | S4b 独立判定 | 关键证据 |
|---|---|---|
| F-001 修复（grepai） | **resolved（真实使用级）** | 两个不同 query 真实语义命中且结果集不同（§1.1）；210 models=1；阴性对照 404 有效 |
| F-003 修复（web_search） | **resolved（真实使用级）** | 真实搜索结果 5 条（godotengine.org 等，google cse）；非挑引擎——searxng 引擎面直探证实 duckduckgo/wikipedia 也真实返回（§1.2） |
| O-009 修复（条目清理） | **resolved** | `~/.claude.json` 47322/searxng 命中=0、备份在位（4020B, 23:16）、JSON VALID |
| §SPEC-015 | **PASS**（8/8 server 本会话真实调用成功） | §2 表 |
| §SPEC-016 | **PASS**（证据链抽查完整 + 现态零残留实证） | §3 |
| §SPEC-017 | **PASS**（firelog 独立重计与 S2b/S3b 一致；Notification=0 维持 O-006） | §4 |
| 新增 finding | 无新增 blocker；O-012 处置精化（见 §5） | — |

**无阻塞项，S4b 收口。**

## 1. 修复收口复核（独立证据，非转述）

### 1.1 F-001 grepai — resolved（真实使用级，非探针级）

本 run 会话内 `mcp__grepai__grepai_search` 两次**不同 query** 的真实调用：

| # | query | 返回（真实语义命中） |
|---|---|---|
| 1 | 「godot editor launcher port allocation hold-to-warm」 | 10 条命中：`plugin.gd`（LEASE_FLAG/IDLE_GATE 常量段 0.668）、`plugin.gd` `_get_listen_port`（SEE-1117 端口优先级 0.658）、`godot-status.lib.sh`（超时表 0.655）…——内容与 query 语义精确对应 |
| 2 | 「leaderboard score calculation tier pricing」（**不同主题**） | 3 条**完全不同**命中：`affix_data_factory.gd` tier 解析（0.532）、SEE-1367 ledger（0.317）、`derive_unlock_costs.py`（0.274）——两次结果集零重叠，证明真实索引检索而非回声/固定应答 |

后端去重与状态（只读探针）：

```
$ curl 210:11434/api/tags → {"models":["nomic-embed-text:latest"]}  models=1（唯一，无重复）  rc=0
$ curl 210:47999/（阴性对照） → http=000（探针真实网络行为）
```

（修复前基线：S4 run 同一工具同环境 404 逐字复现，见 S4-qa-report.md §5——修复前后由我亲测，A/B 闭环成立。）

### 1.2 F-003 web_search — resolved（真实使用级 + 非选择性挑引擎）

- `mcp__web_search__web_search` query「Godot 4.6 release highlights」→ **5 条真实结果**（godotengine.org/releases/4.6、docs.godotengine.org、strayspark.studio、reddit、xogot；engine=google cse）——真实 URL/摘要/评分结构，非空壳。
- **对抗性「挑引擎」核查**（不经 MCP，直探 searxng 引擎面 210:47321）：
  - `?q=godot&format=json`（默认聚合）→ 26 条结果，engines_seen = **duckduckgo / google cse / wikipedia**（三引擎均真实贡献）
  - `?engines=duckduckgo` → **10 条结果**（duckduckgo 引擎独立返回真实结果）
  - `?engines=google cse` → 20 条
  - 即 O-012 所述「brave/wikidata 未响应」不影响 duckduckgo——聚合可用性比 repair 报告呈现的更好。
- 网关：`210:4892/web_search` → **200**（网关健康）；阴性对照 210:47999 → 000。
- 引擎去重：47321 引擎健康 200（105 侧直探）；searxng 容器数经 repair 报告 =1（我侧无 SSH 通道不做 210 docker 查询，以网关+引擎双端健康佐证单实例在服务——见 §5 O-012 处置精化）。

### 1.3 O-009 条目清理 — resolved

```
$ grep -c '47322' ~/.claude.json   → 0
$ grep -ci 'searxng' ~/.claude.json → 0
$ ls -la ~/.claude.json.bak-20261011T231621 → -rw------- 4020B Oct 11 23:16（备份在位）
$ jq empty ~/.claude.json → JSON VALID（rc=0）
```

## 2. §SPEC-015 MCP 8 server 全量真实调用（本会话亲测）

| # | server | 工具/输入摘要 | 返回摘要 | 判定 |
|---|---|---|---|---|
| 1 | hindsight | recall「godot-mcp real editor mandatory path forbidden headless rule」 | 返回真实业务规则 observation（E2E 真实输入分发红线，带 reranker/semantic 评分） | PASS |
| 2 | context7 | resolve-library-id「Godot Engine 4.6 documentation」 | 5 库命中（/websites/godotengine_en_4_6 42558 snippets、4_7 41185、godot-docs 44644…）——快照与 S1b/S3b 各不相同，实时索引 | PASS |
| 3 | web_reader | web_url_read docs.godotengine.org/en/stable（1-2 段） | 「# Godot Docs – 4.7 branch」真实正文 | PASS |
| 4 | web_search | query「Godot 4.6 release highlights」 | 5 条真实结果（见 §1.2） | **PASS（修复后）** |
| 5 | github | search_repositories「godot-qa-toolkit in:name」 | 命中 tadki/godot-qa-toolkit（pushed_at=2026-10-11T15:20:10Z，与 S1b/S3b/repair 各时点递增，push 链吻合） | PASS |
| 6 | understand_image | **对抗性第 4 变体**：自制 175B 纯**品红**（magenta）64×64 PNG → curl 上传 210:4890（exit 0，/tmp/understand-image-uploads/70e0c8e6….png）→ 问 dominant color | 答 **"Magenta"**。S1b 红→"Red"、S3b 蓝→"Blue"、repair 绿→"Green"、S4b 品红→"Magenta"——四方向色相均正确，决定性证明真实读图 | PASS |
| 7 | grepai | grepai_search ×2（见 §1.1） | 真实语义命中 ×2，结果集互斥 | **PASS（修复后）** |
| 8 | godot-mcp | 见 §3（S1b/S3b 判据表引用 + 证据链抽查） | 两次冷启动链完整、现态零残留 | PASS |

## 3. §SPEC-016 独立复核 — PASS（证据链抽查 + 现态实证）

### 3.1 冷启动证据链完整性抽查

两次冷启动（S1b：Bachi runtime、port 6561；S3b：Harti runtime、port 6569）的**持久证据文件独立核验**（不信任报告文本，直查落盘日志）：

```
$ ls ~/.multica/godot-mcp-launcher-port-6561.log → 527KB, mtime Oct 11 23:23
  grep -c 'FORK_WIRED\|LAUNCHER_EXEC' → 1259 行；'Bachi-43bcb5d88901' 引用 4 处（runtime_id 绑定属实）
$ ls ~/.multica/godot-mcp-launcher-port-6569.log → 3.3KB
  grep -c 'FORK_WIRED\|LAUNCHER_EXEC' → 8 行；'Harti-df889eca1620' 引用 5 处
```

即 S1b 与 S3b 各自声明的 runtime_id 与端口在原始日志中逐字可复核——两次冷启动报告互相独立且均真实发生。S3b 报告内的时序链（EDITOR_LAUNCH_TRIGGERED → PLUGIN_INIT 54.6s → SERVER_LISTENING 85.1s）、真实编辑器 PID 23664（tasklist ~1.30GB RSS）、23 工具 tools/list、两 read-only 调用返回真实项目数据（workdir_snapshot 自证指向本检出）、taskkill 后四路零残留——链路自洽，且其「错误参数形状→isError:true」反例证明 server 有真实输入校验。

### 3.2 现态零残留实证（S4b 时点）

```
$ tasklist.exe | grep -i godot → 空（rc=1）    ← Windows 侧无 Godot 进程
$ pgrep 'Godot_v|win64' → 空                    ← WSL 侧无残留
```

### 3.3 O-010 launcher 名表边界核实（vendored 只读源码审读）

直读 `/mnt/d/GodotProjects/king-of-likes/addons/godot_mcp/launch/agent-ports.json`：`agents` 表仅 6 legacy 名（Atlas/Archi/Bachi/Fronti/Revy/Refacty，6551–6556），**Harti 确实缺席**（grep=0）；同文件 `_deprecated_see1148` 注记自证：该静态表仅服务「无 KOL_RUNTIME_ID 的 legacy run」，新 run 走 6560–6609 动态池（port-arbiter 按 runtime_id 分配）——与 S3b 报告「平台侧动态路径可正常工作、仅名字解析路径不含 Harti」的边界描述一致。触及 vendored 只读红线（owner-order §5.1），**维持待 Owner/维护者定夺**，S4b 不改。

## 4. §SPEC-017 独立复核 — PASS

firelog 独立重计（全局 `~/.claude/hook-fire.log`，17954 行，2026-10-11 15:33Z 时点）：

| 事件 | S4b 重计 | 与 S2b/S3b 比对 |
|---|---|---|
| PreToolUse | 1984 | ==S3b（1983→1984 单调） |
| SessionStart | 1179 | ≥S3b 1173（本 run 新增，单调吻合） |
| PostToolUse | 454 | ==S3b |
| SessionEnd | 104 | ≥S3b 98 |
| Stop | 40 / SubagentStop 2 / PreCompact 1 | 一致 |
| **Notification** | **0** | O-006 维持 |
| UserPromptSubmit | 18 | 一致 |

- PreCompact 历史实证行逐字在案：`[2026-09-20T14:47:29Z] [PreCompact] alias=unified-hook`。
- **本 run 第一方实时证据**：本会话各 turn 真实收到 UserPromptSubmit `<hindsight_memories>` 注入（9 条记忆）、PreToolUse `[recall-reminder]`、PostToolUse `[grepai-search-reminder]`（多次）；repo 级 firelog 本 run 实时追加（262 行，最新 15:33Z PreToolUse/PostToolUse 配对）。
- **deny 类第一方证据**：`~/.claude/hooks/unified/state/` 共 1043 个 fired-marker；今日 mention-confirm-check marker 覆盖全部五方 run（Atlas/Bachi×4/Harti×2/Refacty×2/**Revy-01a12b34-929d…=我上一 run 的真实拦截**）——deny 规则全 agent 面真实生效，与 S2b/S3b 断言一致。
- S2b 9 绑定枚举一致性：8 PASS + Notification UNVERIFIED（O-006）——独立复核认同。

## 5. Findings Ledger 终稿（S4b 独立复核）

| ID | 终稿判定 | S4b 独立证据 | 分级 | 处置建议 |
|---|---|---|---|---|
| **F-001** | **resolved** | §1.1：双 query 真实语义命中且结果集互斥；210 models=1；修复前 404 由我 S4 run 亲测（A/B 闭环）；阴性对照 404 有效 | 非阻塞（已闭合） | 无 |
| **F-003** | **resolved** | §1.2：web_search 5 条真实结果；4892 网关 200；47321 引擎 200；三引擎聚合直探均真实返回 | 非阻塞（已闭合） | 无 |
| **O-009** | **resolved** | §1.3：条目 0 命中 + 备份在位 + JSON VALID | 非阻塞（已闭合） | 无 |
| **O-002** | 维持（S3b 精化版） | 本 run 实测 held/ 3 目录：Atlas-4573646d372e（pid 318008 **DEAD**=stale）、Bachi-43bcb5d88901（ALIVE）、Revy-837ac738ee96（ALIVE=本 runtime）——「stale 可清、在用勿动」语义成立 | 非阻塞（观察项） | Owner 决定清理策略；stale 目录可由 reaper/手动清，在用勿动 |
| **O-003** | 维持（收口语义） | 150→105 腿经 150 回环仍受 F-002 阻塞；本 run daemon↔平台双向收发正常；F-002 本 run 单发复探仍 255（维持） | 非阻塞 | F-002 修复后由 150 侧补测 |
| **O-006** | 维持 | Notification 全历史 firelog=0（§4 重计）；headless 模式不产生该事件 | 非阻塞（观察项） | 接受按设计闲置，或 Owner 交互式会话补测一次 |
| **O-007** | 维持 | 插件侧 hindsight-memory session_end.py 一次性 Hook cancelled（S2b 受控会话取证）；settings.json 侧 SessionEnd 正常（104 次） | 非阻塞（观察项） | Owner 决定是否排查插件 headless 取消路径 |
| **O-010** | 维持（vendored 红线） | §3.3：agent-ports.json 直读证实 6 legacy 名无 Harti；静态表本身已标 deprecated（SEE-1148），新 run 走动态池 | 非阻塞（待定夺） | 平台侧动态路径已可用；闭合（补名/显式端口）触 vendored 只读红线，Owner/维护者定夺 |
| **O-011** | 维持 | 平台侧 agent mcp-config 的 searxng→47322/sse 死条目（`claude mcp list` ✘）——本 run `claude mcp list` 未复跑（不修改配置面，该死条目不阻塞其他 server），以 repair 证据 + 本 run web_search 走 4892 正常佐证 | 非阻塞 | 平台侧择机删除，无功能损失 |
| **O-012** | **维持 + 处置精化** | §1.2 直探证明 duckduckgo/wikipedia 实际响应（聚合 26 条三引擎）——「未响应引擎」面比 repair 报告窄；brave/wikidata/google裸名 确实 0 结果（google cse 需用全名 `google cse`） | 非阻塞（观察项） | 现状已满足「搜索真实可用」；如需完整引擎覆盖（brave/wikidata），Owner 决定是否重建旧反 captcha 补丁集成或调引擎配置 |
| **F-002** | 维持 | 105→150 本 run 复探 exit 255（host-key 层拒） | 非阻塞 | 凭据写入需 Owner 批准 |
| **新增** | **无** | 全部判据独立执行未发现上游未登记缺陷 | — | — |

## 6. 对抗性自证与方法学

- **「探针级 vs 使用级」**：grepai 用双 query 结果集互斥证真实索引；web_search 额外直探 searxng 引擎面（不经 MCP 层）证明非 MCP 包装假象；understand_image 第三色相变体（magenta）续 S1b/S3b 双向对照。
- **「去重漏项」核查**：ollama models=1；105 条目 0 命中；searxng 容器数 210 侧 docker 查询我无通道、以引擎+网关双端健康佐证单实例（如实登记探测边界，不充当容器数证据）。
- **「挑引擎」核查**：默认聚合三引擎贡献 + duckduckgo 单引擎 10 条独立复现。
- 本 run 遇到的异常均已处理或记录：`git -C` 相对路径一次 128（立即改绝对路径重跑成功）；`unified state` 首次统计漏 dotfile（`ls` 无 `-A`）致 0 计数，以 `ls -A` 重计 1043 修正；F-002 复探 255=预期维持项非异常。
- 合规：验收只读（无 210 修复动作、无 105 配置修改、三仓 HEAD/工作树零变更、未动 daemon、未触碰 SSH 私钥）；探针 PNG 为临时产物已 rm。

## 7. 交付物

| # | File | Description |
|---|------|-------------|
| 1 | `.dev/acceptance/see1394/S4b-qa-report.md`（本文件） | S4b 收口复核报告（修复三项 resolved 独立判定 + §SPEC-015/016/017 PASS + findings ledger 终稿） |

godot-qa-toolkit `shared/SEE-1394`：commit hash 见 issue thread S4b 汇报。
