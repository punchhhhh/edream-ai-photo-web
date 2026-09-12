export interface AuthUser {
  id: number
  oauth_sub: string
  email: string | null
  display_name: string | null
  avatar_url: string | null
}

export interface ModelConfig {
  id: number
  name: string
  base_url: string
  api_key_masked: string
  chat_model: string
  image_model: string
  video_model: string
  video_provider: string
  is_default: boolean
  created_at: string
  updated_at: string
}

export interface Creation {
  id: number
  input_text: string
  style: string
  expanded_prompt: string
  image_source: 'none' | 'generated' | 'uploaded' | 'merged'
  image_path: string | null
  image_url: string | null
  video_url: string | null
  duration: number
  status: 'pending' | 'generating_video' | 'completed' | 'failed'
  error: string | null
  config_name: string
  chat_model: string
  image_model: string
  video_model: string
  vlog_project_id: number | null
  created_at: string
  updated_at: string
}

export type ImageSource = 'none' | 'generate' | 'upload'

export interface StylePreset {
  id: number
  name: string
  /** 画面语言要点,拼进 AI 拓展 prompt(悬停可看) */
  description: string
  negative_prompt: string
  /** 该风格建议的首帧画幅 */
  image_size: string
  sort_order: number
}

export interface VlogImage {
  image_path: string
  url: string
  width: number
  height: number
  order: number
}

export type VlogMotionTemplate =
  | 'kenburns_in'
  | 'kenburns_out'
  | 'pan_left'
  | 'pan_right'
  | 'drift'

export interface VlogMotionOption {
  key: VlogMotionTemplate
  label: string
  description: string
}

export const VLOG_MOTION_TEMPLATES: VlogMotionOption[] = [
  { key: 'kenburns_in', label: '缓慢推进', description: '镜头逐渐靠近主体' },
  { key: 'kenburns_out', label: '缓慢拉远', description: '从局部展开到完整画面' },
  { key: 'pan_left', label: '向左平移', description: '横向扫过图片细节' },
  { key: 'pan_right', label: '向右平移', description: '从左向右浏览画面' },
  { key: 'drift', label: '轻微漂移', description: '适合人物和旅行照片' },
]

export interface VlogUploadPlan {
  images: VlogImage[]
  ratio: '9:16' | '16:9'
  target_duration: number
  clips: Array<{ reference_paths: string[]; duration: number }>
}

export interface VlogClip {
  id: number
  sequence: number
  reference_paths: string[]
  reference_urls: string[]
  duration: number
  status: 'pending' | 'generating_video' | 'completed' | 'failed'
  error: string | null
  video_url: string | null
  retry_count: number
}

export interface VlogProject {
  id: number
  description: string
  style: string
  image_paths: string[]
  image_urls: string[]
  timeline_data: Array<Record<string, unknown>>
  ratio: '9:16' | '16:9'
  resolution: string
  target_duration: number
  transition_style: VlogTransition
  status: 'pending' | 'generating_video' | 'ready_to_merge' | 'completed' | 'failed'
  error: string | null
  // 服务端合成队列状态:项目本身停在 ready_to_merge,合成成功才转 completed
  merge_status: 'queued' | 'running' | 'failed' | 'done' | null
  merge_progress: number
  merge_error: string | null
  final_video_url: string | null
  final_creation_id: number | null
  config_name: string
  video_model: string
  clips: VlogClip[]
  created_at: string
  updated_at: string
}

export type VlogTransition =
  | 'fade'
  | 'dissolve'
  | 'wipeleft'
  | 'wiperight'
  | 'slideleft'
  | 'slideright'
  | 'circleopen'
  | 'zoomin'

export interface VlogTransitionOption {
  key: VlogTransition
  label: string
  description: string
}

export const VLOG_TRANSITIONS: VlogTransitionOption[] = [
  { key: 'fade', label: '柔和淡化', description: '自然、稳定，适合大多数 Vlog' },
  { key: 'dissolve', label: '溶解', description: '画面颗粒感溶解过渡' },
  { key: 'wipeleft', label: '向左擦除', description: '新画面从右向左推入' },
  { key: 'wiperight', label: '向右擦除', description: '新画面从左向右推入' },
  { key: 'slideleft', label: '向左滑动', description: '带方向感的连续切换' },
  { key: 'slideright', label: '向右滑动', description: '适合顺着运动方向衔接' },
  { key: 'circleopen', label: '圆形展开', description: '从中心向外打开新画面' },
  { key: 'zoomin', label: '推进切换', description: '新画面快速推进覆盖旧画面' },
]

export const STATUS_TEXT: Record<string, string> = {
  pending: '排队中',
  generating_video: '视频生成中',
  ready_to_merge: '片段已就绪',
  completed: '已完成',
  failed: '失败',
  cancelled: '已放弃',
}

/** 统一转场时长(秒),与后端 video_merge.VLOG_TRANSITION_SECONDS 保持一致,用于预计总时长。 */
export const VLOG_TRANSITION_SECONDS = 0.6
