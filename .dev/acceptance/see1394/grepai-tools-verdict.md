# SEE-1394 GrepAI MCP 全工具实测报告（Bachi，Owner 指令 15:53Z）

- 日期：2026-10-11（UTC+8 约 23:55–24:05 执行）
- 背景：Owner 指令「你把 GrepAI 的所有工具都测一测」。F-001 修复（nomic-embed-text 拉回）后 grepai 已恢复，本轮对**全部 14 个工具**逐一真实调用。
- 环境：runtime 注入 `grepai → http://192.168.1.210:4893/mcp`（grepai_bridge stdio 桥）；workspace `kol`（postgres store @210:5435，embedder ollama nomic-embed-text 768 维，14716 symbols ready）

## 全工具实测结果（14/14 真实调用）

| # | 工具 | 输入摘要 | 返回摘要 | 判定 |
|---|---|---|---|---|
| 1 | grepai_search | query「save profile multi-slot serialization manager」，limit 3 | 3 条语义命中：save_data.gd（0.655）、test_profile_manager_see1052.gd ×2，含行级 content | **PASS** |
| 2 | grepai_index_status | 无参 | workspace=kol / king-of-likes / symbols_ready=true / total_symbols=14716 / provider=ollama / model=nomic-embed-text | **PASS** |
| 3 | grepai_list_workspaces | 无参 | kol、litellm-gateway 两个 workspace | **PASS** |
| 4 | grepai_list_projects | workspace=kol | king-of-likes @ /mnt/d/GodotProjects/king-of-likes | **PASS** |
| 5 | grepai_stats | 无参 | 74 queries 累计、tokens_saved 1,089,707（90.13% 节省）、by_command_type: search 29 / trace-callees 18 / trace-callers 26 / trace-graph 1 | **PASS** |
| 6 | grepai_rpg_search | query「save system serialization load profile」，limit 5，kinds=symbol | 5 个 RPG symbol 节点（test_profile_data_survives_save_load / _load_save_data / _on_load_save 等），含 feature_path 与 updated_at | **PASS** |
| 7 | grepai_rpg_fetch | node_id=sym:scenes/ui/load_save_select/load_save_select.gd:_load_save_data | 节点全文：kind=symbol、signature、feature、parents 链（file→subcategory→category→area）、incoming 边（contains + semantic_sim） | **PASS** |
| 8 | grepai_rpg_explore | 同节点 start，depth 1，forward | start_node + nodes/edges 结构返回（该节点无出边——叶子符号，图语义正确） | **PASS** |
| 9 | grepai_refs_readers | symbol=active_slot | 31 条 reader 记录（pause_menu.gd / profile_creation.gd / save_manager.gd autoload ×4 / 26 处测试），逐条含 access_at 行号+context | **PASS** |
| 10 | grepai_refs_writers | symbol=active_slot | 20 条 writer 记录（测试 setup/roundtrip 为主），逐条 access=write + context | **PASS** |
| 11 | grepai_refs_graph | symbol=active_slot | readers+writers 合并视图（31 readers + 20 writers 同上，单独结构） | **PASS** |
| 12 | grepai_trace_callers | symbol=_load_save_data | 2 callers：_load_profile_name(:108)、_load_play_time(:117)，各带 call_site context | **PASS** |
| 13 | grepai_trace_callees | symbol=_load_save_data | 2 callees：ResourceLoader.exists(:125)、ResourceLoader.load(:130)（引擎 API 边界正确降级为匿名 symbol） | **PASS** |
| 14 | grepai_trace_graph | symbol=_load_save_data，depth 1 | root + 3 nodes + 2 direct edges 的调用图 | **PASS** |

## 判定说明

- **14/14 PASS，零失败**。全部为真实调用返回（含具体行号/上下文/图结构），非自报。
- **数据一致性交叉验证**：refs_readers 与 refs_graph 的 reader 集完全一致（31 条）；trace_graph 的 2 条边与 trace_callers 一致——工具族结果互相咬合。
- **语义检索质量**：search 对「save profile multi-slot」直接命中 SEE-1052 profile 测试与 SaveData effects 字段（该特性的实际实现文件），排序合理。
- **fmt/toon 输出模式**：compact 参数在本轮调用未启用（stats 显示历史 compact 2 次/117 次）——compact/toon 属输出格式参数而非独立工具，通过参数覆盖，不单独计工具。
- **已知局限（非本轮缺陷）**：stats 的累计计数含修复前失败尝试；rpg_explore 对叶子符号返回空 edges 属图结构事实（depth 内无出边），非错误。

## 结论

GrepAI MCP 全工具链（语义检索 + RPG 图 + refs/trace 分析 + 运维状态）**全部可用**。F-001 收口状态在工具级进一步证实：不仅 search 恢复，全工具族均正常出数。

验收证据落点：godot-qa-toolkit `shared/SEE-1394-grepai`（本文件）。
