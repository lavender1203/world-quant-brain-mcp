# world-quant-brain-mcp

WorldQuant BRAIN 平台的 MCP（Model Context Protocol）服务端，通过 Streamable HTTP 暴露工具，可在 Claude Code、Codex 等支持 MCP 的客户端中直接调用。

- 默认监听：`http://localhost:8876/mcp`（Docker 默认对外映射为 `8876`）
- 传输协议：Streamable HTTP
- 依赖服务：Redis（用于缓存与并发锁）

---

## 目录

- [一、Docker 安装（推荐）](#一docker-安装推荐)
- [二、Python 安装（Ubuntu / Windows）](#二python-安装ubuntu--windows)
- [三、环境变量配置](#三环境变量配置)
- [四、在 Claude Code 中配置使用](#四在-claude-code-中配置使用)
- [五、在 Codex 中配置使用](#五在-codex-中配置使用)
- [六、生产部署（可选）](#六生产部署可选)

---

## 一、Docker 安装（推荐）

Docker 方式会一并启动 MCP 服务和 Redis，无需在宿主机安装 Python、Playwright 浏览器以及系统依赖。

### 1. 前置准备

- 安装 [Docker](https://docs.docker.com/get-docker/) 与 Docker Compose（Windows 直接安装 Docker Desktop；Ubuntu 安装 `docker-ce` 与 `docker-compose-plugin`）。
- 克隆本仓库：

```bash
git clone https://github.com/lavender1203/world-quant-brain-mcp.git 
cd world-quant-brain-mcp
```

### 2. 准备 `.env`

```bash
cp .env.example .env
```

编辑 `.env`，至少填入：

```dotenv
CREDENTIALS_EMAIL="你的BRAIN账号"
CREDENTIALS_PASSWORD="你的BRAIN密码"
```

其余可选配置见 [环境变量配置](#三环境变量配置)。

### 3. 启动

```bash
docker compose up -d --build
```

- MCP 服务地址：`http://localhost:8876/mcp`
- Redis：`localhost:6479`

查看日志 / 状态：

```bash
docker compose logs -f mcp
docker compose ps
```

停止 / 重启：

```bash
docker compose down
docker compose restart mcp
```

> Windows 用户在 PowerShell / WSL2 终端中执行相同命令即可。建议使用 WSL2 后端的 Docker Desktop。

---

## 二、Python 安装（Ubuntu / Windows）

如不使用 Docker，可直接在本机用 Python 运行。需要本机额外提供 Redis（可用 `docker run -p 6379:6379 redis:6-alpine` 单独启动）。

### Ubuntu

1. 安装系统依赖（Playwright 启动 Chromium 需要）：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git \
    libnspr4 libnss3 libgbm1 libgtk-3-0 libx11-xcb1 libxss1 \
    libatk1.0-0 libatk-bridge2.0-0 libpango-1.0-0 libxrandr2 \
    libxcomposite1 libxdamage1 libxkbcommon0 libcups2 ca-certificates \
    fonts-liberation xz-utils unzip wget
sudo apt-get install -y libasound2 || sudo apt-get install -y libasound2t64 || true
```

2. 创建虚拟环境并安装依赖：

```bash
git clone https://github.com/lavender1203/world-quant-brain-mcp.git 
cd world-quant-brain-mcp

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
python -m playwright install chromium
```

3. 配置 `.env`（同 Docker 部分）：

```bash
cp .env.example .env
# 编辑 CREDENTIALS_EMAIL / CREDENTIALS_PASSWORD
```

4. 启动服务：

```bash
python main.py
```

默认监听 `http://0.0.0.0:8000/mcp`。

### Windows

1. 安装 [Python 3.12+](https://www.python.org/downloads/windows/)（安装时勾选 *Add Python to PATH*）与 [Git for Windows](https://git-scm.com/download/win)。
2. 在 PowerShell 中执行：

```powershell
git clone https://github.com/lavender1203/world-quant-brain-mcp.git 
cd world-quant-brain-mcp

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -r requirements.txt
python -m playwright install chromium
```

> 如 PowerShell 提示执行策略限制，先运行：`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。

3. 复制并编辑配置：

```powershell
Copy-Item .env.example .env
notepad .env
```

4. 启动 Redis（任选其一）：

```powershell
# 方式 A：用 Docker Desktop 临时跑一个 Redis
docker run -d --name mcp-redis -p 6379:6379 redis:6-alpine

# 方式 B：使用 Memurai / WSL2 中的 redis-server
```

5. 启动 MCP：

```powershell
python main.py
```

---

## 三、环境变量配置

所有配置通过 `.env` 注入（亦兼容 `config/user_config.json`，但 `.env` 优先级更高）。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `CREDENTIALS_EMAIL` / `CREDENTIALS_PASSWORD` | ✅ | BRAIN 账号密码 |
| `MCP_HOST` | | 监听地址，默认 `0.0.0.0` |
| `MCP_PORT` | | 监听端口，默认 `8000` |
| `MCP_STREAMABLE_HTTP_PATH` | | MCP 路径，默认 `/mcp` |
| `API_SETTINGS_TIMEOUT` | | API 超时（秒），默认 `30` |
| `FORUM_SETTINGS_BASE_URL` | | 论坛 URL，默认 `https://support.worldquantbrain.com` |
| `FORUM_SETTINGS_HEADLESS` | | Playwright headless，默认 `true` |
| `FORUM_SETTINGS_TIMEOUT` | | 论坛超时，默认 `15` |
| `FORUM_MAX_CONCURRENCY` | | 论坛并发，默认 `1` |
| `FORUM_RATE_LIMIT_SECONDS` | | 论坛调用间隔，默认 `0` |
| `REDIS_HOST` / `REDIS_PORT` | | Redis 地址，Docker 模式自动为 `redis:6379` |

### 流量与并发调优

服务会读取 BRAIN 返回的 `X-RateLimit-*` 响应头，按端点族（`data-fields` 1 次/秒 + 30 次/分、`users/self/alphas` 30 次/分、`alphas/{id}` 2000 次/小时等）自动限速，无需手工配置节流间隔。用 `get_api_traffic_status` 工具可查看当前各端点族的配额、近一分钟请求数和缓存命中情况。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `BRAIN_MAX_CONCURRENCY` | `8` | 同时在途的 HTTP 请求数上限（连接池按此值 ×2 配置） |
| `BRAIN_RATE_LIMIT_SAFETY` | `0.9` | 相对平台配额保留的安全余量，多端同时登录时可调低 |
| `BRAIN_RATE_LIMIT_REDIS_RETRY_SECONDS` | `30` | 限流器降级后重试 Redis 的间隔 |
| `BRAIN_CACHE_DIR` | `./cache` | 永久存储目录（见下） |
| `BRAIN_ALPHA_DETAILS_IS_TTL` | `120` | IS alpha 详情的 Redis TTL，仅合并同一次工作流内的重复读取 |
| `BRAIN_ALPHA_DETAILS_MAX_AGE` | `604800` | OS alpha 详情的磁盘记录新鲜度窗口，设 `0` 则永不过期 |
| `BRAIN_OS_LIST_TTL_SECONDS` | `300` | OS alpha 列表免校验窗口；窗口内 0 请求，过期后用 1 行探针校验。设 `0` 则每次都校验（仍只需 1 个请求） |
| `BRAIN_OS_LIST_RETAIN_SECONDS` | `86400` | 列表条目保留时长，供探针比对 |
| `BRAIN_AUTH_CHECK_TTL_SECONDS` | `300` | 复用登录态的时长，到期才重新校验一次 |
| `BRAIN_CORRELATION_MIN_INTERVAL_SECONDS` | `180` | 平台相关性检查（生产 + PPA 合计）两次**启动**之间的最小间隔，跨进程生效 |
| `BRAIN_CORRELATION_LOCK_TTL_SECONDS` | `7200` | 相关性槽位的持有上限（2 小时）；仅在进程崩溃时兜底释放，实际会自动抬到不低于 `轮询上限 + 300s` |
| `BRAIN_CORRELATION_MAX_WAIT_SECONDS` | `3600` | 单次相关性检查的轮询总预算 |
| `BRAIN_CORRELATION_POLL_MIN_SECONDS` | `5` | 轮询最小间隔；平台返回 `Retry-After: 1` 也不会低于此值 |
| `BRAIN_CORRELATION_BUSY_RETRY_AFTER_SECONDS` | `180` | 拿不到槽位时返回给调用方的 `retry_after` 上限 |
| `BRAIN_CORRELATION_CACHE_TTL_SECONDS` | `300` | 同一 alpha 的生产 / PPA 相关性结果缓存时长；窗口内重复查询不请求平台、不占槽位（设 0 关闭）|
| `FORUM_SSO_TTL_SECONDS` | `1800` | Zendesk SSO 握手复用时长 |
| `FORUM_POST_TTL_SECONDS` | `86400` | 论坛帖子（含评论）缓存时长 |

### 自己的 alpha 语料库

`sync_alpha_corpus` 把账号的全部 alpha 镜像到 `cache/alphas.db`。三个平台事实决定了它的实现方式：

| 事实 | 后果 |
| --- | --- |
| `offset >= 1000` 被拒（`"Cannot display more than the first 1,000 alphas"`） | 不能用 offset 翻页，必须按 `dateCreated` 游标前进 |
| `count` 在 10000 饱和 | 无法从 count 判断真实体量，只能实测（约 2,769/天） |
| `dateCreated` 秒级、同秒最多 8 个 | 游标每次回退 1 秒重叠，靠主键去重，否则会漏掉边界那一秒的行 |
| 平台返回本地偏移（`-04:00`），游标是 UTC | **必须统一为 UTC**，否则文本比较认为 `08-14T22:49-04:00 < 08-15T00:00Z`，同步会误判"游标倒退"而退化成每批 +1 秒（一天要 86400 批） |

入库时间戳一律归一为 UTC，所以 SQLite 的文本序等于时间序。

回测完成后 alpha **立即入库**（`create_simulation` 和多模拟的两条路径都接了），所以「我刚试过这个吗」不必等下次同步。这不增加任何平台请求——回测本来就已经拿到完整记录了。

注意 `alphas_fts` 是 FTS5 的**外部内容表**，不会跟随主表写入自动更新：增量写入必须显式维护三处索引（行、FTS 词条、token 关联），否则新 alpha 会「存进去了但搜不到」。批量同步走 `index=False` + 末尾一次性重建（几十万行时更快），实时写入才开 `index=True`。

`analyze_my_research` 在此之上做分析，**零平台请求**：

```
scope="summary"       各配置的尝试数/命中数/出片率
scope="productivity"  按数据字段的出片率 (hits/attempts)
scope="operators"     按算子的出片率
scope="gaps"          试得最少的配置组合
scope="similar"       FTS5 搜表达式：这个想法我试过没有
scope="best"          最佳 alpha
```

`productivity` 之所以能算出「出片率」而不只是列出好 alpha，是因为语料保留了失败样本——分母。只同步赢家的话这个 scope 就没有意义。

**看出片率时要连样本量一起看**：本账号实测 `generate_stats` 是 57 试 57 中（1.000），但这不是该算子有效——它只出现在本就精心构造的 Python alpha 里，是继承了那一类 alpha 的成功率而非导致它。`ts_scale` 那种 706 试 51.8% 的大样本才可信。

实测（15,290 条样本）：token 索引构建 0.3s，索引查询 0.6ms，FTS5 全文搜索 1.0ms。

### 存储分层

三类数据、三种存储，不是三选一：

| 数据 | 存储 | 理由（实测） |
| --- | --- | --- |
| 目录类大块（datafields/datasets/PnL，单条最大 5.3MB） | **磁盘文件**（zlib 压缩） | 压缩到 4.3%，读取 34ms——比 Redis GET 的 39ms 还快，`json.loads` 才是瓶颈 |
| 自己的 alpha 语料（约 678,000 条） | **SQLite**（`cache/alphas.db`） | 一键一文件会产生 678k 个 inode 且每次查询都是全解压扫描；实测 551 字节/条 → 约 374MB，带索引 + FTS5，零新依赖 |
| 会变的小数据（alpha 列表、pyramid 统计、分布式锁、限流窗口） | **Redis** | 有 TTL、需跨进程共享 |

向量库（机器上已有 Milvus + Ollama 的 `nomic-embed-text`）目前**不使用**。它唯一不可替代的场景是数据字段描述的语义检索（"找关于分析师预期修正的字段"，关键词会漏同义词）。等 FTS5 被证明不够时再加一层即可，不影响前三层。

### 跨进程共享的限流器

限流窗口存在 Redis 里（每个 端点族×时间窗 一个 ZSET，一段 Lua 原子完成剪枝→计数→准入），所以服务进程、后台构建、独立脚本共用同一份配额。改造前实测：独立进程与服务并发时触发 `429 on data-sets`；改造后两个独立进程各发 10 个请求，**零 429**。

学到的配额发布在 `rl:limits:<bucket>`，新进程直接继承，无需自己踩一遍。429 冷却也是共享的（`rl:cool:<bucket>`）——429 是账号级的，不是进程级的。

**降级**：Redis 不可用时自动回落到进程内滑动窗口，服务保持可用；每 `BRAIN_RATE_LIMIT_REDIS_RETRY_SECONDS`（默认 30s）重试一次，恢复后自动切回。`get_api_traffic_status` 的 `rate_limits._backend` 显示当前后端，`sent_last_minute_all_processes` 是所有进程合计的用量。

### 平台相关性检查：每 3 分钟一个槽位

生产相关性（`/correlations/prod`）和 Power Pool 相关性（`/correlations/power-pool`）是同一族"算完再取"的端点，账号级只允许一个在途计算。两者共用**一把 Redis 键**（按账号邮箱哈希分片，多账号互不干扰），语义是**互斥锁 + 限频**二合一：

槽位**同时存在于两层，两层都放行才算拿到**：

| 层 | 载体 | 覆盖范围 | 为什么需要 |
| --- | --- | --- | --- |
| 宿主层 | `cache/locks/*.json` + `flock` | 挂载同一 cache 卷的所有进程 | Redis 宕机或 key 被驱逐时仍然有效 |
| 跨机层 | Redis 键 `lock:brain_correlation:<账号哈希>` | 所有机器 | 唯一能跨宿主的层 |

- 取槽位：先拿文件层，再 `SET key held:<token> EX <ttl> NX`（一段 Lua，同时读回占用原因和剩余时间）；Redis 判忙则立即把文件层原样交还（没跑检查，不欠冷却）。
- 还槽位：**不是 DEL**，而是把两层都原子降级为 `cooldown`（TTL = `BRAIN_CORRELATION_MIN_INTERVAL_SECONDS`，默认 180s）。所以"上一次检查结束"到"下一次检查开始"至少隔 3 分钟。
- 抢不到就**立刻**返回 `status='correlation_busy'` + `reason`（`running` / `cooldown`）+ `retry_after`，绝不排队等待。
- 槽位在**整个轮询过程**中持有（取槽 → 轮询 → `finally` 还槽），而轮询循环是顺序 `await`，因此任意时刻全账号只有一个 `/correlations/*` 请求在途。
- 两层都不可用时直接拒绝检查，**不会**退化成进程内锁——那等于放任 N 个进程同时打平台。

为什么需要宿主层：`docker-compose.yml` 里 Redis 是 `--maxmemory 512mb --maxmemory-policy volatile-lru`，而锁 key 带 TTL，正是可驱逐对象；加上 Redis 中途宕机的窗口，只靠 Redis 会出现两个检查并发。

**锁 TTL 与轮询预算的关系**：TTL 只是崩溃兜底，绝不能在持有者还在轮询时到期，所以代码里 TTL 会自动抬到不低于 `BRAIN_CORRELATION_MAX_WAIT_SECONDS + 300`。想把单次检查的等待放宽到 2 小时，只需调 `BRAIN_CORRELATION_MAX_WAIT_SECONDS=7200`，TTL 会自己变成 7500，无需手工同步。

**注意**：持锁进程被 kill 时，键会带着 2 小时 TTL 留在 Redis 里，期间所有平台相关性检查都返回 busy。手动清除：`docker exec mcp-redis redis-cli --scan --pattern 'lock:brain_correlation*' | xargs -r docker exec -i mcp-redis redis-cli del`。

**结果缓存 5 分钟**：一次检查算完后，结果按 `账号 × 端点 × alpha` 缓存 `BRAIN_CORRELATION_CACHE_TTL_SECONDS`（默认 300s）。窗口内再问同一个 alpha 的生产（或 PPA）相关性，**在取槽位之前**就直接返回缓存，既不发请求也不消耗槽位，因此也不会因为冷却而收到 `correlation_busy`。缓存结果带 `cached: True` 和 `cache_age_seconds`。

- 生产和 PPA 是两个独立的键，同一个 alpha 的两种相关性互不覆盖。
- 只缓存**算完**的结果（`max` 不为 None）；`pending`（轮询超时）和 `correlation_busy` 从不入缓存，仍然可以立刻重试。
- 两层：进程内字典（Redis 宕机也有效）+ Redis 键 `corr_result:<账号哈希>:<prod|power-pool>:<alpha_id>`（覆盖其它 MCP 进程）。缓存被驱逐只是回落到重新请求，不像锁那样会出正确性问题。

**自相关性不受此限制**：`check_self_correlation` 走本地 PnL 缓存计算，不碰这把锁，可以随便调。

### 两层缓存：磁盘永久存储 + Redis 热层

不可变的平台数据存在 `cache/` 下的 zlib 压缩文件里，**永不过期**，用 `sync_platform_cache` 工具手动刷新。Redis 只保留真正会变的数据（alpha 列表、pyramid 统计）和分布式锁。

| 数据 | 存放位置 | 是否过期 | 依据 |
| --- | --- | --- | --- |
| `data-fields` / `data-sets` 目录 | 磁盘 | 否 | 实测 5.3MB JSON 压缩到 226KB（4.3%），磁盘读比 Redis GET 还快 |
| 算子表 `operators` | 磁盘 | 否 | 只在平台发版时变化 |
| alpha PnL | 磁盘 | 否 | 实测 2025-03 提交的 alpha 至今 PnL 仍停在 `2023-12-29`，recordset 只含样本内模拟且长度不变 |
| OS alpha 详情 | 磁盘 | 7 天窗口 | 表达式/settings/IS 指标已冻结，但 `os.osISSharpeRatio` 等会随样本外表现累积而变化 |
| IS alpha 详情 | Redis | 120 秒 | alpha 未提交前仍可编辑 |
| alpha yearly-stats / recordsets | 磁盘 | 否 | 与 PnL 同源：2025-03 提交的 alpha 至今 yearly-stats 仍止于 2023 |
| 教程 / 竞赛协议 | 磁盘 | 否 | 静态文档 |
| `user_alphas` 列表 | Redis | 1 天 | 会随新建 alpha 变化 |
| OS alpha id 列表 | Redis | 5 分钟 | 只在提交 alpha 时变化，提交成功即主动失效 |
| pyramid multipliers / events / 用户资料 | Redis | 1 小时 | 平台按自己的节奏更新 |
| 竞赛详情 | Redis | 1 天 | 公布后不再变动 |

另外：账号自身的 user id 在进程内记忆一次（此前 `get_leaderboard` / `get_user_competitions` 每次都要多花一个 `/users/self` 往返）；`/alphas/{id}/recordsets/*` 在平台计算期间会返回 `200 + 空 body + Retry-After: 1.0`，现已遵守该头（此前硬编码 sleep 2s 并 ×1.5 递增）。

### 自相关检查的性能

目标 alpha 的 PnL/详情走磁盘、OS 池 PnL 走 pickle 宽表、相关性用 pandas 本地算，唯一的网络调用是拉取 OS alpha 列表。

该端点实测约 **50ms/行**，且不支持字段裁剪（`fields`/`only`/`include`/`omit` 均被静默忽略），所以列表本身很贵。因为一天最多提交 5 个 alpha，列表几乎不变，于是改用 **1 行探针**校验：按 `-dateSubmitted` 取 1 行，比对 `count` 和最新 id——提交会同时改变两者，删除会改变 `count`。

USA/TOP3000（249 个 OS alpha）实测：

| 状态 | 端到端 | 请求 | 流量 |
| --- | --- | --- | --- |
| 冷启动（全量分页） | 23.73 s | 6 | 999 KB |
| 免校验窗口内 | **0.03 s** | **0** | 0 |
| 窗口过期（1 行探针） | **0.53 s** | 1 | **4.5 KB** |

三者 `max_self_corr` 完全一致。探针检测到变化时会自动退回全量分页。

通过本服务提交 alpha 会立即让列表失效；**在浏览器里提交**的 alpha 最多 5 分钟后才进入对比池。若两边混用，把 `BRAIN_OS_LIST_TTL_SECONDS` 设为 `0`——每次都用探针校验，仍只需 1 个 4.5KB 的请求。

### 模拟账本（回测去重）

所有回测都经过本服务，所以每次完成的模拟都会记录到 `cache/simulations/ledger.jsonl`——表达式、settings、alpha_id、IS 指标。带来两件事：

**同一个请求不会付第二次钱。** `create_simulation` 按 (type + settings + 表达式) 的指纹查账本，命中就直接返回当时的 alpha（响应带 `from_local_ledger: true` 和 `previously_simulated_at`），实测 **1ms** 对比一次真实回测的数分钟。改动任一参数（如 decay 4→99）会正确 miss 并走真实模拟。

数据是按月批量上线的，新字段可能改变结果，所以**月度数据发布后**值得用 `reuse_existing=false` 强制重跑（先用 `whats_new_in_data` 确认有没有新数据）。

**自己的研究历史可本地检索。** `search_my_simulations(region=, universe=, contains=, min_sharpe=, sort=)` 直接查账本，零平台请求——不必再去分页 `/users/self/alphas`（每行约 50ms/4KB，且限流 30 次/分）。

### 论坛

| | 首次 | 二次 |
| --- | --- | --- |
| `read_full_forum_post`（含 17 条评论） | 5476 ms | **0 ms** |
| `get_glossary_terms` | 启动完整 Playwright 浏览器 | 磁盘读取 |

术语表是一篇几乎不变的文章，此前每次调用都要拉起一个无头浏览器，现已永久存盘。帖子正文写定后不变、评论增长缓慢，按 `FORUM_POST_TTL_SECONDS`（默认 24h）缓存，`force_refresh=true` 可强制回源。

### 数据目录的完整性（重要）

`/data-fields` 的**无过滤窗口硬性截断在 10000 行**，而且 `count` 也在 10000 饱和，所以从返回值上看不出被截断：

```
offset=9950      -> http=200 count=10000
offset=10000     -> http=400 ["Invalid offset. Please use filters to narrow down the result."]
```

USA/TOP3000 的数据集声明 **91,076** 个字段，全局扫描只能拿到 **10,000**（11%），**267 个数据集一个字段都抓不到**（如 `pv87` 声明 6666 个）。

出路是按 `dataset.id` 查询——该过滤会解除上限（`dataset.id=pv87` 返回完整的 6666）。因此：

- `get_datafields(dataset_id=...)` 对该数据集**始终完整**，并永久存盘
- 不带 `dataset_id` 的结果若触及上限，会带上 `capped: true` 和明确的 `warning`
- `build_datafield_catalogue(action="start")` 逐数据集重建完整目录

构建作为**服务进程内的后台任务**运行，与前台请求共享同一个限流器——另起进程会让两个限流器各自以为拥有全部 30 次/分配额，必然互撞 429（实测确认：独立进程跑估算脚本时触发了 `429 on data-sets`，而服务进程内的构建全程零 429）。构建期间 `check_self_correlation` 实测仍是 0.55 秒。

完整性已实测验证：DEU/TOP500 重建后 **178/178 数据集、22,494/22,494 声明字段全部到手**，零字段数据集归零。注意合并后唯一字段是 22,492——因为**一个字段可以同时属于两个数据集**（该配置下 `price_momentum_12m_minus_1m` 同属 `analyst94` 和 `model109`，`baltic_dry_index` 同属 `model193` 和 `model219`）。所以完整性按**抓取行数**判定，不是按去重后的数量，否则每个建完的目录都会被误报为缺失。

完全可续跑：已存盘的数据集自动跳过（实测停止后重启，20 秒跳过已完成的 14 个数据集）。`action="status"` 看进度和 ETA，`action="stop"` 随时中断。
`sync_platform_cache(scope="status")` 会列出每个配置的 `declared / stored / coverage`，哪些配置还有盲区一眼可见。

#### 构建成本与去重

本账号 23 个在用配置合计 **4,635 数据集 / 950,244 声明字段 / 19,005 页**。受 30 页/分限流，全量约 **11.7 小时**。

但字段 id 只取决于 **(region, delay)**，与 universe 无关——实测 `option4` 在 USA/TOP3000 和 USA/TOP500 返回的 1298 个 id 完全相同，四个 USA universe 都是 345 数据集 / 91,076 字段，差异仅在 `userCount` / `alphaCount` / `coverage`。

| mode | 耗时 | 说明 |
| --- | --- | --- |
| `dedup`（默认） | 约 **4.9 小时** | 数据集列表相同的配置直接复用已建目录，payload 里用 `metrics_from_universe` 标明使用指标来自哪个 universe |
| `all` | 约 11.7 小时 | 每个配置单独下载，各自带真实的使用指标。关心 universe 级拥挤度时用 |

注：`MEA/TOP300`、`MEA/TOP400` 在平台上已返回 0 个数据集（本地仍留有它们的 OS PnL 池），构建会跳过并标记 `status: empty`。

### 字段搜索：FTS5，以及为什么暂不上向量库

字段描述有 128,442 条唯一文本，中位长度 75 字符的技术短语。此前的搜索是 Python 子串 AND 匹配，实测有真实的召回和精确率问题：

```
search="dividend cut"  ->  4 条
   anl69_td_xe_dvd  "...which is the cutoff date"   <- 匹配到 cut-off, 误报
漏掉 69 条语义相关的, 例如:
   est_12m_dps_lowerednum_4wks  "Number of lowered analyst estimates of dividend per share"
```

改用 **FTS5 + porter 词干 + BM25 排序**（`cache/alphas.db`，与 alpha 语料同库，便于联表）。建索引 144,516 字段 / 950,240 配置行约 6 秒，**零平台请求**，用 `sync_platform_cache(scope="field_index")` 重建。

效果：`analyst revision` 从返回 "forecast type" 元数据标志变成返回真正的修正指标（898 条），`short squeeze` 83 条全部相关，"cutoff" 误报消失。支持 FTS5 语法：`dividend AND (cut OR lower OR reduce)`。

#### 向量检索：试过了，实测更差，已撤除

FTS5 匹配描述里的**词**而非**意思**，`investor attention` 直接搜返回 0——看起来正是向量检索该解决的场景。所以完整实现并实测了一遍：Ollama 的 `nomic-embed-text`（768 维），106,481 条唯一描述，52 分钟嵌入，312MB，本地 memmap 暴力检索 12ms。

**结果是负面的。** 正确答案 `relative_interest_score`（"The Google Trends popularity score for the search term"）在语义检索里排到 **103,473 / 106,481**——倒数 3%，不是靠前：

| 查询 | FTS5 | 语义检索 |
| --- | --- | --- |
| `investor attention` | 0（但 OR 展开后 18 条，全对） | 1 条，`customer_securities_investment` — 错 |
| `dividend cut` | 3 条 | 2 条，描述就是 `dividend` — 更差 |
| `short squeeze` | 83 条 | 2 条 — 明显更差 |
| `crowded trade risk` | 0 | `mdl239_shortlasso1d` — 唯一胜出 |

原因是通用嵌入模型在按**字面**相近打分：`investor` ≈ `investment`/`securities`，所以 "Customer securities held for investment purposes" 得 0.690；而 "Google Trends popularity score" 里没有任何词像 "investor attention"，尽管金融语义上它就是答案。加任务前缀（`search_query:`/`search_document:`）测过，只把错误答案从 0.690 降到 0.597，**排序没变**。

真正有效的是**把 LLM 的查询扩展用在 FTS5 的布尔语法上**：

```
search="search AND (volume OR interest OR trend)"  ->  18 条, relative_interest_score 全部命中
```

这个缺口需要的是领域知识，不是通用语义相似度。若将来要重试，方向是金融领域微调的嵌入模型，而不是换个向量库。

（Milvus 也评估过：96k 向量本地 memmap 暴力检索 12ms，与 ANN 索引加网络往返无实质差距，还多一个可能挂掉的外部服务，所以即便当初结果是正面的也不会用它。）

### 数据目录的检索方式

平台给每个 datafield 打了 `dateCreated`、每个 dataset 打了 `dateUpdated`，新数据按月批量上线。这两个字段服务端支持 `order=` 和 `>` 过滤，本地缓存也 100% 保留了它们，所以"有没有新数据"完全可以离线回答。

`get_datafields` / `get_datasets` 现在返回**一页结果 + 覆盖全量匹配的分面计数**（dataset / category / type / 日期），而不是整个目录：

| 调用 | 匹配 | 返回 | 输出 |
| --- | --- | --- | --- |
| datafields 全量（旧行为） | 9539 | 9539 | ~637k tokens |
| datafields 默认 `limit=50` | 9539 | 50 | **~3.1k tokens** |
| datafields `since=2026-04-01` | 1206 | 50 | ~3.2k tokens |
| datasets 默认 | 345 | 40 | ~4.9k tokens |

新增参数：`since`（按 `dateCreated`/`dateUpdated` 过滤）、`sort`（含 `-dateCreated` / `-dateUpdated` 取最新）、`limit`、`offset`（响应带 `next_offset`）。

**排序偏向**：默认 `sort="userCount"` 是热度倒序，`userCount=0` 的字段永远排在最后。但对 alpha 研究来说，未被拥挤使用的字段往往更有价值——USA/TOP3000 的 10000 个字段里有 **2375 个 userCount=0**。`facets.userCount` 现在会按 `0 (uncrowded) / 1-10 / 11-100 / >100` 分桶，让这个尾部规模可见；用 `sort="dateCreated"`、`sort="coverage"` 或按 `dataset_id` 逐个浏览可以避开该偏向。

`whats_new_in_data(region, universe, delay, since=None)` 直接回答"上次看之后有什么新数据"——发布时间线、新增最多的数据集、最热门的新字段，约 1.4k tokens，**零平台请求**。要拿到比缓存更新的目录，先跑 `sync_platform_cache(scope="datafields", region=..., universe=..., delay=..., confirm=True)`。

### 候选池

`pool_sync` 的成本与池大小无关。池里的 alpha 是样本内的，而样本内 alpha 的记录由它那次模拟产生——重新模拟会得到**新的 id**，所以 code、settings、pyramid 和全部 IS 指标在条目的生命周期内都是冻结的（对线上记录抽样验证：0 个条目发生漂移）。唯一会变的是"它被提交了"。

因此默认不再逐个读取条目，而是直接问"自本池最旧条目加入以来提交了哪些 alpha"——无论池多大都只要 1-2 页。117 个条目实测：

| | 请求 | 耗时 |
| --- | --- | --- |
| 逐条读取（旧行为） | 117 | — |
| 提交列表（新默认） | **2** | 7.2 s |

需要捕捉手工改动或平台侧删除时，用 `refresh_details=true` 退回逐条读取。

`plan_submission` / `evaluate_candidate` 走的 `get_mutual_correlation` 在 PnL 全部落盘后是**零请求**——117 个条目的完整相关矩阵约 0.3 s 纯本地计算，无需再缓存 pairwise 结果。

`sync_platform_cache` 的用法：

```
scope="status"      # 默认，零请求：各命名空间的条目数、体积、最旧年龄
scope="migrate"     # 零请求：把还留在 Redis 里的不可变数据搬到磁盘并释放内存
scope="operators"   # 1 次请求
scope="datasets"    # 指定 region/universe/delay 刷新某一组，或刷新最旧的 max_entries 组
scope="datafields"  # 同上；单组约 200 次请求（1 次/秒），必须 confirm=True
scope="alphas"      # 刷新指定 alpha_ids 的详情与 PnL
```

不带 `confirm=True` 时只做试算，返回预计请求数和耗时。单次工具调用也可用 `force_refresh=True` 强制回源；`check_submission_status` 已默认绕过缓存。

完整字段参考 `.env.example`。

---

## 四、在 Claude Code 中配置使用

Claude Code 通过 HTTP transport 接入本服务。

### 方式 A：命令行添加

```bash
# 用户级（所有项目可见）
claude mcp add --transport http brain http://localhost:8876/mcp --scope user

# 或项目级（仅当前项目，会写入 .mcp.json）
claude mcp add --transport http brain http://localhost:8876/mcp --scope project
```

Python 直跑模式将 `8876` 改成 `8000` 即可。

### 方式 B：手动写入配置

项目级配置文件 `.mcp.json`（放在仓库根目录）：

```json
{
  "mcpServers": {
    "brain": {
      "type": "http",
      "url": "http://localhost:8876/mcp"
    }
  }
}
```

或用户级 `~/.claude.json`（`mcpServers` 同上结构）。

### 验证

```bash
claude mcp list
```

在 Claude Code 会话中输入 `/mcp` 应能看到 `brain` 已连接，工具列表中会出现 BRAIN 相关工具（创建模拟、查询数据集、论坛操作等）。

---

## 五、在 Codex 中配置使用

Codex 通过 `~/.codex/config.toml` 接入 MCP。Streamable HTTP 服务需启用 rmcp 客户端：

```toml
experimental_use_rmcp_client = true

[mcp_servers.brain]
url = "http://localhost:8876/mcp"
```

- Ubuntu / macOS：`~/.codex/config.toml`
- Windows：`%USERPROFILE%\.codex\config.toml`

保存后重启 Codex CLI / IDE 扩展。进入会话后查看 MCP 状态应能看到 `brain` 已连接。

---

## 六、生产部署（可选）

### Nginx 反向代理 + HTTPS

仓库提供 `deploy/nginx/mcp_http.conf` 示例：

```bash
sudo cp deploy/nginx/mcp_http.conf /etc/nginx/sites-available/mcp_http
sudo ln -s /etc/nginx/sites-available/mcp_http /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

关键点：

- `proxy_http_version 1.1` + `proxy_set_header Connection ""` 保持长连接
- `proxy_buffering off` 流式响应
- `proxy_read_timeout 3600` 容忍 BRAIN 长耗时调用
- 用 certbot / Caddy 在前面接入 TLS

### systemd 守护

```bash
sudo cp deploy/systemd/mcp-http.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mcp-http.service
sudo journalctl -u mcp-http -f
```

该 unit 默认通过 `docker compose` 启停，使用仓库内的 `.env` 作为环境文件，按需修改 `WorkingDirectory` / `User`。

---

## 候选池管理（Candidate Pool）

BRAIN 每天只接受约 4 个 Regular Alpha 提交，但一轮研究往往能产出更多合格 alpha。
这些 alpha 需要排队，而排队期间有两件事必须被正确处理：

1. **金字塔覆盖 = 已提交 + 候选池**。只看已提交会低估覆盖，导致重复挖同一座塔。
2. **提交任何一个 alpha，都会抬高其余候选的生产相关性与自相关**，因为被提交的
   alpha 加入了它们所对标的池子：

   ```
   投影生产相关性(B | 提交 S) = max( B 当前 prod-corr,  max_{A∈S} |corr(A,B)| )
   投影自相关(B | 提交 S)     = max( B 当前 self-corr,  max_{A∈S} |corr(A,B)| )
   ```

因此「保证提交候选后其他候选的生产相关性不超 0.7」等价于
**池内两两相关性必须始终 < 0.7** —— 这个不变量在**入池那一刻**就强制执行，
而不是等到提交当天才发现来不及。

候选之间的相关性用本地 PnL 计算，不占用 BRAIN 的相关性并发槽；
只有候选自身的生产相关性会走那个限流接口，并缓存在条目里。

### 工具

| 工具 | 作用 |
|---|---|
| `pool_check` | 试算：这个 alpha 能否入池。不修改池。一次报出全部违规项，而不是只报第一个 |
| `pool_add` | 通过四道闸后入池；被拒时给出原因且不写入。`force=true` 可强行入池并记录 `forced_reasons` |
| `pool_remove` | 移除候选（例如手工提交后） |
| `pool_list` | 列出候选及其指标、相关性 |
| `pool_pyramid_coverage` | 真实覆盖表：每座塔的「已提交 / 池 / 合计 / 还需提交几个」 |
| `pool_submission_plan` | 排出今日提交批次，并**证明**它不会伤害池中其余候选 |
| `pool_sync` | 刷新条目；已提交（status ACTIVE）的自动移出池 |

四道闸（`pool_check` / `pool_add`）：

- 候选自身生产相关性 < `prod_threshold`（默认 0.70）
- 候选自身自相关 < `self_threshold`（默认 0.70，与平台闸一致；`pool_check` /
  `pool_add` / `pool_submission_plan` 三处默认值相同）
- 与池内每个候选的 |相关性| < `prod_threshold` —— **安全闸**，硬性
- 与池内每个候选的 |相关性| < `mutual_threshold`（默认 0.40）—— **多样性闸**，
  可用 `allow_diversity_fail=true` 单独豁免（永远不会豁免安全闸）

**「算不出来」不等于「安全」**：任一相关性拿不到（PnL 缺失、相关性槽位被占导致
生产相关性未知）都算**闸没过**，只能用 `force=true` 明确接受未经证明的配对；条目里
存的是 `null` 而不是 0，规划时也不会被当成 0 来算。底层的
`get_mutual_correlation` 对无重叠历史的配对返回 `null` 并列进 `unknown_pairs`，
不再折成 0.0。

### 覆盖表状态

- `OS_SUFFICIENT` —— 已提交数达标，塔已点亮
- `NEEDS_<n>_SUBMISSIONS_FROM_POOL` —— 池里候选够，交上去即可点亮
- `SHORT_BY_<n>_CANDIDATES` —— 池子不够，还得继续挖

塔只由**已提交**的 alpha 点亮（默认 `target=3`）；池是能把它推到位的队列，
所以两者并排显示。省略 `region` / `delay` 即跨区域汇总。

### 提交批次的安全判定

`pool_submission_plan` 的 `all_remaining_safe` 是**三值**的：

- `true` —— 批次留下的每个候选都被证明仍在闸内
- `false` —— 有候选会被顶出闸（详见 `remaining_pool_after_batch`）
- `null` —— **证明不了**：某个相关性缺失（`unprovable_for` / `pairwise_unavailable_for`
  列出是谁）。按不安全处理，先 `pool_sync refresh_prod=true` 补数据再规划

批次内部的两两相关性要同时过**生产闸和自相关闸**（判据是
`min(prod_threshold, self_threshold)`）：同一天提交的两个 alpha 会互相进入对方的
自相关池，只看 0.7 会漏掉 0.5~0.7 这段。另外被 `resolve_conflicts` 放弃
（`sacrificed`）的候选**不会**再出现在同一批 `plan` 里。

### 互斥候选的死锁

若池中存在一对互相关 ≥ 0.7 的候选（通常来自 `force=true`），提交任一个都会毁掉
另一个。纯保护式的规划会两个都不提交，于是永久卡住。`pool_submission_plan` 会把
这类组合列进 `conflicts` 并给出保留/放弃建议（按未满足的金字塔需求 → 倍率 → Sharpe
排序）；加 `resolve_conflicts=true` 才会执行该建议，被放弃者列在 `sacrificed` 中。

### 说明

- 池文件存放于 `/app/config/candidate_pool.json`（compose 中的 `mcp_config` 卷，重建镜像不丢失），
  可用 `CANDIDATE_POOL_FILE` 覆盖。
- **写入是加锁的**：所有改池的操作（add / remove / sync）在 `flock`
  （`.candidate_pool.json.lock`）内完成"读→改→写"，锁只覆盖文件操作、不跨网络等待。
  入池还带乐观校验：评估期间池子成员变了就重新评估，不会用旧快照覆盖别人的写入。
- 池文件损坏时会被改名成 `candidate_pool.json.corrupt-<时间戳>` 保留原始字节，
  之后才以空池继续；无法改名时 `save_pool` 直接拒绝写入。
- `pool_sync refresh_prod=true` 会绕过 5 分钟结果缓存（提交后旧值必然是错的），
  但平台每 3 分钟只放行一次相关性检查，所以**一次调用刷不完多条**：遇到第一个
  `correlation_busy` 就停，`prod_refresh` 如实报出 `updated` / `busy` /
  `not_attempted` / `retry_after`，等这个秒数后再调一次继续。
- **这些工具不会提交任何 alpha**，只产出计划，提交由人工执行。
- 测试：`python test_candidate_pool.py`（使用假客户端，不触网）。
