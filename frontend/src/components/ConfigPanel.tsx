import { useEffect, useState } from 'react'
import { createConfig, deleteConfig, listConfigs, testConfig, updateConfig, type ConfigForm, type ConfigTestResult } from '../api'
import type { ModelConfig } from '../types'

interface Props {
  onClose: () => void
  onSaved: (configs: ModelConfig[]) => void
}

const EMPTY_FORM: ConfigForm = {
  name: '',
  base_url: '',
  api_key: '',
  chat_model: '',
  image_model: '',
  video_model: '',
  video_provider: 'video_generations',
  is_default: false,
}

type FormState = typeof EMPTY_FORM

export default function ConfigPanel({ onClose, onSaved }: Props) {
  const [configs, setConfigs] = useState<ModelConfig[]>([])
  const [editingId, setEditingId] = useState<number | null>(null)
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<ConfigTestResult | null>(null)

  // 正在编辑的配置的密钥掩码,提示原密钥仍保留在后端
  const currentMasked = editingId ? (configs.find((c) => c.id === editingId)?.api_key_masked ?? '') : ''

  const reload = async () => {
    const list = await listConfigs()
    setConfigs(list)
    onSaved(list)
  }

  useEffect(() => {
    reload().catch((e) => setError(String(e.message)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const startCreate = () => {
    setEditingId(0) // 0 = 新建
    setForm(EMPTY_FORM)
    setError('')
    setTestResult(null)
  }

  const startEdit = (c: ModelConfig) => {
    setEditingId(c.id)
    setForm({
      name: c.name,
      base_url: c.base_url,
      // 已存密钥不回传前端,编辑时留空即保留
      api_key: '',
      chat_model: c.chat_model,
      image_model: c.image_model,
      video_model: c.video_model,
      video_provider: c.video_provider,
      is_default: c.is_default,
    })
    setError('')
    setTestResult(null)
  }

  const runTest = async () => {
    if (!form.base_url.trim()) {
      setError('填写 API 地址后才能测试')
      return
    }
    if (!form.api_key.trim() && editingId === 0) {
      setError('新建配置需填写密钥后才能测试')
      return
    }
    setTesting(true)
    setError('')
    setTestResult(null)
    try {
      // 编辑已保存配置且密钥留空 → 传 config_id,由后端用存储密钥测试
      const payload: Partial<ConfigForm> & { config_id?: number | null } = { ...form }
      if (editingId && editingId !== 0 && !form.api_key.trim()) {
        payload.config_id = editingId
        delete payload.api_key
      }
      setTestResult(await testConfig(payload))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setTesting(false)
    }
  }

  const save = async () => {
    if (!form.name.trim() || !form.base_url.trim() || (editingId === 0 && !form.api_key.trim())) {
      setError(editingId === 0 ? '名称、API 地址、密钥为必填项' : '名称、API 地址为必填项(密钥留空保留原值)')
      return
    }
    setSaving(true)
    setError('')
    try {
      if (editingId === 0) await createConfig(form)
      else if (editingId) await updateConfig(editingId, form)
      await reload()
      setEditingId(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const remove = async (c: ModelConfig) => {
    if (!confirm(`确定删除配置「${c.name}」吗?`)) return
    try {
      await deleteConfig(c.id)
      if (editingId === c.id) setEditingId(null)
      await reload()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const foundMark = (v: boolean | null | undefined) =>
    v === null || v === undefined ? (
      <span className="muted">未填写</span>
    ) : v ? (
      <span className="ok-text">✓ 在网关模型列表中</span>
    ) : (
      <span className="error-text">✗ 不在列表中(网关未列出时仍可能可用)</span>
    )

  const field = (key: keyof FormState, label: string, placeholder: string, required = false, list?: string) => (
    <label className="field">
      <span>
        {label}
        {required && <em className="req">*</em>}
      </span>
      <input
        value={form[key] as string}
        placeholder={placeholder}
        list={list}
        onChange={(e) => setForm({ ...form, [key]: e.target.value })}
      />
    </label>
  )

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal config-modal" onClick={(e) => e.stopPropagation()}>
        <datalist id="cfg-model-options">
          {(testResult?.models ?? []).map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
        <div className="modal-head">
          <h2>模型配置</h2>
          <button className="icon-btn" onClick={onClose}>
            ✕
          </button>
        </div>
        <p className="modal-tip">
          配置 new-api 网关地址与密钥,后端将按所选配置调用对应模型。AI 拓展用文本模型、首帧图片用图片模型、成片用视频模型。
        </p>

        {error && <div className="alert error">{error}</div>}

        <div className="config-list">
          {configs.length === 0 && <div className="empty">还没有配置,点击下方按钮新增一个</div>}
          {configs.map((c) => (
            <div key={c.id} className={`config-item ${editingId === c.id ? 'active' : ''}`} onClick={() => startEdit(c)}>
              <div className="config-item-main">
                <strong>
                  {c.name}
                  {c.is_default && <span className="tag default-tag">默认</span>}
                </strong>
                <span className="muted">{c.base_url}</span>
                <span className="muted small">
                  {c.chat_model || '未设文本模型'} · {c.image_model || '未设图片模型'} · {c.video_model || '未设视频模型'}
                </span>
              </div>
              <button className="danger-link" onClick={(e) => { e.stopPropagation(); remove(c) }}>
                删除
              </button>
            </div>
          ))}
        </div>

        {editingId !== null ? (
          <div className="config-form">
            <h3>{editingId === 0 ? '新增配置' : `编辑「${form.name}」`}</h3>
            {field('name', '配置名称', '如:我的 new-api', true)}
            {field('base_url', 'API 地址', 'https://your-new-api.com(填到域名或 /v1 均可)', true)}
            {field('api_key', 'API 密钥', editingId === 0 ? 'sk-...(仅保存在后端,不会回显)' : `留空保留原密钥(${currentMasked})`, editingId === 0)}
            {field('chat_model', '文本模型(AI 拓展)', '如 gpt-4o-mini / glm-4-flash', false, 'cfg-model-options')}
            {field('image_model', '图片模型(首帧生成)', '如 dall-e-3 / cogview-3', false, 'cfg-model-options')}
            {field('video_model', '视频模型(成片)', '如 kling-v1-master / cogvideox-3', false, 'cfg-model-options')}
            <label className="field">
              <span>视频接口类型</span>
              <select value={form.video_provider} onChange={(e) => setForm({ ...form, video_provider: e.target.value })}>
                <option value="video_generations">new-api 任务式(/v1/video/generations,可灵/Vidu 等)</option>
                <option value="openai_videos">Sora 风格(/v1/videos)</option>
              </select>
            </label>
            <label className="check-row">
              <input
                type="checkbox"
                checked={form.is_default}
                onChange={(e) => setForm({ ...form, is_default: e.target.checked })}
              />
              设为默认配置
            </label>

            {testResult && (
              <div className="test-result">
                <div className="ok-text">✓ {testResult.note}</div>
                <div className="small">
                  文本模型:{foundMark(testResult.found.chat)}
                </div>
                <div className="small">
                  图片模型:{foundMark(testResult.found.image)}
                </div>
                <div className="small">
                  视频模型:{foundMark(testResult.found.video)}
                </div>
                {testResult.models.length > 0 && (
                  <div className="muted small">模型输入框已支持从 {testResult.models.length} 个模型中下拉选择</div>
                )}
              </div>
            )}

            <div className="form-actions">
              <button className="btn primary" disabled={saving} onClick={save}>
                {saving ? '保存中…' : '保存'}
              </button>
              <button className="btn" disabled={testing} onClick={runTest}>
                {testing ? '测试中…' : '🔌 测试连接'}
              </button>
              <button className="btn" onClick={() => setEditingId(null)}>
                取消
              </button>
            </div>
          </div>
        ) : (
          <button className="btn primary full" onClick={startCreate}>
            + 新增配置
          </button>
        )}
      </div>
    </div>
  )
}
