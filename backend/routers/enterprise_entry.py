from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Enterprise, EnterpriseEntryToken, EnterpriseMembership, User
from ..ops.schemas import EnterpriseBusinessContextOut, EnterpriseEntryResolveIn
from ..ops.service import audit
from .auth import get_current_user

router = APIRouter(prefix="/enterprise-entry", tags=["enterprise-entry"])


def _owner_context(db: Session, user: User) -> EnterpriseBusinessContextOut | None:
    membership = db.scalar(
        select(EnterpriseMembership).where(
            EnterpriseMembership.user_id == user.id,
            EnterpriseMembership.role == "owner",
            EnterpriseMembership.status == "active",
        )
    )
    if membership is None:
        return None
    enterprise = db.get(Enterprise, membership.enterprise_id)
    if enterprise is None or enterprise.status != "approved":
        return None
    return EnterpriseBusinessContextOut(
        enterprise_id=enterprise.id,
        enterprise_name=enterprise.name,
    )


@router.get("/context", response_model=EnterpriseBusinessContextOut | None)
def get_enterprise_context(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return _owner_context(db, user)


@router.post("/resolve", response_model=EnterpriseBusinessContextOut)
def resolve_enterprise_entry(
    payload: EnterpriseEntryResolveIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    entry = db.scalar(
        select(EnterpriseEntryToken).where(
            EnterpriseEntryToken.token == payload.token,
            EnterpriseEntryToken.status == "active",
        )
    )
    if entry is None:
        raise HTTPException(404, "企业专属入口无效或已更新")
    context = _owner_context(db, user)
    if context is None or context.enterprise_id != entry.enterprise_id:
        raise HTTPException(403, "该企业专属入口不属于当前账号")
    audit(
        db,
        user=user,
        enterprise_id=context.enterprise_id,
        action="enterprise.entry.resolve",
        resource_type="enterprise_entry",
        resource_id=entry.id,
    )
    db.commit()
    return context
