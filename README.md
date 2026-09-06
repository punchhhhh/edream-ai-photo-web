# eDream AI 视频创作台

一句话创意 → 选风格 → AI 文本拓展 → 选择画面路径(生成图片 / 上传参考图 / 不用图片)→ 图生视频 / 文生视频 → 最终视频;多段成片可在**视频剪辑台**里浏览器端拼接合成。

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
    └── pipeline.py      视频生成后台线程管线 + 重启恢复
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

**COS 控制台需要做的一件事**:给桶配置跨域访问(CORS),允许来源填前端域名(开发期 `http://localhost:5173`),方法 `GET`——浏览器剪辑台(FFmpeg.wasm)需要直接 fetch 预签名链接拉取素材。

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
| POST | `/api/creations/merged` | 浏览器合成成片入库(剪辑台自动调用) |
| GET | `/api/creations[?limit=]` | 当前用户历史列表 |
| GET | `/api/creations/{id}` | 单条(前端 3s 轮询进度) |
| DELETE | `/api/creations/{id}` | 删除记录及文件 |

后端所有入口(业务接口、`/api/health`、`/api/media` 静态产物)统一在 **`/api` 前缀**下,反向代理只需按该前缀一条规则转发即可;除 `/api/health` 与 `/api/media` 外全部需要登录,所有数据查询都限定当前用户,越权访问返回 404。

## 可靠性与安全

- **重启恢复**:提交到网关拿到任务号后立即落库;服务重启时凭任务号恢复轮询(不重复提交、不浪费已扣费的任务),尚未提交成功的任务才标记失败
- **看门狗**:生成中任务每 30 秒心跳一次,超过「任务超时 + 10 分钟」无心跳的任务自动标记失败,用户不会被单任务并发限制永久锁死
- **轮询容错**:查询任务状态遇瞬时网络抖动/网关 5xx 不致命,连续 6 次失败才判死;提交/生图只在参数类 4xx 时降级重试,避免网关重复受理、重复计费
- **路径白名单**:创建任务只接受本服务生成/上传接口返回的 `images|uploads/<文件名>` 相对路径,拒绝 `..`/绝对路径等穿越写法
- **魔数校验**:上传图片与合成成片按文件头识别真实格式,Content-Type 可伪造不作数
- **流式落盘 + 原子写**:大文件(合成成片上限 500MB)分块写盘不整读进内存;先写 `.tmp` 再 rename,崩溃不会留下半截文件

## 体验细节

- **刷新不丢任务**:页面刷新后会自动恢复仍在生成中的任务进度与表单内容
- **测试连接**:配置表单一键验证网关连通性,并按 `/v1/models` 逐项校验模型名;测试成功后模型输入框支持从网关模型列表下拉选择
- **完成通知**:提交时申请通知权限,生成完成/失败时发系统通知;页面在后台时标签页标题闪烁提醒
- **再创作**:历史记录中一键把参数回填到创作表单
- **下载命名**:下载的视频以创意文本命名,而不是随机文件名

## Vlog 合成与视频剪辑台

多图 Vlog 的片段生成完成后，项目会停在「片段已就绪」，由用户点击后在浏览器本地完成最终合成，不会在片段生成完成时自动加载 wasm。当前浏览器无法完成合成时可以选择「放弃项目」，项目作废（cancelled）后释放唯一活跃名额，即可新建。

合成前可以选择统一应用到每个场景边界的标准转场模板：柔和淡化、溶解、向左/向右擦除、向左/向右滑动、圆形展开和推进切换。已有项目和未传该字段的请求默认使用柔和淡化。

Vlog 还提供「本地图片动效」模式：选择 1–9 张本地图片，套用缓慢推进、缓慢拉远、向左/向右平移或轻微漂移模板，设置单张时长后生成 MP4。图片和合成过程只在当前浏览器处理，不创建 AI 任务，也不写入服务端历史。

AI 片段和本地图片动效都使用浏览器 FFmpeg.wasm:

顶栏「视频剪辑」进入:多选已完成的成片 → 时间线拖拽排序 → 一键合成,合成**全部在浏览器内完成**(FFmpeg.wasm 单线程,引擎自托管于 `frontend/public/ffmpeg/`,`npm install` 后由 postinstall 脚本自动从 node_modules 复制,不依赖 CDN)。

- 同编码参数的素材(同一模型出片)走 `-c copy` 流拷贝拼接:无损、秒级、几乎不占 CPU
- 参数不一致时自动回退 x264 重编码,统一到首段分辨率(等比缩放 + 黑边居中),重编码长片较慢
- 成片自动回传后端保存,在历史记录中以「剪辑合成」标记,可再作为素材继续合成(链式剪辑)
- 前端首次点击浏览器合成或本地动效生成时需加载约 32MB wasm 引擎,之后页面内有缓存

后端仅需 `POST /api/creations/merged` 接收成片入库;剪辑算力完全在用户浏览器,部署到服务器后不占用服务端资源。

## 环境变量(.env)

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://tailaogu@localhost:5432/edream_ai_photo` | PG 连接串 |
| `MEDIA_DIR` | `./media` | 图片/视频落盘目录 |
| `VIDEO_POLL_INTERVAL` | `5` | 视频任务轮询间隔(秒) |
| `VIDEO_TIMEOUT_SECONDS` | `900` | 视频任务超时(秒) |
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
