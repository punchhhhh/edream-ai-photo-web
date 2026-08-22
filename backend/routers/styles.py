from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import StylePreset, User
from ..schemas import StyleOut
from .auth import get_current_user

router = APIRouter(prefix="/styles", tags=["style-presets"])


@router.get("", response_model=list[StyleOut])
def list_styles(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """风格预设列表(前端创作台的风格选择数据源)。"""
    return (
        db.scalars(
            select(StylePreset)
            .where(StylePreset.is_active.is_(True))
            .order_by(StylePreset.sort_order, StylePreset.id)
        )
        .all()
    )
