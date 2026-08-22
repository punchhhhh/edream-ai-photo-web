"""默认风格预设与幂等播种。description 是风格的画面语言要点,拼进 AI 拓展 prompt;
negative_prompt 在视频生成时传给网关;image_size 是该风格建议的首帧画幅。"""

from sqlalchemy import select

from .database import SessionLocal
from .models import StylePreset

DEFAULT_STYLE_PRESETS = [
    {
        "name": "电影质感",
        "description": "电影级摄影质感,浅景深虚化,35mm 胶片色彩科学,黄金时刻侧逆光,光影层次丰富,缓慢的推轨或摇镜,构图讲究,商业大片氛围",
        "negative_prompt": "卡通,插画,低画质,过曝,噪点,画面变形,文字,水印",
        "image_size": "1280x720",
    },
    {
        "name": "动漫风格",
        "description": "日本动画电影风格,赛璐璐上色,干净利落的线条,高饱和明快色彩,戏剧化构图,唯美的光影与云层,风与光的动态张力",
        "negative_prompt": "写实照片,3D 渲染,粗糙线条,低画质,模糊",
        "image_size": "1280x720",
    },
    {
        "name": "3D 卡通",
        "description": "皮克斯式 3D 卡通渲染,柔和的全局光照,圆润可爱的造型与夸张的表情,明快的高饱和配色,细腻的材质与毛发质感",
        "negative_prompt": "恐怖,惊悚,写实人像,2D 平面,粗糙建模,低画质",
        "image_size": "1280x720",
    },
    {
        "name": "写实纪录",
        "description": "纪录片式自然写实,自然光真实还原,轻微手持镜头晃动,生活化的细节,现场感与真实质感,不刻意摆拍",
        "negative_prompt": "卡通,插画,过度修图,电影级特效,摆拍痕迹",
        "image_size": "1280x720",
    },
    {
        "name": "赛博朋克",
        "description": "雨夜霓虹的赛博朋克都市,青色与品红的高对比冷暖撞色,湿润反光的街道,全息广告与巨大楼宇,体积雾与光束,浓烈的未来都市氛围",
        "negative_prompt": "白昼,乡村,低对比,灰暗浑浊,模糊,低画质",
        "image_size": "1280x720",
    },
    {
        "name": "水彩插画",
        "description": "手绘水彩插画,颜料晕染与透气留白,透明层叠的色块,纸张纹理,柔和淡雅的低饱和配色,轻盈松弛的笔触",
        "negative_prompt": "照片写实,3D 渲染,高饱和,锐利数码感,厚涂油画",
        "image_size": "1024x1024",
    },
    {
        "name": "复古胶片",
        "description": "80 年代柯达胶片质感,明显的颗粒感,暖黄色调偏移与轻微褪色,柔和的暗角,怀旧氛围,自然柔和的肤色还原",
        "negative_prompt": "数码锐化,现代感,过饱和,高对比玻璃质感",
        "image_size": "1280x720",
    },
    {
        "name": "奇幻梦境",
        "description": "超现实梦境画面,漂浮与失重元素,柔光光晕与景深虚化,梦幻的粉紫蓝渐变色调,魔幻粒子与光斑,天马行空的想象力",
        "negative_prompt": "写实,日常,平淡,杂乱,低画质",
        "image_size": "1280x720",
    },
    {
        "name": "国风水墨",
        "description": "中国水墨画风格,泼墨挥洒与大面留白,宣纸质感,墨色浓淡干湿的变化,飘逸写意的笔法,禅意的东方美学意境",
        "negative_prompt": "高饱和彩色,照片写实,3D 渲染,西方油画质感",
        "image_size": "1024x1024",
    },
    {
        "name": "极简主义",
        "description": "极简主义构图,大面积留白与单一主体,克制的高级感配色(不超过三种颜色),干净纯粹的背景,秩序感与呼吸感",
        "negative_prompt": "杂乱,堆砌细节,高饱和撞色,复杂背景,低画质",
        "image_size": "1024x1024",
    },
]


def seed_default_styles() -> int:
    """幂等播种默认风格:按名称查重,已存在的不覆盖(方便直接改库做自定义)。返回新增数量。"""
    added = 0
    with SessionLocal() as session:
        existing = set(session.scalars(select(StylePreset.name)).all())
        for index, preset in enumerate(DEFAULT_STYLE_PRESETS):
            if preset["name"] in existing:
                continue
            session.add(StylePreset(**preset, sort_order=index))
            added += 1
        if added:
            session.commit()
    return added
