# quark-follow

用于自动跟踪 SeedHub 影视资源，并将可用的夸克分享补入 OpenList / WebDAV 目标目录。

核心流程：

```text
resources.json
     │
     ▼
resource_check.sh
     │
     ├─ SeedHub / Playwright
     │      └─ 缓存前 20 个夸克入口
     │
     ├─ SQLite
     │      ├─ shows / shares
     │      ├─ share_files
     │      ├─ webdav_files
     │      └─ replace_queue
     │
     ├─ source_check.sh
     │      └─ 扫描夸克分享并更新缓存
     │
     ├─ addfile.sh
     │      └─ 按缺集转存并统一文件名
     │
     └─ replace.sh
            └─ 在指定时间窗口执行更高质量文件替换
```

## 功能

- SeedHub 一级页面解析，识别剧名、季度和总集数。
- 默认维护 SeedHub 前 20 个夸克入口，已有入口低频复查。
- 出现真实缺集时，受控探索前 20 名以外的入口。
- SQLite 缓存夸克 Share 状态和文件集数，减少重复扫描。
- 持久化 `stoken` 缓存，并在失效时自动刷新。
- 根据 WebDAV 当前文件判断缺集，只补当前已经发现的资源范围。
- `addfile.sh` 按指定集数白名单执行转存，不会把其他集混入任务。
- 同一集存在多个来源时优先选择更大的文件。
- 自动刷新 OpenList，并通过 WebDAV 验证转存结果。
- 支持替换队列，在指定时间窗口用更大的资源替换旧文件。
- 提供简单的 Flask Web 管理页面和夸克 / SeedHub API 调用统计。

## 文件

| 文件 | 作用 |
| --- | --- |
| `resource_check.sh` | 主流程，发现资源、判断缺集、生成任务并执行补缺 |
| `seedhub_cache.sh` | 调用 SeedHub Playwright 容器 |
| `docker/seedhub_cache.py` | SeedHub 页面解析与 Share 入口缓存 |
| `source_check.sh` | 扫描 Share 内容并更新 `share_files` |
| `addfile.sh` | 执行夸克转存、刷新和文件重命名 |
| `replace.sh` | 执行 `replace_queue` 中的替换任务 |
| `stoken_cache.sh` | 多个 Quark 脚本共享的 stoken 缓存 |
| `docker/init_db.py` | SQLite 数据库初始化 / 兼容迁移 |
| `docker/api_stats.py` | API 调用统计 |
| `web_app.py` | Web 管理页面 |
| `seedhub_start.sh` | 创建 / 启动 SeedHub Playwright 容器 |
| `seedhub_container.sh` | 管理 SeedHub 容器状态 |
| `resources.json` | 要跟踪的 SeedHub 资源列表 |
| `config.local` | 本机敏感配置，不应提交到 Git |

## 环境

主机需要：

- Bash
- `curl`
- `jq`
- `sqlite3`
- `python3`
- Docker

SeedHub 解析使用镜像：

```text
playwright-python:chromium
```

Web 管理页面额外需要：

```bash
pip install -r requirements-web.txt
```

## 配置

复制配置模板：

```bash
cp config.example config.local
chmod 600 config.local
```

至少填写：

```bash
QUARK_COOKIE='你的夸克 Cookie'
OPENLIST_URL='http://openlist-host:5244'
OPENLIST_TOKEN='你的 OpenList Token'
WEBDAV_URL='http://openlist-host:5244/dav'
WEBDAV_USER='WebDAV 用户名'
WEBDAV_PASS='WebDAV 密码'
```

其他扫描、并行、替换和 TLS 参数都可在 `config.example` 中调整。

> `config.local` 会被 Bash 直接 source，只填写可信的变量赋值；不要提交 Cookie、Token 和密码。

## 资源配置

编辑 `resources.json`：

```json
{
  "resources": [
    {
      "url": "https://www.seedhub.cc/movies/123456/",
      "directory": "/kuake/电视剧"
    }
  ]
}
```

`directory` 可省略，默认使用 `WEBDAV_DEFAULT_ROOT`。

目标目录最终为：

```text
directory / 剧名
```

多季资源会自动在数据库和目标目录名称中保留季度信息；单季资源默认按 S01 处理。

## 运行

先启动 SeedHub 容器：

```bash
./seedhub_container.sh start
```

然后运行完整检查：

```bash
./resource_check.sh
```

单独检查某个资源：

```bash
./resource_check.sh --show-id 1
```

`resource_check.sh` 不是常驻程序，建议由 cron 或其他调度器周期运行。

常用排障：

```bash
./source_check.sh --share-id 3
./source_check.sh --show-id 1
sqlite3 resource.db 'SELECT id,name,total_episodes,webdav_path FROM shows;'
```

## Web 管理

```bash
export QUARK_WEB_USERNAME='admin'
export QUARK_WEB_PASSWORD='你的强密码'
export QUARK_WEB_SECRET='随机字符串'
python3 web_app.py
```

访问：

```text
http://<NAS-IP>:5233
```

Web 页面提供资源状态、Share 详情、任务控制、手动资源管理、日志筛选、API 调用统计、SeedHub 容器管理和受限配置编辑。

## 数据与日志

运行状态主要保存在：

```text
resource.db
api_stats.db
stoken_cache.tsv
logs/
```

其中：

- `resource.db` 保存资源、Share、文件缓存和替换队列。
- `api_stats.db` 保存 API 调用统计。
- `stoken_cache.tsv` 保存 Share 的 stoken 缓存。
- `logs/` 保存各阶段运行日志。

建议在修改数据库或升级脚本前先备份 `resource.db`。

## 安全

本项目会直接操作夸克、OpenList 和 WebDAV 中的真实文件。

启用自动补缺和替换前，建议先使用独立测试目录验证配置。尤其不要把以下内容提交到公开仓库：

```text
Cookie
API Token
WebDAV 密码
config.local
```

仅处理你拥有合法权限的内容，并遵守相关服务的使用条款。
