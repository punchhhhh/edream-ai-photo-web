import { useEffect, useRef, useState } from 'react'
import {
  composeCocreationFirstFrame,
  createCocreationVideo,
  expandCocreation,
  getCreation,
  getCocreationStatus,
  uploadImage,
} from '../api'
import {
  STATUS_TEXT,
  type CoCreationFirstFrame,
  type CoCreationStatus,
  type CoCreationTemplate,
  type Creation,
} from '../types'
import { downloadName, notify, requestNotifyPermission } from '../utils'

interface Props {
  status: CoCreationStatus
  grantId: number
  /** 次数变化后通知父级刷新状态(含提交成功与任务终态) */
  onStatusChange: (status: CoCreationStatus) => void
}

export default function CoCreationPanel({ status, grantId, onStatusChange }: Props) {
  const [templateId, setTemplateId] = useState<number | null>(status.templates[0]?.id ?? null)
  const [text, setText] = useState('')
  const [expanded, setExpanded] = useState('')
  const [expanding, setExpanding] = useState(false)
  const [photoPath, setPhotoPath] = useState<string | null>(null)
  const [photoUrl, setPhotoUrl] = useState<string | null>(null)
  const [photoSkipped, setPhotoSkipped] = useState(false)
  const [uploadingPhoto, setUploadingPhoto] = useState(false)
  const [frame, setFrame] = useState<CoCreationFirstFrame | null>(null)
  const [framing, setFraming] = useState(false)
  const [creation, setCreation] = useState<Creation | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)

  const template: CoCreationTemplate | null =
    status.templates.find((t) => t.id === templateId) ?? null
  const generating = creation?.status === 'pending' || creation?.status === 'generating_video'
  const quotaLeft = Math.max(0, status.limit - status.used)
  const grantUsable = status.available

  const needsPhoto = !!template && template.member_photo !== 'none'
  const photoReady = !!photoPath || (template?.member_photo === 'optional' && photoSkipped)
  // 有成员照片或绑定了 IP 形象参考图时,先合成「合拍首帧」再图生视频
  const needsFirstFrame = !!template && (!!photoPath || template.character_asset_count > 0)
  const photoHint = template?.member_photo_hint || '上传一张清晰的正面照,和企业 IP 同框出镜'
  const confirmMode = template?.first_frame_confirm !== false

  // 生成中轮询任务状态;完成/失败时刷新配额并发系统通知
  useEffect(() => {
    if (!creation || !generating) return
    const timer = setInterval(async () => {
      try {
        const latest = await getCreation(creation.id)
        if (latest.status !== creation.status) {
          if (latest.status === 'completed') notify('✅ 共创视频生成完成', latest.input_text.slice(0, 60))
          else if (latest.status === 'failed') notify('❌ 共创视频生成失败', (latest.error ?? '').slice(0, 80))
          getCocreationStatus(grantId).then(onStatusChange).catch(() => {})
        }
        setCreation(latest)
      } catch {
        /* 网络抖动时继续轮询 */
      }
    }, 3000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creation, grantId, onStatusChange])

  const switchTemplate = (t: CoCreationTemplate) => {
    setTemplateId(t.id)
    setExpanded('')
    setPhotoPath(null)
    setPhotoUrl(null)
    setPhotoSkipped(false)
    setFrame(null)
    setCreation(null)
    setError('')
  }

  const doUploadPhoto = async (file: File) => {
    setUploadingPhoto(true)
    setError('')
    try {
      const media = await uploadImage(file)
      setPhotoPath(media.image_path)
      setPhotoUrl(media.url)
      setPhotoSkipped(false)
      setFrame(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setUploadingPhoto(false)
    }
  }

  const doExpand = async () => {
    if (!template || !text.trim()) return
    setExpanding(true)
    setError('')
    try {
      const { expanded_prompt } = await expandCocreation(grantId, template.id, text.trim())
      setExpanded(expanded_prompt)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setExpanding(false)
    }
  }

  const canComposeFrame =
    grantUsable && !!template && needsFirstFrame && (!needsPhoto || photoReady) && !framing && quotaLeft > 0

  const doComposeFrame = async (): Promise<string | null> => {
    if (!template || !canComposeFrame) return null
    setFraming(true)
    setError('')
    try {
      const result = await composeCocreationFirstFrame(grantId, template.id, {
        text: text.trim() || undefined,
        member_photo_path: photoPath ?? undefined,
      })
      setFrame(result)
      return result.image_path
    } catch (e) {
      setError((e as Error).message)
      return null
    } finally {
      setFraming(false)
    }
  }

  const doComposeAndAutoSubmit = async () => {
    // 企业关闭首帧确认时:合成完自动继续生成视频,成员只等结果
    const framePath = await doComposeFrame()
    if (framePath) await doSubmit(framePath)
  }

  const canSubmit =
    !!template &&
    grantUsable &&
    !!text.trim() &&
    quotaLeft > 0 &&
    !submitting &&
    !generating &&
    (!needsPhoto || photoReady) &&
    (!needsFirstFrame || !!frame)

  const doSubmit = async (framePath?: string) => {
    if (!template) return
    const effectiveFrame = framePath ?? frame?.image_path
    if (template.member_photo === 'required' && !effectiveFrame) return
    setSubmitting(true)
    setError('')
    requestNotifyPermission()
    try {
      const c = await createCocreationVideo({
        grant_id: grantId,
        template_id: template.id,
        text: text.trim(),
        expanded_prompt: expanded.trim() || undefined,
        first_frame_path: effectiveFrame || undefined,
      })
      setCreation(c)
      getCocreationStatus(grantId).then(onStatusChange).catch(() => {})
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  const resetAll = () => {
    setText('')
    setExpanded('')
    setFrame(null)
    setCreation(null)
    setError('')
  }

  // 步骤编号按实际展示的步骤递增(不出镜的模版没有照片步骤)
  let stepNo = 0
  const nextStepNo = () => ++stepNo

  const section = (
    n: number,
    done: boolean,
    title: string,
    hint: string,
    children: React.ReactNode,
    extra?: React.ReactNode,
  ) => (
    <section className={`step ${done ? 'done' : ''}`}>
      <header>
        <span className="step-num">{n}</span>
        <div>
          <h3>{title}</h3>
          <p>{hint}</p>
        </div>
        {extra}
      </header>
      {children}
    </section>
  )

  const videoStep = template && (
    <div className="video-gen">
      <div className="expand-actions">
        {confirmMode || !needsFirstFrame ? (
          <button className="btn primary big" disabled={!canSubmit} onClick={() => doSubmit()}>
            {generating ? '生成中…' : creation?.status === 'completed' ? '再次生成' : '🚀 生成共创视频'}
          </button>
        ) : (
          <span className="muted small">画面确认已由企业关闭,点击「一键合拍」后自动生成</span>
        )}
        {creation?.status === 'completed' && (
          <button className="btn" onClick={resetAll}>
            再创作一条
          </button>
        )}
        {quotaLeft === 0 && <span className="muted small">共创次数已用完</span>}
      </div>

      {creation && generating && (
        <div className="progress">
          <span className="spinner" />
          {STATUS_TEXT[creation.status] ?? creation.status} · 视频生成通常需要 1-5 分钟,完成后可在历史记录中查看
        </div>
      )}

      {creation?.status === 'failed' && (
        <div className="alert error">
          生成失败:{creation.error}
          <button className="btn" onClick={() => doSubmit()}>
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
    </div>
  )

  return (
    <main className="steps">
      {!grantUsable && status.reason && <div className="callout">{status.reason}</div>}
      {error && (
        <div className="alert error">
          {error}
          <button className="icon-btn" onClick={() => setError('')}>
            ✕
          </button>
        </div>
      )}

      {section(
        nextStepNo(),
        !!template,
        '选择互动场景',
        `和企业 IP 一起拍 · 已保留 ${status.used}/${status.limit} 个`,
        <div className="template-cards">
          {status.templates.map((t) => (
            <button
              key={t.id}
              className={`template-card ${templateId === t.id ? 'active' : ''}`}
              onClick={() => switchTemplate(t)}
            >
              {t.cover_url ? (
                <img className="template-cover" src={t.cover_url} alt={t.name} loading="lazy" />
              ) : (
                <div className="template-cover placeholder">🎬</div>
              )}
              <div className="template-card-body">
                <strong>{t.name}</strong>
                {t.description && <span>{t.description}</span>}
                <span className="template-meta">
                  {t.duration}s
                  {t.member_photo === 'required' ? ' · 需出镜' : t.member_photo === 'optional' ? ' · 可出镜' : ''}
                </span>
              </div>
            </button>
          ))}
        </div>,
      )}

      {template && needsPhoto && (
        <section className={`step ${photoReady ? 'done' : ''}`}>
          <header>
            <span className="step-num">{nextStepNo()}</span>
            <div>
              <h3>上传你的照片</h3>
              <p>{photoHint}</p>
            </div>
            {photoReady && <span className="muted small">已就绪</span>}
          </header>
          <div className="photo-upload">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/png,image/jpeg,image/webp,image/gif"
              hidden
              onChange={(e) => {
                const file = e.target.files?.[0]
                if (file) void doUploadPhoto(file)
                e.target.value = ''
              }}
            />
            {photoUrl ? (
              <div className="photo-preview">
                <img src={photoUrl} alt="我的照片" />
                <button className="btn" onClick={() => fileInputRef.current?.click()}>
                  换一张
                </button>
              </div>
            ) : (
              <div className="photo-actions">
                <button
                  className="btn primary"
                  disabled={uploadingPhoto}
                  onClick={() => fileInputRef.current?.click()}
                >
                  {uploadingPhoto ? '上传中…' : '📷 选择照片'}
                </button>
                {template.member_photo === 'optional' && (
                  <button className="btn" disabled={uploadingPhoto} onClick={() => setPhotoSkipped(true)}>
                    不出镜,跳过
                  </button>
                )}
              </div>
            )}
          </div>
        </section>
      )}

      {template && (
        <>
          {section(
            nextStepNo(),
            text.trim().length > 0,
            '一句话创意',
            '描述你想和 IP 的互动,会与企业模版的画面要求结合后生成',
            <div className="expand-area">
              {template.interaction_options.length > 0 && (
                <div className="chips">
                  {template.interaction_options.map((option) => (
                    <button
                      key={option}
                      className={`chip ${text === option ? 'active' : ''}`}
                      onClick={() => setText(option)}
                    >
                      {option}
                    </button>
                  ))}
                </div>
              )}
              <textarea
                className="big-input"
                rows={2}
                maxLength={500}
                placeholder="例如:清晨的咖啡店,和 IP 一起比心合影"
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
              {template.chat_model && (
                <div className="expand-actions">
                  <button className="btn primary" disabled={!grantUsable || !text.trim() || expanding} onClick={doExpand}>
                    {expanding ? 'AI 拓展中…' : expanded ? '重新拓展' : '✨ AI 拓展'}
                  </button>
                  <span className="muted small">用企业网关密钥把创意扩写为视频提示词,可编辑</span>
                </div>
              )}
              {expanded && (
                <textarea className="big-input" rows={4} value={expanded} onChange={(e) => setExpanded(e.target.value)} />
              )}
            </div>,
          )}

          {needsFirstFrame && (
            <section className={`step ${frame ? 'done' : ''}`}>
              <header>
                <span className="step-num">{nextStepNo()}</span>
                <div>
                  <h3>合拍画面</h3>
                  <p>
                    {photoPath
                      ? confirmMode
                        ? '先合成你和 IP 的同框画面,满意后再生成视频'
                        : '自动合成你和 IP 的同框画面并生成视频,无需确认'
                      : confirmMode
                        ? '未上传照片时,将仅用企业 IP 形象合成首帧'
                        : '自动用企业 IP 形象合成首帧并生成视频,无需确认'}
                  </p>
                </div>
                {frame && <span className="muted small">已生成</span>}
              </header>
              <div className="frame-gen">
                <div className="expand-actions">
                  {confirmMode ? (
                    <>
                      <button
                        className="btn primary"
                        disabled={!canComposeFrame || !!frame}
                        onClick={() => doComposeFrame()}
                      >
                        {framing ? '合成中…' : frame ? '已生成' : '📷 生成合拍画面'}
                      </button>
                      {frame && (
                        <button className="btn" disabled={framing} onClick={() => doComposeFrame()}>
                          不满意,重新生成
                        </button>
                      )}
                    </>
                  ) : (
                    <button
                      className="btn primary"
                      disabled={!canComposeFrame || generating}
                      onClick={doComposeAndAutoSubmit}
                    >
                      {framing ? '合成中…' : generating ? '视频生成中…' : '🚀 一键合拍'}
                    </button>
                  )}
                </div>
                {framing && (
                  <div className="progress">
                    <span className="spinner" />
                    正在合成你和 IP 的同框画面,通常需要十几秒
                  </div>
                )}
                {frame && (
                  <div className="frame-preview">
                    <img src={frame.url} alt="合拍画面" />
                  </div>
                )}
              </div>
            </section>
          )}

          {section(
            nextStepNo(),
            creation?.status === 'completed',
            '生成视频',
            `时长 ${template.duration}s(由企业模版设定) · 使用企业网关额度,每人限 ${status.limit} 个`,
            videoStep,
          )}
        </>
      )}
    </main>
  )
}
