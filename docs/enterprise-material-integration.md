# 主业务接入企业素材指南

本文面向主创作业务的后端、生成链路和前端开发者，描述如何消费企业在 Ops 上传的文字、图片、视频和文档素材。接口契约以当前 `master` 为准。

企业共创通过临时 Grant 把 C 端用户与企业关联，不会创建企业成员。主业务调用共创接口时必须携带当前用户的 `grant_id`，服务端据此确定企业并隔离模板与素材。完整流程见 [企业 C 端共创授权、登录与隐私确认设计](enterprise-consumer-authorization-design.md)。

## 1. 本次改动概览

运营平台已作为主业务的一个独立业务目录加入同一代码仓，复用现有 Casdoor、PostgreSQL、对象存储抽象、腾讯云 COS 桶、前端构建和后端服务。

| 范围 | 改动 |
| --- | --- |
| 企业认证 | Casdoor 首次登录后提交企业资料，平台管理员审核通过后启用企业能力 |
| 企业账号 | 一期每个企业只绑定一个 Casdoor Owner，不提供企业成员邀请和角色管理 |
| 企业入口 | Ops 生成主业务专属 URL/二维码；登录后按 token 定位企业并再次校验当前 Owner |
| 素材管理 | 三类业务用途，支持文字和文件的新增、查询、修改、版本替换、删除、预览和下载 |
| 批量上传 | 图片、视频、文档支持同类型多选；校验单文件、批次总大小、文件数和企业剩余额度 |
| 对象存储 | 复用主业务 COS 配置，企业对象使用独立 `enterprises/{enterprise_id}/...` 前缀 |
| 额度 | 默认 2 GB，上传预占、成功计费、失败回滚、删除释放，并记录额度流水 |
| 内部接口 | 提供服务 Token 鉴权的只读查询、批量解析、素材画像和固定版本内容读取 |
| 审计 | 企业、企业入口、素材、额度及内部读取均写入审计日志 |
| 退出登录 | 本站会话与 Casdoor 当前设备会话后台注销，页面保留在主业务或 `/ops` |

主要代码位置：

```text
frontend/src/ops/          企业运营界面
backend/ops/               企业认证、Owner、企业入口、素材、额度和领域服务
backend/internal/          主业务素材接口
backend/storage.py         主业务与 Ops 共用的 local/COS 存储抽象
backend/models.py          企业、Owner 关系、入口、素材版本、额度流水和审计模型
tests/test_ops_enterprise.py  企业隔离和内部接口验收测试
```

## 2. 业务分类与字段

### 2.1 用途 `purpose`

| 值 | 中文 | 主业务推荐用途 |
| --- | --- | --- |
| `ip_setting` | IP 设定 | 人格、身份、外观、性格、口头禅、行为规则等提示词上下文 |
| `ip_visual` | IP 形象 | 角色参考图、立绘、形象视频和 PSD 源文件 |
| `brand_product` | 品牌与产品 | 品牌规范、Logo、包装、产品图片、产品视频和产品说明 |

### 2.2 内容类型 `content_type`

| 值 | 内容 | 使用建议 |
| --- | --- | --- |
| `text` | 在线录入文字 | 直接拼入提示词或结构化上下文 |
| `image` | JPG、PNG、WebP | 下载后作为图片生成或视频生成参考图 |
| `video` | MP4、MOV、WebM | 下载后作为参考视频、片段或后续解析输入 |
| `document` | PDF、DOCX | 当前只存储原文件，主业务按需要增加文档解析 |
| `source` | PSD | 设计源文件，不建议未经转换直接传给生成模型 |

格式组合：

| 用途 | 文字 | 图片 | 视频 | 文档 | 源文件 |
| --- | --- | --- | --- | --- | --- |
| `ip_setting` | 支持 | 不支持 | 不支持 | PDF、DOCX | 不支持 |
| `ip_visual` | 不支持 | JPG、PNG、WebP | MP4、MOV、WebM | 不支持 | PSD |
| `brand_product` | 支持 | JPG、PNG、WebP | MP4、MOV、WebM | PDF、DOCX | 不支持 |

## 3. 租户边界

### 3.1 `enterprise_id` 的来源

主业务不能直接信任浏览器传入的 `enterprise_id`。必须在服务端从以下任一可信关系得到：

1. 当前 Casdoor 用户对应的有效 Owner `enterprise_memberships`。
2. 已在主业务项目或订单上固化的 `enterprise_id`。
3. 可信内部任务消息中由主业务后端写入的 `enterprise_id`。

若当前用户没有已审核且启用的企业关系，主业务应继续走个人素材流程，不应尝试读取企业素材。

### 3.2 企业专属入口

Ops 的唯一 Owner 可生成 `https://studio.ymmjc.com/?enterprise_entry=<token>` 和对应二维码。主业务应保留该查询参数完成 Casdoor 登录回跳，然后调用：

```http
POST /api/enterprise-entry/resolve
Content-Type: application/json

{"token": "<enterprise_entry>"}
```

后端只在“token 有效、企业已审核、当前登录用户是该企业有效 Owner”三项同时满足时返回：

```json
{"enterprise_id": 12, "enterprise_name": "沃乐食品"}
```

解析成功后前端立即从地址栏删除 token。普通主业务访问可调用 `GET /api/enterprise-entry/context` 按当前 Owner 关系获取相同上下文；个人账号返回 `null`。入口 token 不是授权凭据，不能代替 Casdoor 会话，也不能直接传给内部素材接口。

### 3.3 存储与接口隔离

- 个人对象：`users/{user_id}/{kind}/{filename}`。
- 企业对象：`enterprises/{enterprise_id}/materials/{asset_id}/v{version}/{filename}`。
- 企业对象不会通过公开 `/api/media` 返回。
- 搜索、批量解析和素材画像都以 `enterprise_id` 为必选边界。
- 固定版本内容接口同时校验 `enterprise_id + asset_id + version_no`，不匹配统一返回 `404`。
- 主业务不得把 `X-Internal-Token` 或内部内容地址下发给浏览器直接调用。

## 4. 内部接口约定

### 4.1 鉴权与地址

所有接口位于主业务后端可访问的同一服务：

```text
生产基址：https://studio.ymmjc.com/api/internal/v1
请求头：X-Internal-Token: <service-token>
```

生产通过 `INTERNAL_SERVICE_TOKENS` 配置一个或多个随机 Token。Token 只保存在服务端密钥配置中，不能进入前端包、日志、数据库业务字段或任务消息。

未配置 Token 返回 `503`；缺少或错误 Token 返回 `401`。

### 4.2 查询素材

```http
POST /api/internal/v1/materials/search
Content-Type: application/json
X-Internal-Token: <service-token>

{
  "enterprise_id": 12,
  "purposes": ["ip_setting", "ip_visual", "brand_product"],
  "content_types": ["text", "image", "video"],
  "tags": ["沃乐"],
  "asset_ids": [],
  "limit": 100
}
```

筛选语义：

- `enterprise_id` 必填。
- `purposes/content_types/asset_ids` 为空表示不限制对应字段。
- `tags` 为“全部包含”，示例要求素材同时包含请求中的每个标签。
- `limit` 范围为 1 到 500，默认 100。
- 只返回审核通过企业的 `active` 素材，默认按素材 ID 倒序。

响应示例：

```json
[
  {
    "asset_id": 81,
    "version_id": 126,
    "version_no": 3,
    "enterprise_id": 12,
    "purpose": "ip_setting",
    "content_type": "text",
    "name": "沃乐 IP 人格",
    "tags": ["沃乐", "核心设定"],
    "description": "生成内容时必须遵循",
    "text_content": "身份：食品营养科学家……",
    "content_data": {},
    "mime_type": "text/plain",
    "size_bytes": 118,
    "checksum_sha256": "4e9f...",
    "content_url": null
  },
  {
    "asset_id": 82,
    "version_id": 127,
    "version_no": 1,
    "enterprise_id": 12,
    "purpose": "ip_visual",
    "content_type": "image",
    "name": "沃乐角色正面图",
    "tags": ["沃乐", "正面"],
    "description": "标准角色形象",
    "text_content": null,
    "content_data": {},
    "mime_type": "image/png",
    "size_bytes": 2381140,
    "checksum_sha256": "70ad...",
    "content_url": "/api/internal/v1/enterprises/12/materials/82/versions/1/content"
  }
]
```

文字内容直接使用 `text_content`；文件内容通过 `content_url` 获取。不要持久化最终 COS 签名地址，因为它会过期。

### 4.3 批量解析指定素材

```http
POST /api/internal/v1/materials/batch-get
Content-Type: application/json
X-Internal-Token: <service-token>

{
  "enterprise_id": 12,
  "asset_ids": [81, 82, 90],
  "purposes": [],
  "content_types": [],
  "tags": [],
  "limit": 100
}
```

`asset_ids` 必须非空。接口仍会按 `enterprise_id` 和素材状态过滤，主业务必须核对返回数量与 ID 集合；缺失项视为已删除、已停用、跨企业或不存在，不能静默改用其他企业素材。

### 4.4 获取企业完整素材画像

```http
GET /api/internal/v1/enterprises/12/material-profile
X-Internal-Token: <service-token>
```

返回：

```json
{
  "enterprise_id": 12,
  "materials": {
    "ip_setting": [],
    "ip_visual": [],
    "brand_product": []
  }
}
```

适合在创建生成任务前一次性建立企业上下文。不建议每个模型步骤都重复调用。

### 4.5 获取固定文件版本

```http
GET /api/internal/v1/enterprises/12/materials/82/versions/1/content
X-Internal-Token: <service-token>
```

- COS 模式返回短时 `307` 跳转，调用端应允许跟随重定向。
- local 模式直接流式返回文件。
- 文字素材没有文件内容，`content_url` 为 `null`。
- 下载后建议重新计算 SHA-256，并与查询结果的 `checksum_sha256` 比对。

## 5. 主业务推荐调用链

### 5.1 创建生成任务

1. 用已校验的当前 Owner 上下文或业务项目在服务端确定 `enterprise_id`，不要信任前端自由提交的企业 ID。
2. 按用途、内容类型、标签或用户明确选择的素材 ID 调用 `search`。
3. 校验每条结果的 `enterprise_id` 与当前任务一致。
4. 把将要使用的素材版本快照写入生成任务。
5. 文字直接进入提示词组装；文件通过固定版本内容接口下载到任务工作目录。
6. 校验文件 SHA-256 后再提交给图片、视频或文档处理链路。
7. 生成完成后保留素材引用快照，供审计、复现和问题排查。

### 5.2 任务落库字段

建议新建通用引用表，而不是只在任务表保存一组可变素材 ID：

```text
generation_material_refs
  id
  business_type       creation / vlog / other
  business_id         主业务任务 ID
  enterprise_id
  asset_id
  version_id
  version_no
  purpose
  content_type
  checksum_sha256
  name_snapshot
  tags_snapshot
  created_at
```

最低要求是保存：

```text
enterprise_id
asset_id
version_id
version_no
checksum_sha256
```

不要只保存 `asset_id`，否则企业替换素材后，历史任务无法确认当时使用的是哪个版本。

### 5.3 Python 调用示例

```python
import hashlib

import httpx


def load_enterprise_materials(base_url: str, token: str, enterprise_id: int):
    headers = {"X-Internal-Token": token}
    with httpx.Client(base_url=base_url, headers=headers, timeout=60, follow_redirects=True) as client:
        response = client.post(
            "/api/internal/v1/materials/search",
            json={
                "enterprise_id": enterprise_id,
                "purposes": ["ip_setting", "ip_visual", "brand_product"],
                "content_types": ["text", "image", "video"],
                "tags": [],
                "asset_ids": [],
                "limit": 100,
            },
        )
        response.raise_for_status()
        materials = response.json()

        for material in materials:
            if material["enterprise_id"] != enterprise_id:
                raise RuntimeError("企业素材边界校验失败")
            if not material["content_url"]:
                continue
            content = client.get(material["content_url"])
            content.raise_for_status()
            digest = hashlib.sha256(content.content).hexdigest()
            if digest != material["checksum_sha256"]:
                raise RuntimeError(f"素材校验失败：{material['asset_id']}")

        return materials
```

## 6. 提示词和媒体使用建议

### `ip_setting`

- 多条文字按“名称 + 内容”拼装，不要只取最新一条覆盖其他设定。
- 建议给系统提示词单独划出企业设定区，并限制总字符数。
- PDF/DOCX 当前未自动抽取文本，主业务需要显式增加解析器后才能进入提示词。

### `ip_visual`

- `image` 可作为角色一致性参考图。
- `video` 可作为动作、镜头或角色动态参考。
- `source` 为 PSD，应先转换成模型支持的 PNG/WebP，不要直接交给生成接口。

### `brand_product`

- `text` 可用于品牌名、卖点、禁用词和产品信息。
- `image` 可作为 Logo、包装、产品外观和场景参考。
- `video` 可作为产品片段或动作参考，是否直接进入成片由主业务决定。
- 文档需要解析后再提炼为提示词，不应把二进制内容直接发送给模型。

## 7. 错误处理

| 状态码 | 含义 | 主业务处理 |
| --- | --- | --- |
| `401` | 内部 Token 缺失或错误 | 终止调用并告警，不重试业务请求 |
| `404` | 企业、素材或版本不可用，或企业边界不匹配 | 标记素材不可用，要求重新选择或刷新任务上下文 |
| `422` | 用途、参数或批量请求不合法 | 修正请求，不做自动重试 |
| `503` | 内部素材服务未配置 Token | 阻止企业素材功能上线并检查生产配置 |
| `5xx` | 服务或对象存储异常 | 有退避地重试，不能回退到其他企业素材 |

主业务的降级原则是“本企业素材不可用时不用企业素材”，绝不能搜索或复用其他企业的素材。

## 8. 配置与联调

服务端配置：

```dotenv
INTERNAL_SERVICE_TOKENS=<独立随机令牌，多个用逗号分隔>
ENTERPRISE_BUSINESS_BASE_URL=https://studio.ymmjc.com
ENTERPRISE_DEFAULT_QUOTA_MB=2048
ENTERPRISE_BATCH_MAX_FILES=20
ENTERPRISE_BATCH_MAX_MB=1024
```

建议按以下顺序联调：

1. 在 Ops 创建企业并由平台管理员审核通过。
2. 生成企业入口，确认 Owner 登录后进入主业务；企业 B 使用企业 A 的入口返回 `403`。
3. 分别创建 IP 设定文字、IP 形象图片和品牌产品图片。
4. 使用正确内部 Token 查询完整素材画像。
5. 使用企业 A 查询企业 A 素材并下载固定版本。
6. 使用企业 A 的路径请求企业 B 素材，确认返回 `404`。
7. 替换一个素材版本，确认旧任务仍能通过旧 `version_no` 读取原版本。
8. 停用企业，确认入口解析、查询和内容读取均不可用。
9. 删除素材，确认新任务无法获取，已落库任务保留引用快照用于审计。

## 9. 当前边界

一期明确不包含以下能力：

- PDF/DOCX 文本抽取、分段和向量化。
- PSD 自动转图片。
- 视频转码、抽帧或内容理解。
- 素材自动评分、自动选择或跨素材去重。
- 浏览器直接访问内部接口。
- 一个企业绑定多个 Casdoor 用户及企业内角色管理。

这些能力应由主业务生成链路按实际模型能力逐步增加，但不能绕开 `enterprise_id`、固定版本和校验和三项约束。
