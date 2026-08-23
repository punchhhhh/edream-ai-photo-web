import { useCallback, useEffect, useRef, useState } from 'react'
import ConfigPanel from './components/ConfigPanel'
import EditPanel from './components/EditPanel'
import HistoryPanel from './components/HistoryPanel'
import {
  createCreation,
  expandText,
  fetchMe,
  generateImage,
  getCreation,
  listConfigs,
  listCreations,
  listStyles,
  logout,
  uploadImage,
} from './api'
import { STATUS_TEXT, type AuthUser, type Creation, type ImageSource, type ModelConfig, type StylePreset } from './types'
import { downloadName, notify, requestNotifyPermission } from './utils'

const CONFIG_KEY = 'edream_config_id'

const IMAGE_SIZES = [
  { value: '1024x1024', label: '方形 1:1' },
  { value: '1280x720', label: '横屏 16:9' },
  { value: '720x1280', label: '竖屏 9:16' },
]

const DURATIONS = [4, 5, 10]

export default function App() {
  const [me, setMe] = useState<AuthUser | null>(null)
  const [configs, setConfigs] = useState<ModelConfig[]>([])
  const [styles, setStyles] = useState<StylePreset[]>([])
  const [configId, setConfigId] = useState<number | null>(null)
  const [configOpen, setConfigOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)

  // 步骤状态
  const [text, setText] = useState('')
  const [style, setStyle] = useState('')
  const [expanded, setExpanded] = useState('')
  const [expanding, setExpanding] = useState(false)
  const [path, setPath] = useState<ImageSource | null>(null)
  const [imageSize, setImageSize] = useState('1280x720')
  const [image, setImage] = useState<{ path: string; url: string } | null>(null)
  const [imageBusy, setImageBusy] = useState(false)
  const [duration, setDuration] = useState(5)
  const [creation, setCreation] = useState<Creation | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const config = configs.find((c) => c.id === configId) ?? null

  // 未登录先跳 OAuth;fetchMe 的 401 不走 api.ts 的自动跳转(auth 路径除外),这里显式处理
  useEffect(() => {
    fetchMe()
      .then(setMe)
      .catch(() => {
        window.location.href = '/api/auth/login'
      })
  }, [])

  const doLogout = async () => {
    let ssoLogoutUrl: string | null = null
    try {
      ssoLogoutUrl = (await logout()).sso_logout_url
    } catch {
      /* 本地会话可能已失效,直接回登录即可 */
    }
    // 有 SSO 登出地址时先去 Casdoor 注销,登出后回到站点再走正常登录;否则回登录入口
    window.location.href = ssoLogoutUrl ?? '/api/auth/login'
  }

  const refreshConfigs = useCallback(async () => {
    const list = await listConfigs()
    setConfigs(list)
    setConfigId((prev) => {
      if (prev && list.some((c) => c.id === prev)) return prev
      const saved = Number(localStorage.getItem(CONFIG_KEY))
      if (saved && list.some((c) => c.id === saved)) return saved
      return list.find((c) => c.is_default)?.id ?? list[0]?.id ?? null
    })
  }, [])

  useEffect(() => {
    refreshConfigs().catch((e) => setError((e as Error).message))
    // 风格预设从后端拉取(启动时幂等播种,可改库自定义)
    listStyles().then(setStyles).catch(() => {})
  }, [refreshConfigs])

  useEffect(() => {
    if (configId) localStorage.setItem(CONFIG_KEY, String(configId))
  }, [configId])

  // 刷新页面后,恢复仍在生成中的任务进度与表单内容
  const restoredRef = useRef(false)
  useEffect(() => {
    if (restoredRef.current) return
    restoredRef.current = true
    listCreations(20)
      .then((list) => {
        const running = list.find((c) => c.status === 'pending' || c.status === 'generating_video')
        if (!running) return
        fillFromCreation(running)
        setCreation(running)
      })
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 生成中轮询任务状态;完成/失败时发系统通知
  useEffect(() => {
    if (!creation || (creation.status !== 'pending' && creation.status !== 'generating_video')) return
    const timer = setInterval(async () => {
      try {
        const latest = await getCreation(creation.id)
        setCreation(latest)
        if (latest.status !== creation.status) {
          if (latest.status === 'completed') {
            notify('✅ 视频生成完成', latest.input_text.slice(0, 60))
          } else if (latest.status === 'failed') {
            notify('❌ 视频生成失败', (latest.error ?? '').slice(0, 80))
          }
        }
      } catch {
        /* 网络抖动时继续轮询 */
      }
    }, 3000)
    return () => clearInterval(timer)
  }, [creation])

  // 页面在后台时,用标题闪烁提醒结果
  useEffect(() => {
    if (!creation || (creation.status !== 'completed' && creation.status !== 'failed')) return
    if (!document.hidden) return
    const base = 'eDream AI 视频创作台'
    const msg = creation.status === 'completed' ? '✅ 视频生成完成' : '❌ 视频生成失败'
    let on = true
    const timer = setInterval(() => {
      document.title = on ? msg : base
      on = !on
    }, 1200)
    const stopOnVisible = () => {
      if (!document.hidden) {
        document.title = base
        clearInterval(timer)
      }
    }
    document.addEventListener('visibilitychange', stopOnVisible)
    return () => {
      clearInterval(timer)
      document.title = base
      document.removeEventListener('visibilitychange', stopOnVisible)
    }
  }, [creation?.status])

  const doExpand = async () => {
    if (!configId || !text.trim()) return
    setExpanding(true)
    setError('')
    try {
      const { expanded_prompt } = await expandText(configId, text.trim(), style)
      setExpanded(expanded_prompt)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setExpanding(false)
    }
  }

  const doGenerateImage = async () => {
    if (!configId || !expanded.trim()) return
    setImageBusy(true)
    setError('')
    try {
      const media = await generateImage(configId, expanded.trim(), imageSize)
      setImage({ path: media.image_path, url: media.url })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setImageBusy(false)
    }
  }

  const doUpload = async (file: File) => {
    setImageBusy(true)
    setError('')
    try {
      const media = await uploadImage(file)
      setImage({ path: media.image_path, url: media.url })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setImageBusy(false)
    }
  }

  const canSubmit =
    !!config?.video_model &&
    !!expanded.trim() &&
    (path === 'none' || ((path === 'generate' || path === 'upload') && !!image)) &&
    !submitting &&
    (!creation || creation.status === 'completed' || creation.status === 'failed')

  const doSubmit = async () => {
    if (!configId || !canSubmit) return
    setSubmitting(true)
    setError('')
    requestNotifyPermission() // 用户手势中申请通知权限,完成时可发系统通知
    try {
      const c = await createCreation({
        input_text: text.trim(),
        style,
        expanded_prompt: expanded.trim(),
        image_source: path === 'none' ? 'none' : path === 'generate' ? 'generated' : 'uploaded',
        image_path: path === 'none' ? null : image!.path,
        config_id: configId,
        duration,
      })
      setCreation(c)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  // 把历史记录/进行中任务的内容回填到创作表单
  const fillFromCreation = (c: Creation) => {
    setText(c.input_text)
    setStyle(c.style)
    setExpanded(c.expanded_prompt)
    setPath(c.image_source === 'none' ? 'none' : c.image_source === 'generated' ? 'generate' : 'upload')
    setImage(c.image_path && c.image_url ? { path: c.image_path, url: c.image_url } : null)
    setError('')
  }

  const reuseCreation = (c: Creation) => {
    fillFromCreation(c)
    setCreation(null)
    setHistoryOpen(false)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const resetAll = () => {
    setText('')
    setStyle('')
    setExpanded('')
    setPath(null)
    setImage(null)
    setCreation(null)
    setError('')
  }

  const selectPath = (p: ImageSource) => {
    setPath(p)
    setImage(null)
  }

  const selectStyle = (s: StylePreset) => {
    setStyle(style === s.name ? '' : s.name)
    // 联动建议画幅(还没生成图片时才覆盖用户选择)
    if (style !== s.name && !image && IMAGE_SIZES.some((x) => x.value === s.image_size)) {
      setImageSize(s.image_size)
    }
  }

  const generating = creation?.status === 'pending' || creation?.status === 'generating_video'
  const stepDone = (n: number) =>
    (n <= 1 && text.trim().length > 0) ||
    (n === 2 && style !== '') ||
    (n === 3 && expanded.trim().length > 0) ||
    (n === 4 && (path === 'none' || ((path === 'generate' || path === 'upload') && !!image))) ||
    (n === 5 && creation?.status === 'completed')

  const section = (
    n: number,
    title: string,
    hint: string,
    children: React.ReactNode,
    extra?: React.ReactNode,
  ) => (
    <section className={`step ${stepDone(n) ? 'done' : ''}`}>
      <header>
        <span className="step-num">{Number.isInteger(n) ? n : '4+'}</span>
        <div>
          <h3>{title}</h3>
          <p>{hint}</p>
        </div>
        {extra}
      </header>
      {children}
    </section>
  )

  if (!me) {
    return (
      <div className="app">
        <div className="callout">正在检查登录状态…</div>
      </div>
    )
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">✦</span>
          <div>
            <h1>eDream AI 视频创作台</h1>
            <p>一句话创意 → AI 拓展 → 图片 → 视频</p>
          </div>
        </div>
        <div className="topbar-actions">
          <span className="muted small" title={me.oauth_sub}>
            {me.display_name || me.email || me.oauth_sub}
          </span>
          <button className="btn" onClick={doLogout}>
            退出登录
          </button>
          {configs.length > 0 ? (
            <select className="config-select" value={configId ?? ''} onChange={(e) => setConfigId(Number(e.target.value))}>
              {configs.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                  {c.is_default ? '(默认)' : ''}
                </option>
              ))}
            </select>
          ) : (
            <span className="muted small">暂无配置</span>
          )}
          <button className="btn" onClick={() => setConfigOpen(true)}>
            模型配置
          </button>
          <button className="btn" onClick={() => setEditOpen(true)}>
            视频剪辑
          </button>
          <button className="btn" onClick={() => setHistoryOpen(true)}>
            历史记录
          </button>
        </div>
      </header>

      {configs.length === 0 && (
        <div className="callout">
          还没有模型配置。点击
          <button className="link-btn" onClick={() => setConfigOpen(true)}>
            模型配置
          </button>
          填写 new-api 地址与密钥后即可开始创作。
        </div>
      )}

      {error && (
        <div className="alert error">
          {error}
          <button className="icon-btn" onClick={() => setError('')}>
            ✕
          </button>
        </div>
      )}

      <main className="steps">
        {section(
          1,
          '一句话创意',
          '用一句话描述你想看到的画面',
          <textarea
            className="big-input"
            rows={2}
            maxLength={500}
            placeholder="例如:黄昏的海边,一只橘猫追着浪花奔跑"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />,
          creation?.status === 'completed' && (
            <button className="btn" onClick={resetAll}>
              再创作一条
            </button>
          ),
        )}

        {section(
          2,
          '选择风格',
          '决定画面整体调性:风格要点会融入 AI 拓展,负向提示词在生成视频时生效',
          <div className="chips">
            {styles.map((s) => (
              <button
                key={s.id}
                className={`chip ${style === s.name ? 'active' : ''}`}
                title={s.description}
                onClick={() => selectStyle(s)}
              >
                {s.name}
              </button>
            ))}
          </div>,
        )}

        {section(
          3,
          'AI 文本拓展',
          config?.chat_model ? `将创意扩写为视频提示词(当前模型:${config.chat_model})` : '当前配置未设置文本模型,可在模型配置中填写',
          <div className="expand-area">
            <div className="expand-actions">
              <button className="btn primary" disabled={!text.trim() || !config?.chat_model || expanding} onClick={doExpand}>
                {expanding ? 'AI 拓展中…' : '✨ AI 拓展'}
              </button>
              {expanded && (
                <button className="btn" disabled={expanding} onClick={doExpand}>
                  重新拓展
                </button>
              )}
            </div>
            {expanded && (
              <>
                <textarea
                  className="big-input"
                  rows={4}
                  value={expanded}
                  onChange={(e) => setExpanded(e.target.value)}
                />
                <p className="muted small">拓展结果可直接编辑,生成图片与视频都会使用这段提示词。</p>
              </>
            )}
          </div>,
        )}

        {section(
          4,
          '选择画面路径',
          '图生视频更有掌控感,纯文生视频更自由',
          <div className="path-cards">
            <button className={`path-card ${path === 'generate' ? 'active' : ''}`} onClick={() => selectPath('generate')}>
              <span className="path-icon">🖼️</span>
              <strong>先生成图片</strong>
              <span className="muted small">用图片模型生成首帧,再图生视频</span>
            </button>
            <button className={`path-card ${path === 'upload' ? 'active' : ''}`} onClick={() => selectPath('upload')}>
              <span className="path-icon">📤</span>
              <strong>上传参考图</strong>
              <span className="muted small">用自己的图片作为首帧参考</span>
            </button>
            <button className={`path-card ${path === 'none' ? 'active' : ''}`} onClick={() => selectPath('none')}>
              <span className="path-icon">🎬</span>
              <strong>不选图片</strong>
              <span className="muted small">直接文生视频</span>
            </button>
          </div>,
        )}

        {path === 'generate' &&
          section(
            4.5,
            '生成首帧图片',
            config?.image_model ? `当前图片模型:${config.image_model}` : '当前配置未设置图片模型,可在模型配置中填写',
            <div className="image-gen">
              <div className="expand-actions">
                <select value={imageSize} onChange={(e) => setImageSize(e.target.value)}>
                  {IMAGE_SIZES.map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label}
                    </option>
                  ))}
                </select>
                <button className="btn primary" disabled={!expanded.trim() || !config?.image_model || imageBusy} onClick={doGenerateImage}>
                  {imageBusy ? '生成中…(可能需要十几秒)' : image ? '重新生成' : '生成图片'}
                </button>
              </div>
              {image && <img className="preview-image" src={image.url} alt="生成的首帧" />}
            </div>,
          )}

        {path === 'upload' &&
          section(
            4.5,
            '上传参考图',
            '支持 PNG / JPEG / WebP / GIF,不超过 20MB',
            <div className="image-gen">
              <input
                ref={fileRef}
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                hidden
                onChange={(e) => {
                  const f = e.target.files?.[0]
                  if (f) doUpload(f)
                  e.target.value = ''
                }}
              />
              <button className="btn primary" disabled={imageBusy} onClick={() => fileRef.current?.click()}>
                {imageBusy ? '上传中…' : image ? '更换图片' : '选择图片'}
              </button>
              {image && <img className="preview-image" src={image.url} alt="参考图" />}
            </div>,
          )}

        {section(
          5,
          '生成视频',
          config?.video_model ? `当前视频模型:${config.video_model}` : '当前配置未设置视频模型,可在模型配置中填写',
          <div className="video-gen">
            <div className="expand-actions">
              <span className="muted small">时长:</span>
              {DURATIONS.map((d) => (
                <button key={d} className={`chip ${duration === d ? 'active' : ''}`} onClick={() => setDuration(d)} disabled={generating}>
                  {d}s
                </button>
              ))}
              <button className="btn primary big" disabled={!canSubmit} onClick={doSubmit}>
                {generating ? '生成中…' : creation?.status === 'completed' ? '再次生成' : '🚀 生成视频'}
              </button>
            </div>

            {creation && generating && (
              <div className="progress">
                <span className="spinner" />
                {STATUS_TEXT[creation.status] ?? creation.status} · 视频生成通常需要 1-5 分钟,可稍后在历史记录中查看
              </div>
            )}

            {creation?.status === 'failed' && (
              <div className="alert error">
                生成失败:{creation.error}
                <button className="btn" onClick={doSubmit}>
                  重试
                </button>
              </div>
            )}

            {creation?.status === 'completed' && creation.video_url && (
              <div className="video-result">
                <video src={creation.video_url} controls />
                <a className="btn" href={creation.video_url} download={downloadName(creation)} target="_blank" rel="noreferrer">
                  下载视频
                </a>
              </div>
            )}
          </div>,
        )}
      </main>

      {configOpen && (
        <ConfigPanel
          onClose={() => setConfigOpen(false)}
          onSaved={(list) => {
            setConfigs(list)
            setConfigId((prev) => {
              if (prev && list.some((c) => c.id === prev)) return prev
              return list.find((c) => c.is_default)?.id ?? list[0]?.id ?? null
            })
          }}
        />
      )}
      {editOpen && <EditPanel onClose={() => setEditOpen(false)} />}
      {historyOpen && (
        <HistoryPanel
          onClose={() => setHistoryOpen(false)}
          onChanged={() => refreshConfigs().catch(() => {})}
          onDeleted={(id) => {
            if (creation?.id === id) setCreation(null)
          }}
          onReuse={reuseCreation}
        />
      )}
    </div>
  )
}
