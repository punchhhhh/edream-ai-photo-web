from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ModelConfig, User
from ..schemas import (
    VIDEO_PROVIDERS,
    ConfigTestIn,
    ConfigTestOut,
    ModelConfigIn,
    ModelConfigOut,
    ModelConfigUpdateIn,
)
from ..services.ai_client import AICallError, AIClient
from .auth import get_current_user

router = APIRouter(prefix="/configs", tags=["model-configs"])


def _get_or_404(db: Session, user: User, config_id: int) -> ModelConfig:
    config = db.get(ModelConfig, config_id)
    if config is None or config.user_id != user.id:
        raise HTTPException(404, f"模型配置 {config_id} 不存在")
    return config


def _clear_other_defaults(db: Session, user: User, keep_id: int) -> None:
    for other in db.scalars(
        select(ModelConfig).where(ModelConfig.user_id == user.id, ModelConfig.is_default.is_(True))
    ):
        if other.id != keep_id:
            other.is_default = False


@router.get("", response_model=list[ModelConfigOut])
def list_configs(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return [
        ModelConfigOut.from_config(c)
        for c in db.scalars(
            select(ModelConfig).where(ModelConfig.user_id == user.id).order_by(ModelConfig.id.desc())
        )
    ]


@router.post("", response_model=ModelConfigOut)
def create_config(
    payload: ModelConfigIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if payload.video_provider not in VIDEO_PROVIDERS:
        raise HTTPException(422, f"video_provider 仅支持:{', '.join(VIDEO_PROVIDERS)}")
    config = ModelConfig(user_id=user.id, **payload.model_dump())
    db.add(config)
    db.flush()
    if config.is_default:
        _clear_other_defaults(db, user, config.id)
    # 第一个配置自动设为默认,方便开箱即用
    if not db.scalars(
        select(ModelConfig).where(ModelConfig.user_id == user.id, ModelConfig.is_default.is_(True))
    ).first():
        config.is_default = True
    db.commit()
    db.refresh(config)
    return ModelConfigOut.from_config(config)


@router.put("/{config_id}", response_model=ModelConfigOut)
def update_config(
    config_id: int,
    payload: ModelConfigUpdateIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if payload.video_provider not in VIDEO_PROVIDERS:
        raise HTTPException(422, f"video_provider 仅支持:{', '.join(VIDEO_PROVIDERS)}")
    config = _get_or_404(db, user, config_id)
    data = payload.model_dump()
    # 密钥留空表示沿用原值,前端不需要(也拿不到)已存密钥
    if not data["api_key"]:
        data["api_key"] = config.api_key
    for k, v in data.items():
        setattr(config, k, v)
    if config.is_default:
        _clear_other_defaults(db, user, config.id)
    db.commit()
    db.refresh(config)
    return ModelConfigOut.from_config(config)


@router.post("/test", response_model=ConfigTestOut)
def test_config(
    payload: ConfigTestIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """验证网关连通性与密钥,并比对模型名是否在网关模型列表中(不消耗生成 token)。"""
    if payload.config_id:
        # 用服务端存储的密钥测试,密钥不出库
        config = _get_or_404(db, user, payload.config_id)
        base_url, api_key = config.base_url, config.api_key
        chat_model = payload.chat_model or config.chat_model
        image_model = payload.image_model or config.image_model
        video_model = payload.video_model or config.video_model
    elif payload.base_url and payload.api_key:
        base_url, api_key = payload.base_url, payload.api_key
        chat_model, image_model, video_model = (
            payload.chat_model,
            payload.image_model,
            payload.video_model,
        )
    else:
        raise HTTPException(422, "请先保存配置后测试,或在表单中填写 API 地址与密钥")

    try:
        with AIClient(base_url, api_key) as client:
            models = client.list_models()
    except AICallError as e:
        raise HTTPException(502, str(e)) from e
    model_set = set(models)

    def found(name: str) -> bool | None:
        return None if not name else name in model_set

    return ConfigTestOut(
        ok=True,
        models=models[:500],
        found={
            "chat": found(chat_model),
            "image": found(image_model),
            "video": found(video_model),
        },
        note=f"连接成功,网关共有 {len(models)} 个可用模型" if models else "连接成功,但网关未返回模型列表",
    )


@router.delete("/{config_id}")
def delete_config(
    config_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    config = _get_or_404(db, user, config_id)
    was_default = config.is_default
    db.delete(config)
    db.commit()
    if was_default:
        first = db.scalars(
            select(ModelConfig).where(ModelConfig.user_id == user.id).order_by(ModelConfig.id)
        ).first()
        if first:
            first.is_default = True
            db.commit()
    return {"ok": True}
