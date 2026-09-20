# quark-follow

`quark-follow` 是一个面向自用环境的影视资源补集与维护脚本集。它会从 SeedHub 页面发现夸克分享链接，扫描分享内的剧集文件，与 OpenList/WebDAV 中的现有文件比对，并将可用的缺集转存到夸克目标目录。可选的夜间替换流程会在新文件明显更大时，将已有文件替换为更好的版本。

> 本项目会操作夸克云盘、OpenList 和 WebDAV 中的真实文件。请先在测试目录、测试资源和 `DRY_RUN=true` 下验证配置；不要把 Cookie、Token 或密码提交到公开仓库。

## 工作流程

```text
resources.json
      │
      ▼
resource_check.sh ──► SeedHub/Playwright ──► SQLite（剧集与分享缓存）
      │                                             │
      ├──► WebDAV 扫描 ─────────────────────────────┤
      │                                             ▼
      ├──► source_check.sh（扫描夸克分享） ──► 计算缺集并生成 tasks
      │                                             │
      ├──► addfile.sh（转存、刷新 OpenList、验证 WebDAV）
      │
      └──► replace.sh（仅在配置的夜间窗口执行已入队的替换）
```

日常只需要运行 `resource_check.sh`。它会为 `resources.json` 中的每个条目依次执行发现、扫描、补集和验证；该脚本本身**不会无限循环**，应由 cron、系统定时器或其他调度器按需触发。

## 组件与职责

| 文件 | 职责 | 是否建议直接运行 |
| --- | --- | --- |
| `resource_check.sh` | 主入口；编排资源发现、WebDAV 对比、候选选择、补集和替换队列。 | 是 |
| `seedhub_start.sh` | 创建或启动用于解析 SeedHub 的 Playwright 容器。 | 首次部署/容器停止时 |
| `seedhub_cache.sh` | 在容器内执行 SeedHub 解析器。 | 通常由主入口调用 |
| `source_check.sh` | 扫描已缓存的夸克分享，并更新 `share_files`。 | 排障时可直接运行 |
| `addfile.sh` | 读取临时 `tasks`，执行夸克转存并验证结果。 | 通常由主入口调用 |
| `replace.sh` | 执行数据库 `replace_queue` 中的待处理替换。 | 通常由主入口调用 |
| `dock/seedhub_cache.py` | Playwright SeedHub 页面解析器。 | 在容器内由包装脚本运行 |
| `dock/init_db.py` | SQLite 初始化参考脚本。 | 仅初始化/排障时 |

## 前置条件

### 本机命令

主机需要安装并可执行以下命令：

- `bash`、GNU 常用工具（`awk`、`sed`、`grep`、`sort`、`cut`、`tr`、`date`、`sleep`、`mktemp`、`md5sum`）；
- `curl`、`jq`、`sqlite3`；
- `python3`（主入口会用它处理路径/数据）；
- Docker（仅 SeedHub 解析流程需要）。

### 外部服务和凭据

1. 可用的夸克账号 Cookie；
2. 可访问的 OpenList 实例及 API Token；
3. OpenList 提供的 WebDAV 地址、用户名和密码；
4. 可拉取/本地可用的 `playwright-python:chromium` Docker 镜像。该镜像名是 `seedhub_start.sh` 中的当前默认值；若使用其他镜像，请确保其中有 `python3`、Playwright Chromium 和 `Xvfb`。

## 安装与首次配置

```bash
git clone <repository-url> quark-follow
cd quark-follow

# 创建并编辑本机真实配置；config.local 会被 shell source，只能填写受信任的 shell 变量赋值。
cp config.example config.local
chmod 600 config.local
${EDITOR:-vi} config.local

# 编辑待追踪资源。
${EDITOR:-vi} resources.json

# 启动 SeedHub/Playwright 容器。
./seedhub_start.sh
```

### 配置 `config.local`

`config.example` 是提交到仓库的脱敏模板；首次安装时复制为不受 Git 跟踪的 `config.local`。运行脚本和后续 Web 配置修改均读取 `config.local`。`config.local` 是 shell 配置文件，变量值通常用单引号包裹。至少应替换下列占位值：

```bash
QUARK_COOKIE='完整 Cookie'
OPENLIST_URL='http://openlist-host:5244'
OPENLIST_TOKEN='OpenList API Token'
WEBDAV_URL='http://openlist-host:5244/dav'
WEBDAV_USER='WebDAV 用户名'
WEBDAV_PASS='WebDAV 密码'
```

常用开关：

| 配置项 | 默认值 | 说明 |
| --- | ---: | --- |
| `DRY_RUN` | `false` | 设为 `true` 时只扫描，不执行实际转存。首次部署建议启用。 |
| `WEBDAV_DEFAULT_ROOT` | `/kuake/其他` | `resources.json` 未指定目录时使用的父目录。 |
| `MAX_PARALLEL_TASKS` | `2` | `addfile.sh` 同时处理的剧集数；夸克 API 仍会按 `QUARK_API_DELAY` 限速。 |
| `RESOURCE_TASK_MAX` | `3` | 单次为一个资源生成的最大补集任务数。 |
| `RESOURCE_AUTO_ADD` | `true` | 是否自动执行补集。 |
| `RESOURCE_AUTO_DISCOVER` | `true` | 是否继续发现新的 SeedHub 分享。 |
| `REPLACE_ENABLED` | `true` | 是否允许生成并执行替换任务。 |
| `REPLACE_WINDOW_START_HOUR` / `REPLACE_WINDOW_END_HOUR` | `2` / `7` | 替换任务的本地小时窗口，左闭右开。 |
| `LOG_DIR` | 脚本目录下的 `logs` | 所有生产脚本的日志目录。 |

### 配置 `resources.json`

支持顶层 `resources` 数组（也兼容直接数组）。每个资源必须提供 HTTP/HTTPS URL；`directory` 可省略，且提供时必须以 `/kuake` 开头：

```json
{
  "resources": [
    {
      "url": "https://www.seedhub.cc/movies/123456/",
      "directory": "/kuake/电视剧"
    },
    {
      "url": "https://www.seedhub.cc/movies/654321/"
    }
  ]
}
```

实际目标目录由父目录和 SeedHub 标题构成。例如 `directory` 为 `/kuake/电视剧`、页面标题为 `示例剧` 时，目标目录为 `/kuake/电视剧/示例剧`。

## 运行

### 首次安全演练

1. 在 `config.local` 中设置 `DRY_RUN=true`；
2. 启动容器：`./seedhub_start.sh`；
3. 运行主流程：`./resource_check.sh`；
4. 检查终端输出、`logs/resource_check.log`、`logs/seedhub_cache.log`、`logs/source_check.log`、`logs/addfile.log` 和数据库中的缓存；
5. 确认目录与候选资源正确后，再将 `DRY_RUN=false` 并重新运行。

### 日常执行

```bash
./seedhub_start.sh       # 容器已运行时可安全重复执行
./resource_check.sh
```

主入口使用锁目录避免同一工作目录内的重复执行。它在存在失败或未解决缺集时会以非零状态退出，方便定时任务或监控系统报警。`source_check.sh` 扫描失败时同样会返回非零。

可用于排障的命令：

```bash
# 查看 source_check 支持的范围控制
./source_check.sh --help

# 只重新扫描一个分享或一个剧集（ID 来自 resource.db）
./source_check.sh --share-id 3
./source_check.sh --show-id 1

# 查看已缓存的剧集、分享和替换队列
sqlite3 resource.db 'SELECT id, name, total_episodes, webdav_path FROM shows;'
sqlite3 resource.db 'SELECT id, show_id, seedhub_rank, status, fail_count, url FROM shares ORDER BY show_id, seedhub_rank;'
sqlite3 resource.db 'SELECT id, show_id, episode, status, error FROM replace_queue ORDER BY id DESC LIMIT 20;'
```

### 定时调度示例

下面示例每 30 分钟运行一次，并把标准输出和错误输出追加到独立的调度日志。请根据实际安装路径、时区和运行账户调整：

```cron
*/30 * * * * cd /path/to/quark-follow && ./seedhub_start.sh >/dev/null 2>&1 && ./resource_check.sh >> logs/cron.log 2>&1
```

不要让多个调度器、不同路径的副本或手工进程同时操作同一个夸克目录；当前部分 API 限速锁和缓存是以单个工作目录/进程临时目录为边界设计的。

## 数据、日志与恢复

- `resource.db` 保存剧集、分享、分享内文件、WebDAV 文件和替换队列，是运行状态的核心。升级、重建或调试前先备份它。
- `tasks` 是 `resource_check.sh` 与 `addfile.sh` 间的临时接口，会在后续运行中重新生成，不应手工当作长期任务队列维护。
- 所有脚本日志均写入 `logs/`。生产流程使用 `seedhub_start.log`、`seedhub_cache.log`、`source_check.log`、`addfile.log`、`resource_check.log` 和 `replace.log`；`dock/` 中的初始化和调试脚本也会写入同一目录。
- 每条日志均带有可供 Web 端筛选的阶段标记：`[PARSE]`（SeedHub/分享解析）、`[ADD]`（补集转存）、`[REPLACE]`（替换队列）、`[CHECK]`（主流程检查）或 `[INIT]`（数据库初始化）。建议由 logrotate 或外部脚本处理轮转和保留期。
- 分享连续扫描失败达到 `SHARE_DEAD_FAIL_COUNT`（默认 `3`）后会标为 `dead`；修复 Cookie、网络或分享后，可在数据库中审慎地恢复其状态，再进行指定范围扫描。

推荐的备份方式：

```bash
mkdir -p backups
sqlite3 resource.db ".backup 'backups/resource-$(date +%F-%H%M%S).db'"
```

## 已知限制与改进建议

以下是基于当前代码结构的优先改进方向：

2. **统一路径与部署方式（高优先级）**：`dock/init_db.py` 的数据库路径是硬编码的 `/root/scripts/quark-follow/resource.db`，而运行脚本根据自身目录定位数据库。应改为从脚本位置或环境变量推导路径，并提供可重复执行的一键初始化命令。
3. **补齐可重复测试（高优先级）**：现有 Python 文件中包含依赖真实站点、真实数据库和可视浏览器的试验脚本。应将页面解析、集数提取、文件名解析和 SQLite 迁移拆分为纯函数，并以 HTML fixture、临时 SQLite 和 HTTP mock 覆盖正常与失败场景；CI 至少运行 Bash 语法检查、Python 静态检查和单元测试。
4. **收敛重复实现（中优先级）**：夸克请求、限速、`stoken` 缓存、剧集名解析等逻辑散落在多个 Bash 脚本中。抽取公共库后可减少修复不一致、并降低维护成本；全局限速锁也应改为跨入口共享的稳定锁文件。
5. **明确数据库迁移策略（中优先级）**：当前运行数据库已经出现 `share_scan_rank` 这类后加字段，而初始化脚本的建表定义未完整体现。建议维护带版本号的迁移表/迁移脚本，在启动时校验并升级 schema，而不是依赖手工修库。
6. **提升可观测性（中优先级）**：增加结构化汇总（扫描数、缺集数、转存成功/失败数、耗时），并为 cron 提供通知钩子或退出码说明。日志也应统一目录、轮转与脱敏策略。
7. **降低外部依赖脆弱性（中优先级）**：SeedHub 页面选择器、夸克接口和预构建 Docker 镜像都可能变化。应固定镜像版本/提供 Dockerfile，记录兼容版本，并为解析失败输出可诊断的页面/选择器信息（同时避免泄露 Cookie）。

## 安全说明

- `config.local` 中保存高权限 Cookie、Token 和 WebDAV 密码；它已被 `.gitignore` 忽略，应保持仅运行账户可读，避免上传、粘贴到 issue 或写入日志。
- `config.local` 会被 `source` 执行，因此它不是普通 INI/JSON 文件；不要从不可信来源复制内容。
- 运行账户应只拥有目标 OpenList/WebDAV 路径的必要权限。先使用一个隔离的测试目录验证自动创建目录、重命名和替换行为。
- 替换流程会在确认新文件存在后删除旧文件。即使脚本包含恢复尝试，也不能代替备份；启用 `REPLACE_ENABLED` 前请确认备份与夜间窗口设置。

## 免责声明

本项目仅供个人学习和自动化运维使用。请确保你对所访问、转存和存储的内容拥有合法权限，并遵守相关服务条款与当地法律。
