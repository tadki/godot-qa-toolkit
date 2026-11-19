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
| L3 | 覆盖率前置过滤（历史形态挂 `--reuse-coverage` 旗标——该旗标已随 SEE-1594 A13 退役） | 没被测试覆盖的 mutant 不用跑 | **采纳并升级为同源投影**：`/2` 派生缓存（绑定映射内容与执行证据）→ 新鲜 `/6` map 现场投影；手动 `/1` store 已退役；provenance/绑定校验 + 可插桩锚语义（不可插桩行 ≠ uncovered） | 同一轮插桩的行事实服务选面与过滤；无需为默认 mutation 再跑一次 coverage |
| L4 | 沙盒目录克隆 + 多进程 worker 池 | 隔离变异副作用 + 并行执行 | **SEE-1420 拒绝沙盒（内存注入）；SEE-1550 反转为 per-worker 隔离磁盘影子槽**（hardlink COW，无硬链接 FS 退真拷贝；`.godot` 整树实拷贝） | take_over_path 内存注入存在可达性缺口：启动前完成的 autoload / preload / 组合根装配继续持有旧 Script（KOL 23 autoload 实证，假 survived）；hardlink COW 把拷贝税压到被改写文件本身，槽归还断言保留「中断无残骸」性质 |
| L5 | baseline 动态超时熔断 | 超时判定要随基线耗时自适应 | **采纳（小改）**：自适应上限 = `max(timeout, baseline×F+1)`，`--timeout-factor` 默认 2.0 只放大不收紧；**拒绝 UB 默认 10**（JVM 抖动所迫，gqt 域无此噪声） | 倍数是环境参数不是语义 |
| L6 | Babashka 快启动 / no-mutate / 常驻热 worker | 削启动税 + 跳过不可变区域 | **no-mutate 采纳**（`# gqt: no-mutate` 函数级 + 行级）；**Babashka 不适用**（gqt 是 Python 常驻 CLI，无冷启动问题）；**常驻热 worker 拒绝本轮降 backlog**（Godot 引擎级状态泄漏风险，重启条件量化见定案 §五） | 快启动语义在 gqt 域不存在；热 worker 的假 verdict 代价不对称 |
| L7 | `--mutation` / `--lines` 单点重跑 | 秒级复核单个位点 | **采纳并扩展**：稳定 id 或行号规格（单行/区间/多段）选中位点，使用选中行的测试集合并集；1 baseline + N selected，绕过差分复用与 coverage 前置过滤（防假阴性的公平抽查通道） | 卡尺只收窄位点与测试面，不改变 kill 判定 |

## 2. gqt Mutation 架构：五阶段流水线

```
源文件 ─▶ ① scope manifest 提取 ─▶ ② 差分引擎 ─▶ ③ 预算规划 ─▶ ④ worker 池执行 ─▶ ⑤ resume v2 持久化
              │                      │               │               │                  │
        scopemanifest.py        diffengine.py    planner.py     executor.py          resume2.py
        （ops.py 产 mutant）   （resume2 读盘）  （timing 协商）  （+gutrun/shadow·shadowpool/beacon）  （coverlink 供锚）
```

### ① scope manifest 提取 — `scopemanifest.py`（+ `ops.py` 变异点收集）

- scope 集 = 顶层 func + 内部 class 整体黑盒 + module（非 func 顶层声明：class_name/@export/const/signal）；lambda 归外层 func；无变异位点的 scope 不进 manifest（省 hash 与存储）。
- 位点 id = `<file>#<scope_id>#<n>`：scope_id 带 `func:`/`class:` kind 前缀（跨 kind 同名不碰撞），同 kind 同名消歧 `func:foo#2`，n 为 scope 内 1-based 扫描序。
- header hash（module scope）只收非 func 顶层声明——func 变更绝不触发全文件重跑（单向保守，见 D2）。
- `ops.py` 承担 AST 级变异点收集与语法预检（invalid_mutant 提前剔除，不跑 GUT）。

### ② 差分引擎 — `diffengine.py`（读 `resume2.py` 落盘）

- manifest 在场即差分（默认开启），跳过规则：未改 scope 且上次 killed/suspect/invalid_mutant → 复用；survived / timeout / timeout_pinned / run_error → **永远重跑**；scope hash 变更 → 该 scope 全跑；header hash 变更 → 全文件全跑；hash 全同 → 零执行速断。
- `--mutate-all` 是唯一显式退路；被跳过位点逐条 id 披露 `diff_skipped`。

### ③ 预算规划与行级选面 — `planner.py` / `truth_select.py`（+ timing store 协商）

- 默认 `--select line`：从 `/6` map 的 `line_to_tests` 与 `script_to_lines` 取得每个位点所在行的测试脚本集，按 `frozenset(line→tests)` 签名分组；同签名位点共享一个内容寻址选择目录与 baseline。`fn` / `file` 只作显式校准和回退视图，`full` 只作显式全脸审计。
- 显式 `--lines` 支持单行、连续区间与多段，先与实际位点求交并披露 `requested / hit / skipped_no_site`；`--mutation` 与 `--lines` 都使用选中行的测试并集，无映射位点在卡尺通道披露后公平执行。
- budget 按**差分后 retry 数**计（截断保序，拒轮转）；位点数 > budget 强制披露 `total_sites`。
- 自适应 timeout：baseline 永远实测（判定语义），timing store 历史 p90 只喂预算锚（缓存锚只抬下限）；`--timeout-factor` 可覆盖放大系数。
- resume key 负面清单：`--mutation-warning`、`--timeout-factor`、`timeout_streak` 绝不进任何 key——调配置不引发重跑。行级选择目录 glob 仍在 key 内，组面改变会自然失效旧条目，避免跨测试集假复用。

### ④ worker 池执行 — `executor.py`（+ `gutrun.py` / `inject.py` / `godot_gate.py`）

- 默认并行（jobs = `max(2, cpu//2)`），单文件同样 per-mutant 分片；变异点 ≤ jobs 自动退串行；共享 baseline 快照（worker 自跑 baseline 会 N 倍放大启动税且污染 timeout 锚）。
- 磁盘影子注入（SEE-1550 M0，反转 SEE-1420 内存注入）：每 worker 隔离 hardlink COW 影子槽（`.godot` 整树实拷贝），mutant 经「临时文件 + `os.replace`」于引擎启动前写入影子内目标 `.gd`，真实仓零写入；四级 beacon（磁盘读回哈希 → load 编译 → autoload 同源 → GUT 运行证据）钉住全加载形态可达性；Linux/win64 统一持久 warm 池（池锁不可用时才回退 fresh ext4 农场），分层新鲜度权威 + 每轮具名 reason（`shadow.py` / `shadowpool.py` / `beacon.py`）。
- kill 判定 = 失败测试名集合相对 baseline 的差集（不看 exit code）；`godot_gate` 跨进程 flock 槽位闸统一 mutation/coverage 的 Godot 并发上限。
- `gutrun.py` 承担 GUT 子进程调用与 launch-failure 甄别（毒条目防护：启动失败类 run_error 不落盘，后续差分/续跑不得误复用）。

### ⑤ resume v2 持久化 — `resume2.py`（+ `coverlink.py` 覆盖事实）

- 落盘 `<root>/.qa-cache/gqt-mutation-resume-v2/<file-key>/`：manifest.json（per-file 索引层：schema/header hash/含位点 scope hash 表，收尾方单点写）+ `<stable-id>.json`（per-mutant 条目：verdict + 全签名五元组 + scope_hash + timeout_streak）。
- tmp + `os.replace` 原子写；各 worker 各写各 id，无跨 worker 竞态；损坏/身份不符条目 = 拒收重跑，绝不脏合并。
- timeout_streak：复用位点重判仍 timeout → +1；streak ≥ 2 → 独立 verdict `timeout_pinned`（永不并入 killed、不计 ok 门禁、持续可见）；任一后续 run 非超时 → 解除清零。毒 run 守卫：单 run timeout ≥50% 且 ≥5 条 → 本窗口 streak 挂起并披露。
- `coverlink.py` 统一 prefilter 数据源：手动 `/1` store 已物理退役；默认行级选面把同一轮 `/6` 行事实按实际 `tests_glob` 投影成 `/2` 派生缓存，或在新鲜 map 上现场投影。`/2` 绑定映射内容与执行证据；源码、recipe、测试面或身份绑定漂移即失效，全部层不可用时回退不过滤，宁多跑不假阴性。

## 3. 优化思想提炼（抽象层）

1. **哈希换算力**：用 SHA（scope 级 + header 级）把"要不要跑"变成 O(1) 判定——算力买断执行力。差分的正确性不在哈希本身而在**跳过规则的白名单**：只有"可证明不受影响"的位点才允许跳过。
2. **差分保真（宁重跑不假绿）**：加速必须以不产生假绿为前提。survived / timeout / timeout_pinned / run_error 永不缓存信任——缓存只加速"已证明的杀死"，从不加速"未证明的存活"。红线优先于任何性能收益。
3. **卡尺闭环**：稳定 id 与行规格是差分体系的"卡尺"——补测迭代里 survivor → 补断言 → `--mutation <id>` 复核，或用 `--lines 10-20,30` 一次覆盖多个改动段；选中位点使用对应行的测试并集，把验证成本收敛到实际改动面。它同时是差分与覆盖率过滤的防假阴性抽查通道（无映射位点也可公平执行）。
4. **质量不降的显式化**：每一次跳过都必须留下机器可查的痕迹——`diff_skipped` 逐条 id、`untouched_ranges` 行号区间、`uncovered` 独立 verdict、verdict 全签名五元组防 id 漂移、timeout_pinned 永不洗白进 killed。**信息不披露 = 优化不合法**。
5. **机制适配性（语义 vs 机制分离）**：UB 的沙盒克隆、Babashka 启动器、JVM timeout×10 都是各自生态约束的产物；gqt 的磁盘影子注入（SEE-1550 起；更早的内存注入被可达性缺口证伪）、常驻 CLI、自适应 ×2 是 GDScript/GUT 域的同语义重造。跨体系移植时先问"这是问题还是解法"——问题可移植，解法未必。
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

沙盒目录克隆（SEE-1420 以内存注入严格优于为由拒绝；**SEE-1550 反转**——per-worker 磁盘影子隔离重新采纳文件级隔离语义，理由与形态见 §1 L4 与 §2 ④）；单文件靶向（放弃多文件 worker 池优势）；Babashka 启动器（常驻 CLI 无冷启动问题）；源文件尾 manifest 注入；LCOV 外部格式（复用自家 JSON + provenance）；UB 默认 timeout-factor=10（JVM 抖动所迫）；timeout 判 killed（UB 语义，gqt 以 timeout=存疑 + pinned 降级链替代）；文件全局 MNN 编号（插入位移不稳定）；嵌套 scope 方法级粒度（本期，KOL 实测无场景）；lazy adopt 迁移器（复杂度买回收益恒零，S1 自攻撤销）；budget 轮转/默认重估（D6）。

### 演进留白（重启条件，定案 §五）

P5 常驻热 worker：scoped 单轮 GUT 均值 < 7s 成主流 / mutant 数 ≥200 成典型 / 启动税占比 ≥30% / GUT 提供同进程状态复位 API 且通过状态污染探针——触发其一再评估。风险主因是 Godot 引擎级状态泄漏（autoload/static var/ResourceLoader/scene tree）→ 假 verdict，代价不对称。header hash 逐条细化与嵌套粒度扩展同为纯增量演进位。

## 5. SEE-1564 增量：测量面与通道身份归位

- **可测量才可优化**：非 dry-run 输出新增 `stage_timings` 八阶段累计墙钟 + `rss_peak_mb`（本进程 VmHWM 高水位）+ `disk_delta_bytes`（`.qa-cache` 与 ext4 影子农场树增量）。单位级只披露本单元亲自执行的阶段，运行级在信封上合并父层编排与分片执行阶段；**同名阶段跨层语义不同，必须分层披露而非盲目相加**——`slot_sync` 即例：它是槽获取→归还全程，天然包含 `per_mutant`，槽位固定成本取其差。测量面只加不改、不参与 ok 门禁。
- **测量旋钮与默认零漂移**：`GQT_MUTATION_JOBS_CAP` / `GQT_MUTATION_POOL_MAX_SLOTS` / `GQT_MUTATION_BASELINE_IN_FARM` 均为 opt-in 测量通道；全 unset 时与既有默认路径逐位同构，且仅在 set 时披露。baseline-in-farm 不改变「真实仓 baseline 权威」（D5）的默认语义，只提供 FS 轴实验位；制备失败即降级回真实仓并如实披露生效值。
- **身份键必须与执行通道同源（D-1）**：manifest 与记录分属不同写入方时，键的解析必须走同一通道解析入口；否则差分复用静默失效——加速机制失效却不报警，是差分体系独有的风险形态。cold 注入为 no-op，修复不迁移任何旧键，A/B 跨侧隔离不变。
- **通道无关事实归一到 canonical 身份（D-2）**：coverage 行事实与执行通道无关，读/写两侧统一按 cold 身份落盘与查找；manifest/记录仍按通道隔离。原则：身份的每一维只在它真实影响该事实时才进入键——否则维度越多，误失配面越大、优化越容易被静默关停。

## 6. SEE-1571 增量：四条默认加速通道

- **零引用短路（E2）**：派生测试 glob 零命中时，全量 GUT 退路替换为「有意不执行 + stderr/summary 双披露」定终态。跳过的合法性来自判据面（gdself 闭包零测试引用），不是猜测；显式 `--tests`、dry-run、零位点与收集错误都不短路，多文件报告保留该文件记录。设计取舍：该文件不再提供全量质量证据，只提供零引用事实与披露。
- **成本感知分片（E6-5）**：`stage_timings` 归一为四键先验，单文件 run 收尾方单点原子写；串/并行估算只在小规模域（≤8 位点）内生效。域门是正确性/稳定性防线而非参数调优——慢槽或异常先验不得把大规模目标误降级，出域与无先验的 fail-safe 都是现行并行。决策线 stderr 全披露，0 值保留旧先验消除串行轮的逐轮翻转。
- **gdself 索引进程内复用（E6-4）**：规划相位按 project root 只读复用一份正向索引，进程结束即失效、零持久化。复用的等价锚是「多文件规划结果与逐文件构建逐位一致」；不把缓存身份引入 verdict 面。
- **prewarm 新鲜度证明（E1）**：跳过导入的唯一依据是「当前输入内容摘要 + `.godot` 整树输出封印 + 引擎链内容身份」三者与证书逐位一致。证书签发要求两次成功导入内容定点，导入失败/错误输出/树歧义/未知 importer/自定义插件/锁不可用一律不签发。keep sidecar 是非导入配置输入（字节入摘要、不入 provider 闭包、零输出合法），bare 启动器无法解析为具体文件时同样不签发。真实仓、baseline-in-farm 与普通 farm 的 baseline 执行根在启动前双绑定，失配先重签证/重建——加速只作用于可证明新鲜的导入，绝不把 mtime 或存在性当内容证明。

## 7. SEE-1587 增量：行级主真值与同轮 Coverage Store

- **行事实唯一主真值**：`test-attribution/6` 保存 per-test/per-script covered line sets、`line_to_tests` 反查与可插桩行面；file/function 边由行事实 + 源码 registry 派生，不再独立测量。同一轮插桩因此同时服务选面、CRAP 输入与 mutation prefilter，无需第二次 GUT。
- **模板与增量单源**：attribution prepcache algo 使用 `attr-line/1` 编码行级粒度，缓存读回连带 `block_map`；全量与增量共用 `_instrument_pair`。生产变更替换相关文件的行事实，测试变更按测试身份整体替换，派生视图与反查从合并结果重算。
- **执行完整性先于空面结论**：无 bundle、零测试或零探针且非 vacuous 都不是有效真值；合法空面必须有探针点火或真实 vacuous 证据。auto 模式对失效产物重建，显式旧 schema 产物 fail-loud，不把工具链失效消费成零引用假绿。
- **投影的身份边界**：`/2` 条目绑定 map schema、recipe pristine、目标文件 sha 与测试面摘要，另保留源码、tests_glob、binary/identity 校验。任一漂移只使该层失效并披露；所有层失效时不过滤，绝不把陈旧覆盖当成当前事实。
- **执行机制复用**：worker、共享 baseline、并行与 killed 证书差分仍按 chunk 工作；行级只改变 chunk 的测试集合签名。glob 进 resume key，升级或组面变化后自然重跑。显式 `--jobs 1` 串行仍使用文件级并集，是正确但收敛较弱的兼容边界。
- **验收 harness 不得假绿**：行级基准以 A/A 噪声带裁定 S2/S0 性能门，只有 killed/survived 参与 verdict 对账；run_error 等不可判定项单列排除且不凑 compared。中断/待续输出 `status=pending`、`ok=false`、RC=1，全部完成并通过完整性门后才给终判。

## 8. SEE-1589 增量：三项提速机制在真实目标可达

B 轮的目标不是「再优化一次」，而是解释并消除**机制在真实项目上结构性不可达**：三处缺陷都不是参数问题，而是判据域与世界不一致。

- **路由判据域（闭包谓词）**：旧引用图把注释与字符串中的 autoload/`class_name` 名字当成 compile-time 边，并对每个名逐文件跑正则（真实项目 20.6s/进程）；闭包从真实 34 文件膨胀到 487 文件，绝大多数目标恒判冷。定案是把「编译期可达」收窄为 token 级事实——只有真实标识符使用才成边。语义不变（autoload / GUT 域 / 场景持有者仍恒冷），但判据回到它声称测量的东西。
- **槽位复用域（平台分叉）**：旧实现只让 win64 走 warm 池，其余平台一律 fresh 农场——Linux 上「warm 复用」不是性能差，而是代码路径不可达且无披露。定案是 Linux/win64 同走一套 warm 池与分层新鲜度权威；平台差异只保留在池锁可用性（不可用才回退 fresh 并具名披露），不再以平台身份替用户做不可见决策。
- **摘要域（proof 与槽树不对称）**：prewarm 证书只在顶层排除 `.git`/`.qa-cache`，影子槽却在任意深度排除；嵌套的 `addons/godot_mcp/.git` 指针使每轮摘要必失配，形成 `proof-drift-persists` 与每轮整槽重建。gqt 自生成物（`.gqt-worker` / `.gqt_beacon_host*` / `.gqt-attr-select`）与重链临时文件同样污染槽摘要。定案是把两侧收敛到同一排除域，并让重链在成功路径恒清临时文件——证明只在它能证明的域内生效。
- **披露先于信任**：三项机制的每次决策都有机器可读 reason（`worker_channel` / `farm` / `prewarm` 块），会话致命后的实际降级态（多数腿已冷跑）不得继续报 worker。原则与 §3.4 相同：机制失效不可怕，静默失效才不可接受。
- **阶段全归因**：旧 `stage_timings` 只覆盖约 56% 墙钟，farm 制备、prewarm/import、路由扫描与规划不在账上。定案新增 `farm_provision` / `planning` / `route_scan` 阶段与 `stage_reconciliation`（墙钟 − 已归因 = 未解释；并发重叠以 `over_attributed_s` 显式披露），使「提速不见了」可以被定位而不是被估算。
- **A 面契约收敛**：有效选面统一由单一判别点给出（`line`/`fn`/`file`/`full`/`lines`/`mutation`/`explicit_tests`）；单文件对外恒为 flat 信封；`measure run` 区分「union 已认证」与「映射本轮可用」；全脸超时建议在到达预算上限后改指可执行的窄面路径，而不是回显同一个超时值。共同原则：**契约描述实际生效面，不描述调用者以为生效的面**。
