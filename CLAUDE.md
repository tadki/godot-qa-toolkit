# godot-qa-toolkit 本地主仓库规则（SEE-1416 事件后比照 KOL 设立）

**主仓库本地路径**：`/home/jerry/tools/godot-qa-toolkit/`（WSL 规范检出，gqt 全局 CLI 的 editable 安装源）

## 主仓库保护（比照 KingOfLikes CLAUDE.md §1 同款约束）

- **严禁切换本地主仓库分支，本地主仓库分支应永远为 master**
- **严禁直接在本地主仓库修改/提交代码**：agent 的一切代码工作必须走 Multica 受管 worktree（`multica repo checkout` 产出，位于 `~/.multica_workspaces/.repos/…/github.com+tadki+godot-qa-toolkit.git/worktrees/`）或独立任务检出；共享分支（`shared/SEE-XXX`）只允许在 worktree/独立检出内 checkout，**不得在本主检出上 checkout**
- 入主通道只有一条：worktree/共享分支 → GitHub PR → merge origin/master；repo-sync 负责把远端 master 同步回本主检出
- 本主检出上 **禁止运行 `gqt mutation/coverage`** 之外的长驻流程；repo-sync daemon（`.dev/repo-sync/`）是本目录唯一的常驻写手，其余常驻进程不得指向本目录
- push 纪律：只推 `shared/SEE-XXX` 共享分支（显式 refspec），严禁直推 master

## 背景

2026-10-15/16 SEE-1416 事故：agent 在本主检出上 checkout 共享分支，与 repo-sync 的分支守卫（stash+强切 master）互相拉锯，导致 4 个 commit 直接落在本地 master 并被同步器如实推上 origin/master，绕过 PR。本规则防止复发；架构级修复（受管 worktree 化、repo-sync 守卫改跳过）另行 issue 跟进。
