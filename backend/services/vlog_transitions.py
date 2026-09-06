"""Allowlisted Vlog transition templates shared by the API and browser merge client."""

from dataclasses import dataclass


@dataclass(frozen=True)
class VlogTransition:
    key: str
    label: str
    description: str


VLOG_TRANSITIONS = (
    VlogTransition("fade", "柔和淡化", "前后画面自然交叉淡化"),
    VlogTransition("dissolve", "溶解", "画面颗粒感溶解过渡"),
    VlogTransition("wipeleft", "向左擦除", "新画面从右向左推入"),
    VlogTransition("wiperight", "向右擦除", "新画面从左向右推入"),
    VlogTransition("slideleft", "向左滑动", "新画面向左滑入并带出旧画面"),
    VlogTransition("slideright", "向右滑动", "新画面向右滑入并带出旧画面"),
    VlogTransition("circleopen", "圆形展开", "新画面从中心向外展开"),
    VlogTransition("zoomin", "推进切换", "新画面快速推进覆盖旧画面"),
)

VLOG_TRANSITION_KEYS = frozenset(item.key for item in VLOG_TRANSITIONS)
DEFAULT_VLOG_TRANSITION = "fade"


def normalize_vlog_transition(value: str | None) -> str:
    candidate = (value or DEFAULT_VLOG_TRANSITION).strip().lower()
    if candidate not in VLOG_TRANSITION_KEYS:
        raise ValueError(f"不支持的 Vlog 转场模板: {value}")
    return candidate
