"""企业共创视频:成员基于企业模版、用企业主的网关密钥生成视频。

可见性:企业认证通过 + 成员关系启用 + 企业配置了启用模版 + 服务端已配置共创网关。
次数限制:每用户非失败的共创任务数上限(默认 3,见 settings.cocreation_max_videos_per_user)。
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Creation, User
from ..schemas import (
    CoCreationExpandIn,
    CoCreationStatusOut,
    CoCreationTemplateOut,
    CoCreationVideoCreateIn,
    CreationOut,
    ExpandOut,
)
from ..services import cocreation
from ..services.ai_client import AICallError, AIClient
from ..services.pipeline import ACTIVE_STATUSES, start_creation_thread
from ..settings import settings
from .auth import get_current_user
from .creations import _to_out

router = APIRouter(tags=["cocreation"])


def _status_out(db: Session, user: User) -> CoCreationStatusOut:
    limit = settings.cocreation_max_videos_per_user
    if not cocreation.cocreation_gateway_ready():
        return CoCreationStatusOut(available=False, reason="平台尚未开启企业共创服务")
    context = cocreation.cocreation_context(db, user.id)
    if context is None:
        return CoCreationStatusOut(
            available=False, reason="企业认证通过后才能参与共创"
        )
    templates = cocreation.list_templates(db, context.enterprise.id)
    if not templates:
        return CoCreationStatusOut(
            available=False, reason="企业还没有配置共创视频模版"
        )
    used = cocreation.used_video_count(db, user.id, context.enterprise.id)
    return CoCreationStatusOut(
        available=True,
        templates=[CoCreationTemplateOut.model_validate(t) for t in templates],
        used=used,
        limit=limit,
    )


@router.get("/cocreation/status", response_model=CoCreationStatusOut)
def cocreation_status(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    return _status_out(db, user)


@router.post("/cocreation/expand", response_model=ExpandOut)
def cocreation_expand(
    payload: CoCreationExpandIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """用企业主的网关密钥做 AI 文本拓展(模版配置了 chat_model 才可用)。"""
    context = cocreation.cocreation_context(db, user.id)
    if context is None:
        raise HTTPException(403, "企业认证通过后才能使用共创服务")
    template = cocreation.get_template(db, context.enterprise.id, payload.template_id)
    if template is None or not template.is_active:
        raise HTTPException(404, "视频模版不存在或已停用")
    if not template.chat_model:
        raise HTTPException(422, "当前模版未配置文本模型,不支持 AI 拓展")
    base_url, api_key = cocreation.enterprise_gateway_credentials(
        db, context.enterprise.id
    )
    # 模版画面要求作为风格要点交给 LLM,让拓展结果贴合企业模版调性
    with AIClient(base_url, api_key) as client:
        try:
            expanded = client.expand_prompt(
                template.chat_model,
                payload.text,
                style_description=template.prompt,
            )
        except AICallError as e:
            raise HTTPException(502, str(e)) from e
    return ExpandOut(expanded_prompt=expanded)


@router.post("/cocreation/videos", response_model=CreationOut)
def create_cocreation_video(
    payload: CoCreationVideoCreateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not cocreation.cocreation_gateway_ready():
        raise HTTPException(403, "平台尚未开启企业共创服务")
    context = cocreation.cocreation_context(db, user.id)
    if context is None:
        raise HTTPException(403, "企业认证通过后才能使用共创服务")
    template = cocreation.get_template(db, context.enterprise.id, payload.template_id)
    if template is None or not template.is_active:
        raise HTTPException(404, "视频模版不存在或已停用")
    if not template.video_model:
        raise HTTPException(422, "当前模版未配置视频模型,请联系企业管理员")

    used = cocreation.used_video_count(db, user.id, context.enterprise.id)
    if used >= settings.cocreation_max_videos_per_user:
        raise HTTPException(
            409,
            f"共创次数已用完(每人限 {settings.cocreation_max_videos_per_user} 个可保留视频)"
            ",可在历史记录中删除不需要的共创视频后重试",
        )

    # 与个人创作共用"同用户同时只有一个生成中任务"的约束
    active = db.scalars(
        select(Creation)
        .where(Creation.user_id == user.id, Creation.status.in_(ACTIVE_STATUSES))
        .order_by(Creation.id.desc())
    ).first()
    if active is not None:
        raise HTTPException(
            409,
            f"已有视频正在生成中(任务 {active.id}),请等待完成或失败后再提交新任务",
        )

    expanded = (payload.expanded_prompt or payload.text).strip()
    creation = Creation(
        user_id=user.id,
        input_text=payload.text.strip(),
        style="",
        expanded_prompt=cocreation.combine_prompt(template, expanded),
        image_source="none",
        duration=template.duration,
        status="pending",
        config_name=f"企业共创 · {template.name}",
        chat_model=template.chat_model,
        image_model="",
        video_model=template.video_model,
        enterprise_id=context.enterprise.id,
        template_id=template.id,
        template_name=template.name,
    )
    db.add(creation)
    try:
        db.commit()
    except IntegrityError:
        # 并发提交竞态由 (user_id, active) 部分唯一索引兜底
        db.rollback()
        raise HTTPException(409, "已有视频正在生成中,请勿重复提交") from None
    db.refresh(creation)

    start_creation_thread(creation.id)
    return _to_out(creation)
