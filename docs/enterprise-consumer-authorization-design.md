# 企业 C 端共创授权说明

## 目标与边界

企业在 Ops 配置模板、素材和专属入口。C 端用户通过 URL 或二维码进入，使用 Casdoor 登录或注册，同意服务与隐私说明后获得临时授权，并使用该企业的模型配置和素材生成视频。

本次只调整企业与 C 端用户的关系层：

- `EnterpriseMembership` 只表示企业 Owner，不保存 C 端用户。
- C 端用户与企业之间使用 `EnterpriseConsumerGrant` 临时关联。
- 模板、素材绑定、首帧合成、企业模型解析和视频生成 `pipeline.py` 保持原逻辑。
- 企业素材仍由服务端按 `enterprise_id` 读取，不向其他企业开放。

## 用户流程

1. 用户打开 `/?enterprise_entry=<token>`，页面公开展示企业名称、授权时长和视频次数，不产生账号关系。
2. 未登录用户点击“登录或注册”，由 Casdoor 完成认证；OAuth `next` 保留完整企业入口地址。
3. 回到入口后，用户用一个复选框确认服务与隐私说明，明确知悉企业可查看本次文字、任务状态和生成视频。
4. 一期统一自动审批，确认后立即创建有效 Grant 并进入企业共创工作台。
5. 授权有效时再次打开同一入口会直接恢复，不重复创建或重置额度。
6. 授权过期、次数耗尽或被撤销后，用户需要重新确认并申请新的授权期。
7. 退出登录只清除登录会话，页面仍停留在当前企业入口。

## 授权规则

- 默认入口有效期：30 天。
- 默认单次授权有效期：24 小时。
- 默认单次授权视频提交次数：3 次，企业可在 Ops 调整新授权的默认值。
- 视频请求成功创建即计数；生成失败或删除视频均不返还次数。
- 首帧继续使用现有每日防滥用限制，不新增“每个授权 6 次”的规则。
- 授权过期或撤销只阻止新的拓展、首帧和视频提交，不中断已经提交的后台任务。

Grant 状态：`pending / active / expired / exhausted / revoked / rejected`。一期正常申请直接进入 `active`，其他状态为运营和后续人工审批预留。

## 数据关系

`EnterpriseConsumerGrant` 保存：

- `enterprise_id / entry_id / user_id`
- 状态、申请时间、批准时间、有效起止时间
- `video_limit / video_used`
- 服务协议与隐私协议版本、用户确认时间
- 撤销人、撤销时间、原因及最后使用时间

同一入口与用户同时最多存在一个 `pending` 或 `active` Grant；历史授权不删除。`Creation.enterprise_grant_id` 记录视频对应的授权，`EnterpriseCocreationUsageLedger` 记录不可回退的视频提交次数。

## 接口

入口与授权：

```text
GET  /api/enterprise-entry/preview?token=...
GET  /api/enterprise-entry/access-context?token=...
POST /api/enterprise-entry/apply
```

共创接口均按当前登录用户和 `grant_id` 校验企业：

```text
GET  /api/cocreation/status?grant_id=...
GET  /api/cocreation/templates/{id}/cover?grant_id=...
POST /api/cocreation/expand
POST /api/cocreation/first-frame
POST /api/cocreation/videos
```

三个 POST 请求在原参数中增加 `grant_id`。客户端不得自行传企业 ID、企业模型配置或企业素材地址。

Ops 授权管理：

```text
GET  /api/ops/v1/cocreation-grants
POST /api/ops/v1/cocreation-grants/{id}/approve
POST /api/ops/v1/cocreation-grants/{id}/reject
POST /api/ops/v1/cocreation-grants/{id}/revoke
POST /api/ops/v1/cocreation-grants/{id}/renew
```

企业只能管理本企业 Grant，并继续通过现有 `/api/ops/v1/cocreation/videos` 查看本企业共创结果。

## 主业务适配

1. 从企业入口登录后调用 `access-context`，取得当前用户的 Grant。
2. Grant 不存在或不可用时展示确认页；调用 `apply` 后保存返回的 `grant.id`。
3. 拉取状态、模板封面、文本拓展、首帧和视频提交时都携带该 `grant_id`。
4. `status.available=false` 时停止新的模型调用并展示 `reason`。
5. 视频生成仍轮询原有 Creation 接口，不需要修改任务状态机或生成流水线。
6. 不缓存企业素材地址，也不要把 Grant 当作企业成员或永久租户关系。

## 安全与验收

- 未登录预览没有副作用，申请必须登录并提交当前协议版本。
- Grant 必须属于当前用户；模板、封面、素材和生成任务必须属于 Grant 对应企业。
- 其他用户冒用 Grant 或跨企业引用模板均被拒绝。
- C 端不会获得企业 API Key，企业不能查看用户个人模型配置或其他企业创作。
- Ops 可查看本企业授权、用量和共创视频，并可撤销或续期。
- 入口轮换或停用后旧 URL 不能再发起新申请。
