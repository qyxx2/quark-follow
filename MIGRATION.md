# 迁移与部署指南

本指南用于把**现有 quark-follow 实例**迁移到另一台 Linux/NAS，或移动到同一设备上的新目录。项目会操作真实的夸克、OpenList 和 WebDAV 文件，迁移完成前不要直接启动自动补集或替换任务。

## 1. 在旧设备备份

1. 暂停 cron/计划任务，等待 `resource_check.sh`、`addfile.sh`、`replace.sh` 和 Web 后台任务全部结束。
2. 在项目目录中备份 SQLite 数据库：

   ```bash
   sqlite3 resource.db ".backup 'resource.db.migrate.bak'"
   ```

3. 将以下文件安全地复制到新设备：备份数据库（恢复时命名为 `resource.db`）、`resources.json`、`config.local`。需要保留调用统计、日志或缓存时，再复制 `api_stats.db`、`logs/`、`stoken_cache.tsv`；stoken 缓存可以重新生成。

不要复制旧的 `.venv`、PID/锁文件或临时文件。不要把 `config.local`、Cookie、Token 或密码提交到 Git。

## 2. 在新设备准备项目

1. 将仓库克隆或放到最终目录。宿主需要 Linux/NAS 环境，以及 Bash、`curl`、`jq`、`sqlite3`、Python 3（含 venv）和 Docker。
2. 在项目目录安装 Web 依赖，并修正脚本执行权限：

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements-web.txt
   chmod +x ./*.sh
   ```

   当前仓库中的 Shell 文件没有统一记录可执行权限；Web 后台任务也会直接执行这些脚本，因此这一步不能省略。

3. 将旧设备的 `resource.db.migrate.bak` 放到新项目目录并命名为 `resource.db`，同时恢复 `resources.json` 和 `config.local`。注意：仓库当前也跟踪了一个 `resource.db`；迁移现有实例时应明确用旧设备备份替换它，而不是把仓库中的副本当作最新业务数据。
4. 编辑 `config.local`，确认夸克 Cookie、OpenList URL/Token、WebDAV URL/用户名/密码都指向新环境，并设置权限：

   ```bash
   chmod 600 config.local
   ```

   如果 OpenList/WebDAV 的目标目录结构也发生变化，还要检查 `resources.json` 中的 `directory` 以及数据库中的目标路径；仅修改服务 URL 不会自动改写既有数据库路径。

## 3. 适配当前版本的目录硬编码

当前 `main` 中以下位置仍可能包含旧目录 `/root/scripts/quark-follow`。迁移到其他目录前，检查并改为基于脚本位置计算路径；后续版本若已修复，则无需重复修改。

- **`start-web.sh`**：将固定的 `cd /root/scripts/quark-follow` 和 Python 路径改为：

  ```bash
  BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
  cd "$BASE_DIR" || exit 1
  PYTHON="$BASE_DIR/.venv/bin/python3"
  ```

- **`restart-web.sh`**：将固定的 `BASE="/root/scripts/quark-follow"` 改为：

  ```bash
  BASE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
  ```

- **`docker/init_db.py`**：将固定的数据库路径改为：

  ```python
  DB_FILE = Path(__file__).resolve().parents[1] / "resource.db"
  ```

Web 的重启流程会再次调用 `start-web.sh`。确保启动时的 `QUARK_WEB_USERNAME`、`QUARK_WEB_PASSWORD` 和固定的 `QUARK_WEB_SECRET` 在重启后仍然可用；不要只在一次终端会话中临时导出这些变量，也不要将实际凭据提交到 Git。

## 4. Docker 与首次验证

SeedHub 使用镜像 `playwright-python:chromium`，仓库未提供该镜像的 Dockerfile。先确认目标设备已有镜像：

```bash
docker image inspect playwright-python:chromium
```

如果迁移到新设备且镜像不存在，可在旧设备上导出并传输镜像文件，再在新设备导入：

```bash
docker save -o playwright-python-chromium.tar playwright-python:chromium
# 将文件复制到新设备后执行：
docker load -i playwright-python-chromium.tar
```

同一台设备仅移动了项目目录时，旧的 `seedhub-playwright` 容器仍可能绑定旧宿主机路径。停止所有相关任务后，删除旧容器，再从新目录重建：

```bash
docker stop seedhub-playwright 2>/dev/null || true
docker rm seedhub-playwright 2>/dev/null || true
./seedhub_container.sh start
./seedhub_container.sh status
```

确认 Web 登录、配置地址和目标目录无误后，再手动运行资源检查。计划任务不会随 Git 仓库迁移；如果旧设备使用了 cron 或 NAS 定时任务，需要在新设备重新配置，并使用新项目目录。首次检查建议指向独立测试目录，确认行为正确后再启用自动补集和替换。

> 如果只是创建全新、独立的实例，不要盲目复制旧实例的数据库和资源清单；先明确需要继承哪些资源及状态。
