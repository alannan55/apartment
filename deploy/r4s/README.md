# R4S / iStoreOS 部署说明

适用于 4GB R4S、已挂载的 USB 硬盘和少数固定管理人员。Windows 调试使用 `utils` conda 环境和项目根目录的 `启动公寓.cmd`；R4S 使用以下 ARM64 Docker 配置，不复制 Windows Python 环境。

## 结构与端口

- `app`：Linux ARM64 Python + Django + Gunicorn，1 个进程 / 2 个线程。SQLite、上传文件保存在硬盘 `/data`；代码及账号目录只读。应用没有发布宿主机端口。
- `gateway`：Caddy + DuckDNS DNS 插件，自动申请/续期免费证书。默认只发布 TCP 8443，浏览器访问 `https://alan-stream.duckdns.org:8443`。若该端口已有服务，修改 `.env` 的 `HTTPS_PORT`。
- 使用 DNS-01 验证，不需要为证书申请开放 80/443。该网关不会更新 DuckDNS 的 IP，请继续保留 iStoreOS 的 DDNS 更新任务。
- `/admin/` 和 `/health/` 不经公网网关转发；iStoreOS 管理、SSH、Docker 控制端口不应开放到公网。普通账号使用公寓页面即可完成日常维护。
- 不启用整个域名的 HSTS，以免影响同一域名下已有 HTTP 服务。`check --deploy` 可能提示这一项；公寓服务仍要求 HTTPS 和安全 Cookie。
- Docker 的端口发布会创建网络规则。最终需要从外网验证 IPv4/IPv6 的实际开放端口，不能仅凭容器配置判断宿主机防火墙。应用容器与路由器共享内核，容器权限限制不能替代独立设备隔离。

## 1. 准备硬盘目录与配置

确认 iStoreOS 的磁盘向导已经把硬盘按固定路径挂载；建议 ext4。项目可以存放在硬盘上的 `apartment-project` 目录。下面的目录仅为示例，请替换为实际挂载点。

```sh
cd /实际项目路径/deploy/r4s
cp .env.example .env
```

编辑 `.env`：

| 设置 | 用途 |
| --- | --- |
| `APARTMENT_STORAGE_ROOT` | 硬盘上专供本项目使用的目录，不是临时目录或系统盘目录 |
| `APARTMENT_DOMAIN` | 实际 DuckDNS 域名；示例按最新提供的 `alan-stream.duckdns.org` |
| `HTTPS_PORT` | 默认 8443，与串流和路由器管理端口错开 |
| `DJANGO_SECRET_KEY` | 至少 50 字符的随机密钥，可在电脑运行 `python -c "import secrets; print(secrets.token_urlsafe(64))"` 生成 |
| `ACME_EMAIL` | 证书联系邮箱 |
| `DUCKDNS_TOKEN` | DuckDNS 账号的 API token，不提交到 Git，不分享日志中的实际配置 |
| `DNS_RESOLVER` | 可从 R4S 访问的递归 DNS，默认 `223.5.5.5:53` |

正式设置中的密钥、域名、token 必须替换，`.env.example` 不能直接用于运行。之后的 shell 示例假定：

```sh
apartment_storage=/mnt/your-usb-disk/apartment
```

确认这个路径确实位于已挂载的硬盘上，再创建目录：

```sh
mkdir -p "$apartment_storage/data/media" "$apartment_storage/config" "$apartment_storage/caddy-data" "$apartment_storage/caddy-config"
chown -R 10001:10001 "$apartment_storage/data" "$apartment_storage/config" "$apartment_storage/caddy-data" "$apartment_storage/caddy-config"
chmod 700 "$apartment_storage/config"
chmod 600 .env
```

`chown` 只用于这些新建的项目目录，不要对整个硬盘执行。Compose 使用 `create_host_path: false`，目录未准备好时会拒绝启动；应用还会检查数据库是否存在，避免误启动空库。

## 2. 构建镜像

需要 Docker 和 Docker Compose v2 或更新版本。若 iStoreOS 没有 `docker compose`，先通过其 Docker 工具安装 Compose；不要把 YAML 直接交给只支持单容器的界面。

```sh
docker compose config --quiet
docker compose build
```

首次需要下载 Python、字体、Python 依赖和 Caddy 构建依赖。R4S 本地编译 Caddy 可能耗时；也可在电脑用 Buildx 构建 ARM64 镜像，导出后用 `docker load` 加载到 R4S：

```sh
# 从项目根目录执行；只构建，不连接 R4S、不申请证书。
docker buildx build --platform linux/arm64 -t apartment-app:local --output type=docker,dest=apartment-app.tar .
docker buildx build --platform linux/arm64 -t apartment-caddy:local --output type=docker,dest=apartment-caddy.tar -f deploy/r4s/Dockerfile.caddy deploy/r4s
```

R4S 加载两份 tar 后可跳过 `docker compose build`。构建不需要真实 DuckDNS token；实际凭据只在运行时传入。

## 3. 迁入现有数据和创建账号

先停止电脑上公寓程序，再复制现有 `db.sqlite3` 到 `$apartment_storage/data/db.sqlite3`，同时把现有 `media/` 内容复制到 `$apartment_storage/data/media/`。只复制数据库可能丢失上传图片。不要让电脑和 R4S 分别维护两份生产数据，也不要覆盖已在运行的 R4S 数据库。

复制后让容器用户能够访问：

```sh
chown -R 10001:10001 "$apartment_storage/data"
```

本地已配置的 `18611133192`、`18518912031` 两个账号保存在项目 `config/accounts.json`（只有密码哈希）。该文件不在 Git 或 Docker 镜像内；部署时单独复制到 `$apartment_storage/config/accounts.json`，即可沿用相同账号和密码。不要覆盖 R4S 已有的账号文件；已有文件时用下述 `account` 命令逐个添加。首次复制后设置权限：

```sh
# 在 deploy/r4s 目录执行；先单独传入本地文件到 R4S 项目的 config/accounts.json。
# 仅当目标账号文件尚不存在时复制。
test ! -e "$apartment_storage/config/accounts.json" && cp ../../config/accounts.json "$apartment_storage/config/accounts.json"
chown 10001:10001 "$apartment_storage/config/accounts.json"
chmod 600 "$apartment_storage/config/accounts.json"
```

需要创建其他账号时临时将账号目录改为可写；普通服务运行时该目录仍只读。绑定整个目录，账号命令原子替换 JSON 后能立即被应用读取。已迁入账号文件时跳过 `account owner`，直接运行 `check_runtime`。

```sh
docker compose run --rm --no-deps --volume "$apartment_storage/config:/config:rw" app python manage.py account owner
docker compose run --rm --no-deps app python manage.py check_runtime --require-db
```

密码交互输入，启用 Django 的密码强度验证，不写进 shell 历史。命令只写账号文件，不清空业务数据。首次登录时 Django 用户表会建立相应的会话记录，账号文件始终是凭据来源。

如果是没有既有业务数据的全新安装，需明确初始化空库：

```sh
docker compose run --rm --no-deps app python manage.py migrate --noinput
```

现有项目不要执行清空业务数据或初始化导入操作。正常启动会应用尚未执行的数据库迁移；更新前先备份。

## 4. 以后正式启动

```sh
docker compose up -d --no-build
docker compose ps
docker compose logs --tail=100 app gateway
```

应用先验证账号、磁盘目录和中文字体，再检查部署设置、迁移数据库、启动 Gunicorn。数据目录或账号配置有问题时不会启动业务服务。网关等应用健康后启动，通过 DNS 申请证书；DNS/API 可访问性及公网连通性需要在 R4S 上实测。

防火墙仅允许所选 HTTPS 端口进入网关，保留既有串流规则。不要添加 DMZ 或开放应用 8000、Docker API、iStoreOS 管理和 SSH。从手机移动网络验证登录、房态图片中文、报备下载、退出后无法访问，以及没有访问管理后台的入口。

在 iStoreOS 配置外置盘的开机挂载；实际部署后要验证重启后磁盘和容器均能恢复。Docker 的 `restart: unless-stopped` 不保证硬盘未挂载时也能正常运行。

## 账号文件格式与维护

位置为 `$apartment_storage/config/accounts.json`；本地开发默认是项目 `config/accounts.json`。可用 `APARTMENT_ACCOUNTS_FILE` 环境变量指定其他位置。

```json
{
  "users": [
    {
      "username": "owner",
      "password_hash": "此处由 account 命令生成 Django 密码哈希",
      "is_active": true,
      "is_superuser": false
    }
  ]
}
```

上面的哈希文字只是格式说明，不能直接作为实际账号文件。`config/accounts.example.json` 是无账号模板；项目没有默认密码或自助注册。

- `username`：区分大小写，唯一；不要带首尾空格。
- `password_hash`：只接受 Django 哈希格式，不保存明文；使用命令创建或重设密码。
- `is_active`：设置 `false` 禁用；删除该条目也会取消访问。已有会话在下次请求时失效，无需重启服务。
- `is_superuser`：默认 `false`。普通账号拥有相同业务维护权限；`true` 允许 Django 管理后台，但公网网关仍阻止访问后台。
- 文件缺失、损坏或有重复账号时登录失败，已有会话也停止访问，不回退到数据库密码。

```sh
# 创建另一位管理人员，或重设密码并重新启用账号
docker compose run --rm --no-deps --volume "$apartment_storage/config:/config:rw" app python manage.py account manager

# 禁用账号
docker compose run --rm --no-deps --volume "$apartment_storage/config:/config:rw" app python manage.py account manager --disable

# 只检查文件和用户名，不修改
docker compose run --rm --no-deps app python manage.py account manager --dry-run
```

只有明确需要维护 Django 后台时才加 `--admin`。账号文件与后台数据库用户不是两套可任选的密码来源，`createsuperuser` 不能替代账号文件。直接修改 JSON 时先备份，并使用临时文件替换，避免应用读到编辑中的半份文件。

登录使用 CSRF 保护；退出仅接受 POST。登录入口每个 IP 在 5 分钟窗口内最多 10 次提交（包括成功登录），超过后返回 429。计数保存在单个 Gunicorn 进程内存，重启后重置；适合当前单进程方案，不替代公网防火墙。Caddy 覆盖转发 IP/Host/协议，后端不发布公网端口，不要让未受信任的代理或客户端直接访问后端。

## 备份与更新

程序运行时，用 SQLite 备份 API 生成一致性快照：

```sh
docker compose run --rm --no-deps app python manage.py backup_database --dry-run
docker compose run --rm --no-deps app python manage.py backup_database
```

默认输出到 `$apartment_storage/data/backups/`，不覆盖已有备份。命令显示进度，支持 Ctrl+C，失败会清理临时文件。它只备份数据库；`data/media/`、`config/accounts.json`、`.env` 和证书目录需要另外保存。数据库备份与敏感账号文件不要放到公网可下载目录。

建议之后在 iStoreOS 定时任务中每天生成快照，并定期把快照、上传文件和必要配置复制到电脑或异地。当前代码没有替你创建定时任务。不要只保留同一块机械硬盘上的副本，定期清理旧快照。

更新前生成并保存备份；停止服务后更新代码/镜像，再启动。回滚涉及数据库迁移时应恢复对应快照和镜像，不直接覆盖仍在运行的数据库。恢复后核对房态、人员、账务和报备数量。

## 本地验证与范围

```powershell
conda run -n utils python manage.py check
conda run -n utils python manage.py test
conda run -n utils python manage.py makemigrations --check --dry-run
docker compose --env-file deploy/r4s/.env.example -f deploy/r4s/compose.yaml config --quiet
```

`.env.example` 可用于语法检查，不能用于实际部署。Linux 字体由镜像安装；Windows 继续使用已有中文字体。SQLite 必须放在 R4S 本机挂载的数据盘，不通过 SMB/WebDAV 打开数据库文件。实际的 ARM64 镜像构建、证书签发、硬盘供电/休眠、外网访问和重启恢复，需要在部署阶段验证。

参考：[Django 自定义认证](https://docs.djangoproject.com/en/5.2/topics/auth/customizing/)、[Caddy DuckDNS 插件](https://github.com/caddy-dns/duckdns)、[DuckDNS API](https://www.duckdns.org/spec.jsp)。
