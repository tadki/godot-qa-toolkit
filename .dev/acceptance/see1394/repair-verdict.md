# SEE-1394 F-001/F-003/O-009 修复执行报告（Bachi）

- 日期：2026-10-11（UTC+8 约 23:05–23:25 执行）
- 授权范围（Owner 15:00Z + Atlas 登记修订 42）：F-001 拉回 ollama 模型 / F-003 恢复 Docker searxng / O-009 清陈旧条目；「恢复原位、恢复后去重、MCP 全量复测」
- 执行机：210 MacMini（经既有 SSH 通道）+ 105 本机

## 结论摘要

| 项 | 修复前 | 修复后 | 判定 |
|---|---|---|---|
| **F-001** grepai | `404 model "nomic-embed-text" not found` | `grepai_search` 返回真实语义命中（top: proxy_driver.ts 0.596） | **已修复** |
| **F-003** web_search | `cannot reach search backend` | `web_search` 返回真实搜索结果（godotengine.org 等 3 条，引擎 google cse） | **已修复** |
| **O-009** searxng 陈旧条目 | `~/.claude.json` 含 `searxng→210:47322/sse` | 备份后删除，`~/.claude.json` JSON 校验 VALID、mcpServers 为空 | **已修复** |

## 一、F-001：拉回 nomic-embed-text

**修复前（S 诊断，12:25Z 实测）**：`ollama list` 空表；`/api/tags`=`{"models":[]}`；blobs 1.4GB 含 `*-partial`。

```
$ ssh 210 "ollama list | tail -1; ls ~/.ollama/models/blobs/ | grep -c partial"
  （空表）17 partial 分片
$ ssh 210 "rm -f ~/.ollama/models/blobs/*-partial*; echo rm_exit=\$?"
  rm_exit=0；blobs remaining=0
$ ssh 210 "ollama pull nomic-embed-text"
  ［第 1 次］Error: pull model manifest: Get "https://registry.ollama.ai/.../manifests/latest": EOF（registry 瞬断）
  ［第 2 次］verifying sha256 digest → writing manifest → success（rc=0）
$ ssh 210 "ollama list; curl -s http://localhost:11434/api/tags"
  nomic-embed-text:latest  0a109f422b47  274 MB  embedding_length=768  capabilities=[embedding]
$ curl -X POST /api/show name=nonexistent-model-xyz（阴性对照） → 404（探针有效）
```

**端到端复测（105，真实调用非自报）**：`mcp__grepai__grepai_search` query「warmup hold-to-warm proxy state machine」→ 返回 3 条真实语义命中（`kol/.../proxy_driver.ts` score 0.596 + see1325 evidence + test_see1148_port_arbiter.sh）——postgres store 索引完好，无需重建（诊断预判证实）。

## 二、F-003：恢复 Docker searxng

**查证（修复部署路径决策）**：

```
$ ssh 210 "docker images -a; docker volume ls; docker ps -a --format '...'; docker system df"
  仅 multica 三件套（backend/web/postgres）；Volume 仅 multica_backend_uploads；镜像 3/容器 3/缓存 0
  → 零 searxng 残留（Docker clear 清空坐实）
$ tmutil latestbackup → 2026-10-11-203628.backup 存在
$ ls TM 备份目录（经 ssh） → 不可读（.timemachine TCC 边界，BatchMode 会话无全盘访问权）
→ 恢复路径判定：原形态=Docker（Owner 确认）但原容器数据不可恢复（clear 无残留 + TM 经通道不可及）→ 按授权走「重新部署」
```

**重新部署（原端口 47321 语义）**：

```
$ ssh 210 "cat > /Users/jerry/searxng/settings.yml"  # use_default_settings:true + secret_key(随机 hex) + limiter:false + formats:[html,json]
  settings_written=0
$ ssh 210 "docker pull searxng/searxng:latest" → Digest 85492ba8… Downloaded
$ ssh 210 "docker run -d --name searxng --restart unless-stopped -p 47321:8080 -v /Users/jerry/searxng/settings.yml:/etc/searxng/settings.yml:ro searxng/searxng:latest"
  container 0c5c01e9431b…
$ ssh 210 "docker ps …; curl -w '%{http_code}' http://localhost:47321/"
  searxng Up / 0.0.0.0:47321->8080/tcp
  47321 health http=200
$ curl "http://localhost:47321/search?q=godot+engine&format=json" → 真实返回（wikipedia infobox "Godot (game engine)"）
```

**端到端复测（105，真实调用非自报）**：`mcp__web_search__web_search` query「Godot 4.6 release notes」→ 返回 3 条真实结果（godotengine.org/releases/4.6 等，engine=google cse，note: brave/duckduckgo/wikidata 等部分聚合引擎未响应——见观察项 O-010）。

**与 web_search 网关的对接**：未改任何网关配置——`web_search.py` 的 `SEARXNG_URL` 默认即 `localhost:47321`，容器在 210 上恰以该语义在位（`-p 47321:8080`，同一 host 命名空间可见）。

## 三、O-009：清理陈旧条目

```
$ cp ~/.claude.json ~/.claude.json.bak-20261011T231621   ← 带后缀时间戳备份
$ jq 'del(.mcpServers.searxng)' ~/.claude.json > ~/.claude.json.new && jq empty ~/.claude.json.new && mv ...
  mv_exit=0；after: .mcpServers == {}；jq empty → VALID
```

**登记项 O-011（平台侧残留）**：runtime 注入 mcp-config（multica agent 级，平台持有）仍含 `searxng→210:47322/sse` 死条目——非 105 本地可清理面，超出授权范围未动；建议 Owner/平台侧择机从 agent mcp-config 删除（现删去后无任何功能损失，web_search 走 4892）。

## 四、去重核查

```
$ ssh 210 "ollama list | tail -n +2 | wc -l"     → 1（单一模型，无重复）
$ ssh 210 "docker ps --filter name=searxng -q | wc -l" → 1（单容器）
$ ssh 210 "docker images | grep -c searxng"     → 1（单镜像）
$ jq '[.mcpServers|keys[]|select(test("searx"))]|length' ~/.claude.json → 0（条目唯一性达成）
```

## 五、MCP 8 server 全量快速复测

| server | 复测方式与证据 | 判定 |
|---|---|---|
| grepai | `grepai_search`「warmup hold-to-warm…」→ 3 条真实命中 | PASS（修复后） |
| web_search | `web_search`「Godot 4.6 release notes」→ 3 条真实结果 | PASS（修复后） |
| hindsight | `recall`「PR merge gate owner sign-off」→ 返回 SEE-1273 决策观察 | PASS |
| context7 | `resolve-library-id`「GDScript」→ 命中 /websites/gdscript_tutorials 等 | PASS |
| web_reader | `web_url_read` godotengine.org/releases/4.6 → 4.6 发布页正文 | PASS |
| github | `get_file_contents` diag-f001-f003.md @ shared/SEE-1394 → 返回全文（sha 74e69c72） | PASS |
| understand_image | 重做绿色 136B PNG 上传 4890 → 模型答 "Green" | PASS |
| godot-mcp | 引用 S1b/S3b 判据表（本机编辑器实机拉起 + S3b 独立冷启动复现 15:00Z），本轮未重复拉起编辑器 | PASS（引用） |

## Findings Ledger（本轮）

| ID | 内容 | 处置建议 |
|---|---|---|
| F-001 | **关闭（已修复）**：模型已拉回，grepai_search 端到端恢复 | 复测证据保留本报告 |
| F-003 | **关闭（已修复）**：searxng 容器 `23:12` 新部署于 47321，web_search 端到端恢复 | 后续观察稳定性 |
| O-009 | **关闭（已修复）**：105 `~/.claude.json` searxng 陈旧条目已删，备份 `~/.claude.json.bak-20261011T231621` | 回退路径即还原备份 |
| **O-010**（新） | 新部署 searxng 为官方镜像默认引擎集，google cse 正常，brave/duckduckgo/wikidata 聚合引擎未响应（旧环境曾有 `~/.config/searxng-patches/google.py` 反 captcha 补丁，不含在官方镜像内；原容器 clear 后该补丁的容器内集成方式不可考） | 非阻塞；如需完整引擎覆盖，Owner 可决定是否重建旧补丁集成 |
| **O-011**（新） | 平台侧 agent mcp-config 仍含 `searxng→47322/sse` 死条目（`claude mcp list` 显示 ✘ Failed to connect——该 server 客户端连接失败不影响其他 server） | Owner/平台侧择机删除；无功能损失 |
| 附注 | `ollama pull` 第 1 次 registry EOF 瞬断，第 2 次成功——无遗留 | 无 |

## 六、追加修复：searxng 接入 OpenClash 代理组（Owner 15:22Z 追加指令）

**Recall 取证**：Owner 提示「当时 searxng 要接路由器 OpenClash 特定代理组」。本人 bank 无相关记忆 → 按 Owner 指示以 `hindsight memory reflect claude_code` 两路召回确认：① 现役架构为 SearXNG-US / SearXNG-Home（+ 2026-09-02 后增补 SearXNG-Google 粘性）三个 socks5 listener 按引擎分流；② listener 在路由器全局 `authentication:` 下带凭据，用户名 ejhome。随后经 210→router(192.168.1.1) 只读核验 live 配置：`/etc/openclash/config/AllServers.yaml` 存在 `socks5-searxng(57896→SearXNG-US)`、`socks5-searxng-home(57897→SearXNG-Home)`、`socks5-searxng-google(57898→SearXNG-Google)` 三条 listener 与全局 `authentication:` 条目（凭据与 recall 值一致，报告中不落明文）。

**初版缺漏说明**：本报告第二节部署的是官方镜像默认出站（裸直连），虽能工作但与原架构不符——更正：原架构 searxng 出站必须走 OpenClash 代理组（export 出口 IP 信誉是 CAPTCHA 关键，早年 WARP 直连曾因出口 IP 被 Google 302/CAPTCHA）。

**落地（210，同一容器，settings.yml 更新 + docker restart）**：

```yaml
outgoing:
  proxies:
    all: socks5h://ejhome:<REDACTED>@192.168.1.1:57896        # 默认出口 → SearXNG-US 专线 LB 组
  networks:
    searxng_google: { proxies: socks5h://ejhome:<REDACTED>@192.168.1.1:57898 }  # google → 粘性专线
    searxng_home:   { proxies: socks5h://ejhome:<REDACTED>@192.168.1.1:57897 }  # karmasearch → 住宅
engines:
  - { name: google, network: searxng_google }
  - { name: karmasearch, network: searxng_home }
  - { name: duckduckgo, disabled: true }    # 与原配置一致（TLS 指纹被封）
  - { name: startpage, disabled: true }
```

- 首次写入 list 形态 `proxies: - [url]` 报 `ValueError: proxies type has to be str, dict or None`（容器 CrashLoop），更正为 str 形态后 `docker restart` → `health http=200`。settings.yml 权限收紧为 600。
- 引擎侧验证：`curl localhost:47321/search?q=godot+engine&format=json` → **20 条真实结果**（google cse × godotengine.org 等）。
- **105 端到端复测**：`web_search`「Godot 4.6 release notes performance」→ 4 条真实结果（google cse，含 godotengine.org/releases/4.6 等）——经 OpenClash 出口链路完整可用。

## 合规自证

- 改动面：仅授权三处 + 本次追加的 settings.yml 出站代理接入（Owner 15:22Z 追加指令直接覆盖）；未动三仓 HEAD 与工作树、未改 sshd/known_hosts/凭据文件、未改网关与 LiteLLM 配置。路由器侧全程只读（grep/awk 读取，未写配置）。
- 阴性对照：ollama /api/show 未知模型 → 404；诊断期死端口 47999 → 000；searxng 错误的 list 形态 proxies → CrashLoop 判死可见（判据敏感）。
- 全部端到端判定为真实工具调用返回，非自报。
