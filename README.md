# eDream AI 视频创作台

一句话创意 → 选风格 → AI 文本拓展 → 选择画面路径(生成图片 / 上传参考图 / 不用图片)→ 图生视频 / 文生视频 → 最终视频;多段成片在 **Vlog 工作台**编排、由服务端 FFmpeg 合成。

单页应用,**仅支持 OAuth 登录**(Casdoor 授权码模式,参照 ai-relay-broker 的身份体系),所有数据按用户隔离。模型配置(new-api 网关地址、密钥、模型名)在前端填写、存到后端 PostgreSQL,**密钥只保存在服务端、任何接口都只回传掩码**;后端按配置调用模型,提交生成后前端只拿到任务记录(任务 id + 状态),密钥全程不出后端。同一用户同时只允许一个生成中的视频任务。

## 技术栈

- 后端:Python 3.10+ / FastAPI / SQLAlchemy 2 / PostgreSQL / httpx / PyJWT
- 前端:React 18 + TypeScript + Vite
- 模型调用:OpenAI 兼容协议(new-api 网关),以后切自建模型服务时保持同协议即可直接复用

## 目录结构

```
backend/                 后端
├── app.py               FastAPI 入口(所有路由统一挂在 /api 前缀下,含 /api/media 静态产物)
├── settings.py          配置(.env:数据库、media 目录、OAuth、轮询参数)
├── database.py          SQLAlchemy engine / Session / get_db
├── models.py            users / oauth_states / oauth_sessions / model_configs / creations
├── auth.py              JWT 验证(JWKS)+ 会话签发/校验
├── schemas.py           Pydantic 请求/响应模型(密钥只回传掩码)
├── media.py             本地媒体落盘/魔数嗅探/路径白名单
├── storage.py           资产存储层(本地磁盘 / 腾讯云 COS,按用户组织 key)
├── routers/
│   ├── auth.py          登录/回调/登出/当前用户(/api/auth/*)
│   ├── configs.py       模型配置 CRUD(/api/configs,按用户隔离)
│   └── creations.py     拓展/生图/上传/创建任务/历史(/api/*,按用户隔离 + 并发限制)
└── services/
    ├── ai_client.py     OpenAI 兼容调用层(文本/图片/视频)
    ├── pipeline.py      视频生成后台线程管线 + 重启恢复
    ├── video_merge.py   服务端 FFmpeg 渲染(转场滤镜图/动效段/进度解析)
    ├── merge_queue.py   合成任务队列:调度轮询认领 + 全局并发上限 + 恢复/看门狗
    ├── vlog_pipeline.py Vlog 多片段生成管线(分组/串行生成/恢复)
    └── vlog_transitions.py 转场模板清单(前后端共用)
frontend/                前端(Vite + React + TS)
media/                   产物目录(images / uploads / videos),经 /api/media 静态服务
tests/                   pytest(认证/隔离/并发限制/安全加固/重启恢复)
main.py                  uvicorn 启动入口
```

## 数据表

| 表 | 说明 |
| --- | --- |
| `users` | OAuth 用户落地,`oauth_sub` 唯一(兼容 Casdoor 的 `sub` / `id` / `owner/name` 三种主体标识) |
| `oauth_states` | 授权码流程的一次性 state(10 分钟有效,防 CSRF/重放) |
| `oauth_sessions` | 服务端会话,Cookie 存原始 token、库里只存 sha256 哈希,默认 7 天 |
| `model_configs` | 网关配置,`user_id` 外键级联删除;`api_key` 仅服务端使用 |
| `style_presets` | 风格预设(全局内容表):结构化画面语言描述 + 负向提示词 + 建议画幅,启动时幂等播种默认风格,可改库自定义 |
| `creations` | 生成任务记录;`user_id` 外键 + **部分唯一索引**:`(user_id) WHERE status IN ('pending','generating_video')`,数据库层保证每用户同时只有一个生成中任务 |

## 快速开始

### 1. 数据库

本地 PostgreSQL 创建库(已有可跳过):

```bash
createdb edream_ai_photo
```

连接串通过 `.env` 配置(参考 `.env.example`),首次启动自动建表。

### 2. 后端

服务器需安装 FFmpeg(Vlog 服务端合成依赖,缺失时合成提交返回 503):

```bash
brew install ffmpeg        # macOS
# apt install ffmpeg       # Debian/Ubuntu
```

```bash
uv venv && uv pip install -e .   # 或 pip install -e .
.venv/bin/python main.py          # 127.0.0.1:8000
```

### 3. 前端

```bash
cd frontend
npm install
npm run dev                       # localhost:5173(已代理 /api、/media 到 8000)
```

### 4. 登录

- **正式**:在 `.env` 配置 `AUTH_MODE=jwt` 与 Casdoor 应用参数(见环境变量表),在 Casdoor 后台把回调地址 `http://<host>/api/auth/callback` 加入应用白名单。首次访问自动跳转 Casdoor 登录,登录成功后首次自动建用户。
- **本地开发**:`AUTH_MODE=dev` 时访问 `/api/auth/login` 直接给固定用户(`DEV_AUTH_SUB`,默认 `dev-user`)发会话,无需 Casdoor;API 调试也可直接带 `X-Casdoor-Sub: <sub>` 请求头。

### 5. 配置模型

打开页面 → 「模型配置」→ 新增:

| 字段 | 说明 |
| --- | --- |
| API 地址 | new-api 地址,填到域名或 `/v1` 均可 |
| API 密钥 | new-api 的 sk- 令牌;**保存后不再回显**,编辑时留空表示保留原值 |
| 文本模型 | AI 拓展用,如 `gpt-4o-mini`、`glm-4-flash` |
| 图片模型 | 首帧生成用,如 `dall-e-3`、`cogview-3` |
| 视频模型 | 成片用,如 `kling-v1-master`、`cogvideox-3` |
| 视频接口类型 | `new-api 任务式`(/v1/video/generations,可灵/Vidu/PixVerse 等)或 `Sora 风格`(/v1/videos) |

可建多套配置,顶栏下拉切换;生成记录会快照当时的配置与模型名。

## 资产存储(腾讯云 COS)

生成的图片、上传的参考图、成片视频统一作为**资产**管理:

- 对象按用户组织:key 为 `{COS_PREFIX}/users/{用户id}/{images|uploads|videos}/{文件}`,数据库只存 key,不存任何外部地址
- 桶设为**私有读写**;用户每次访问列表/详情时,后端实时生成**临时预签名链接**(默认 1 小时过期,`COS_PRESIGN_EXPIRES_SECONDS` 可调),链接不落库
- 换后端零迁移:数据库里只有 key,`STORAGE_BACKEND` 在 local/cos 间切换即生效(历史 `{kind}/{文件}` 旧布局同样兼容)
- 未配置 COS 时默认落本地磁盘(`MEDIA_DIR`,经 `/api/media` 静态服务),开发与测试零依赖
- 启动时校验:`STORAGE_BACKEND=cos` 但配置不全或未安装 SDK(`pip install '.[cos]'`)直接启动失败

**COS 控制台不再需要配置 CORS**:早期浏览器合成(FFmpeg.wasm)需要直接拉取预签名链接,合成迁移到服务端后该配置可以移除。

## 风格预设

风格不是简单的一个词,而是 `style_presets` 表里的结构化预设:

- **画面语言要点**(description):机位/镜头、光影、色调、质感、氛围的完整描述,AI 拓展时拼进 prompt,要求 LLM 融入画面描述——风格的正向影响在拓展文本里固化,后续生图/生视频都继承
- **负向提示词**(negative_prompt):视频生成时随请求传给网关(可灵/Vidu 等支持 `negative_prompt` 的渠道生效;不识别的网关返回参数错误时由降级重试自动剔除)
- **建议画幅**(image_size):前端选中风格时联动首帧画幅(如国风水墨默认方形)
- 默认 10 个风格(电影质感/动漫/3D 卡通/赛博朋克/国风水墨等)启动时幂等播种,直接改库即可自定义、调序、下线(`is_active`)

## 视频生成两种接口模式

- **new-api 任务式**(默认):`POST /v1/video/generations` 提交(图生视频传 `image_url`,本地图片自动转 base64 data URL),轮询 `GET /v1/video/generations/{task_id}`,完成后下载视频落盘。
- **Sora 风格**:`POST /v1/videos`(参考图走 `input_reference` multipart),轮询 `GET /v1/videos/{id}`,完成后从 `/v1/videos/{id}/content` 下载。

状态字段做了宽容解析(多种网关的任务状态/视频 URL 字段命名都能识别);远端视频会尽量拉回本地,失败则直接保留远端链接。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/auth/login?next=` | 跳转 Casdoor 登录(dev 模式直接发会话) |
| GET | `/api/auth/callback` | OAuth 回调:换 token → 验签 → 建用户/会话 |
| GET | `/api/auth/me` | 当前用户信息 |
| POST | `/api/auth/logout` | 注销会话 |
| GET/POST | `/api/configs` | 配置列表 / 新增(响应只有 `api_key_masked`) |
| PUT/DELETE | `/api/configs/{id}` | 编辑(密钥留空=保留)/ 删除 |
| POST | `/api/configs/test` | 测试连接:传 `config_id` 用存储密钥测,或表单里填地址+密钥 |
| GET | `/api/styles` | 风格预设列表(创作台风格选择数据源) |
| POST | `/api/expand` | AI 文本拓展(命中风格预设时拼入画面语言要点) |
| POST | `/api/generate-image` | 文生图(首帧) |
| POST | `/api/upload` | 上传参考图 |
| POST | `/api/creations` | 提交视频生成(后台线程执行);**同用户已有生成中任务时返回 409** |
| POST | `/api/creations/merged` | 接收合成成片入库(兼容保留,前端已不再调用) |
| POST | `/api/vlogs/{id}/merge` | 提交服务端合成(入队立即返回,进度随项目轮询) |
| GET | `/api/creations[?limit=]` | 当前用户历史列表 |
| GET | `/api/creations/{id}` | 单条(前端 3s 轮询进度) |
| DELETE | `/api/creations/{id}` | 删除记录及文件 |

后端所有入口(业务接口、`/api/health`、`/api/media` 静态产物)统一在 **`/api` 前缀**下,反向代理只需按该前缀一条规则转发即可;除 `/api/health` 与 `/api/media` 外全部需要登录,所有数据查询都限定当前用户,越权访问返回 404。

## 可靠性与安全

- **重启恢复**:提交到网关拿到任务号后立即落库;服务重启时凭任务号恢复轮询(不重复提交、不浪费已扣费的任务),尚未提交成功的任务才标记失败。服务端合成则相反——本地计算无计费,重启时运行中的任务直接重新排队重跑
- **合成队列**:用户提交只入队,调度线程按全局并发上限认领执行;进度与心跳定期落库,看门狗回收超时任务并终止进程,放弃项目会同时取消排队/运行中的合成
- **看门狗**:生成中任务每 30 秒心跳一次,超过「任务超时 + 10 分钟」无心跳的任务自动标记失败,用户不会被单任务并发限制永久锁死
- **轮询容错**:查询任务状态遇瞬时网络抖动/网关 5xx 不致命,连续 6 次失败才判死;提交/生图只在参数类 4xx 时降级重试,避免网关重复受理、重复计费
- **路径白名单**:创建任务只接受本服务生成/上传接口返回的 `images|uploads/<文件名>` 相对路径,拒绝 `..`/绝对路径等穿越写法
- **魔数校验**:上传图片与合成成片按文件头识别真实格式,Content-Type 可伪造不作数
- **流式落盘 + 原子写**:大文件(合成成片上限 500MB)分块写盘不整读进内存;先写 `.tmp` 再 rename,崩溃不会留下半截文件

## 体验细节

- **刷新不丢任务**:页面刷新后会自动恢复仍在生成中的任务进度与表单内容
- **测试连接**:配置表单一键验证网关连通性,并按 `/v1/models` 逐项校验模型名;测试成功后模型输入框支持从网关模型列表下拉选择
- **完成通知**:提交时申请通知权限,生成完成/失败时发系统通知;页面在后台时标签页标题闪烁提醒
- **再创作**:历史记录中一键把普通视频参数回填到创作表单；Vlog 成片显示“编辑 Vlog”，会基于原分镜创建可修改副本，原项目保持不变
- **下载命名**:下载的视频以创意文本命名,而不是随机文件名

## Vlog 服务端合成

多图 Vlog 的最终合成在**服务端 FFmpeg** 完成:片段生成完成后项目停在「片段已就绪」,用户点击「生成最终 Vlog」即向后端提交合成任务(立即入队返回),前端轮询项目状态展示排队/合成进度;合成期间可以关闭页面,完成后在项目与历史记录中查看成片。

- **任务队列**:提交只落库(`merge_status=queued`),后端调度线程轮询认领、按 `MAX_MERGE_WORKERS` 全局并发上限执行,合成是 CPU 大户故限全局而非每用户。并发计数与取消都在进程内,**按单进程部署设计**:多 uvicorn worker 会各自计数且无法跨进程取消,扩容需引入跨进程任务队列
- **渲染管线**:素材(本地存储 key / COS 拉取)统一画幅与帧率 → 逐段 0.6 秒 xfade 转场 + acrossfade 音频交叉淡化 → x264 编码;图片动效段(缓慢推进/拉远、平移、漂移)由 zoompan 在合成前先行渲染
- **素材服务端化**:AI 片段完成后立即把上游视频下载入库(瞬时失败自动重试 3 次),配置 COS 即进对象存储;历史遗留的「只有上游临时链接」片段,合成拉取成功时会自动回填入库,链接过期不再影响后续合成
- **体积控制**:单个项目时间线最多 20 个片段组(前后端同时校验);成片超过 `MAX_VIDEO_UPLOAD_MB` 时自动压缩重试(先降码率 CRF 30,仍超限再降一半分辨率 CRF 32),两轮后仍超限才失败
- **可靠性**:渲染进度与心跳定期落库(压缩/上传等无进度回调阶段由节拍线程保活);看门狗回收超时无心跳的任务;服务重启或优雅停机时运行中的合成自动重新排队(本地计算无计费,重跑安全),停机会先终止 ffmpeg 再入队,不留孤儿进程;用户放弃项目会终止进行中的合成进程
- **失败重试**:合成失败项目保持「片段已就绪」,显示失败原因,可再次提交
- **保留策略**:页面提示「视频最多保留一个月,请及时下载备份」;当前后端不做自动删除,后续如需清理可基于 `created_at` 定期回收
- 依赖:服务器需安装 `ffmpeg` 与 `ffprobe`(如 `brew install ffmpeg` / `apt install ffmpeg`);缺失时启动打警告、提交合成返回 503

合成前可以选择统一应用到每个场景边界的标准转场模板：柔和淡化、溶解、向左/向右擦除、向左/向右滑动、圆形展开和推进切换。已有项目和未传该字段的请求默认使用柔和淡化。

Vlog 工作台按用户手动添加的片段组工作：每组可以选择 1–9 张图片走 AI 图生视频（单图和多图都支持）、1 张图片走本地动态效果，或直接复用历史生成视频/本地视频。所有组最后统一选择转场并提交服务端合成。AI 组的一句话补充会传给每个 AI 片段；历史记录中的单图 AI 视频与多图 AI 视频都可以作为视频素材复用。

「本地图片动效」模式：选择 1 张本地图片，套用缓慢推进、缓慢拉远、向左/向右平移或轻微漂移模板并设置片段时长，合成时由服务端渲染成 MP4,不创建 AI 任务；最终混合成片保存到服务端历史。

已完成的 Vlog 可从历史记录点击「编辑 Vlog」，或在项目详情点击「编辑副本」。图片分组、风格、画幅、描述和转场会回填到新草稿；素材的服务端引用会一并恢复,无需重新上传。

## 环境变量(.env)

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://tailaogu@localhost:5432/edream_ai_photo` | PG 连接串 |
| `MEDIA_DIR` | `./media` | 图片/视频落盘目录 |
| `VIDEO_POLL_INTERVAL` | `5` | 视频任务轮询间隔(秒) |
| `VIDEO_TIMEOUT_SECONDS` | `900` | 视频任务超时(秒) |
| `MAX_MERGE_WORKERS` | `1` | 服务端合成全局并发上限(进程内计数,按单进程部署设计) |
| `MERGE_POLL_INTERVAL` | `2` | 合成调度线程轮询排队任务的间隔(秒) |
| `MERGE_TIMEOUT_SECONDS` | `1800` | 单个合成任务硬超时(秒),超时无心跳由看门狗回收 |
| `MAX_UPLOAD_MB` | `20` | 参考图大小上限 |
| `STORAGE_BACKEND` | `local` | `local` = 本地磁盘;`cos` = 腾讯云 COS(需 `pip install '.[cos]'`) |
| `COS_REGION` / `COS_BUCKET` | 空 | COS 地域(如 `ap-guangzhou`)/ 存储桶完整名称(含 APPID) |
| `COS_SECRET_ID` / `COS_SECRET_KEY` | 空 | COS 密钥(建议使用仅授权该桶的子账号) |
| `COS_PREFIX` | `edream` | 对象 key 统一前缀,留空表示不加 |
| `COS_PRESIGN_EXPIRES_SECONDS` | `3600` | 临时预签名链接有效期(秒) |
| `AUTH_MODE` | `jwt` | `jwt` = Casdoor OAuth;`dev` = 本地免登 |
| `OAUTH_ISSUER` / `OAUTH_AUDIENCE` | 空 | JWT 签发方/受众校验(可选) |
| `OAUTH_JWKS_URL` | 空 | Casdoor JWKS 地址,如 `https://<casdoor>/.well-known/jwks` |
| `OAUTH_AUTHORIZE_URL` | 空 | `https://<casdoor>/login/oauth/authorize` |
| `OAUTH_TOKEN_URL` | 空 | `https://<casdoor>/api/token` |
| `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` | 空 | Casdoor 应用凭据 |
| `OAUTH_REDIRECT_URI` | 自动按请求拼 | Casdoor 后台登记的回调地址 |
| `SESSION_TTL_HOURS` | `168` | 会话有效期(7 天) |
| `SESSION_COOKIE_SECURE` | `false` | HTTPS 部署时设 true |
| `CORS_ORIGINS` | localhost 三件套 | 跨域来源,逗号分隔;生产改为实际前端域名 |
| `DEV_AUTH_SUB` | `dev-user` | dev 模式固定用户标识 |

## 后续规划位

- `/api/media` 静态目录目前靠 UUID 文件名不可猜测做软隔离,需要强隔离时可改为按用户分目录 + 鉴权路由
- 任务目前跑在单进程的后台线程内(重启可恢复,但)多实例部署需引入真正的任务队列与 worker 认领机制
- 切换自建模型服务:保持 OpenAI 兼容协议改 `base_url` 即可;如接口形态不同,扩展 `backend/services/ai_client.py` 的 provider
