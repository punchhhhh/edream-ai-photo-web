"""企业后台 · 共创视频管理:模版 CRUD 与成员共创视频查看。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Creation, EnterpriseVideoTemplate, User
from ..routers.auth import get_current_user
from .. import storage
from .access import require_enterprise
from .enterprise import EDIT_ROLES
from .schemas import (
    CoCreationVideoOut,
    VideoTemplateCreateIn,
    VideoTemplateOut,
    VideoTemplateUpdateIn,
)
from ..services import cocreation
from .service import audit

router = APIRouter(prefix="/ops/v1", tags=["ops-cocreation"])


def _template_for_enterprise(
    db: Session, enterprise_id: int, template_id: int
) -> EnterpriseVideoTemplate:
    template = db.get(EnterpriseVideoTemplate, template_id)
    if template is None or template.enterprise_id != enterprise_id:
        raise HTTPException(404, "视频模版不存在")
    return template


@router.get("/video-templates", response_model=list[VideoTemplateOut])
def list_video_templates(
    db: Session = Depends(get_db), user=Depends(get_current_user)
):
    context = require_enterprise(db, user)
    return cocreation.list_templates(db, context.enterprise.id, include_inactive=True)


@router.post("/video-templates", response_model=VideoTemplateOut)
def create_video_template(
    payload: VideoTemplateCreateIn,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    template = EnterpriseVideoTemplate(
        enterprise_id=context.enterprise.id,
        created_by_user_id=user.id,
        **payload.model_dump(),
    )
    db.add(template)
    db.flush()
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="video_template.create",
        resource_type="video_template",
        resource_id=template.id,
        details={"name": payload.name, "video_model": payload.video_model},
    )
    db.commit()
    db.refresh(template)
    return template


@router.patch("/video-templates/{template_id}", response_model=VideoTemplateOut)
def update_video_template(
    template_id: int,
    payload: VideoTemplateUpdateIn,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    template = _template_for_enterprise(db, context.enterprise.id, template_id)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in changes.items():
        setattr(template, field, value)
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="video_template.update",
        resource_type="video_template",
        resource_id=template.id,
        details={"fields": sorted(changes)},
    )
    db.commit()
    db.refresh(template)
    return template


@router.delete("/video-templates/{template_id}")
def delete_video_template(
    template_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    template = _template_for_enterprise(db, context.enterprise.id, template_id)
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise.id,
        action="video_template.delete",
        resource_type="video_template",
        resource_id=template.id,
        details={"name": template.name},
    )
    db.delete(template)
    db.commit()
    return {"ok": True}


@router.get("/cocreation/videos", response_model=list[CoCreationVideoOut])
def list_cocreation_videos(
    limit: int = 200,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """企业主/管理员查看本企业全部成员的共创视频。"""
    context = require_enterprise(db, user, roles=EDIT_ROLES)
    rows = db.execute(
        select(Creation, User)
        .join(User, User.id == Creation.user_id)
        .where(
            Creation.enterprise_id == context.enterprise.id,
            Creation.template_id.isnot(None),
        )
        .order_by(Creation.id.desc())
        .limit(min(limit, 500))
    ).all()
    result: list[CoCreationVideoOut] = []
    for creation, creator in rows:
        result.append(
            CoCreationVideoOut(
                id=creation.id,
                creator_id=creator.id,
                creator_sub=creator.oauth_sub,
                creator_name=creator.display_name,
                template_id=creation.template_id,
                template_name=creation.template_name,
                input_text=creation.input_text,
                video_model=creation.video_model,
                duration=creation.duration,
                status=creation.status,
                error=creation.error,
                # 播放地址输出时推导;video_path 为空时回退到远端链接
                video_url=(storage.url(creation.video_path) if creation.video_path else None)
                or creation.video_url,
                created_at=creation.created_at,
            )
        )
    return result
