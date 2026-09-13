import { useEffect, useState } from 'react'
import { createCocreationVideo, expandCocreation, getCreation, getCocreationStatus } from '../api'
import { STATUS_TEXT, type CoCreationStatus, type CoCreationTemplate, type Creation } from '../types'
import { downloadName, notify, requestNotifyPermission } from '../utils'

interface Props {
  status: CoCreationStatus
  /** 次数变化后通知父级刷新状态(含提交成功与任务终态) */
  onStatusChange: (status: CoCreationStatus) => void
}

export default function CoCreationPanel({ status, onStatusChange }: Props) {
  const [templateId, setTemplateId] = useState<number | null>(status.templates[0]?.id ?? null)
  const [text, setText] = useState('')
  const [expanded, setExpanded] = useState('')
  const [expanding, setExpanding] = useState(false)
  const [creation, setCreation] = useState<Creation | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const template: CoCreationTemplate | null =
    status.templates.find((t) => t.id === templateId) ?? null
  const generating = creation?.status === 'pending' || creation?.status === 'generating_video'
  const quotaLeft = Math.max(0, status.limit - status.used)

  // 生成中轮询任务状态;完成/失败时刷新配额并发系统通知
  useEffect(() => {
    if (!creation || !generating) return
    const timer = setInterval(async () => {
      try {
        const latest = await getCreation(creation.id)
        if (latest.status !== creation.status) {
          if (latest.status === 'completed') notify('✅ 共创视频生成完成', latest.input_text.slice(0, 60))
          else if (latest.status === 'failed') notify('❌ 共创视频生成失败', (latest.error ?? '').slice(0, 80))
          getCocreationStatus().then(onStatusChange).catch(() => {})
        }
        setCreation(latest)
      } catch {
        /* 网络抖动时继续轮询 */
      }
    }, 3000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creation])

  const switchTemplate = (t: CoCreationTemplate) => {
    setTemplateId(t.id)
    setExpanded('')
    setError('')
  }

  const doExpand = async () => {
    if (!template || !text.trim()) return
    setExpanding(true)
    setError('')
    try {
      const { expanded_prompt } = await expandCocreation(template.id, text.trim())
      setExpanded(expanded_prompt)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setExpanding(false)
    }
  }

  const canSubmit =
    !!template &&
    !!text.trim() &&
    quotaLeft > 0 &&
    !submitting &&
    !generating

  const doSubmit = async () => {
    if (!template || !canSubmit) return
    setSubmitting(true)
    setError('')
    requestNotifyPermission()
    try {
      const c = await createCocreationVideo({
        template_id: template.id,
        text: text.trim(),
        expanded_prompt: expanded.trim() || undefined,
      })
      setCreation(c)
      getCocreationStatus().then(onStatusChange).catch(() => {})
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  const resetAll = () => {
    setText('')
    setExpanded('')
    setCreation(null)
    setError('')
  }

  const stepDone = (n: number) =>
    (n === 1 && !!template) || (n === 2 && text.trim().length > 0) || (n === 3 && creation?.status === 'completed')

  const section = (n: number, title: string, hint: string, children: React.ReactNode, extra?: React.ReactNode) => (
    <section className={`step ${stepDone(n) ? 'done' : ''}`}>
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

  return (
    <main className="steps">
      {error && (
        <div className="alert error">
          {error}
          <button className="icon-btn" onClick={() => setError('')}>
            ✕
          </button>
        </div>
      )}

      {section(
        1,
        '选择企业模版',
        `企业统一的画面要求会自动应用 · 已保留 ${status.used}/${status.limit} 个`,
        <div className="chips">
          {status.templates.map((t) => (
            <button
              key={t.id}
              className={`chip ${templateId === t.id ? 'active' : ''}`}
              title={`${t.description}${t.prompt ? `\n画面要求:${t.prompt}` : ''}`}
              onClick={() => switchTemplate(t)}
            >
              {t.name}
            </button>
          ))}
        </div>,
        template && (
          <span className="muted small">
            {template.video_model} · {template.duration}s
          </span>
        ),
      )}

      {template && (
        <>
          {section(
            2,
            '一句话创意',
            '描述你想看到的画面,会与企业模版的画面要求结合后生成',
            <div className="expand-area">
              <textarea
                className="big-input"
                rows={2}
                maxLength={500}
                placeholder="例如:清晨的咖啡店,阳光洒进落地窗"
                value={text}
                onChange={(e) => setText(e.target.value)}
              />
              {template.chat_model && (
                <div className="expand-actions">
                  <button className="btn primary" disabled={!text.trim() || expanding} onClick={doExpand}>
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

          {section(
            3,
            '生成视频',
            `时长 ${template.duration}s(由企业模版设定) · 使用企业网关额度,每人限 ${status.limit} 个`,
            <div className="video-gen">
              <div className="expand-actions">
                <button className="btn primary big" disabled={!canSubmit} onClick={doSubmit}>
                  {generating ? '生成中…' : creation?.status === 'completed' ? '再次生成' : '🚀 生成共创视频'}
                </button>
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
        </>
      )}
    </main>
  )
}
