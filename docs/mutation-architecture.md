# gqt Mutation 架构与设计思想（SEE-1417/1420 体系提炼）

> 权威来源：`src/godot_qa_toolkit/mutation/`（实现）与定案《SEE-1417-mutation-upgrade-plan.md》（SEE-1420 issue 附件）。本文管**设计思想与体系谱系**；接口契约见 [contract.md](contract.md) §4.3/§4.4，操作指引见 Multica 技能 `gqt-cli` / `gqt-mutation-guide`。实现与本文冲突时以代码为准并回改本文档。
>
> 体系源流：Uncle Bob（Robert C. Martin）在 clean-code 生态公开的变异测试性能优化实践（Clojure/SPEC 变异工作流）。SEE-1417 以 plan-debate（三草案 + 两轮对抗讨论）完成差距对照与取舍定案，SEE-1420 落地。本文明确定位为**语义采纳、机制按问题域重造**——不逐件照搬。

## 1. Uncle Bob 体系对照（7 层，语义 vs 机制）

取舍原则：**每一层先拆出"它解决什么问题"（语义），再检查 gqt 的问题域是否相同**；语义相同则采纳，机制不同则按 gqt 域重造或拒绝。

| 层 | UB 机制 | 语义（问题） | gqt 定案 | 理由 |
|---|---|---|---|---|
| L1 | 单文件靶向 + `--scan` + 大文件警告熔断 | 先算清要跑多少，别盲跑 | **部分采纳**：拒绝单文件靶向（多文件 + worker 池是 gqt 既有优势）；`--dry-run` 升级为差分计数预估；新增 `--mutation-warning`（默认 50，截断前全量计数）+ 截断 N/M 强制披露 | 警告熔断的语义保留；靶向的机制与 gqt 域优势冲突 |
| L2 | 函数级 SHA-256 差分 + 快照持久化 | 没变的东西不重跑 | **采纳（核心）**：scope 级哈希差分 + per-file manifest + 差分跳过规则 + survived 永不缓存信任 | 语义完全同构；哈希粒度按 GDScript 结构重定义（见 §2） |
| L3 | 覆盖率前置过滤 + `--reuse-coverage` | 没被测试覆盖的 mutant 不用跑 | **采纳**：复用 `gqt coverage` 自家行级事实（不引 LCOV 外部格式），provenance 校验 + 可插桩锚语义（不可插桩行 ≠ uncovered） | 数据面自有；"外部格式互操作"机制被拒绝 |
| L4 | 沙盒目录克隆 + 多进程 worker 池 | 隔离变异副作用 + 并行执行 | **拒绝沙盒，保留 worker 池**：内存注入（磁盘目标文件全程原始内容）严格优于目录克隆——中断无残骸、无文件系统拷贝税 | 沙盒是 JVM 生态"进程内改字节码不可行"的妥协；GDScript 源码级内存注入无此约束 |
| L5 | baseline 动态超时熔断 | 超时判定要随基线耗时自适应 | **采纳（小改）**：自适应上限 = `max(timeout, baseline×F+1)`，`--timeout-factor` 默认 2.0 只放大不收紧；**拒绝 UB 默认 10**（JVM 抖动所迫，gqt 域无此噪声） | 倍数是环境参数不是语义 |
| L6 | Babashka 快启动 / no-mutate / 常驻热 worker | 削启动税 + 跳过不可变区域 | **no-mutate 采纳**（`# gqt: no-mutate` 函数级 + 行级）；**Babashka 不适用**（gqt 是 Python 常驻 CLI，无冷启动问题）；**常驻热 worker 拒绝本轮降 backlog**（Godot 引擎级状态泄漏风险，重启条件量化见定案 §五） | 快启动语义在 gqt 域不存在；热 worker 的假 verdict 代价不对称 |
| L7 | `--mutation` / `--lines` 单点重跑 | 秒级复核单个位点 | **采纳**：`--mutation <file>#<scope_id>#<n>` / `--lines`，1 baseline + N selected，绕过差分复用与 coverage 前置过滤（防假阴性的公平抽查通道） | 直接采纳，id 寻址按 D3 重定义 |

## 2. gqt Mutation 架构：五阶段流水线

```
源文件 ─▶ ① scope manifest 提取 ─▶ ② 差分引擎 ─▶ ③ 预算规划 ─▶ ④ worker 池执行 ─▶ ⑤ resume v2 持久化
              │                      │               │               │                  │
        scopemanifest.py        diffengine.py    planner.py     executor.py          resume2.py
        （ops.py 产 mutant）   （resume2 读盘）  （timing 协商）  （+gutrun/inject）  （coverlink 供锚）
```

### ① scope manifest 提取 — `scopemanifest.py`（+ `ops.py` 变异点收集）

- scope 集 = 顶层 func + 内部 class 整体黑盒 + module（非 func 顶层声明：class_name/@export/const/signal）；lambda 归外层 func；无变异位点的 scope 不进 manifest（省 hash 与存储）。
- 位点 id = `<file>#<scope_id>#<n>`：scope_id 带 `func:`/`class:` kind 前缀（跨 kind 同名不碰撞），同 kind 同名消歧 `func:foo#2`，n 为 scope 内 1-based 扫描序。
- header hash（module scope）只收非 func 顶层声明——func 变更绝不触发全文件重跑（单向保守，见 D2）。
- `ops.py` 承担 AST 级变异点收集与语法预检（invalid_mutant 提前剔除，不跑 GUT）。

### ② 差分引擎 — `diffengine.py`（读 `resume2.py` 落盘）

- manifest 在场即差分（默认开启），跳过规则：未改 scope 且上次 killed/suspect/invalid_mutant → 复用；survived / timeout / timeout_pinned / run_error → **永远重跑**；scope hash 变更 → 该 scope 全跑；header hash 变更 → 全文件全跑；hash 全同 → 零执行速断。
- `--mutate-all` 是唯一显式退路；被跳过位点逐条 id 披露 `diff_skipped`。

### ③ 预算规划 — `planner.py`（+ timing store 协商）

- budget 按**差分后 retry 数**计（截断保序，拒轮转）；位点数 > budget 强制披露 `total_sites`。
- 自适应 timeout：baseline 永远实测（判定语义），timing store 历史 p90 只喂预算锚（缓存锚只抬下限）；`--timeout-factor` 可覆盖放大系数。
- resume key 负面清单：`--mutation-warning`、`--timeout-factor`、`timeout_streak` 绝不进任何 key——调配置不引发重跑。

### ④ worker 池执行 — `executor.py`（+ `gutrun.py` / `inject.py` / `godot_gate.py`）

- 默认并行（jobs = `max(2, cpu//2)`），单文件同样 per-mutant 分片；变异点 ≤ jobs 自动退串行；共享 baseline 快照（worker 自跑 baseline 会 N 倍放大启动税且污染 timeout 锚）。
- 内存注入：变异产物经 host 脚本以 env 指路，磁盘目标文件全程原始内容，每轮后幂等恢复；SIGTERM handler 先恢复再终止。
- kill 判定 = 失败测试名集合相对 baseline 的差集（不看 exit code）；`godot_gate` 跨进程 flock 槽位闸统一 mutation/coverage 的 Godot 并发上限。
- `gutrun.py` 承担 GUT 子进程调用与 launch-failure 甄别（毒条目防护：启动失败类 run_error 不落盘，防 `--resume` 误复用）。

### ⑤ resume v2 持久化 — `resume2.py`（+ `coverlink.py` 覆盖事实）

- 落盘 `<root>/.qa-cache/gqt-mutation-resume-v2/<file-key>/`：manifest.json（per-file 索引层：schema/header hash/含位点 scope hash 表，收尾方单点写）+ `<stable-id>.json`（per-mutant 条目：verdict + 全签名五元组 + scope_hash + timeout_streak）。
- tmp + `os.replace` 原子写；各 worker 各写各 id，无跨 worker 竞态；损坏/身份不符条目 = 拒收重跑，绝不脏合并。
- timeout_streak：复用位点重判仍 timeout → +1；streak ≥ 2 → 独立 verdict `timeout_pinned`（永不并入 killed、不计 ok 门禁、持续可见）；任一后续 run 非超时 → 解除清零。毒 run 守卫：单 run timeout ≥50% 且 ≥5 条 → 本窗口 streak 挂起并披露。
- `coverlink.py`：`gqt coverage` 成功 run 的行级事实（含 `instrumentable_lines` 可插桩集 + provenance）落盘 `<root>/.qa-cache/gqt-coverage/<key>.json`，供 `--reuse-coverage` 前置过滤；provenance 不符 = 缓存作废，宁多跑不假阴性。

## 3. 优化思想提炼（抽象层）

1. **哈希换算力**：用 SHA（scope 级 + header 级）把"要不要跑"变成 O(1) 判定——算力买断执行力。差分的正确性不在哈希本身而在**跳过规则的白名单**：只有"可证明不受影响"的位点才允许跳过。
2. **差分保真（宁重跑不假绿）**：加速必须以不产生假绿为前提。survived / timeout / timeout_pinned / run_error 永不缓存信任——缓存只加速"已证明的杀死"，从不加速"未证明的存活"。红线优先于任何性能收益。
3. **卡尺闭环**：单点重跑是差分体系的"卡尺"——补测迭代里 survivor → 补断言 → `--mutation <id>` 秒级确认，把"验证一个修复"的成本从全量轮降到 1 baseline + 1 mutant；它同时是差分与覆盖率过滤的防假阴性抽查通道（被过滤位点的公平复核）。
4. **质量不降的显式化**：每一次跳过都必须留下机器可查的痕迹——`diff_skipped` 逐条 id、`untouched_ranges` 行号区间、`uncovered` 独立 verdict、verdict 全签名五元组防 id 漂移、timeout_pinned 永不洗白进 killed。**信息不披露 = 优化不合法**。
5. **机制适配性（语义 vs 机制分离）**：UB 的沙盒克隆、Babashka 启动器、JVM timeout×10 都是各自生态约束的产物；gqt 的内存注入、常驻 CLI、自适应 ×2 是 GDScript/GUT 域的同语义重造。跨体系移植时先问"这是问题还是解法"——问题可移植，解法未必。
6. **预算协商与负面清单**：配置参数（警告阈值、超时系数）是每 run 的执行策略，不是 verdict 的身份成分——凡不改变"这个 mutant 是否已死"判断的东西绝不进缓存 key，否则调一次配置全库重跑。

## 4. 决策沉淀（D1–D7）

| 决策 | 定案 | 依据要点 |
|---|---|---|
| D1 差分粒度 | 顶层 func + 内部 class 黑盒 + module；lambda 归外层 func | 三方 Round 2 汇合 + KOL 实测（内部 class 方法级场景仅 1 例 0 方法，嵌套粒度 YAGNI）；粒度决策可逆，嵌套扩展为未来纯增量 |
| D2 header 差分语义 | header hash 只收非 func 顶层声明；header 变更 → 全文件重跑（单向保守） | "改一个函数触发全量重跑"会让差分自杀（H1 攻击成立）；单向防顶层 const 引用 func 的双向依赖拖死 func 差分 |
| D3 稳定 id | `<file>#<scope_id>#<n>`，id 仅为寻址提示；跨 run 正确性交全签名 (line, column, kind, original, mutated) + scope hash 双校验，漂移 = 告警 + 拒绝复用 + 降级重跑 | 文件全局 MNN 编号被"插入位移"反例击中；scope 锚定 + 全签名兜底是"可读寻址"与"不可伪造身份"的分工 |
| D4 差分跳过规则 | killed/suspect/invalid_mutant 复用；survived/timeout/timeout_pinned/run_error 永远重跑；默认开启，`--mutate-all` 退路；diff_skipped 逐条披露 | 照抄 clj sites-to-retry 语义；timeout_pinned 按 survived 同类处理防解除路径失效变永久毒条目（S2' 修补） |
| D5 timeout_pinned | streak ≥ 2 转独立降级档：永不并入 killed、不计 ok 门禁、持续可见；毒 run 守卫（≥50% 且 ≥5 条）挂起 streak | N=2 主持人裁决（gqt 域 timeout 已自适应放宽，3 只增加白付等待）；不进门禁防静默洗白 CI 判定 |
| D6 budget-on-retry | budget 按差分后 retry 数计；拒轮转（破坏 seq 寻址与差分固定基准）、拒默认重估（隐式第二披露面更差）；retry_total/executed/untouched_ranges 强制披露；resume key 负面清单 | 尾部饿死的出路是 P3 卡尺工具化，不是公平性轮转——为公平引入不可验证性不成立 |
| D7 P2/P3 顺序 | P3 卡尺先行（同时服务 P2 验收与饿死复核）；coverage 实测并行不阻塞；P2 验收 = covered verdict 集合一致 + uncovered 全显式 + 卡尺抽查（拒绝"全量严格等价"这一不可能成立的判据） | 覆盖率数值不进验收证据（机制正确性 vs 质量达标分离，owner-order L5） |

### 拒绝项与理由（定案 §六）

沙盒目录克隆（内存注入严格优于——中断无残骸、无拷贝税）；单文件靶向（放弃多文件 worker 池优势）；Babashka 启动器（常驻 CLI 无冷启动问题）；源文件尾 manifest 注入；LCOV 外部格式（复用自家 JSON + provenance）；UB 默认 timeout-factor=10（JVM 抖动所迫）；timeout 判 killed（UB 语义，gqt 以 timeout=存疑 + pinned 降级链替代）；文件全局 MNN 编号（插入位移不稳定）；嵌套 scope 方法级粒度（本期，KOL 实测无场景）；lazy adopt 迁移器（复杂度买回收益恒零，S1 自攻撤销）；budget 轮转/默认重估（D6）。

### 演进留白（重启条件，定案 §五）

P5 常驻热 worker：scoped 单轮 GUT 均值 < 7s 成主流 / mutant 数 ≥200 成典型 / 启动税占比 ≥30% / GUT 提供同进程状态复位 API 且通过状态污染探针——触发其一再评估。风险主因是 Godot 引擎级状态泄漏（autoload/static var/ResourceLoader/scene tree）→ 假 verdict，代价不对称。header hash 逐条细化与嵌套粒度扩展同为纯增量演进位。
