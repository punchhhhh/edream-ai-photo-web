# 企业素材运营平台

运营平台与主创作业务在同一代码仓、同一 Casdoor 会话和同一存储抽象下运行，页面入口为 `/ops`。企业租户、成员角色、素材权限和空间额度由本应用管理，Casdoor 只负责人员身份认证。

生产建议使用 `https://ops.studio.ymmjc.com` 作为运营域名，仍部署同一份前端构建，并由 nginx 将根路径重定向到 `/ops`、将 `/api/` 转发到同一后端。Casdoor 应同时登记主站与运营站的 `/api/auth/callback`；启用两个域名时不要把 `OAUTH_REDIRECT_URI` 固定为单一域名，让后端按当前请求域名生成回调地址。`CORS_ORIGINS` 需包含两个来源。

## 代码边界

```text
frontend/src/ops/       /ops 独立界面
backend/ops/            企业认证、RBAC、素材、额度
backend/internal/       主业务只读素材接口
backend/storage.py      local / COS 共用存储抽象
```

## 身份与状态

- 平台管理员：`ENTERPRISE_ADMIN_SUBS` 中的 Casdoor `oauth_sub`，或 `platform_user_roles` 中启用的 `platform_admin`。
- 企业角色：`owner`、`admin`、`editor`、`viewer`。一期一个用户只允许属于一个有效企业。
- 企业认证：`pending → approved/rejected`；通过后可 `suspended/archived`。
- 素材：`uploading → active → deleting → deleted`，上传失败为 `failed`。

企业通过认证时自动创建默认 2GB 额度。Owner/Admin 管理成员，Owner/Admin/Editor 管理素材，Viewer 只读。平台管理员不自动成为任何企业成员。

## 素材规则

| 用途 | 支持内容 |
| --- | --- |
| `ip_setting`（IP 设定） | 在线文字、PDF、DOCX |
| `ip_visual`（IP 形象） | JPG、PNG、WebP、PSD、MP4、MOV、WebM |
| `brand_product`（品牌与产品） | 在线文字、JPG、PNG、WebP、PDF、DOCX、MP4、MOV、WebM |

文字上限 5000 字；图片 20MB、文档 50MB、PSD 200MB、视频 500MB。图片、视频和文档支持同类型多选，默认每批最多 20 个、总大小不超过 1GB，且不能超过企业实时剩余空间。前后端都会检查，任一文件格式、单文件大小、批次总大小或额度不合规时整批拒绝，不产生部分成功。

页面先选择上述用途，再只展示该用途允许的文字、图片、视频或文档入口。旧数据中的 `ip_persona` 自动迁到 `ip_setting`，`brand_identity`、`product_material` 和 `scene_reference` 自动迁到 `brand_product`。

文件对象使用 `enterprises/{enterprise_id}/materials/{asset_id}/v{version}/...`。本地企业素材放在 `ENTERPRISE_MEDIA_DIR`，不会进入公开 `/api/media`；COS 上传时保存实际 MIME 元数据，并在权限校验后生成带响应类型的 5 分钟预签名地址，下载签名额外带附件文件名。

## 运营接口

| 方法与路径 | 能力 |
| --- | --- |
| `GET /api/ops/v1/profile` | 当前身份、企业、角色和额度 |
| `POST/PUT/DELETE /api/ops/v1/enterprise` | 申请、修改、撤销待审核企业 |
| `GET/POST/PATCH/DELETE /api/ops/v1/members` | 企业成员管理 |
| `GET /api/ops/v1/assets` | 素材筛选列表 |
| `POST /api/ops/v1/assets/text` | 创建文字素材 |
| `POST /api/ops/v1/assets/file` | 上传文件素材 |
| `POST /api/ops/v1/assets/files` | 同用途、同类型批量上传文件 |
| `GET/PATCH/DELETE /api/ops/v1/assets/{id}` | 详情、修改、删除 |
| `POST /api/ops/v1/assets/{id}/file` | 创建新的文件版本 |
| `GET /api/ops/v1/assets/{id}/content` | 鉴权内联预览；增加 `download=true` 时作为附件下载 |
| `/api/ops/v1/admin/*` | 企业审核、停用、额度和平台管理员管理 |

## 主业务内部接口

内部接口使用 `X-Internal-Token`，不接受 Casdoor Cookie 代替服务凭据。生产必须通过 `INTERNAL_SERVICE_TOKENS` 配置独立随机 Token，并限制为私网访问。

| 方法与路径 | 能力 |
| --- | --- |
| `POST /api/internal/v1/materials/search` | 按企业、用途、内容类型、标签查询 |
| `POST /api/internal/v1/materials/batch-get` | 批量解析指定素材 |
| `GET /api/internal/v1/enterprises/{id}/material-profile` | 按用途聚合企业完整素材上下文 |
| `GET /api/internal/v1/materials/{id}/versions/{version}/content` | 读取固定文件版本 |

主业务必须从服务端业务记录取得 `enterprise_id`，不能信任浏览器提交的任意企业 ID。生成任务应保存返回的 `asset_id、version_id、version_no、checksum_sha256`，避免素材更新影响历史任务。

## 额度一致性

上传先在数据库锁定并增加 `reserved_bytes`，批量上传按整批总大小一次预占；全部对象成功后再逐项转入 `used_bytes`，任一对象失败会删除本批已写对象、标记失败记录并释放整批预占。文件替换保留历史版本并继续占用额度，删除素材时删除全部版本后统一释放。所有变动写入 `enterprise_quota_ledger`，认证、成员、素材、额度和内部读取写入 `audit_logs`。

## 本地开发与验收

```powershell
uv sync --extra dev
uv run python -m pytest -q
cd frontend
npm install
npm run lint
npm run build
```

启动后端和前端后访问 `http://127.0.0.1:5173/ops`。完整验收链路：

1. 未登录访问 `/ops` 跳转 Casdoor，登录回调后建立同站会话；首次登录未认证企业时只能提交认证资料。
2. 平台管理员审核通过后，企业 Owner 获得默认额度并可进入素材管理；Viewer 只能查看和下载。
3. 分别录入文字、批量上传同类型图片/视频/文档/PSD，校验用途、魔数、单文件上限、批次数量、批次总大小和企业总额度；成功后对象 key 必须含企业 ID。
4. 图片在页面内通过鉴权接口预览，文件通过 `download=true` 下载；本地文件不允许从 `/api/media` 公开读取，COS 仅返回短时签名地址。
5. 完成素材新增、查询、文字修改、文件替换（新版本）和删除；确认额度流水与实际占用一致。
6. 使用另一个企业账号访问素材详情、预览地址或删除接口，均应返回 404；企业停用后，成员端和内部 RPC 均不可再读取。
7. 内部服务使用独立 Token 按 `enterprise_id` 和三类用途查询，并固定记录素材版本与校验和后再进入视频生成。
