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
  created_at: string
  updated_at: string
}

export type ImageSource = 'none' | 'generate' | 'upload'

export const STYLE_OPTIONS = [
  '电影质感',
  '动漫风格',
  '3D 卡通',
  '写实纪录',
  '赛博朋克',
  '水彩插画',
  '复古胶片',
  '奇幻梦境',
  '国风水墨',
  '极简主义',
]

export const STATUS_TEXT: Record<string, string> = {
  pending: '排队中',
  generating_video: '视频生成中',
  completed: '已完成',
  failed: '失败',
}
