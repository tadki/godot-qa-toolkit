# SEE-1268：2 库拆库与无感同步 设计文档

> 责任人：Bachi（实施）· 来源：SEE-1256 v4 终版方案 + Owner 拍板的 2 库拆分架构修正（Atlas 2026-09-09 转达）
> 状态：in_progress · 分支：shared/SEE-1268

## 0. 架构基线（Owner 已批，替代 v4 单库案）

**v4 md 原案为单库**（godot-qa-toolkit 内含 mcp/ 控制面 + gherkin/complexity/mutation/coverage/determinism），已由 Owner 修正为 **2 库拆分**：

```
① godot-mcp（运行态库 = tadki fork 升级版）
   addons/godot_mcp/   ← addon GDScript（17 commands + ws + lease，editor 内）
   server/ + proxy/    ← 上游 node 代理层
   launch/             ← 我们的补丁控制面（.dev/godot-mcp/ 现有内容抽出）

② godot-qa-toolkit（工具态库，新建）
   gherkin/ coverage/ mutation/ complexity/ godot-determinism/
   统一 CLI + JSON 契约

③ KingOfLikes（消费方，零复制）
   addons/godot_mcp/  → submodule 指 ①
   .dev/qa-toolkit/    → submodule 指 ②
```

**关键修正语义**：
- 控制面（launcher/proxy/shim/端口仲裁）职责是"拉起并驱动 addon"，必须与 addon 同属运行态库 ①，**不进 ②**（纠正 v4 画岔处）。
- addon 源码权威源 = ① fork（上游内核不动，只做"新 tool 注册"式增量）。
- godot-determinism 的逻辑/调度在 ②；需要的新 commands（pixel region/UI hit/虚拟光标）注册进 ① 的 addon——两库在确定性断言上协作，职责分离。
- 拆库理由：fork（运行态：怎么驱动 Godot）与 qa-toolkit（工具态：测试用什么手段）职责本质不同，合库互相拖累演进。

## 1. 目标

把五阶段 QA 工具链（②）与 godot-mcp 运行态控制面（①）从 KingOfLikes 拆成两个独立通用库，实现三处无感自动同步：

1. 各库 remote master 永远最新（各库自身开发流）
2. 游戏库 master 的 submodule 指针与内容自动到最新（repo-sync 双入口补丁）
3. 全部 agents worktree checkout 后工具原地可用（hook 侧 submodule 兜底）

全程无人工步骤。vendored addon 内核只读（upstream 不动），增量走 fork。

## 2. 现状实证（源码级结论，落地依据）

### 2.1 双线同步链路

| 线 | 链路 | 现状 |
|---|---|---|
| **master 线** | systemd `kingoflikes-start-all.service` → `.dev/launch/kingoflikes-start.sh` → `repo_sync_daemon.py`（5s 轮询）→ `repo_sync.py::run_sync` | 只管游戏库 D 盘 master ↔ origin；`do_pull_ff_only` 成功后**无任何 submodule update**（全库 grep 证实）；`has_submodule_changes()`（repo_sync.py:486）把 `-` 未初始化判 intermediate → daemon 拒同步（保护行为） |
| **agents 线** | SessionStart hook → `.claude/hooks/repo-checkout.sh` → `multica repo checkout`（daemon bare 缓存 worktree） | bare 缓存 worktree 不含 submodule 内容；hook 全文无 submodule 处理（grep 证实）；对齐后调 `prepare-worktree.sh` 只写 lease |

### 2.2 控制面（`.dev/godot-mcp/launch/`）耦合面 —— 拆 ① 时用

- **自包含内核**：`godot-mcp-shim.mjs`（findRepoRoot 上溯 6 级找 project.godot）、`godot-mcp-proxy.mjs`（4400 行，7 个本地 import 全部同目录相对引用：see1129×2、see1240×3、warmup-stage-parser、godot-mcp-resolve）、launcher（929 行）、start-godot-editor.sh、configure-mcp-port.sh、5 个 lib.sh、agent-ports.json。
- **repo 强耦合点（拆库需参数化）**：
  - `SHARED_MASTER_WORKTREE="/mnt/d/GodotProjects/king-of-likes"`（proxy :2218 / launcher :224 / configure :227）——写成 `KOL_SHARED_MASTER` 环境变量带默认值
  - launcher 中 7 处 `workdir/KingOfLikes-Godot` 字面拼接（runtime registry 解析）——改成 `KOL_REPO_DIRNAME` 可配置、默认 KingOfLikes-Godot
  - proxy `isGodotWorktree()` 探测 `.dev/godot-mcp/launch` 目录存在——迁移后路径探测需兼容新旧两处
  - `.mcp.json`（git 跟踪）args 指向 `.dev/godot-mcp/launch/godot-mcp-shim.mjs`
  - 约 80 个文件引用 `.dev/godot-mcp/launch` 路径（含 82 个 godot-mcp 测试 + 23 个 .dev/tests + 3 个 .claude hooks）
- **外部依赖**：proxy 经 `godot-mcp-resolve.mjs` 启动 fork 的 server（`/mnt/d/GodotProjects/forks/godot-mcp/server/dist/cli.js`，tadki/godot-mcp，本地有未提交 SEE-1141 Track D 绝对鼠标改动）。
- **vendored addon**（`addons/godot_mcp/`）与 launch 控制面**零代码引用**（grep 证实）——只通过 sidecar lease 文件（`.godot/mcp-lease.json`）和 WebSocket 端口握手解耦。红线内核独立于控制面，迁移控制面不触碰内核。

### 2.3 五阶段工具现状 —— 建 ② 时用

- **复杂度**：gdtoolkit 4.5.0 已装（MIT、活跃），`gdradon cc` 可用但**文本输出无 JSON**；底层 API（`gdtoolkit.parser` + `radon.cc_visit` via `gd2py.convert_code`）可编程调用，且 gdtoolkit 自带 lark GDScript AST（tree-sitter 级地基）。复杂度 gate 直接用其 API。
- **Gherkin**：自研（Owner 否决 fork Akdr：3 天 0-star 玩具）。轻量 parser + GUT 桥接 runner。
- **mutation**：生态空白，自研简单版（lark AST 算子 AOR/ROR/UOI/边界 + GUT 子集运行 + kill rate）。
- **coverage**：自研简单版行覆盖（M2）。
- **godot-determinism**：逻辑/调度在 ②；需要的确定性命令（pixel region/UI hit/虚拟光标）注册进 ① addon——两库协作。

## 3. 库结构

### 3.1 ② godot-qa-toolkit（工具态，已建 tadki/godot-qa-toolkit）

```
godot-qa-toolkit/
├── README.md / LICENSE / pyproject.toml
├── src/godot_qa_toolkit/
│   ├── cli.py                # 统一 CLI：gqt <subcommand>，全部输出统一 JSON 契约
│   ├── gherkin/              # 自研 parser + runner + (M2) godot_bridge GUT 桥
│   ├── complexity/           # gdtoolkit API（gd2py + radon.cc_visit）封装 + 阈值 gate
│   ├── mutation/             # M2：AST 算子 + runner + kill rate
│   ├── coverage/             # M2：行覆盖插桩
│   └── godot-determinism/    # M2+：确定性断言逻辑/调度（命令注册进 ①）
├── tests/                    # pytest（自身 TDD 骨架）
└── docs/
```

> 无 mcp/ 目录、无 addon 源码（2 库修正：控制面进 ①、addon 权威源进 ①）。

### 3.2 ① godot-mcp（运行态，= tadki fork 升级，M3 组装）

```
godot-mcp/                    # 已有 tadki/godot-mcp fork（server 已随上游）
├── addons/godot_mcp/         # ← 从 KOL addons/ 迁入（upstream 内核只读，增量注册）
├── server/                   # ← 上游 node 代理层（fork 现状）
├── proxy/ 或等价             # ← 上游 proxy（fork 现状）
└── launch/                   # ← 从 KOL .dev/godot-mcp/launch/ 抽入控制面
```

**统一 JSON 契约**（② 所有子命令，P0' 落点）：

```json
{"tool": "complexity", "ok": true, "failures": [], "summary": {...}}
{"tool": "gherkin", "ok": false, "failures": [{"scenario": "...", "reason": "..."}], "summary": {"passed": 3, "failed": 1}}
```

每个工具输出机器可判 `ok` + 结构化 failures；exit 0/1。主观判定不进工具。

## 4. 分期落地

### M1（已完成，commit `8a2d58bc` @ shared/SEE-1268 + tadki/godot-qa-toolkit@`06e2814`）

1. **repo-sync submodule 改造**（master 线）：`do_submodule_update`（`update --init --recursive`）；`do_pull_ff_only` / LOCAL_AHEAD push / DIVERGED rebase+merge 全部 4 成功路径追加。`has_submodule_changes()` 判定不变（保护行为），靠 init 消除 `-`。测试 5 case（真实 temp git submodule 链路）。
2. **agents 线兜底**：`repo-checkout.sh` `ensure_submodules_materialized`（限时 60s，失败出 hint 不阻塞），checkout + 对齐两处调用。测试 9 case。
3. **② 骨架 + Gherkin + complexity gate**：tadki/godot-qa-toolkit 建库（32 pytest）；gherkin 严格自研 + complexity gdtoolkit API + 统一 JSON 契约；`--recursive` 一次带两级 submodule。
4. **submodule 挂回（② 挂载点）**：`.dev/qa-toolkit/` → tadki/godot-qa-toolkit。

> M1 全部工作与 2 库架构**兼容**：sync 双入口架构无关；gherkin/complexity 属 ②；`.dev/qa-toolkit` 挂载点即 ② 挂载点。2 库修正只把"控制面进 ② mcp/"改为"控制面进 ①"，M1 未把控制面迁进 ②（仅空占位，未提交），无需返工。

### M2（同 issue 内，工具态 ② 深化）

mutation runner（lark AST 算子）+ coverage 行覆盖 + godot_bridge GUT 桥 + godot-determinism 逻辑/调度骨架。

### M3（功能稳定后，运行态 ① 组装 —— 独立成批）

- **① godot-mcp 运行库组装**：
  - addons 源码权威源迁入 ①（addons/godot_mcp/ 从 KOL 移入 fork；upstream 内核只读，增量"新 tool 注册"式演进）
  - `.dev/godot-mcp/launch/` 控制面整体抽入 ① launch/；参数化 `KOL_SHARED_MASTER` / `KOL_REPO_DIRNAME` / isGodotWorktree 探测路径
  - SEE-1141 Track D 本地未提交改动补 commit/push（Owner 已确认"改了要马上能用"）
- **消费方改造**：KingOfLikes `addons/godot_mcp/` → submodule ①；`.dev/godot-mcp` 路径引用（约 80 处）按 grep 清单逐项换；`.mcp.json` + 3 个 .claude hooks + daemon 启动链适配
- **控制面测试树**（72 sh + 17 mjs）随迁 ①，路径引用批量替换
- **② 的 godot-determinism 确定性命令注册进 ① addon**（两库协作闭环）

**M3 独立成批的原因**：控制面 + addon 是生产关键路径（6 个 agent 的 MCP 工具全靠它），addon 权威源切换 + 80 处引用 + daemon 参数化改造必须独立 QA 轮次，避免与工具态混批。v4 方案"先在 KingOfLikes worktree 开发跑通、功能稳定后抽库"即指此节奏。

## 4.5 M3 执行计划（Owner 拍板 · 2026-09-10）

> 来源：Owner 构想（godot-mcp 所有功能进一个库、与 KOL 解耦、KOL master submodule 挂 master 直接能用、可单独安装到其他 Godot 项目）+ Revy 实测论证（`kol-addon-hist` 存在的唯一原因是「submodule 无法挂子路径」，重组 main 后该前提消失）。本计划替代 §4 M3 的旧表述（旧表述仍可作为范围索引，冲突处以本节为准）。
> **目标库形态**（Owner 已确认）：

```
tadki/godot-mcp（main，重组后）
├── plugin.cfg / plugin.gd / commands/ / core/ / game_bridge/ / ui/   ← addon 本体在根（submodule 挂载点直接可用）
├── launch/          ← 控制面 33 项（sh/mjs/py/json，0 个 .gd，不进 Godot 扫描）
├── server/          ← node 代理层（ts，0 个 .gd）
├── docs/ README/ LICENSE/ INSTALL/ CONTRIBUTING/
└── test/            ← 上游自带的 addon test/ 剔除（对齐现 76 文件消费基线）
```

KOL 侧：`addons/godot_mcp` submodule **改指 main**；`kol-addon-hist` 快照分支**退役**。解耦后的 KOL 专属设置（master worktree 路径、agent 端口分配、systemd 路径）**留在 KOL 仓库**（KOL 侧 env/配置文件），godot-mcp 库内只留「通用默认值 + 环境变量可覆盖」。

### 4.5.1 现状实证（2026-09-10 复核，落地依据）

| 事实 | 数据 |
|---|---|
| fork main 现状 | `a429416`，目录为上游结构：addon 在 `godot/addons/godot_mcp/`（嵌套）+ `launch/`（33 项，Bachi 已迁入 main）+ `server/` |
| `kol-addon-hist` | `5719847`，与 main 的 addon 子树**路径+内容完全一致**（blob 同 SHA，含 SEE-1141 Track D 绝对鼠标）；差异仅 main 子树多出 `test/`（45 文件）——快照分支 = 剔除 test/ 的消费视图 |
| main→快照落后 | 快照 ahead_by 2（两次快照导出 commit），main 无领先 commit |
| KOL 消费现状 | `addons/godot_mcp` gitlink = `5719847`（= 快照 head），生产运行正常 |
| launch/ KOL 硬编码 | 25/33 文件含 `KOL_`/`king-of-likes`/`KingOfLikes-Godot` 引用；`KOL_XXX` 形态的**环境变量已是参数化接口**（launcher :117-135 用法表、proxy :97-333 共 20+ 个 `KOL_*` env 读取）——真正需要改的是**硬编码路径字面量**：`SHARED_MASTER_WORKTREE="/mnt/d/..."`（launcher :224 / proxy :2218 / configure :227 / prepare-worktree :142）、`workdir/KingOfLikes-Godot` 目录名拼接（launcher :303,362,418,458,466,482,491 共 7 处）、reaper.service 的 ExecStart 绝对路径（installer 戳入，留 KOL 侧解决）、`kol-runtime.lib.sh` 文件名本身 |
| isGodotWorktree 探测 | `project.godot` + `.dev/godot-mcp/launch` 存在（proxy :2229）——**两层判断中第二层在重组后失效**（launch 移到子库内、探测方与被探测方同库），需改为「project.godot + launch/ 同库存在」或按新布局调整 |
| KOL 消费方依赖点 | `.mcp.json`（args 指 `.dev/godot-mcp/launch/godot-mcp-shim.mjs`）；3 个 .claude hooks（repo-checkout :388/:722、auto-pr :65/:102、push-guard :296）按路径调 launch 内脚本；`.dev/tests` 22 处 + `.dev/godot-mcp/tests` 35 处引用 launch 路径；`docs/project/DESIGN.MD` 提及 MCBGameBridge 归属 |
| CI | `ci-lint.yml` prune 了两个 submodule 路径——挂载点不变则无 CI 改动 |

### 4.5.2 坑点清单（生产风险地图，实施前必读）

| # | 坑点 | 影响 | 规避 |
|---|---|---|---|
| K1 | **6 个 agent 的生产 MCP 在跑**——launcher/proxy 换路径瞬间，所有 agent 的 godot-mcp 工具失联 | 生产中断 | 切换放在**合并后的下一个 worktree 对齐周期**：各 agent checkout 到新 master 后重启会话自然用新路径；切换窗口内新旧路径**并存过渡**（见 §4.5.4 T3 过渡层） |
| K2 | **multica daemon / mcp-config 路径缓存**：平台侧 mcp_config 的 args 指向 workdir 内路径（SEE-1080 教训：路径漂移导致工具不注册） | 工具静默不注册 | `.mcp.json` 保持相对路径 `.dev/godot-mcp/launch/godot-mcp-shim.mjs` 不变——子库挂载点路径不变，shim 在子库内则 args 需变为 `addons/godot_mcp/launch/...`；**首选 shim 兼容层**（§4.5.4 T3），平台 mcp_config 零改动 |
| K3 | **proxy isGodotWorktree 双探测失效**：`.dev/godot-mcp/launch` 判断在重组后指向空目录 | worktree 解析全挂、editor 起不来 | T2 参数化时同步改探测逻辑（launch/ 与 project.godot 同级同库即可），并保留旧路径探测一版过渡 |
| K4 | **上游同步能力放弃**：重组 main 目录后无法再 `git merge` 上游 tadki fork 上游 | 长期演进锁定 | Owner 已拍板接受（解耦优先）。缓解：上游内核（server/、addon 命令面）只做增量注册；若未来要跟上游，用 subtree/patch 系列手动移植，不走 merge |
| K5 | **KOL 硬编码默认值语义反转**：`SHARED_MASTER_WORKTREE` 写死值在别的项目装上是**错误路径**（不是缺省值而是错的值） | 单独安装即坏 | 全部硬编码路径改为 `env 可覆盖`，且**默认值语义分层**：通用默认（如 `~` 前不存在则探测失败）与 KOL 默认（KOL 侧 `.env`/hook 注入）分离；KOL 仓库负责注入自己的值 |
| K6 | **两个 submodule 生命周期不同步**：`.dev/qa-toolkit` 与 `addons/godot_mcp` 在切换批次中都要动 gitlink，同步链路（§7 矩阵）在过渡期出现「main 已重组、快照未更新」中间态 | dangling 或回退混乱 | 顺序锁死：**先在 fork 内完成重组 + fast-forward 保护 main**（T1），再动 KOL gitlink（T4）；快照分支保留只读不删，作为回退锚点 |
| K7 | **tests 树路径引用漂移**（22+35 处）漏改导致 QA 假绿 | 回归盲区 | 批量替换后跑全量两棵测试树；grep 清单（§4.5.1 末行）作为改动验收 checklist |
| K8 | **reaper systemd unit 的 ExecStart 绝对路径**是 installer 戳进 unit 文件的（非仓库内引用） | reaper 静默失联 | 切换批次里 Owner/Bachi 手动重装 reaper（`install-resident-reaper.sh` 指新路径）；列入切换 runbook 而非代码改动 |

### 4.5.3 阶段分解（每阶段独立可交付、可 QA、可回退）

**T1 — fork main 重组（纯 fork 侧，KOL 零影响）**
1. fork `main` 上新分支 `refactor/addon-root-restructure`：`git mv godot/addons/godot_mcp/* → 根`、剔除 `test/`、`launch/`/`server/` 保持原位。
2. 目标树与 `kol-addon-hist` 差异只剩：快照无 test/ 而重组后 main 也无 test/ → **两者树应逐字节一致**（用 `git diff kol-addon-hist` 验证为空，除 README/docs 等明确豁免项）。
3. 合入 main（squash 或 merge 均可）；Owner 给 main 开分支保护（同 §7.4(b) 规格：linear history / 禁 force-push / 禁 delete——本次经 Revy 已授权模式可复用）。
4. `kol-addon-hist` 停止更新，标记 deprecated（分支描述/README 注明），**不删**。
5. 验收：main 上 `gh api` 树检查 + KOL 侧临时 clone `submodule update` 到新 main 验证 plugin.cfg 在根、Godot import 无错（临时分支验证，不动生产 gitlink）。

**T2 — launch/ 参数化（fork 侧代码改造，309 行硬编码收敛）**
1. 新增 `launch/env.sh`（或等价 lib）：集中定义全部可覆盖变量及默认值——`GODOT_MCP_SHARED_MASTER`（原 SHARED_MASTER_WORKTREE）、`GODOT_MCP_REPO_DIRNAME`（原 KingOfLikes-Godot）、`GODOT_MCP_WORKSPACES_BASE`（原 `~/multica_workspaces`）等；**KOL_* 旧变量名保留为别名**（`KOL_SHARED_MASTER=${KOL_SHARED_MASTER:-${GODOT_MCP_SHARED_MASTER:-}}` 式），存量调用零破坏。
2. launcher/proxy/configure/prepare-worktree 的路径字面量全部改读变量；`kol-runtime.lib.sh` 改名 `runtime.lib.sh`（KOL 前缀属于 KOL，不带入库——文件名也解耦），函数 `kol_slot_hash_for` → `mcp_slot_hash_for`（保留旧名 alias）。
3. isGodotWorktree 探测改双落点：`project.godot` + （`launch/` 或 `.dev/godot-mcp/launch/` 任一存在）——新旧布局都认，过渡期兼容。
4. 验收：TDD——每个参数化变量一个用例（默认值生效 / env 覆盖生效 / KOL_ 别名生效）；fork 侧全量测试树绿；**在 KOL 内以 submodule 挂 T2 后的 main 临时验证一个 agent 的完整 MCP 启动链**（真机，单人，不切生产 gitlink）。

**T1/T2 实际落地偏差补记（2026-09-10，Atlas 验收 + Revy 对抗性 QA 通过后记入；fork main head `8be66eb`）**

> 以下为实施时与上述规格条文的偏差与扩展，均已通过独立 QA（T1：树逐 blob 一致 + 真机 import；T2：20→24 用例 + 真机链路 + T2-M1 修复复验）。后续阶段与维护者以本补记为准。

| # | 偏差 / 扩展 | 规格原文 vs 实际落地 | 影响与理由 |
|---|---|---|---|
| D1 | **isGodotWorktree 三落点（非双落点）** | T2 第 3 点规格为「`launch/` 或 `.dev/godot-mcp/launch/` 任一存在」；实际 proxy 实现认三处：root `launch/`（T1 后 fork 新布局）、legacy `.dev/godot-mcp/launch`（KOL 现状）、post-T4 `addons/godot_mcp/launch`（KOL submodule 挂载形态） | 覆盖 gitlink 切换后的消费视图，T4 无需再改探测；测试用例覆盖三布局 |
| D2 | **env.sh 统一 export（T2-M1）** | T2 第 1 点规格只要求「集中定义 + 别名保留」；Revy QA 发现 `KOL_SHARED_MASTER` 等别名在 env.sh 内解析为 `GODOT_MCP_*` 后**未 export**，`launcher.sh` `exec node proxy` 后子进程读不到 → proxy `isSharedMasterWorktree`（§7.1 防线 3）在 KOL 旧名注入场景 fail-open 为空集 | 修复（`8be66eb`）：env.sh 尾部统一 `export` 4 个 `GODOT_MCP_*` 变量（幂等，canonical 路径零变化）；`launch/test_see1273_t2_param.sh` 新增第 7 节 4 例**跨进程断言**（child bash 断言，变异测试证明非恒真）。**T3 落 `.dev/env/kol-mcp.env` 用 `KOL_*` 注入时已无此坑** |
| D3 | **watch_contract 重指** | T1 剔除 addon `test/` 后，server 的 `watch-contract.test.ts` 原引用 `godot/addons/godot_mcp/test/watch_contract.json` 失效（ENOENT） | 契约件还原到 `launch/watch_contract.json` 并重指 `CONTRACT_PATH`（`server/src/__tests__/tools/watch-contract.test.ts`），内容与快照基线逐字节一致（纯搬迁），4/4 绿 |
| D4 | **FORK_CLI 字面量解耦（超规格同类收敛）** | §4.5.1 清单未含此项；实际 resolve.mjs/shim.mjs 内 `/mnt/d/GodotProjects/forks/godot-mcp` 绝对路径一并解耦为 `GODOT_MCP_FORK_CLI` env + 相对默认（`launch/../server/dist/cli.js`），默认未就位时回退 upstream npx | 真机验证两形态均通过（默认回退 npx / env 注入 `stage=FORK_WIRED`），`/mnt/d` 字面量全链路 0 次 |
| D5 | **`plugin.gd` 的 `KOL_MCP_PORT` 参数化** | §4.5.1 未列入 309 行清单（Revy T1 QA 发现，预存 lineage 非 T1 引入）；按 T2 同一模式收敛：读 `KOL_MCP_PORT` 后回退 `GODOT_MCP_PORT`，launcher/configure 端口解析增 `GODOT_MCP_PORT` 支路 + 出口同时 export 两变量 | KOL 旧 launcher 兼容保持（KOL_ 别名优先） |
| D6 | **T1 快照树豁免清单实证** | T1 第 2 点「逐字节一致」实际按消费面比对：main 滤掉 `launch/ server/ docs/ .github/ README.md LICENSE CONTRIBUTING.md INSTALL.md .gitignore` 后 76 文件 blob+mode 三元组与快照 `5719847` 完全一致 | 豁免项为 fork 库自持资产（定义上无快照对应物）；快照分支 deprecation commit `30ad9ff` 仅追加 `DEPRECATED.md`，未删未改写 |
| D7 | **T1 中断与重做记录** | 首批 T1 曾误派合入（`91295ce`→`7b81a03`），Owner 指令（comment 01a08bca）回滚 fork main 至 `e55f1dd` 后在正确 issue 下重做（cherry-pick → `e649597` → merge `fa59113`） | 过程留档：不影响最终树一致性结论 |

**T2 遗留 Final Review 清单（不阻塞 T3）**：T2-L1（isGodotWorktree 测试为 bash 重实现，建议抽可测模块）、T2-I1（toollist 快照漂移为 fork main 既有缺陷 `c812dce`，Final Review 决定是否 T5 顺手 `vitest -u`）、`test_see1273_t2_param.sh` 第 7 节 d 例第二分支宽松（LOW）、末行缺换行（INFO）。

**T3 — KOL 侧适配（合并入 shared 分支，PR 正常走 sweep）**
1. `.mcp.json` args 改指 `addons/godot_mcp/launch/godot-mcp-shim.mjs`；`.dev/godot-mcp/` 目录下**保留一个 3 行兼容 shim**（转发到 submodule 内 shim，带 deprecation 告警）——K2 平台缓存路径兜底，一个 PR 周期后（全 agent 都对齐过）删。**实施偏差（T3 落地时确认，Atlas 采纳）**：`.mcp.json` repoint 挪到 **T4 与 gitlink 切换同批原子完成**——过渡窗内 `.mcp.json` 保持旧相对路径，兼容 shim 在 submodule 内路径不存在时回退 legacy 真链路（绝不空转发），硬约束「T3→T4 过渡窗零中断」优先于本条的 repoint 时点；因此 T4 单 commit 实际含三原子项：gitlink 切换 + `.gitmodules` 删 `branch` + `.mcp.json` repoint（对应 T3 兼容 shim 的转发目标在 T4 后生效）。
2. 3 个 .claude hooks 的 launch 路径引用按「优先 submodule 内路径、兜底旧路径」改写。
3. KOL 专属值落 KOL 仓库：`.dev/env/kol-mcp.env`（agent 端口分配、shared master 路径等），由 hook / start 脚本 source——**KOL 特化配置只在这一处**，godot-mcp 库内不含任何 KOL 值（Owner 约束）。
4. `.dev/tests` 22 处路径替换；`.dev/godot-mcp/tests` 35 处**随 T2 移入 fork**（它们测的就是库代码），KOL 侧只留消费方集成测试。
5. DESIGN.MD / CLAUDE.md / runbook 等文档同步（Archi 责任）。**文档同步已完成（2026-09-10，先于 T3 QA）**：`docs/project/DESIGN.MD`（MCPGameBridge 归属补 submodule/重组终态表述）、根 `CLAUDE.md` §6（控制面路径过渡期总注）、`.dev/godot-mcp/docs/godot-mcp-runbook.md` 与 `mcp-multi-port-usage.md`（文件头过渡期说明——legacy `.dev/godot-mcp/launch/` 路径 T4 前仍为生产唯一路径）、`.dev/docs/devtests.md` §1（测试树归属过渡期说明）、`.dev/docs/multica/environment-variables.md`（KOL_AGENT_NAME 桥接段过渡期注）、`docs/code/godot-exec-pitfalls.md`（proxy 路径权威源注）。gitlink 相关表述均以 T4 未发生为前提写过渡期语义。

**T4 — gitlink 切换 + 生产切换（单 commit，最小爆炸半径）**
1. KOL `addons/godot_mcp` gitlink `5719847` → 新 main head；`.gitmodules` 删 `branch = kol-addon-hist`（跟踪 main 默认分支）。
2. repo-sync 4 路径 + repo-checkout materialize 无需改动（指针变化即触发——§7.1 场景 ③④ 已自动化，健康指针 auto-pr 保留）。
3. 切换 runbook（执行人：Owner 或 Bachi，手工项）：重装 reaper unit（K8）；确认 6 个 agent 下次会话 checkout 到新 master 后 MCP 工具注册正常（`mcp-assert-registration.sh` 现成工具）；观察一个完整 daemon 周期 state.json。
4. 回退方案：gitlink revert 一个 commit 即回 `5719847`（快照分支未删 = 完整回退锚点）；`.mcp.json` 兼容 shim 未删则平台侧无需回滚。

**T5 — 收尾**
1. 确认全 agent 生产稳定 ≥1 天后：删 KOL `.dev/godot-mcp/` 兼容 shim 与空目录；`kol-addon-hist` 删除（Owner 确认后）。
2. §5 同步终态矩阵增补「godot-mcp main」行（remote head = `gh api repos/tadki/godot-mcp/commits/main`）。
3. 本节状态标注完结；坑点 K1–K8 逐条打勾销账。

**T5-limited 实施记录（2026-09-11，M3 主体合并后受限版收尾）**
- CI-1 ✅：fork main toollist snapshot 同步（`0388b9b`，工具面 21==21 无增减，diff 仅 SEE-1141 两文件 schema；CI workflow 回绿）。T2-I1 销账。
- CI-2 ✅：Release workflow 失败为 repo Settings 层（`default_workflow_permissions=read` + `can_approve_pull_request_reviews=false`，workflow YAML 本就有 permissions）——API 开启后 dispatch 验证 release-please 成功建 release PR（godot-mcp 5.0.0），Release workflow 回绿。
- T5-limited 瘦身评估结论：**本批零删除**。launch/ 全部 42 文件为自洽运行体（内部互引闭包 + systemd 安装面模板），daemon 兼容兜底期必须整体保留；docs/ 5 个零外部引用文档为生产组件历史设计档案（fork 无副本，删除丢 lineage）。完整瘦身等 T5 终版（兼容期结束后）。
- 快照分支删除、兼容 shim/legacy shim 删除：均等生产稳定 ≥1 天后 T5 终版（Owner 批）。

### 4.5.4 各阶段负责人与 QA 形态

| 阶段 | 实施 | QA | 不可逆操作 |
|---|---|---|---|
| T1 | Bachi | Revy（树一致性 + 真机 import） | fork main 合并（有保护，可 revert） |
| T2 | Bachi | Revy（参数化 TDD + 单 agent 真机链路） | 无（fork 分支） |
| T3 | Bachi | Revy（hooks/测试树回归） | 无（KOL shared 分支 PR） |
| T4 | Bachi 提 PR | Revy 真机 + Atlas /code-review | **gitlink 切换 + reaper 重装**（生产，回退方案已备） |
| T5 | Bachi/Archi | 抽查 | 删快照分支（Owner 确认） |

### 4.5.5 明确不做（防范围蔓延）

- 不迁移 `.dev/repo-sync/`（主库同步器属 KOL infra，与 godot-mcp 库无关）。
- 不动 `.dev/launch/kingoflikes-start.sh` / systemd unit 的 KOL 硬编码（KOL 侧合法特化，只整理不参数化到库）。
- 不在上游 server/ 与 addon 内核里注册任何 KOL 命名的东西（延续 vendored 红线精神：增量注册、无 KOL 概念渗入）。
- 本批不做 godot-determinism 命令注册（②→① 协作项，等 T1–T5 落定后独立小批）。

## 5. 同步终态矩阵（验收标准）

| 对象 | 同步机制 | 验证方法 |
|---|---|---|
| ② qa-toolkit remote master | ② 自身开发流 | `gh api repos/tadki/godot-qa-toolkit/commits/master` |
| ① godot-mcp remote master | ① fork 自身开发流（M3 起） | `gh api repos/tadki/godot-mcp/commits/master` |
| 游戏库 remote master | PR 合入（sweep 流程）；submodule 指针随 PR | 指针 commit 在 master |
| 游戏库本地 master（D 盘） | repo-sync daemon **每个周期**（含 SYNCED 稳态）+ 迁移残留自愈 | `.dev/qa-toolkit/` + `addons/godot_mcp/` 内容随指针落地 + state.json success |
| agents worktree | multica repo checkout + **hook 兜底**（对齐成功 / checkout 成功 / **重入快路径** 三处） | 新 worktree checkout 后两挂载点内容可用，零额外步骤 |

## 6. 设计取舍记录

1. **submodule 兜底放 repo-checkout hook 而非 prepare-worktree.sh**：prepare-worktree 只负责 MCP lease，且在 `|| true` 静默纪律下运行；submodule init 失败必须出 hint（禁静默失败）。hook 是唯一带 output_hint 通道的对齐出口。
2. **repo-sync 只加 submodule update 不改 has_submodule_changes 判定**：`-` 状态拒同步是保护行为；正确姿势是 pull 后主动 init 消除 `-`。
3. **复杂度 gate 用 gdtoolkit API 而非 gdradon CLI**：gdradon 只有文本输出，API 直接拿 (name, complexity, lineno) 结构化结果，gd2py 保结构不保值——恰好是 cc_visit 所需。
4. **Gherkin 自研而非 GUT 内嵌**：② 是通用库不能依赖 GUT；桥接层 godot_bridge 负责映射。
5. **mutation 覆盖率隔离**：kill rate 永不进验收报告（owner-order L5 条文），工具输出照常机器化。
6. **① 与 ② 职责分离**（2 库修正）：控制面 + addon 是运行态（怎么驱动 Godot），工具是工具态（用什么手段测）——合库互相拖累演进；确定性断言上两库协作（② 出逻辑、命令注册进 ① addon）。
7. **M1 与 2 库架构兼容，无返工**：M1 交付的 sync 双入口架构无关；② 挂载点/工具正确；控制面从未迁入 ②（mcp/ 空目录未被 git 跟踪），M3 直接落 ① 组装。
8. **submodule 落地必须覆盖稳态与快路径**（2026-09-10 生产事故复盘）：仅"指针变化时才 update"不够——(a) repo-sync 的 `SYNCED` 早返回分支不 materialize，导致经旧 daemon / 手工 pull 到达 submodule 状态的仓库永久缺失 addon；(b) repo-checkout 的重入快路径跳过对齐流程，也就跳过了其中的 materialize。两处已补。
9. **迁移残留必须自愈且可逆**：vendored→submodule 切换会把旧文件留在磁盘，`git submodule update --init` 拒绝克隆进非空目录并**每周期失败**（D-drive 缺 plugin.cfg 的直接原因）。自愈策略：把残留**移走**（备份到 `.dev/logs/submodule-residue/`，绝不删除）+ 一并移走 `.git/modules/<path>` 陈旧注册 + 重试一次；非残留类失败照常显式报错，不掩盖。

## 7. 子库同步终态架构（Owner 已批 · 2026-09-10）

> 来源：Atlas 子库同步终态架构设计讨论（SEE-1268 thread 01a08a7b，Owner 拍板按推荐全部落地）。
> 状态：dangling 拦截（GAP-1）已随 PR #573–#577 合入 master `be33098a`；本章 (a)–(d) 四项规格为本批待实施项，供 Bachi 直接实施。

### 7.1 6 场景链路矩阵

子库同步涉及两个库（主库 KOL + 兄弟库 qa-toolkit / godot-mcp）× 三个节点（工作区 / 本地仓库 / remote），共 6 条链路操作。每场景列出上行下行路径、责任方、自动化覆盖与 agent 操作指引。

| # | 场景 | 链路路径 | 责任方 | 自动化覆盖 | agent 操作指引 |
|---|---|---|---|---|---|
| ① | **只改子库**（qa-toolkit / godot-mcp 改代码） | 上行：子库工作区 `commit` → `push` 子库 remote。下行：无（父库消费另走 ③⑤⑥） | 开发 agent（手工） | 无自动化——子库独立开发，父库不代管；dangling 由消费侧防线拦截 | 在子库仓库直接 `commit + push` 其子库 remote；**不要在父库推未推的子库 commit 指针**（GAP-1 会拒） |
| ② | **只改主库代码**（KOL worktree 改游戏逻辑） | 上行：auto-pr-on-stop auto-commit → push shared 分支 → PR → merge。下行：repo-sync 同步回 D 盘 master | 开发 agent | 完整：auto-pr 全套 + repo-sync 下行 | 正常开发，stop 时 auto-pr 自动提交推送；无需手工 git 操作 |
| ③ | **只改 gitlink**（子库推完 remote 后，父库挪指针） | 上行：子库 `push`（①）→ 父库 worktree 指针变化 → auto-pr commit → push → PR → merge | 开发 agent | 本批 (d) 补齐：auto-pr 条件保留 gitlink（当前为无差别 unstage 缺口） | ① 先在子库 `push`；② 回 KOL 让工作区指针前移（`git -C <挂载点> pull` 或 checkout 到目标 SHA）；③ stop 时 auto-pr 自动提交（d 落地后）；遇 dangling 拒推说明子库没推，先补① |
| ④ | **主库代码 + gitlink 一起改** | 同 ③，两个动作合一个 commit | 开发 agent | 同 ③（依赖 (d)） | 同 ③；健康指针自动随代码一起进 auto-commit |
| ⑤ | **主库 remote 下行同步**（origin/master → D 盘 master） | repo-sync daemon 5s 轮询：pull --ff-only / LOCAL_AHEAD push / DIVERGED rebase+merge / SYNCED 稳态 4 路径全部 materialize；SYNCED 周期附加滞后告警 (a) | daemon（无人工） | 最完整：4 路径 submodule update + #575 SYNCED 自愈 + 迁移残留自愈 + #577 dangling 拦截 + 本批 (a) 滞后告警 | 无操作；异常看 daemon 日志与 `.dev/repo-sync/state.json` |
| ⑥ | **消费落地**（agents worktree materialize） | SessionStart → repo-checkout（bare 缓存 worktree）→ `ensure_submodules_materialized` 兜底（对齐成功 / checkout 成功 / 重入快路径 3 处）；CI 走 `submodules: recursive` | hook / CI（无人工） | 完整：hook 3 入口 + CI recursive | 无操作；checkout 后两挂载点内容原地可用 |

### 7.2 防线清单（每道防线的节点归属与「为何不冗余」）

| 防线 | 位置 | 管辖节点 | 为何不冗余 |
|---|---|---|---|
| repo-sync 4 路径 submodule update | `.dev/repo-sync/repo_sync.py`（pull --ff-only / push 后 / rebase+merge 后 / SYNCED 稳态） | master 线（D 盘本地仓库内容） | 子库内容落地的唯一自动化通道，其余防线不碰内容 |
| repo-checkout `ensure_submodules_materialized` | `.claude/hooks/repo-checkout.sh` | agents 线（multica bare 缓存 worktree） | 与 repo-sync 覆盖不同节点（agents worktree vs D 盘 master），互不重复 |
| CI `submodules: recursive` | `.github/workflows/`（checkout action 配置） | CI runner | 与前两者覆盖不同域（CI 环境 vs 本地工作区） |
| GAP-1 双通道拦截：`repo_sync.do_push` 前 `ensure_gitlinks_reachable` + `push-guard` Check 8 | `.dev/repo-sync/repo_sync.py:1009` / `.claude/hooks/push-guard.sh:538` | 父库 push 出口：自动化通道（daemon 子进程，hook 管不到）vs agent 手敲 push | 两条推送通道互不重叠——只做一处必漏另一条；拦截逻辑同、落点不同，属纵深防御 |
| 滞后告警 (a)（本批新增） | `.dev/repo-sync/repo_sync.py` SYNCED 分支 | master 线稳态周期 | 检测对象是「子库 remote 超前父库指针」——与 dangling 拦截（指针超前 remote）方向相反，不重复；只告警不拦截 |

### 7.3 冗余红线（未来扩展判断依据）

**同一节点的重复检测 = 冗余；不同节点的相同检测 = 纵深防御。**

判定时问一个问题：新防线与既有防线是否作用于**同一个出口/节点**？

- 同一节点再加一道同款检测 → 冗余擦屁股，禁止（例：auto-pr 再加一道 dangling 拦截，与 GAP-1 双通道落点重叠 → 冗余）。
- 不同节点各自需要 → 纵深防御，正当（例：GAP-1 双通道，因为 daemon push 与手敲 push 是两条互不覆盖的通道）。
- 检测对象不同即使落点相同也不是冗余（例：滞后告警 (a) 与 dangling 拦截同落 repo-sync，但前者查「超前」后者查「dangling」，正交）。

历史教训：auto-pr 的无差别 gitlink unstage 与 GAP-1 拦截落在同一节点（主库 push 前）两头互打——一个说「剥离防意外」、一个说「检测再阻断」，本质是设计时没想清楚方向；(d) 把 auto-pr 改为「有条件保留」后二者统一。

### 7.4 本批 4 项决策落地规格（供 Bachi 直接实施）

#### (a) 滞后告警：repo-sync SYNCED 周期检测子库 remote 超前

**触发点**：`repo_sync.py` `run_sync` 的 SYNCED 分支（约 :1394），在 `do_submodule_update` 成功后、写 state 前执行。

**逻辑**：
1. 遍历 `.gitmodules` 的每个 submodule（无 `.gitmodules` 跳过，零开销）。
2. 读该 submodule 的跟踪分支（`.gitmodules` 的 `branch` 字段，缺省 `master`）；当前挂载：`.dev/qa-toolkit`→`master`，`addons/godot_mcp`→`kol-addon-hist`。
3. `git ls-remote <url> <branch>`（单次网络往返，套既有 `probe_timeout_s` 30s 上限）取子库 remote head SHA。
4. 与父库 index 中该路径的 gitlink SHA 比较（`git ls-files --stage` 已封装在 `_iter_gitlinks`）。
5. 差 > 0（即 gitlink 不是 remote head 本身——落后）→ `logger.warning` 告警；相等或无法判断（网络失败 / 分支不存在）→ 静默跳过，**不置 success=False、不拦截同步循环**。

**告警文案格式**（单行，log 级别 WARNING）：

```
submodule '<path>' is behind its remote: gitlink <short-sha> vs <url>#<branch> <short-sha> — 父库 pin 旧版可能为设计意图；如需跟进，先推子库已就绪的指针变更（消费决策在主库 PR）
```

**边界**：滞后是**合法状态**（父库 pin 版本是 submodule 的核心语义），本防线只暴露不动作；告警噪音控制靠「仅在状态变化时升级」（可选：state.json 记录上次告警的 remote head，同一 head 不重复告警）。

#### (b) 子库分支保护：qa-toolkit master + godot-mcp kol-addon-hist

**性质**：Owner 在 GitHub Settings 手动开启，本文档只记录规格与验收方式，不涉及代码实施。

**规格**（GitHub → repo Settings → Branches → Add branch ruleset / protection rule）：

| 仓库 | 分支 | 规则 |
|---|---|---|
| `tadki/godot-qa-toolkit` | `master` | Require linear history = on；Allow force pushes = off；Allow deletions = off |
| `tadki/godot-mcp` | `kol-addon-hist` | 同上 |

**理由**：GAP-1 的 push-time 校验是快照——校验通过后子库若被 force-push 重写历史，父库指针重新 dangling。分支保护消除「rewrite 历史」这条残余路径，是「push 时可达 ⇒ 永远可达」不变量成立的前提。

**验收方式**：

```
gh api repos/tadki/godot-qa-toolkit/branches/master/protection --jq .required_linear_history.enabled   # 期望 true
gh api repos/tadki/godot-mcp/branches/kol-addon-hist/protection --jq .required_linear_history.enabled # 期望 true
# 且两分支 force-push 实测被拒（非破坏验证：gh api 返回 404 → 未开启，返回 JSON → 已开启）
```

当前状态（2026-09-10 实证）：两分支均 404 未保护，待 Owner 开启。

#### (c) 递归校验：`ensure_gitlinks_reachable` 预留 `--recursive` 骨架

**目标**：`repo_sync.py:1009` 的 `ensure_gitlinks_reachable` 增加嵌套 gitlink 校验的**扩展点**，当前两子库均无嵌套（行为与现状一致，仅做骨架不做死）。

**规格**：
1. 函数签名加 `recursive: bool = False`（默认关，行为零变化）。
2. 主循环内、单个子库校验通过后，若 `recursive=True` 且该子库工作区存在自己的 `.gitmodules` → 对子库递归调用一次（深度限 1 层：递归调用时强制 `recursive=False`，避免无限下钻；当前无嵌套即走不到）。
3. 递归命中的 dangling 报错文案带父路径前缀（如 `submodule '.dev/qa-toolkit/<嵌套路径>' ...`），保持点名能力。
4. `do_push` 调用处本次**不改动**（仍默认非递归）；push-guard Check 8 同样不动。`recursive=True` 的启用留给未来出现真实嵌套子库时打开。

**边界**：`-` 未初始化子库照常跳过（递归也依赖子库工作区存在）；probe 超时预算不变（每层独立 `probe_timeout_s`）。

#### (d) auto-pr gitlink 条件保留：dangling 剥离+告警 / 健康保留

**现状**（`.claude/hooks/auto-pr-on-stop.sh:187-191`）：`git add -A` 后无差别 `git reset HEAD -- <path>` 剥离**所有** gitlink——合法的指针推进（场景 ③④）也被丢弃，agent 必须手工 `git add` 才能提交指针，是当前唯一真正的自动化丝滑度缺口。

**目标行为**：逐 gitlink 判定——**健康保留**（进 auto-commit）、**dangling 剥离 + 告警**（不进 commit，明示原因）。

**判定优先级**（按序短路，复用 GAP-1 判定语义）：

1. **工作区与 index 一致**（该 gitlink 无变更）→ 无需处理，跳过。
2. **`-` 未初始化**（子库工作区不存在 `.git`）→ 剥离 + 告警「子库未初始化，指针变更不落 commit」——materialize 流程管辖，不算 dangling。
3. **无法解析子库 origin URL**（`git -C <child> remote get-url origin` 失败/为空）→ 剥离 + 告警「无法判定指针健康度，保守剥离」。
4. **dangling**（fetch --dry-run 探测 / tips 祖先交叉校验失败——与 `ensure_gitlinks_reachable` 同判定）→ 剥离 + 告警「dangling gitlink：子库 commit 未推 remote，已剥离；先补推子库再重新挪指针」。
5. **健康**（SHA 在子库 remote 可达）→ **保留**，随 auto-commit 一起提交。

**实施要点**：
- 判定逻辑抽成共享 shell 函数（建议放 `.claude/hooks/lib/` 或从 push-guard Check 8 提取公共段），push-guard 与 auto-pr 共用一份，避免两处实现漂移（违反 7.3 冗余红线的「同节点重复检测」）。
- 网络探测套短超时（沿用 push-guard 的 20s `GITLINK_PROBE_TIMEOUT`）；探测失败（超时/网络错误，非 "not our ref"）按**保守剥离 + 告警**处理，不阻塞 stop hook。
- 无 `.gitmodules` 或 index 中无 gitlink → 整段零开销跳过（保持现状性能特征）。
- 告警统一走 stderr（`[auto-pr]` 前缀），失败不 exit 2（stop hook 纪律：不阻断会话结束）。

**验收要点**（供 Revy QA 参考）：合法指针推进随 stop 自动进 commit；dangling 指针被剥离且告警点名子库+SHA；`-` 未初始化不误报 dangling；无 gitlink 变更的 stop 路径零网络开销。

**T5 终版实施记录（2026-09-11，Owner 指令推进：3-agent 并发复测 PASS 后直接推进遗留项）**
- 门禁复测 ✅：SEE-1280 三 agent 并发全 PASS——Revy（registered=YES / call_ok=YES / retries=0，**新路径直连**，port 6555）、Fronti（YES/YES/0，旧路径兜底，动态池 6563）、Refacty（YES/YES/0，旧路径兜底，动态池 6562），runtime_id 互不冲突；Revy 份数据证明 `.mcp.json` 新路径直连形态已在生产真实生效。
- `kol-addon-hist` 退役 ✅：Owner 本批显式指令，remote 分支已删除（`gh api delete refs/heads/kol-addon-hist`，核验 ls-remote 零命中）。GAP-1 语义影响：现 gitlink `00d9c77` 为 fork main 历史内祖先（可达性由 main 承载），删除快照分支不再产生 dangling 风险——与 T4 时「唯一旧锚点」状态相比，当前锚点需求已消失（gitlink 已是 main 可达 commit）。
- T5 终版剩余项（继续挂起）：删 `.dev/godot-mcp/` 兼容 shim + legacy shim 与空目录——**仍未到时机**：3-agent 复测中 Fronti/Refacty 的 daemon mcp_config 仍直连旧路径 launcher（平台缓存按 agent 下次会话自然刷新），shim 删除须等全 agent daemon 均已脱离旧路径（以 `mcp_launch_script` 解析落点抽查或 daemon config 巡检为准），届时另行小批执行。
- §5 同步矩阵增补：「godot-mcp main」行 remote head = `0388b9b`（T5-limited 后），KOL gitlink `00d9c77`（其直接祖先，fast-forward 关系）。
- K1–K8 销账：K1（生产瞬断）✅ 未发生（K1 设计实证）；K2（平台缓存）✅ shim 兜底实证（Fronti/Refacty 路径形态）；K3（探测）✅ 三落点；K4（上游 merge 放弃）✅ Owner 已拍板；K5（默认值语义）✅ 三层别名；K6（两 submodule 顺序）✅ 按 K6 顺序锁死执行；K7（测试树路径）✅ 22+35 处替换全绿；K8（reaper 重装）⏳ Owner/Bachi 手工项（runbook §2.1）。
