import type {
  AuthUser,
  Creation,
  ModelConfig,
  StylePreset,
  VlogProject,
  VlogUploadPlan,
} from './types'

// 配置表单里用户本次输入的内容;编辑已存配置时 api_key 留空表示保留原密钥
export type ConfigForm = Omit<ModelConfig, 'id' | 'api_key_masked' | 'created_at' | 'updated_at'> & {
  api_key: string
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const isJsonBody = typeof init?.body === 'string'
  const res = await fetch(path, {
    ...init,
    headers: isJsonBody ? { 'Content-Type': 'application/json', ...init?.headers } : init?.headers,
  })
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith('/api/auth/')) {
      // 会话失效,跳转 OAuth 登录;next 让登录后回到当前页
      window.location.href = `/api/auth/login?next=${encodeURIComponent(window.location.pathname)}`
      throw new Error('登录已失效,正在跳转登录…')
    }
    let detail = `${res.status} ${res.statusText}`
    try {
      const data = await res.json()
      if (typeof data.detail === 'string') detail = data.detail
      else if (Array.isArray(data.detail)) detail = data.detail.map((d: any) => d.msg ?? '').join('; ')
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return res.json()
}

// ---- 登录 ----
export const fetchMe = () => request<AuthUser>('/api/auth/me')

// sso_logout_url:jwt 模式下先去 Casdoor 注销 SSO 会话,否则退出后会被自动登回;dev 模式为 null
export const logout = () =>
  request<{ ok: boolean; sso_logout_url: string | null }>('/api/auth/logout', { method: 'POST' })

// ---- 模型配置 ----
export const listConfigs = () => request<ModelConfig[]>('/api/configs')

export const createConfig = (payload: ConfigForm) =>
  request<ModelConfig>('/api/configs', { method: 'POST', body: JSON.stringify(payload) })

export const updateConfig = (id: number, payload: ConfigForm) =>
  request<ModelConfig>(`/api/configs/${id}`, { method: 'PUT', body: JSON.stringify(payload) })

export const deleteConfig = (id: number) => request<{ ok: boolean }>(`/api/configs/${id}`, { method: 'DELETE' })

export interface ConfigTestResult {
  ok: boolean
  models: string[]
  found: { chat: boolean | null; image: boolean | null; video: boolean | null }
  note: string
}

// 编辑已保存配置且密钥留空时传 configId,由后端用存储的密钥测试,密钥不出库
export const testConfig = (payload: Partial<ConfigForm> & { config_id?: number | null }) =>
  request<ConfigTestResult>('/api/configs/test', { method: 'POST', body: JSON.stringify(payload) })

// ---- 创作流程 ----
export const expandText = (configId: number, text: string, style: string) =>
  request<{ expanded_prompt: string }>('/api/expand', {
    method: 'POST',
    body: JSON.stringify({ config_id: configId, text, style }),
  })

export const generateImage = (configId: number, prompt: string, size: string) =>
  request<{ image_path: string; url: string }>('/api/generate-image', {
    method: 'POST',
    body: JSON.stringify({ config_id: configId, prompt, size }),
  })

export const uploadImage = (file: File) => {
  const form = new FormData()
  form.append('file', file)
  return request<{ image_path: string; url: string }>('/api/upload', { method: 'POST', body: form })
}

export const createCreation = (payload: {
  input_text: string
  style: string
  expanded_prompt: string
  image_source: 'none' | 'generated' | 'uploaded'
  image_path: string | null
  config_id: number
  duration: number
}) => request<Creation>('/api/creations', { method: 'POST', body: JSON.stringify(payload) })

export const listCreations = (limit = 50) => request<Creation[]>(`/api/creations?limit=${limit}`)

export const getCreation = (id: number) => request<Creation>(`/api/creations/${id}`)

export const deleteCreation = (id: number) => request<{ ok: boolean }>(`/api/creations/${id}`, { method: 'DELETE' })

// ---- 风格预设 ----
export const listStyles = () => request<StylePreset[]>('/api/styles')

// ---- 多图 Vlog ----
export const uploadVlogImages = (files: File[]) => {
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  return request<VlogUploadPlan>('/api/vlogs/upload', { method: 'POST', body: form })
}

export const createVlog = (payload: {
  config_id: number
  image_paths: string[]
  image_groups?: string[][]
  image_group_descriptions?: string[]
  ratio?: '9:16' | '16:9'
  style: string
  description: string
  transition_style: string
  timeline_data?: Array<Record<string, unknown>>
}) => request<VlogProject>('/api/vlogs', { method: 'POST', body: JSON.stringify(payload) })

export const createLocalVlog = (payload: {
  ratio: '9:16' | '16:9'
  style: string
  description: string
  transition_style: string
  timeline_data: Array<Record<string, unknown>>
}) => request<VlogProject>('/api/vlogs/local', { method: 'POST', body: JSON.stringify(payload) })

export const uploadVlogVideo = (file: File) => {
  const form = new FormData()
  form.append('file', file)
  return request<{ asset_path: string; url: string }>('/api/vlogs/assets/video', { method: 'POST', body: form })
}

export const planVlog = (imagePaths: string[]) =>
  request<Omit<VlogUploadPlan, 'images'>>('/api/vlogs/plan', {
    method: 'POST',
    body: JSON.stringify({ image_paths: imagePaths }),
  })

export const getVlog = (id: number) => request<VlogProject>(`/api/vlogs/${id}`)

export const getLatestVlog = () => request<VlogProject | null>('/api/vlogs/latest')

export const retryVlogClip = (projectId: number, clipId: number) =>
  request<VlogProject>(`/api/vlogs/${projectId}/clips/${clipId}/retry`, { method: 'POST' })

export const abandonVlog = (projectId: number) =>
  request<VlogProject>(`/api/vlogs/${projectId}/abandon`, { method: 'POST' })

// 提交服务端合成:入队立即返回,进度随项目轮询(merge_status/merge_progress)
export const mergeVlog = (projectId: number) =>
  request<VlogProject>(`/api/vlogs/${projectId}/merge`, { method: 'POST' })
