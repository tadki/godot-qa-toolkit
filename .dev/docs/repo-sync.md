# GQT repo-sync（轮询式 git 双向同步守护）

> 来源：KOL 主仓 `.dev/repo-sync/`（SEE-476 起逐步演进，经 SEE-565/869/894/1268/1285/1292/1033 硬化）的等价移植（SEE-1406）。部署动作（enable/启动 systemd 服务）由 Owner 决定，本仓库交付物默认不启动任何常驻服务。

## 运行方式

| 入口 | 用途 |
|---|---|
| `python3 .dev/repo-sync/repo_sync.py --config .dev/repo-sync/repo_sync_config.json [--dry-run] [--direction {bidirectional,push-only}]` | 单次同步：branch guard → fetch 重试 → 四态检测 → pull `--ff-only` / push / rebase→merge 降级 |
| `nohup python3 .dev/repo-sync/repo_sync_daemon.py --config .dev/repo-sync/repo_sync_config.json >> .dev/logs/repo-sync-daemon.log 2>&1 &` | 轮询守护（5s 轮询、指数退避、连续失败熔断、multica 告警） |
| `.dev/launch/godot-qa-toolkit-start.sh` | 一键启动脚本（后台 repo-sync + 前台 multica daemon） |
| `.dev/systemd/godot-qa-toolkit-start.service` | systemd **system 级** unit（`WantedBy=multi-user.target` + `User=jerry`，`ExecStart` 指向启动脚本） |

验收/本地验证一律 `--dry-run`——**零写入语义**：dry-run 只做观测（branch guard/fetch/四态检测照常执行），不向 repo_path 落任何文件（state 与日志文件均不写入；stdout 照常输出）：

```bash
GQT_REPO_ROOT=~/tools/godot-qa-toolkit \
  python3 .dev/repo-sync/repo_sync.py --config .dev/repo-sync/repo_sync_config.json --dry-run
```

## 配置字段（`.dev/repo-sync/repo_sync_config.json`）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `repo_path` | `__GQT_REPO_ROOT__/` | 同步目标工作树；`__GQT_REPO_ROOT__` 哨兵由 `GQT_REPO_ROOT` 环境变量解析（默认 `~/tools/godot-qa-toolkit` 规范检出） |
| `remote` / `branch` | `origin` / `master` | 同步远端与分支（branch guard 仅允许 master，与 KOL 一致） |
| `lock_file` / `pid_file` | `/tmp/godot-qa-toolkit-sync.lock` / `...-daemon.pid` | flock 互斥与 daemon 单实例（陈锁按 PID 回收） |
| `log_dir` / `state_file` | `.dev/logs` / `.dev/logs/repo-sync-state.json` | 相对 `repo_path` 解析 |
| `alert_issue_id` | `a3181de2-539c-4624-9db8-30b1003ec6f9` | multica 告警 inbox（复用 KOL 同步告警 issue） |
| `daemon_poll_interval_seconds` | `5` | 轮询间隔；失败指数退避至 `daemon_backoff_max_seconds` |
| `daemon_max_consecutive_failures` | `10` | 连续持久失败熔断阈值（触发后停守护 + 告警） |
| `subprocess_timeout_seconds` | `120` | 单次同步子进程超时 |
| `fetch_retry_count` / `fetch_retry_delay_seconds` | `3` / `2.0` | 瞬态网络错误 fetch 重试 |
| `stash_on_dirty` | `true` | 脏树自动 stash/pop |
| `gitlink_forward_enabled` | `false` | SEE-1285 gitlink 自动 forward 三道 gate 开关（**GQT 默认关闭**） |
| `conflict_resolution_strategy` | `rebase_first` | DIVERGED 处理：rebase_first / merge_first / block_only |

## 告警

- DIVERGED 自动处置失败 → `multica issue comment add <alert_issue_id>`（单次同步层）。
- 守护层连续失败 ≥ 阈值 → 熔断停守护 + 同通道告警；transient 错误不计入熔断（指数退避重试）。

## 与 KOL 实现的差异点

| 维度 | KOL | GQT |
|---|---|---|
| 哨兵/env | `__KOL_REPO_ROOT__` / `KOL_REPO_ROOT` | `__GQT_REPO_ROOT__` / `GQT_REPO_ROOT`（沿用 GQT 既有 `GQT_TOOLKIT_ROOT` 风格） |
| 默认检出路径 | `/mnt/d/GodotProjects/king-of-likes` | `~/tools/godot-qa-toolkit`（全局 editable 安装工作树） |
| 锁/PID 文件名 | `king-of-likes-sync.*` | `godot-qa-toolkit-sync.*` |
| `gitlink_forward_enabled` | `true`（有 submodule，三道 gate 适用） | **`false`**（GQT 无 `.gitmodules`；代码保留，无 submodule 时所有 gitlink 路径自动短路） |
| 启动接线 | `.dev/launch/kingoflikes-start.sh` + `.dev/systemd/kingoflikes-start-all.service` | `.dev/launch/godot-qa-toolkit-start.sh` + `.dev/systemd/godot-qa-toolkit-start.service` |
| 复杂度门 | 无（KOL 侧无此工具） | `gqt complexity .dev/repo-sync --warn 10 --max 15` 纳入本仓质量门 |
| 测试树 | `.dev/tests`（与游戏测试混居） | `.dev/tests`（本仓仅 repo-sync unit + integration） |

## 测试

```bash
python -m pytest .dev/tests -q                                   # 全量（unit + integration）
python -m pytest .dev/tests -q -k "lock"                         # 锁与并发
python -m pytest .dev/tests -q -k "backoff or circuit or alert or category"  # 守护可靠性
```

集成测试全部使用 `tmp_path` 临时 clone + bare remote，绝不触碰 `~/tools/godot-qa-toolkit` 规范检出。
