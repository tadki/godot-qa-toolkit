# SEE-1394 S3 验收硬化报告（§SPEC-012）

> 硬化对象：`~/see1392-verify/verify.sh`（SEE-1392 硬化版，51 断言，shell 脚本）及其 S1/S2 已交付证据。
> 执行者：Harti（105 本机，DESKTOP-INVD2QR / WSL2 jerry）。全部为机器命令证据（命令 + 输出 + 退出码），无 headless/自报充当 PASS。
> 变异/覆盖率维度：硬化目标为 shell 验收脚本，非 .gd/.ts 制品，gqt mutation/coverage、Stryker、vitest 维度不适用；适用硬化判据为 issue description §SPEC-012（双跑一致性 / 反例探测 / 期望注入），全部机器判定。
> verify.sh 本体哈希（实验前后一致，未改动）：`adafd75cce7de7716fb7bca537a0a53c2541c5e03bfcfab4067eeba8f35aee92`

## 1. 硬化计划

| 判据项 | 判据类型 | 目标 | 达标阈值（§SPEC-012 引用） |
|--------|----------|------|------|
| H1 双跑一致性 | 机器命令 | `~/see1392-verify/verify.sh` | 连跑两次 stdout/stderr 逐字节一致、退出码一致；V4-A11 drift 转红除外并登记 |
| H2 反例探针判死 | 机器命令 | 关闭端口 2299 | `ssh-keyscan -p 2299 -T 5 192.168.1.105 \| wc -l` == 0 |
| H3 期望注入复判 | 机器命令 | verify.sh 的 /tmp 临时副本 | 注入错误期望 → 判 FAIL；钉实时真值 → 判 PASS（双向证明断言读实时值） |
| H4 S1/S2 抽样独立复跑 | 机器命令 | §SPEC-002/005/006/007/009/010 判据命令 | 独立执行结果与 S1/S2 报告结论一致 |

## 2. 判定执行结果

### H1 双跑逐字节一致性 — PASS

命令：`bash ~/see1392-verify/verify.sh > runN.out 2> runN.err`（连跑两次）；`cmp run1.out run2.out`；`cmp run1.err run2.err`

| 项 | run1 | run2 |
|---|---|---|
| stdout 字节数 | 2525 | 2525 |
| stderr 字节数 | 0 | 0 |
| 退出码 | 2 | 2 |
| PASS 行数 | 49 | 49 |
| SUMMARY | `checks=51 fails=2` | `checks=51 fails=2` |

`cmp` 结果：stdout `STDOUT_BYTE_IDENTICAL`；stderr `STDERR_BYTE_IDENTICAL`。

两次 FAIL 行完全一致：

```
FAIL V4-A11: known_marketplaces.json sha256 (synced snapshot) | expected=[95ff27ccdd174c69feff02379f6aa19de3ac40d621b69cb26c1bfc818d56e5b1] actual=[e8e2704d8fbec715799530ac66dc4bed9132b30b6afaba8cd615554c175c1ddf]
FAIL V6-A4: KOL worktree clean | expected=[0] actual=[1]
```

- **V4-A11**：预登记 drift（claude 运行时重写 known_marketplaces.json，同步时点钉值），按判据除外登记，不判缺陷。本机实测 actual=`e8e2704d…`，与 S1 观察到的 drift 方向一致。
- **V6-A4**：O-002 登记项。本 run 对抗性复核根因：`git -C /mnt/d/GodotProjects/king-of-likes status --porcelain` → ` M addons/godot_mcp`（exit 0）；子模块 `git status` 无 `+` 前缀（gitlink 与记录一致），脏来源为子模块内 untracked `addons/godot_mcp/launch/held/` 运行时残留。**新观察**：残留正在累积——`launch/held/` 现含 3 个会话目录（`Atlas-4573646d372e`、`Atlas-c36447ba0908`、`Bachi-43bcb5d88901`，见 §3 O-002 注记）。

### H2 反例探针判死 — PASS

SPEC 判据命令：`ssh-keyscan -p 2299 -T 5 192.168.1.105 | wc -l` → **0**（`PIPESTATUS[0]=1`，exit 1 = 连接失败，判死成立）。

对抗性对照（证明探针非恒 0、具有判活能力）：

| 探针 | 结果 keys |
|---|---|
| 关闭端口 `192.168.1.105:2299` | 0（exit 1） |
| 活跃端口 `192.168.1.105:22`（Windows OpenSSH） | 3 |
| 活跃端口 `<HIP>:2222`（verify.sh V2-A5 同路径，经宿主网关） | 3 |

结论：探针双向敏感——死端口判 0、活端口判 3，H2 判据有效。

### H3 期望注入复判（双向）— PASS

注入实验仅在 verify.sh 的 /tmp 临时副本上进行（副本 `chmod 600`，实验后已 `rm`）；本体哈希实验前后一致（见文首）。

**实验 A（PASS→FAIL 方向）**：`sed` 将 V3-A1 期望 `v25.9.0` 改为 `v99.99.99`。

```
INJECT_A_EXIT=3
FAIL V3-A1: node version | expected=[v99.99.99] actual=[v25.9.0]
FAIL V4-A11: …（drift，同 H1）
FAIL V6-A4: …（O-002，同 H1）
=== SUMMARY: checks=51 fails=3 ===
```

错误期望被如实判 FAIL，fails 2→3、exit 2→3，断言非恒 PASS。

**实验 B（FAIL→PASS 方向）**：将 V4-A11 期望从同步时点钉值改为实时真值 `e8e2704d8fbec715799530ac66dc4bed9132b30b6afaba8cd615554c175c1ddf`。

```
LIVE_SHA=e8e2704d8fbec715799530ac66dc4bed9132b30b6afaba8cd615554c175c1ddf
INJECT_B_EXIT=1
PASS V4-A11: known_marketplaces.json sha256 (synced snapshot)
=== SUMMARY: checks=51 fails=1 ===
```

钉实时真值后该断言如实转 PASS，fails 2→1、exit 2→1。**双向证明**：断言读取实时值做比较，不存在硬编码 PASS/FAIL。

### H4 S1/S2 抽样独立复跑（不复用其输出结论）— PASS

全部为本 run 独立执行，与 S1/S2 报告结论逐条比对一致：

| §SPEC | 判据命令（独立复跑） | 实测输出 | rc | 与 S1/S2 结论 |
|---|---|---|---|---|
| 002 | `node -v` | `v25.9.0` | 0 | 一致 |
| 002 | `claude --version \| head -1` | `2.1.204 (Claude Code)` | 0 | 一致 |
| 002 | `multica --version \| head -1` | `multica 0.6.1 (commit: 2ea01ae4e, built: 2026-10-01T10:25:57Z)` | 0 | 一致 |
| 002 | `~/.local/bin/uv --version` | `uv 0.11.21 (x86_64-unknown-linux-gnu)` | 0 | 一致 |
| 002 | `git --version` | `git version 2.43.0` | 0 | 一致 |
| 002 | `python3 --version` | `Python 3.12.3` | 0 | 一致 |
| 002 | `~/.local/bin/kimi --version \| head -1` | `kimi, version 1.50.0` | 0 | 一致 |
| 002 | `sha256sum /usr/local/bin/multica` | `e069d85e…f343`（=钉值） | 0 | 一致 |
| 002 | `sha256sum ~/.local/bin/uv` | `2856d1bf…6e32`（=钉值） | 0 | 一致 |
| 002 | `stat -c %a ~/.claude/.credentials.json` | `600` | 0 | 一致 |
| 005/010 | `curl -s -o /dev/null -w %{http_code} --connect-timeout 8 http://192.168.1.210:13581/health` | `200` | 0 | 一致 |
| 010 | `curl … http://192.168.1.210:9278/` | `404`（≠000） | 0 | 一致 |
| 009 | `node /mnt/d/GodotProjects/king-of-likes/addons/godot_mcp/server/dist/cli.js --version` | `4.1.0` | 0 | 一致 |
| 006 | `multica daemon status --output json` | `status=running`、`device_name=DESKTOP-INVD2QR`、`pid=13522`、`daemon_id=019ed3f5-07e8-7b3f-bd23-d6f69cdef9af`（=SEE-1392 注册值） | 0 | 一致 |
| 006 | `multica issue runs 01a12afe-…` | 含本 run `01a12b26-618e-7da9-b129-df889eca1620`（status=running，trigger_comment_id=`01a12b26-6186-…`，agent_id=77320e17-…=Harti） | 0 | 一致（收侧实证） |
| 007 | `claude -p "ping" --model haiku` | stdout 非空（`pong` 开头） | 0 | 一致 |
| 005 | hindsight MCP 真实召回（本 run 两次：get_page(owner-order) 完整返回；recall(daemon 注册值) 返回 daemon 相关记忆） | 正常内容返回 | — | 一致 |

§SPEC-007 smoke 子进程附注（观察项，非缺陷）：嵌套 claude 会话 stderr 打印了一条 issue metadata `working_branch` 提示（指 KingOfLikes-Godot 仓无 `shared/SEE-1394` 分支——该分支仅存于 godot-qa-toolkit）与一行 SessionEnd hook cancelled 提示；exit 0 且 stdout 非空，判据达成，不另行登记 finding。

## 3. blocker 清单

无新增 blocker。硬化未推翻 S1/S2 任何结论；已登记项复核如下：

| 编号 | 状态 | 本步复核 |
|---|---|---|
| F-001 grepai/ollama | 维持登记（非阻塞，待 Owner 在 210 上 `ollama pull nomic-embed-text`） | 本任务未触碰语义后端 |
| F-002 105→150 免密 | 维持登记（非阻塞，修复需 Owner 批准） | 本任务全部断言本机执行，未涉该通道 |
| O-002 KOL worktree 非净 | 维持登记（非阻塞） | 根因复核确认（子模块 untracked `launch/held/`）；**残留累积中**（S1 时点→现为 Atlas×2 + Bachi×1 共 3 个会话目录），V6-A4 的 actual 计数随残留增长而漂移——若后续要求该断言长期可重复，建议处置时连同残留清理一并考虑（Owner 决定） |
| O-003 150→105 腿 | 维持登记（待 S4 收口） | 105 侧无法复测，同 S1 结论 |

硬化维度新观察（非 blocker）：

- **V2-A5 flap 风险注记**：该断言走网络 banner 探测（3 次连探），双跑窗口内 3/3 一致；但属网络依赖断言，理论上存在 flap 可能。本硬化未发现实际 flap，仅如实注记。
- **V6-A4 计数漂移**：判据值「0」钉的是「无残留」语义而非固定计数；残留持续增长期间，跨时间的复跑 actual 会变化（同一次双跑内仍逐字节一致）。登记为 O-002 的附属注记，处置建议同 O-002。

## 4. 硬化结论

**PASS** — §SPEC-012 三项判据全部达成：① verify.sh 连跑两次 stdout/stderr 逐字节一致、退出码一致（exit=2，2 条 FAIL 均为预登记项：V4-A11 drift 除外登记 + V6-A4 O-002）；② 反例探针判死有效（2299 → 0 keys/exit 1），且对照实验证明探针双向敏感（活端口 → 3 keys），非恒 0；③ 期望注入双向有效（错误期望判 FAIL：fails 2→3；钉实时真值判 PASS：fails 2→1），证明断言读实时值而非硬编码。S1/S2 抽样独立复跑（§SPEC-002/005/006/007/009/010 共 17 条）全部与其报告结论一致。未发现「探针判不死 / 双跑漂移 / 注入判不出」类反例。verify.sh 可作为 S4 真机验收的可信基线（V4-A11 按 drift 语义解读）。

## 5. 测试产物

| # | File | Description |
|---|------|-------------|
| 1 | `.dev/acceptance/see1394/S3-hardening.md`（本文件） | S3 验收硬化报告（§SPEC-012 全判据结构化证据） |

硬化实验的 verify.sh 注入副本（/tmp/verify-inject-A/B.sh）为临时产物，`chmod 600` 后已于实验结束 `rm` 删除（owner-order §4 条 45）；verify.sh 本体未做任何改动（哈希前后一致）。
