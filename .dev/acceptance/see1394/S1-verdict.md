# SEE-1394 S1 环境与通道检查报告（Bachi）

- 日期：2026-10-11（本机时区 UTC+8，约 21:00–21:15 采集）
- 执行机：**105 本机**（DESKTOP-INVD2QR / jerry / WSL2）
- 范围：§SPEC-001/002/003/004/008/009/010/011 逐项实证 + 复跑 `~/see1392-verify/verify.sh` 基线 + F-001 复核
- 判据引用：见 SEE-1394 issue description Spec 表（不重抄命令定义）

## 结论摘要

| §SPEC | 判定 | 备注 |
|---|---|---|
| §SPEC-001 机器身份 | **PASS** | 4/4 断言通过 |
| §SPEC-002 工具链与凭据 | **PASS** | verify.sh V3-A1~A9 全 PASS + .credentials.json 权限 600 |
| §SPEC-003 claude 配置树 | **PASS**（V4-A11 按预登记 drift 处理） | V4-A1~A10 PASS；V4-A11 转红=drift，不判缺陷 |
| §SPEC-004 SSH 通道与保活 | **PASS** | V2-A1~A7 全 PASS，宿主网关 banner 3/3 |
| §SPEC-008 三仓检出一一致性 | **PASS** | 四仓 fetch 后 HEAD==upstream；gitlink 与 origin/master 记录一致 |
| §SPEC-009 fork 构建可启动 | **PASS** | V7-A1/A2，4.1.0 exit 0 |
| §SPEC-010 出站平台栈 | **PASS** | 210:9278 http=404（≠000）、210:13581/health=200 |
| §SPEC-011 150↔105 直连 | **部分 FAIL（登记 finding，非阻塞）** | 105→150 转红=F-002；150→105 腿无法从 105 侧复测（见 O-003） |

- verify.sh 复跑：**51 checks，49 PASS / 2 FAIL**，exit=2。两条 FAIL 均已归因（V4-A11 drift 预登记；V6-A4 实为子模块内 untracked 残留，非环境缺陷）。
- **无阻塞项**：multica daemon running（device DESKTOP-INVD2QR）、平台栈双向可达、runtime 平台事件真实收到（本 run 即证据）。

## 逐项实证（命令 + 输出 + 退出码）

### §SPEC-001 机器身份与运行时底座 — PASS

```
$ whoami            → jerry            (exit 0)
$ hostname          → DESKTOP-INVD2QR  (exit 0)
$ uname -r          → 6.18.33.1-microsoft-standard-WSL2 (含 microsoft/WSL2) (exit 0)
$ ls -d /mnt/d/GodotProjects → /mnt/d/GodotProjects (exit 0)
```

### §SPEC-002 工具链版本与二进制一致性 — PASS

verify.sh V3 组（引用运行输出，run 见下）：

```
PASS V3-A1 node v25.9.0
PASS V3-A2 claude 2.1.204 (Claude Code)
PASS V3-A3 multica 0.6.1 (2ea01ae4e...)
PASS V3-A4 multica binary sha256 e069d85e...
PASS V3-A5 uv 0.11.21 (x86_64-unknown-linux-gnu)
PASS V3-A6 uv binary sha256 2856d1bf...
PASS V3-A7 git version 2.43.0
PASS V3-A8 python3 3.12.3
PASS V3-A9 kimi 1.50.0
```

claude 凭据在位且权限正确（V4-A2 sha256 + V4-A3 权限）：`PASS V4-A3 .credentials.json permission 600`。退出码整体 exit=0（该组无失败）。

### §SPEC-003 claude 配置树完整 — PASS（V4-A11 预登记 drift）

V4-A1~A10 全 PASS：

```
PASS V4-A1  settings.json sha256 (08335e59…)
PASS V4-A2  .credentials.json sha256
PASS V4-A3  permission 600
PASS V4-A4  hooks/dispatcher.sh sha256
PASS V4-A5  hooks/hook-wrapper.sh sha256
PASS V4-A6  unified-hook.sh sha256
PASS V4-A7  engine.js sha256
PASS V4-A8  rules-v2.json sha256
PASS V4-A9  rules tree file count == 89
PASS V4-A10 installed_plugins.json sha256
```

```
FAIL V4-A11: known_marketplaces.json sha256 (synced snapshot)
  expected=[95ff27ccdd174c69feff02379f6aa19de3ac40d621b69cb26c1bfc818d56e5b1]
  actual=[e5a3c5c12a8454f6555a77d60e6d1bd13763a72e136e828af37c141cc58f1519]
```

**归因：drift（预登记）**。V4-A11 为同步时点快照钉值，claude 运行时会重写 known_marketplaces.json（SEE-1392 issue description 与 verify.sh 头注均已声明）。非缺陷，无需处置。

### §SPEC-004 WSL SSH 通道与保活 — PASS

V2 组全 PASS：

```
PASS V2-A1 sshd listener running
PASS V2-A2 init.d ssh autostart script present
PASS V2-A3 portproxy 0.0.0.0:2222 connectaddress == live WSL IP
PASS V2-A4 firewall rule enabled: WSL SSH / WSL SSH 2222 / OpenSSH SSH Server (sshd) / Preview (sshd)（4 条均 True）
PASS V2-A5 SSH banner probe via host 172.17.192.1:2222 (3/3)
PASS V2-A6 .wslconfig idle timeouts disabled（instanceIdleTimeout=-1 + vmIdleTimeout=-1）
PASS V2-A7 wsl-keepalive scheduled task Running
```

补充实存值：`hostname -I` 首字段 = `172.17.196.205`；portproxy 行：`0.0.0.0  2222  172.17.196.205  22` —— connectaddress 与实时 WSL IP 一致。

### §SPEC-008 三仓检出与远端一致 — PASS

四仓逐仓 `git fetch` + HEAD vs @{upstream}：

```
/mnt/d/GodotProjects/king-of-likes          master     e357d1bc… == @{u}=origin/master e357d1bc…   [HEAD==upstream]
workdir/KingOfLikes-Godot                   agent/bachi/43bcb5d88901  e357d1bc… == @{u}=origin/master e357d1bc…   [workspace 检出在 agent 分支，HEAD==upstream]
workdir/godot-mcp                           agent/bachi/43bcb5d88901  721d251a… == @{u}=origin/main    721d251a…   [HEAD==upstream]
workdir/godot-qa-toolkit                    agent/bachi/43bcb5d88901  ab0c3bd9… == @{u}=origin/shared/SEE-1394 ab0c3bd9… [HEAD==upstream]
```

fetch 退出码：KOL 主检出首跑一次 `gnutls_handshake() failed`（TLS 抖动，登记 O-001），立即复测 `git fetch` exit=0；其余三仓首跑均成功。

KOL gitlink 复核（fetch 后）：

```
$ git ls-tree origin/master .dev/qa-toolkit addons/godot_mcp
160000 commit ab0c3bd93eaad20e0f5ffca305e1cae0b3943548  .dev/qa-toolkit     (exit 0)
160000 commit 721d251a041519b8c3354986310dfa94cf4604b3  addons/godot_mcp     (exit 0)
$ git submodule status
 ab0c3bd93eaad20e0f5ffca305e1cae0b3943548 .dev/qa-toolkit        （首列空格，无 '-' 前缀）
 721d251a041519b8c3354986310dfa94cf4604b3 addons/godot_mcp     （首列空格，无 '-' 前缀）
```

### §SPEC-009 godot-mcp fork 构建可启动 — PASS

verify.sh V7 组（引用）：

```
PASS V7-A1 dist/cli.js exists and executable
PASS V7-A2 dist/cli.js --version == 4.1.0 (exit 0)
```

### §SPEC-010 出站平台栈可达 — PASS

verify.sh V8 组（引用）+ 附带 V5/V8 交叉项：

```
PASS V8-A1 multica server 210:9278 reachable (http=404, !=000)
PASS V8-A2 hindsight API outbound 210:13581/health == 200
PASS V5-A1 hindsight client config present（~/.hindsight/claude-code.json 在位）
PASS V5-A2 hindsight API 210:13581 /health == 200
```

### §SPEC-011 150↔105 直连免密复测 — **105→150 FAIL（F-002）；150→105 腿无法从 105 侧复测（O-003）**

判据命令与结果：

```
$ ssh -o BatchMode=yes jerry@192.168.1.150 true
Host key verification failed.                                   exit=255   → FAIL
$ ssh -o BatchMode=yes jerry@192.168.1.150 "ssh -o BatchMode=yes -p 2222 jerry@192.168.1.105 hostname"
Host key verification failed.                                   exit=255   → 外层腿已断，内层未及执行
```

按约束「该方向不通则登记 finding，勿修复」，追加只读取证以精确定位归因：

```
$ grep -c '' ~/.ssh/known_hosts        → 3（且无 192.168.1.150 条目）
$ ssh-keyscan -T 5 192.168.1.150       → 捕获 SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.19 banner（TCP/网络层通）
$ ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -v jerry@192.168.1.150 true  （内存态信任，不落盘）
  debug1: Offering public key: /home/jerry/.ssh/id_ed25519 ED25519 SHA256:qMExd0om…
  jerry@192.168.1.150: Permission denied (publickey,password).   exit=255
```

## verify.sh 基线复跑（run 1，2026-10-11）

```
$ bash ~/see1392-verify/verify.sh
=== SUMMARY: checks=51 fails=2 ===
exit=2
```

转红归因（2 条）：

| 断言 | 现象 | 归因 | 处置建议 |
|---|---|---|---|
| V4-A11 | known_marketplaces.json sha256 与钉值不一致 | **drift（预登记）**：claude 运行时重写该文件，快照钉值属同步时点 | 无需处置；后续 verify.sh 硬化（S3）可考虑改为结构校验或接受重写 |
| V6-A4 | KOL worktree clean expected=0 actual=1 | **非环境缺陷**：`git status --porcelain` 输出 ` M addons/godot_mcp`，根因为子模块 addons/godot_mcp 内 untracked 目录 `launch/held/`（运行时残留）；子模块 HEAD 与 KOL 记录的 gitlink 完全一致（721d251a） | 建议 owner 决定清理 `launch/held/`（或纳入 .gitignore）；验收只读，本次未清理 |

复跑仅为单次（连跑两次 diff 属 S3 硬化范围 §SPEC-012）。

## Findings Ledger

| ID | 内容 | 复核结论 | 处置建议 |
|---|---|---|---|
| **F-001**（预登记） | grepai 语义检索后端不可用 | **确认**。三重证据：① `~/.grepai/workspace.yaml` embedder = ollama，endpoint **http://192.168.1.210:11434**，model nomic-embed-text；② `curl http://192.168.1.210:11434/api/tags` 返回 `{"models":[]}`（ollama 服务存活但无模型）；③ `mcp__grepai__grepai_search` 实测复现：`Ollama returned status 404: model "nomic-embed-text" not found` | **纠正预登记的处置建议**：预登记写的是「105 上 ollama pull」——实测 105 本机未安装 ollama（`which ollama` exit=1），embedder 指向的是 **210**。正确处置：在 **210** 上 `ollama pull nomic-embed-text`，pull 后复测 grepai_search（postgres store 在 210:5435 维度 768 与该模型一致，理论无需重建索引；若仍有维度/模型不匹配再重建）。非阻塞，本验收不修复 |
| **F-002**（新登记） | 105→150 免密 SSH 不通（§SPEC-011 判据 FAIL） | 双层归因（均已实证）：① 105 侧 `~/.ssh/known_hosts`（3 行）无 192.168.1.150 条目 → BatchMode 在 host-key 信任阶段即失败；② 绕过信任层后认证仍失败（`Permission denied (publickey,password)`，105 的 id_ed25519 未被 150 接受）。105→150 本身非既定操作通道，非阻塞 | 需 owner 批准后修复（验收勿动凭据）：105 侧 `ssh-keyscan 192.168.1.150 >> ~/.ssh/known_hosts` + 将 105 `~/.ssh/id_ed25519.pub` 追加至 150 `~/.ssh/authorized_keys`（或经 owner 确认密钥方案） |
| **O-001** | KOL 主检出首跑 `git fetch` 一次性 TLS 握手失败（gnutls_handshake） | 立即复测 exit=0，瞬态网络抖动 | 无需处置；若复频建议排查出站网络/代理 |
| **O-002** | KOL V6-A4 转红根因：godot_mcp 子模块内 untracked `launch/held/` 目录 | 运行时残留，非同步缺陷 | owner 决定清理或 .gitignore |
| **O-003** | 150→105 腿（既定操作通道）无法从 105 侧复测——外层需要 105→150，而该方向即 F-002 | 非阻塞：今日 SEE-1392 Harti/Revy 已独立实证该方向（最近已知良好状态 12:34Z 前）；且本 run 经 daemon↔平台双向收发正常，105 侧 sshd/banner 正常（V2-A5 3/3） | 建议 S3/S4 或 150 侧执行者复测 150→105 判据命令（issue 判据原文即为经 150 回环） |

## 附：multica daemon 状态（佐证无阻塞）

```
$ multica daemon status --output json  （截取）
  daemon_id: 019ed3f5-07e8-7b3f-bd23-d6f69cdef9af
  device_name: DESKTOP-INVD2QR
  cli_version: 0.6.1
  active_task_count: 1
```

与 SEE-1392 daemon 注册子步骤交付值（pid 13522 / daemon_id 一致）吻合。
