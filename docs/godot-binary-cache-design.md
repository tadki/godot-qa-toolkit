# Godot 二进制缓存方案设计（SEE-1292 ②b 产出，供 ②c 在 KOL / godot-mcp 落地同一方案）

> 依据 Owner 07:59Z 指令：「严格意义上 kol、qa toolkit、godot mcp 三个库都要想一想 ci 的缓存需要哪些」。本文是设计文档；qa-toolkit 本库 CI 只需 pip 缓存（`.github/workflows/ci-pytest.yml`），Godot 二进制缓存按方案在 KOL 的 ci-gut-tests.yml 与 godot-mcp 的 headless Godot 层落地（②c / ②a 后续）。

## 1. 现状问题

KOL `ci-gut-tests.yml` 当前每次 run 用 `wget` 下载 Godot 4.6.2（~50MB zip）→ unzip → /usr/local/bin。实测该 run 总耗时 ~39s，下载安装段约占 8-15s 且受 GitHub release CDN 波动影响。godot-mcp 的 `launch/tests/hooks/see1273/test_see1273_t1_import.sh` 类 editor 可用测试需要 Godot 二进制，尚无 CI 层缓存方案。

## 2. 方案：actions/cache 缓存已解压二进制

```yaml
- name: Cache Godot binary
  id: godot-cache
  uses: actions/cache@v4
  with:
    path: /usr/local/bin/godot
    key: godot-${{ runner.os }}-4.6.2-stable
    # 注意：runner os 版本随 GitHub 镜像升级（ubuntu-22.04 → 24.04），
    # /usr/local/bin 下缓存的 ELF 依赖 glibc——key 里带 runner.os 已足够粗；
    # 若出现加载失败，把 key 升级为含 runner.image 的形式并作废旧 key。

- name: Install Godot 4.6.2 (cache miss path)
  if: steps.godot-cache.outputs.cache-hit != 'true'
  run: |
    GODOT_VERSION=4.6.2
    wget -q "https://github.com/godotengine/godot/releases/download/${GODOT_VERSION}-stable/Godot_v${GODOT_VERSION}-stable_linux.x86_64.zip" -O /tmp/godot.zip
    unzip -q /tmp/godot.zip -d /tmp/godot
    sudo mv "/tmp/Godot_v${GODOT_VERSION}-stable_linux.x86_64" /usr/local/bin/godot
    sudo chmod +x /usr/local/bin/godot
    godot --version
```

设计要点：

1. **版本进 key**：`godot-<os>-<version>` —— Godot 升版只改 key 与 Install 段两处；不升 key 会导致拿到旧二进制却以为装了新版（缓存的经典陷阱）。
2. **缓存对象是解压后的单个 ELF**（/usr/local/bin/godot），不是 zip：省去每次 unzip 的 ~3-5s；单文件也让 cache 体积最小（~130MB 解压后；GitHub Actions cache 单仓上限 10GB，单条无硬限，此体积可忽略）。
3. **cache-hit 时跳过下载**（`if: steps.godot-cache.outputs.cache-hit != 'true'`），但保留一步 `godot --version` 健康检查——缓存条目损坏时 fail fast 而非跑到测试层才炸。
4. **不用 setup-godot 类第三方 action**：减少供应链面；官方 release 直链 + cache 已够，且与 KOL 现有 Install 段逐行兼容（最小 diff 落地）。

## 3. 免费额度评估（ubuntu runner 分钟计费）

| 项 | 数值 | 依据 |
|---|---|---|
| 计费 | Linux runner 1×（免费公共仓库无额度限制；私有仓库 2000 分钟/月） | GitHub Actions 计费文档 |
| 每次下载段耗时 | 无缓存 8-15s → 命中缓存 ~2s（restore + version check） | KOL ci-gut-tests run 34665814505 实测 39s 总耗时，下载段占比推算 |
| 月节省 | 假设 KOL ~200 runs/月 × ~10s ≈ 33 分钟 | 量级评估，非精确 |
| 缓存存储 | 单条 ~130MB；GitHub cache 总上限 10GB/仓，7 天未访问自动淘汰 | Actions 缓存文档 |
| Godot 导入缓存（可选进阶） | `.godot/` 目录（class cache 等）也可入缓存（key: godot-import-hash(project文件集)），headless `--import` 段 ~10-20s 可省。注意 `.godot/` 含平台相关二进制缓存，跨 runner 镜像升级可能失效——首期不启用，单独评估后落地 | 实测 t1_import 类步骤 |

结论：方案在免费额度内无压力——Linux runner 分钟单价 1×，节省为纯收益；存储占用可忽略。

## 4. 边界声明：GUI 实机层留 dev box（owner-order §5.1 红线）

- 本方案只覆盖 **headless 可用** 的 Godot 二进制（`--headless` 跑 GUT / `--import`）。
- **GUI 实机测（真实 editor 打开、截图、多 agent lease 冲突、AC-DECPL-009 类全链路回归）不在 CI 环境目标内**：GitHub runner 无显示服务且按 owner-order §5.1，实机验收必须 godot-mcp + 真实 editor 实测，禁止 headless CONDITIONAL PASS —— 该层永久留在 WSL2 dev box，CI 侧以 launch-special.yml 的 env bucket「documented skip」留档可见性（见 godot-mcp 分层先例）。
- CI 的 Godot 缓存不替代 dev box 的 Godot 安装；两套环境版本须以同一 key 口径（4.6.2-stable）对账，避免「CI 绿但 dev box 行为不同」的版本漂移。

## 5. 落地清单（②c 消费）

- [ ] KOL `ci-gut-tests.yml`：Install 段替换为 §2 模板（diff 最小，GODOT_VERSION 常量保留）
- [ ] godot-mcp headless Godot 层（t1_import 类毕业进 CI 时）：同一模板 + docs/ci-cache-matrix.md 对账
- [ ] 各自 run 日志附 cache hit 证据行（`Cache restored from key: godot-...`）
