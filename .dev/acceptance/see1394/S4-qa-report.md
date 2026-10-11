# SEE-1394 S4 真机 QA 报告（Revy，2026-10-11）

- 执行机：**105 本机**（DESKTOP-INVD2QR / jerry / WSL2 kernel 6.18.33.1-microsoft-standard-WSL2）
- 范围：§SPEC-001~013 **独立复核**——不复用上游结论，逐条亲自执行判据命令采集结构化证据；verify.sh 仅作辅助基线（本体 sha256 核对 + 源码审读 + 注入复判先行完成）
- 心态：对抗式——以「默认上游结论是错的」预设执行，主动找反例（见 §3 过程反例与 §5 findings）

## 0. §SPEC-013 前置：verify.sh 可信基线核验

| 核验项 | 命令 | 结果 | 退出码 |
|---|---|---|---|
| 附件↔落盘逐字节一致 | `multica attachment download 01a12ad2-7dff-72b9-9e2a-3ddb0920300c` + `cmp` | SEE-1392 附件 verify.sh（11191B）与 `~/see1392-verify/verify.sh` **BYTE-IDENTICAL**，sha256 同为 `adafd75cce7de7716fb7bca537a0a53c2541c5e03bfcfab4067eeba8f35aee92` | 0 |
| 与 S3 实验前后哈希一致 | `sha256sum` | `adafd75c…` == S3-hardening.md 记录值（S3 注入实验未触碰本体，本次复核再证） | 0 |
| 源码审读 | 通读 165 行全文 | 51 断言均为实时读值 `assert_eq`/`assert_file_sha` 比较，无硬编码 PASS；输出无时间戳/PID（确定性）；FAIL 输出 expected/actual 可追溯 | — |

结论：verify.sh 可作辅助基线；但 S4 判据以**我自己的命令**独立执行为准（下文各节均为独立取证，verify.sh 双跑仅作 §SPEC-012 复核项）。

## 1. §SPEC-001~012 逐条独立结论

### §SPEC-001 机器身份与运行时底座 — PASS

```
$ whoami    → jerry            (exit 0)
$ hostname  → DESKTOP-INVD2QR  (exit 0)
$ uname -r  → 6.18.33.1-microsoft-standard-WSL2   （含 microsoft/WSL2，PASS）
$ ls -d /mnt/d/GodotProjects → 目录在位 (exit 0)
```

### §SPEC-002 工具链版本与二进制一致性 — PASS

| 判据 | 实测 | 退出码 |
|---|---|---|
| node | v25.9.0 | 0 |
| claude | 2.1.204 (Claude Code) | 0 |
| multica | multica 0.6.1 (commit: 2ea01ae4e, built: 2026-10-01T10:25:57Z) | 0 |
| multica binary sha256 | `e069d85efcac007a3eeb9b029ec8d4b12a13aac591c13badf4ca97abb3cef343` ==钉值 | 0 |
| uv | uv 0.11.21 (x86_64-unknown-linux-gnu) | 0 |
| uv binary sha256 | `2856d1bf7e5bcfba7df20ae6f939340ae1f8022d2d103cada1a6da597df86e32` ==钉值 | 0 |
| git | git version 2.43.0 | 0 |
| python3 | Python 3.12.3 | 0 |
| kimi | kimi, version 1.50.0 | 0 |
| credentials 权限 | `stat -c %a` → 600 | 0 |

### §SPEC-003 claude 配置树完整 — PASS（V4-A11 按预登记 drift，不判缺陷）

独立 sha256 逐文件比对（与 150 参照钉值）：

```
PASS V4-A1  settings.json        08335e59… == 钉值
PASS V4-A2  .credentials.json    c358c489… == 钉值
PASS V4-A4  hooks/dispatcher.sh  65cba9d3… == 钉值
PASS V4-A5  hooks/hook-wrapper.sh 2f304cd8… == 钉值
PASS V4-A6  unified/unified-hook.sh 2e3aca61… == 钉值
PASS V4-A7  unified/engine.js    50b908a9… == 钉值
PASS V4-A8  unified/rules-v2.json e1435a80… == 钉值
PASS V4-A9  rules 树文件数 = 89（期望 89）
PASS V4-A10 installed_plugins.json 7c6cbc58… == 钉值
FAIL V4-A11 known_marketplaces.json expected=95ff27cc… actual=bb284fa1…（exit 0，FAIL 如实判出）
```

V4-A11 归因 **drift（预登记）**：该文件为 claude 运行时重写的同步时点快照，非长期不变量（SEE-1392 Final Review 登记 + issue description 已声明）。本 run 实测 actual `bb284fa1…` 与 S1 时点 `e5a3c5c1…`、S3 时点 `e8e2704d…` 均不同且方向连续——正是「运行时持续重写」的预期表现，不判缺陷。

### §SPEC-004 WSL SSH 通道与保活 — PASS

| 断言 | 实测 | 退出码 |
|---|---|---|
| sshd listener | `sshd: /usr/sbin/sshd -D [listener]`（pid 1640） | 0 |
| init.d 自启脚本 | /etc/init.d/ssh 存在（root, 0755） | 0 |
| portproxy | `0.0.0.0  2222  172.17.196.205  22`，与实时 `hostname -I` 首字段 **172.17.196.205 一致** | 0 |
| 防火墙 4 规则 | `OpenSSH SSH Server Preview (sshd)\|True` / `OpenSSH SSH Server (sshd)\|True` / `WSL SSH\|True` / `WSL SSH 2222\|True` | 0 |
| 宿主网关 banner 3 连探 | HIP=172.17.192.1，`ssh-keyscan -p 2222` ×3 → **3/3** 含 ssh- banner | 0 |
| .wslconfig | `instanceIdleTimeout=-1` + `vmIdleTimeout=-1` 均在 | 0 |
| wsl-keepalive 计划任务 | 状态 **Running** | 0 |

### §SPEC-005 hindsight 功能可用 — PASS

| 断言 | 实测 | 退出码 |
|---|---|---|
| client 配置在位 | ~/.hindsight/claude-code.json 存在（0600），endpoint=http://192.168.1.210:13581 | 0 |
| /health==200 | `curl -w %{http_code}` → **200** | 0 |
| 真实召回（本 run 实证） | `agent_knowledge_recall("SEE-1392 105 迁移 daemon 注册 daemon_id device_name", max_tokens=512)` 返回 5 条带评分记忆；`agent_knowledge_get_page(owner-order)` 返回完整 standing rules 页（本报告遵守其规则） | — |

### §SPEC-006 multica daemon 与 issue 收发闭环 — PASS

| 断言 | 实测 | 退出码 |
|---|---|---|
| daemon status | `status=running`、`device_name=DESKTOP-INVD2QR`、`daemon_id=019ed3f5-07e8-7b3f-bd23-d6f69cdef9af`（==SEE-1392 注册值）、pid 13522、uptime 1h25m10s、failed_terminal_report_count=0 | 0 |
| 收侧：本 run 真实收到 | `multica issue runs` 含 **本 run `01a12b34-929d-7314-86fd-837ac738ee96`，status=running，agent=2660961d…（Revy），trigger_comment_id=01a12b34-928f-7592-bb4a-a49b21237ee6**（即本次派单评论） | 0 |
| 发侧：评论回执 | S4 汇报评论经 `multica issue comment add --parent 01a12b34-928f-… --content-file ./reply.md` 发出，exit 0；回执 ID 见 issue thread（后以补记 commit 回填至本报告） | 0 |

未做任何 stop/restart；daemon 判 running 以 JSON 字段为准（非名义进程）。

### §SPEC-007 claude 会话可正常起 — PASS

```
$ claude --version → 2.1.204 (Claude Code)   (exit 0)
$ claude -p "ping" --model haiku  → stdout "pong"（非空）  (exit 0)
```

smoke 走真实 claude CLI 子进程（timeout 120s 罩护），非「本会话能跑」替代。

### §SPEC-008 三仓检出与远端一致 — PASS

四仓 `git fetch`（唯一许可的写侧操作，均 rc=0）后逐仓比对：

| 检出 | 分支 | upstream | HEAD | 一致 |
|---|---|---|---|---|
| /mnt/d/GodotProjects/king-of-likes | master | origin/master | e357d1bc52da7e4ff86519ab7133afc60e6568df | **HEAD==upstream** |
| workdir/KingOfLikes-Godot | agent/revy/837ac738ee96 | origin/master | e357d1bc… | **HEAD==upstream** |
| workdir/godot-mcp | agent/revy/837ac738ee96 | origin/main | 721d251a041519b8c3354986310dfa94cf4604b3 | **HEAD==upstream** |
| workdir/godot-qa-toolkit | agent/revy/837ac738ee96 | origin/shared/SEE-1394 | c59d282c3ae7072e15248b2bd1741943bce6f037 | **HEAD==upstream**（本报告交付 commit 除外，见 §4） |

- KOL gitlink 复核：`git ls-tree origin/master` → `.dev/qa-toolkit=ab0c3bd9…`、`addons/godot_mcp=721d251a…`，与 origin/master 记录一致。
- `git submodule status` 两行首列均为空格，**无 `-` 前缀**。
- KOL 主检出 worktree 非净（` M addons/godot_mcp`，count=1）：归因 O-002（子模块内 untracked 残留），非环境缺陷，按观察项语义登记。

### §SPEC-009 godot-mcp fork 构建可启动 — PASS

```
$ ls -la <KOL>/addons/godot_mcp/server/dist/cli.js → -rwxrwxrwx … 3178 B（存在且可执行）
$ node <CLI> --version → 4.1.0   (exit 0)
```

### §SPEC-010 出站平台栈可达 — PASS（附双向对照）

```
$ curl 210:9278/          → http=404（≠000，可达）
$ curl 210:13581/health   → http=200
[对照] curl 210:9/（死端口） → http=000 ——探针确为真实网络行为，非自报
```

### §SPEC-011 150↔105 直连免密复测 — 105→150 FAIL（**F-002 维持**，非阻塞）；150→105 腿按 O-003 语义收口

```
$ ssh -o BatchMode=yes -o ConnectTimeout=8 jerry@192.168.1.150 true
Host key verification failed.                                   exit=255 → FAIL（105→150）
[归因层 1] grep '192.168.1.150' ~/.ssh/known_hosts → 无匹配（文件共 3 行）
[归因层 2] ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null …（仅内存态信任，不落盘）
  → Warning: Permanently added … / jerry@192.168.1.150: Permission denied (publickey,password).  exit=255
[TCP 层] ssh-keyscan -T 5 192.168.1.150 → 捕获 SSH-2.0-OpenSSH_9.6p1 banner（网络层通）
```

- **105→150 腿**：FAIL 与 F-002 登记一致，双层归因（host-key 层 + 认证层）独立复现。按约束不修复（涉及凭据写入需 Owner 批准）。
- **150→105 腿**：其判据本身经 150 回环，外层即 F-002，无法从 105 侧复测 → 按 **O-003 语义**收口登记。补充实证（不替代判据，仅佐证入站路径完好）：从 105 侧直探 LAN 入口 `ssh-keyscan -p 2222 -T 5 192.168.1.105` → 返回 **2 条 ssh- keys**，即 150→105 将走的 2222 portproxy 通道在 LAN 侧确实在服务 SSH；叠加本 run daemon↔平台双向收发正常（§SPEC-006）。
- 遵守约束：存活判据用 TCP 2222 banner，全程未以 ping 断言。

### §SPEC-012 验收硬化复核 — PASS（S4 独立复执行，非引用 S3 输出）

| 判据 | S4 独立复执行 | 结果 | 退出码 |
|---|---|---|---|
| 双跑逐字节一致 | `bash verify.sh` ×2 → `cmp` stdout/stderr | **BYTE-IDENTICAL**（两跑均 `checks=51 fails=2`，exit 均=2；FAIL 仅 V4-A11 drift + V6-A4/O-002，与预登记一致） | 0 |
| 反例探针判死 | `ssh-keyscan -p 2299 -T 5 192.168.1.105 \| wc -l` | **0**（判死）；对照：:22 → 3 keys、宿主网关:2222 → 3 keys，探针双向敏感非恒 0 | 0 |
| 期望注入复判 | /tmp 副本（chmod 600）sed 注入 `v99.99.99` | V3-A1 如实判 FAIL，`checks=51 fails=3`、exit=**3**（本体 2→3）；实验后副本已 rm，本体 sha256 复验仍 `adafd75c…` 未动 | 3（注入副本，预期值） |

全程无 headless/自报充当 PASS。

### §SPEC-013 QA 真机独立复核 — PASS（本报告即交付物）

前置（附件↔落盘哈希核对 + 源码审读）见 §0；§SPEC-001~012 逐条独立判据执行见 §1；findings ledger 独立复核见 §5。

## 2. verify.sh 辅助基线双跑记录

```
run1: === SUMMARY: checks=51 fails=2 ===  exit=2
run2: === SUMMARY: checks=51 fails=2 ===  exit=2
stdout/stderr cmp → BYTE-IDENTICAL
FAIL V4-A11: expected=[95ff27cc…] actual=[bb284fa1…]   → drift（预登记，不判缺陷）
FAIL V6-A4:  expected=[0] actual=[1]                    → O-002（观察项，不判缺陷）
```

## 3. 对抗过程反例（QA 自证，非环境发现）

- **自建校验脚本一次假性全 PASS 被抓**：S4 首版 V4 组比对脚本因 bash 关联数组含空格 key 被 `tr` 拆散，导致全部 PASS 为「空值对空值」的假 PASS。以修正版脚本重跑后才得出 §SPEC-003 真实结论（其中 V4-A11 如实判 FAIL）。该反例说明本报告所有 PASS 均经过真实值比对，方法学上不可自报。
- **V4-A11 drift 值三次不同**（e5a3c5c1→e8e2704d→bb284fa1）：若 verify.sh 把它判 PASS 反而可疑；如实判 FAIL + drift 语义登记才是正确行为。

## 4. 交付物

| # | File | Description |
|---|---|---|
| 1 | `.dev/acceptance/see1394/S4-qa-report.md`（本文件） | S4 真机 QA 报告：§SPEC-001~013 逐条独立结论 + findings ledger 终稿 |

godot-qa-toolkit `shared/SEE-1394`：本报告 commit hash 见 issue thread 汇报（S4 汇报评论附回执）。

## 5. Findings Ledger 终稿（S4 独立复核）

| ID | 内容 | S4 独立复核 | 分级 | 处置建议 |
|---|---|---|---|---|
| **F-001** grepai 语义检索后端不可用 | **确认**（三重独立证据）：① 本 run 直接调用 `mcp__grepai__grepai_search` 现场复现 `Ollama returned status 404: model "nomic-embed-text" not found`；② `curl 210:11434/api/tags` → `{"models":[]}`（服务存活无模型）；③ `~/.grepai/workspace.yaml` embedder 指向 **210:11434**/nomic-embed-text（768 维，postgres store 210:5435）；105 本机无 ollama（`which ollama` rc=1） | 非阻塞 | 维持 S1 纠正后的处置：在 **210** 上 `ollama pull nomic-embed-text`（store 维度 768 匹配，理论无需重建索引）；待 Owner 决定，验收不修复 |
| **F-002** 105→150 免密 SSH 双层不通 | **确认**（独立复现）：层 1 known_hosts 无 150 条目（host-key 层即拒，exit 255）；层 2 内存态信任后 `Permission denied (publickey,password)`（105 id_ed25519 未被 150 接受）；TCP 层通（keyscan 有 banner）。105→150 非既定操作通道 | 非阻塞 | 修复需 Owner 批准（凭据写入）：105 侧 `ssh-keyscan 192.168.1.150 >> ~/.ssh/known_hosts` + 105 公钥追加至 150 authorized_keys；验收勿动 |
| **O-002** KOL worktree 非净（子模块 untracked `launch/held/`） | **确认且持续累积**：根因子模块内 `?? launch/held/`（gitlink 本身与记录一致）；held 会话目录现 **4 个**（Atlas-4573646d372e、Atlas-c36447ba0908、Bachi-43bcb5d88901、Revy-837ac738ee96——含本次 S4 runtime 自身），S1 时点 1 个 → S3 时点 3 个 → S4 时点 4 个。V6-A4 actual 计数随残留漂移 | 非阻塞（观察项） | Owner 决定：清理残留或纳入 .gitignore（建议连 V6-A4 断言语义一并处理） |
| **O-003** 150→105 腿无法从 105 侧复测 | **按语义收口**：判据经 150 回环、外层即 F-002；佐证入站路径完好（105 侧直探 `192.168.1.105:2222` 返回 2 条 ssh- keys；本 run daemon↔平台双向收发正常）；该方向最近已知良好（SEE-1392 12:34Z 独立实证） | 非阻塞 | 如 Owner 需要 end-to-end 复测，可在 F-002 修复后由 150 侧执行者跑判据命令 |
| **无新增 finding** | S4 全部判据独立执行未发现上游未登记的环境缺陷；两条 verify.sh 转红均落在预登记 drift/观察项语义内 | — | — |

## 6. 约束遵守声明

- 验收只读：未修改 105 任何运行时状态、未动三仓 HEAD/工作树（唯一 git fetch 为许可操作；本报告 commit 仅写入 godot-qa-toolkit 交付路径，§SPEC-008 对应的 upstream 一致性以 fetch 时点为准并如实登记）；未 stop/restart daemon；未触碰任何 SSH 私钥（F-002 归因仅用内存态信任参数，不落盘）。
- 严禁 headless/自报：每条断言附命令+输出+退出码；工具链/平台栈探针均附对照实验。
- 注入实验仅在 /tmp 副本（chmod 600，实验后 rm），verify.sh 本体哈希前后一致。
