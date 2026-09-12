# CI 缓存矩阵 + 测试归属判据（SEE-1292 AC-DECPL-007；三库对账版）

> 依据 Owner 07:59Z 指令：「一个测试怎么确定自己属于哪一个库，怎么保证测试在 ci 的环境最大可用」。本文为 qa-toolkit 库落档版，同时承载三库矩阵总表；各库在各自 docs/ 留同口径落档版（godot-mcp: docs/、KOL: .dev/docs/）。

## 1. 三库 CI 缓存矩阵

| 库 | workflow（现有清单） | 触发 | 缓存需求 | 已落地 | Godot 依赖 |
|---|---|---|---|---|---|
| **godot-mcp**（fork tadki/godot-mcp） | `ci.yml`（server build+test+protocol）、`launch-ci.yml`（fast tier，61 行清单 = 60 个 `test_*` 入口 + 1 个 `selftest_` helper；shell 47 + node 14）、`launch-special.yml`（long/env/drift 三桶，dispatch+周 cron）、`release.yml`、`docs-live.yml`、`claude.yml` | push/PR main；special 手动+cron | **npm**（setup-node cache，server/package-lock）✅ 已落地；Godot 二进制层未落地——fork CI 现无 Godot 依赖（t1_import 类测试在 env bucket 留档，毕业进 CI 时按 docs/godot-binary-cache-design.md §2 同模板落地） | npm ✅ / Godot ⏳ | headless 可用（t1_import 类）；GUI 实机层留 dev box |
| **KOL**（KingOfLikes-Godot） | `ci-gut-tests.yml`（GUT unit 硬闸口）、`ci-lint.yml`（Phase A advisory）、`pr-cleanup.yml` | push/PR master；gut-tests 手动 dispatch（②c，shared 分支缓存证据 run 用） | **Godot 二进制** ✅ SEE-1292 ②c 落地（actions/cache@v4，key `godot-<os>-4.6.2-stable`，cache-hit 跳过下载 + `godot --version` fail-fast）；pip（gdtoolkit，ci-lint）未缓存（advisory 低频，暂缓）；`.godot/` 导入缓存可选进阶（设计文档 §3，首期不启用） | Godot ✅（②c） | headless GUT 必须；实机测留 dev box（§5.1） |
| **qa-toolkit**（本库） | `ci-pytest.yml`（65 项 pytest 单测） | push master / 任意 PR / 手动 dispatch | **pip**（gdtoolkit wheel）✅ 本轮落地 | ✅ | 零（单测全 mock GUT 路径） |

免费额度结论：三库全部 ubuntu runner（Linux 1× 计价），公共仓库无额度限制 / 私有仓库 2000 分钟月额度内余量充足；缓存收益为纯减时。

## 2. 测试归属判据：「一个测试怎么确定自己属于哪一个库」

判据**按被测对象的归属域**（测试消费的资产/依赖），不按测试语言或目录名：

| 判定问题 | 归属 | 例 |
|---|---|---|
| 测试消费 fork 内核/控制面资产（launch/、server/、addon 协议行为、lease/端口/proxy）？ | **godot-mcp**（fork `launch/tests/`） | test_see1148_port_arbiter、test_see1273_t2_chain（shim replay） |
| 测试消费游戏系统资产（autoload/、systems/、scenes/、tests/<module> GUT 测试）？ | **KOL**（`tests/<module>` 或 `.dev/tests/` 消费方集成） | GUT unit、see1220 消费方集成 |
| 测试消费 qa-toolkit 自身工具行为（parser/gate/mutation/coverage 逻辑）？ | **qa-toolkit**（`tests/unit/`） | test_parser、test_mutation |

细化规则（与 `.dev/docs/devtests.md` 已有边界裁定一致）：

1. **两棵测试树边界**（KOL 内）：被测对象是仓库级工具链（multica CLI/hooks/autopilot）→ `.dev/tests/`；被测对象是 godot-mcp fork 资产 → fork `launch/tests/`（SEE-1273 已终态）。
2. **qa-toolkit 的 GUT 依赖是「消费被测项目的 GUT」，不是「自带 GUT 资产」**：mutation/coverage 跑的是 `--project-root` 指向项目的 `addons/gut/`，工具自身资产为零 Godot 文件——所以其单测（mock GUT）归 qa-toolkit，而**真实跑 GUT 的端到端验证**归被测项目所在库的 CI（KOL ci-gut-tests 已覆盖）。
3. **新测试落位流程**：先答「被测资产在哪个库」→ 该库测试树；跨界（如 qa-toolkit 真实 GUT 冒烟）默认归被测项目侧，工具侧只留 mock 单测，避免同资产双树漂移。
4. **归属即维护权**：测试红时修测试的库就是资产所在库；drift 名单（godot-mcp launch-special drift bucket）由该库收敛。

## 3. 「怎么保证测试在 CI 环境最大可用」——headless 可跑性分级

四级分类（godot-mcp launch-special 三桶先例，三库通用口径）：

| 级别 | 定义 | CI 处置 | 例 |
|---|---|---|---|
| **fast** | 干净 ubuntu-latest checkout 上 headless 必绿，≤分钟级 | push/PR 硬闸口 | qa-toolkit 65 pytest 项；godot-mcp fast tier（含 SEE-1292 ②c 自 drift 毕业的 ws4_status_doctor / t16_runtime_identity 两项）；KOL GUT unit |
| **long** | headless 必绿但 ≥2min | dispatch/周 cron，非闸口 | godot-mcp t14 reaper grace（~90s+ 真实时钟） |
| **env-bound** | 需要 WSL2 dev box 资产（Windows Godot、live editor、/mnt/d、真实 lease 冲突） | **CI 显式留档 skip**（documented skip），永久 dev box；禁止伪装成 CONDITIONAL PASS | godot-mcp env bucket（测试树口径 7 项，见 launch-special.yml env echo 行）；KOL 实机测（owner-order §5.1） |
| （drift） | 当前红、非环境问题 | 非 blocking evidence run，收敛后毕业进 fast | godot-mcp drift 名单（SEE-1292 ②c 收敛 ws4/t16 两项后 -2） |

最大可用性的四条通用手段：

1. **headless 优先**：测试设计阶段就要求无显示服务可跑；确需 GUI 的按 env-bound 留档，不硬塞进 CI。
2. **环境前置显式化**：class cache 缺失类前置（KOL 的 `godot --headless --import`）写进 workflow 步骤而非依赖本地残留。
3. **缓存版本 key 化**：pip/Godot/npm 三类缓存的 key 都带版本与 lockfile hash（见 docs/godot-binary-cache-design.md §2），避免「缓存命中但版本漂移」。
4. **环境受限显式留档**：跑不了的测试在 workflow 里以显式 SKIPPED 行列出（可 grep），保持非 CI 可执行面可见——不静默省略。

## 4. 对账说明

本表对账基准（2026-09-12 实读；②c 落地后回改状态）：

- godot-mcp：`gh workflow list` 6 条（CI / Claude Code / Docs live / Launch CI fast / Launch Special / Release）；fast tier 4m39s、special 43s 实测 run 全绿（SEE-1291）；②c drift 收敛 -2（ws4/t16 毕业，launch-ci.yml +2 项 / launch-special.yml -2 项），快层 run 34689443552 @ 872bb9f 全绿。
- KOL：`.github/workflows/` 3 条；ci-gut-tests ②c 前最近 run 39s success（push master，run 34665814505）；②c 缓存落地见 `ci-gut-tests.yml` Godot 段，缓存证据 run 34688009654（cache not found，写入）→ 34688072659（cache hit，`Cache restored from key: godot-Linux-4.6.2-stable`）双 run success。
- qa-toolkit：②b 新增 `ci-pytest.yml` 1 条（pip 缓存 run 34685325408 冷 22s / 34685485739 命中 19s）。

godot-mcp 侧 Godot 二进制层落地后须回改本表「godot-mcp 已落地」列。文档与 workflow 实际不一致时，以 workflow 为准并回改本文档。
