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
  ratio: '9:16' | '16:9'
  resolution: string
  target_duration: number
  status: 'pending' | 'generating_video' | 'ready_to_merge' | 'completed' | 'failed'
  error: string | null
  final_video_url: string | null
  final_creation_id: number | null
  config_name: string
  video_model: string
  clips: VlogClip[]
  created_at: string
  updated_at: string
}

export const STATUS_TEXT: Record<string, string> = {
  pending: '排队中',
  generating_video: '视频生成中',
  ready_to_merge: '片段已就绪',
  completed: '已完成',
  failed: '失败',
}
