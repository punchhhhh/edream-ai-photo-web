# 生产部署(studio.ymmjc.com)

生产环境部署与发布手册。本地开发见 [README](README.md) 的「快速开始」。

## 环境概览

| 项 | 值 |
| --- | --- |
| 域名 | `https://studio.ymmjc.com`(HTTPS,Let's Encrypt,80 强跳 443) |
| 服务器 | 腾讯云广州 CVM,`ubuntu@134.175.68.24`(SSH 公钥免密) |
| 部署目录 | `/opt/edream`(后端 + 前端 dist + `.venv` + `.env` + `media/`) |
| 进程 | systemd 服务 `edream.service`:uvicorn 监听 `127.0.0.1:8000`,单进程 |
| Web 入口 | nginx,站点配置 `/etc/nginx/sites-enabled/studio.ymmjc.com.conf`:托管 `frontend/dist` 静态资源(SPA) + `/api/` 反代 8000(读写超时 600s,`client_max_body_size 600m`) |
| 数据库 | 本机 PostgreSQL 16,库 `edream_ai_photo`,连接串在服务器 `.env` |
| 资产存储 | 腾讯云 COS `studio-1436584532`(ap-guangzhou,私有读写,前缀 `edream`,预签名链接访问) |

> 登录提示:部分开发机代理会把 `studio.ymmjc.com` 解析成 fake-IP(如 `28.0.0.167`),SSH 一律直连 IP `134.175.68.24`。

## 目录与文件

```
/opt/edream/
├── backend/            后端代码(rsync 从本地同步)
├── frontend/dist/      前端构建产物(nginx 根目录,rsync --delete 同步)
├── .venv/              Python 虚拟环境
├── .env                生产配置(数据库/OAuth/COS,不在 git 内)
├── media/              本地存储时代的存量产物(已全量迁移 COS,仅作备份)
├── migrate_media_to_cos.py  存量媒体迁移脚本(幂等,可重复跑做增量补偿)
├── pyproject.toml      依赖清单(rsync 同步)
└── main.py
/opt/edream-backups/    发布前自动备份(*.tar.gz,含 backend、dist、pyproject、main.py、.env)
```

服务器 `.env` 关键项(完整项含义见 README 环境变量表):

```bash
DATABASE_URL=postgresql+psycopg2://...
MEDIA_DIR=/opt/edream/media
AUTH_MODE=jwt
SESSION_COOKIE_SECURE=true
CORS_ORIGINS=https://studio.ymmjc.com
OAUTH_REDIRECT_URI=https://studio.ymmjc.com/api/auth/callback
STORAGE_BACKEND=cos
COS_REGION=ap-guangzhou
COS_BUCKET=studio-1436584532
COS_SECRET_ID=...
COS_SECRET_KEY=...
COS_PREFIX=edream
```

## 日常发布流程

在项目根目录执行(前端构建在本机做,服务器无需 node):

```bash
# 1. 本机构建前端
cd frontend && npm run build && cd ..

# 2. 备份服务器现状(时间戳区分)
TS=$(date +%Y%m%d%H%M%S)
ssh ubuntu@134.175.68.24 "cd /opt/edream && sudo tar -czf /opt/edream-backups/edream-pre-deploy-$TS.tar.gz backend frontend/dist pyproject.toml main.py .env && sudo chmod 644 /opt/edream-backups/edream-pre-deploy-$TS.tar.gz"

# 3. 同步代码(只动代码与产物,不碰 .env / media / .venv)
rsync -az --delete --exclude='__pycache__' --exclude='.pytest_cache' -e ssh backend/ ubuntu@134.175.68.24:/opt/edream/backend/
rsync -az --delete -e ssh frontend/dist/ ubuntu@134.175.68.24:/opt/edream/frontend/dist/
rsync -az -e ssh pyproject.toml main.py ubuntu@134.175.68.24:/opt/edream/

# 4. 依赖有变化时(pyproject.toml 改动过才需要)
ssh ubuntu@134.175.68.24 "cd /opt/edream && .venv/bin/pip install -e '.[cos]'"

# 5. 重启(单元文件改过才需要 daemon-reload)
ssh ubuntu@134.175.68.24 "sudo systemctl daemon-reload; sudo systemctl restart edream.service"
```

### 发布后验证清单

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://studio.ymmjc.com/          # 200
curl -s -o /dev/null -w "%{http_code}\n" https://studio.ymmjc.com/api/styles # 401(未登录预期,证明代理通)
ssh ubuntu@134.175.68.24 "systemctl is-active edream.service"               # active
ssh ubuntu@134.175.68.24 "sudo journalctl -u edream --since '2 min ago' --no-pager | tail"
```

- 启动日志出现 `Application startup complete` 即通过;`STORAGE_BACKEND=cos` 但配置不全或缺 SDK 时会**启动失败**,不会带病运行。
- 浏览器实际登录后走一遍:生成图片(写入 COS)、Vlog 合成(依赖服务器 ffmpeg)、历史记录回看(存量对象经预签名链接可访问)。

### 数据库变更

无需手工迁移:启动时 `init_db` 自动建新表;已有表的新增列由 `backend/database.py` 里的幂等 `ALTER TABLE` 补齐(如 `vlog_projects` 的 `transition_style` / `timeline_data` / `merge_*` 列)。删列、改列类型等破坏性变更需手工处理。

## 回滚

```bash
# 用发布前备份整体还原代码与配置
ssh ubuntu@134.175.68.24 "cd /opt/edream && sudo tar -xzf /opt/edream-backups/edream-pre-deploy-<TS>.tar.gz && sudo systemctl restart edream.service"
```

数据库回滚需另行处理:启动迁移只加列不删列,老代码遇到多出来的列不受影响,一般可直接回滚代码。

## 存量资产迁移(本地磁盘 → COS)

`STORAGE_BACKEND` 从 `local` 切到 `cos` 前,必须先把 `media/` 存量传进桶,否则历史记录读不到文件:

```bash
ssh ubuntu@134.175.68.24 "/opt/edream/.venv/bin/python /opt/edream/migrate_media_to_cos.py"
```

脚本幂等(桶里已存在的自动跳过),切换后可再跑一遍补偿切换窗口期落盘的增量文件。

## 运维注意事项

- **单进程设计**:合成任务队列的并发计数与取消都在进程内,`edream.service` 必须保持单 uvicorn worker;多实例部署需先引入跨进程任务队列(README「后续规划位」)。
- **ffmpeg 依赖**:服务端合成需要 `ffmpeg` 与 `ffprobe`(`apt install ffmpeg`),缺失时启动打警告、提交合成返回 503。
- **密钥管理**:COS SecretId/Key、OAuth client secret 只存服务器/本地 `.env`,不进 git、不写文档。当前使用主账号级密钥,建议换成仅授权 `studio-1436584532` 的子账号密钥(换时服务器与本地 `.env` 同步更新)。
- **证书续期**:Let's Encrypt 由 certbot 自动续期,手动续:`sudo certbot renew`。
- **磁盘**:确认 COS 运行稳定后,`/opt/edream/media/`(约 1.1G)可删除腾空间;删除前确认无回滚到本地存储的计划。
