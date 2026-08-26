import { useCallback, useEffect, useRef, useState } from 'react'
import { completeVlog, createVlog, getLatestVlog, getVlog, planVlog, retryVlogClip, uploadVlogImages } from '../api'
import { mergeVlogClips, type MergeProgress } from '../services/ffmpegClient'
import { STATUS_TEXT, type ModelConfig, type StylePreset, type VlogImage, type VlogProject, type VlogUploadPlan } from '../types'

const LAST_VLOG_KEY = 'edream_last_vlog_id'

interface Props {
  config: ModelConfig | null
  styles: StylePreset[]
}

type Plan = Omit<VlogUploadPlan, 'images'>

export default function VlogPanel({ config, styles }: Props) {
  const [images, setImages] = useState<VlogImage[]>([])
  const [plan, setPlan] = useState<Plan | null>(null)
  const [style, setStyle] = useState('写实纪录')
  const [description, setDescription] = useState('')
  const [project, setProject] = useState<VlogProject | null>(null)
  const [uploading, setUploading] = useState(false)
  const [planning, setPlanning] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [mergeProgress, setMergeProgress] = useState<MergeProgress | null>(null)
  const [error, setError] = useState('')
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const planRequest = useRef(0)
  const mergingProject = useRef<number | null>(null)

  useEffect(() => {
    if (styles.length && !styles.some((item) => item.name === style)) {
      setStyle(styles.find((item) => item.name === '写实纪录')?.name ?? styles[0].name)
    }
  }, [style, styles])

  useEffect(() => {
    const id = Number(localStorage.getItem(LAST_VLOG_KEY))
    const loadProject = id ? getVlog(id) : getLatestVlog()
    loadProject
      .then((saved) => {
        localStorage.setItem(LAST_VLOG_KEY, String(saved.id))
        setProject(saved)
        setStyle(saved.style)
        setDescription(saved.description)
        setImages(
          saved.image_paths.map((path, index) => ({
            image_path: path,
            url: saved.image_urls[index],
            width: saved.ratio === '9:16' ? 720 : 1280,
            height: saved.ratio === '9:16' ? 1280 : 720,
            order: index,
          })),
        )
        setPlan({
          ratio: saved.ratio,
          target_duration: saved.target_duration,
          clips: saved.clips.map((clip) => ({ reference_paths: clip.reference_paths, duration: clip.duration })),
        })
      })
      .catch(() => localStorage.removeItem(LAST_VLOG_KEY))
  }, [])

  useEffect(() => {
    if (!project || !['pending', 'generating_video'].includes(project.status)) return
    const timer = window.setInterval(() => {
      getVlog(project.id).then(setProject).catch(() => {})
    }, 3000)
    return () => window.clearInterval(timer)
  }, [project])

  const mergeProject = useCallback(async (current: VlogProject) => {
    if (mergingProject.current === current.id) return
    const sources = current.clips.map((clip) => ({ url: clip.video_url ?? '', duration: clip.duration }))
    if (sources.some((source) => !source.url)) {
      setError('片段已完成，但暂时没有可读取的视频地址')
      return
    }
    mergingProject.current = current.id
    setError('')
    try {
      const merged = await mergeVlogClips(sources, current.ratio, setMergeProgress)
      setMergeProgress({ stage: 'encode', ratio: 1, detail: '正在保存成片' })
      const saved = await completeVlog(current.id, merged.blob, merged.duration ?? current.target_duration)
      setProject(saved)
      setMergeProgress(null)
    } catch (cause) {
      setError((cause as Error).message)
      setMergeProgress(null)
      mergingProject.current = null
    }
  }, [])

  useEffect(() => {
    if (project?.status === 'ready_to_merge') mergeProject(project)
  }, [mergeProject, project])

  const updatePlan = async (next: VlogImage[]) => {
    setImages(next.map((image, order) => ({ ...image, order })))
    setPlan(null)
    if (next.length < 2) return
    const requestId = ++planRequest.current
    setPlanning(true)
    try {
      const nextPlan = await planVlog(next.map((image) => image.image_path))
      if (requestId === planRequest.current) setPlan(nextPlan)
    } catch (cause) {
      if (requestId === planRequest.current) setError((cause as Error).message)
    } finally {
      if (requestId === planRequest.current) setPlanning(false)
    }
  }

  const upload = async (files: File[]) => {
    if (files.length < 2 || files.length > 9) {
      setError('一次请选择 2–9 张图片')
      return
    }
    setUploading(true)
    setError('')
    setProject(null)
    localStorage.removeItem(LAST_VLOG_KEY)
    try {
      const result = await uploadVlogImages(files)
      setImages(result.images)
      setPlan({ ratio: result.ratio, target_duration: result.target_duration, clips: result.clips })
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setUploading(false)
    }
  }

  const moveImage = (from: number, to: number) => {
    if (to < 0 || to >= images.length || from === to || project) return
    const next = [...images]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    updatePlan(next)
  }

  const submit = async () => {
    if (!config || !plan || images.length < 2) return
    const confirmed = window.confirm(
      `将生成 ${plan.clips.length} 个场景片段，预计 ${plan.target_duration} 秒，并调用 ${plan.clips.length} 次 ${config.video_model}。确认开始？`,
    )
    if (!confirmed) return
    setSubmitting(true)
    setError('')
    try {
      const created = await createVlog({
        config_id: config.id,
        image_paths: images.map((image) => image.image_path),
        style,
        description: description.trim(),
      })
      setProject(created)
      localStorage.setItem(LAST_VLOG_KEY, String(created.id))
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  const retryClip = async (clipId: number) => {
    if (!project) return
    setError('')
    try {
      setProject(await retryVlogClip(project.id, clipId))
    } catch (cause) {
      setError((cause as Error).message)
    }
  }

  const reset = () => {
    setImages([])
    setPlan(null)
    setProject(null)
    setDescription('')
    setError('')
    setMergeProgress(null)
    mergingProject.current = null
    localStorage.removeItem(LAST_VLOG_KEY)
  }

  const busy = uploading || planning || submitting || !!mergeProgress || !!project?.status.match(/pending|generating_video|ready_to_merge/)
  const canGenerate =
    !!config?.video_model.toLowerCase().includes('seedance-2') && !!plan && images.length >= 2 && !busy && !project
  const imageByPath = new Map(images.map((image) => [image.image_path, image]))

  return (
    <main className="vlog-workspace" aria-busy={busy}>
      <div className="vlog-form-column">
        {error && (
          <div className="alert error" role="alert">
            <span>{error}</span>
            <button className="icon-btn" type="button" aria-label="关闭错误" onClick={() => setError('')}>
              ×
            </button>
          </div>
        )}

        <section className="vlog-section">
          <div className="vlog-section-head">
            <div>
              <h2>图片素材</h2>
              <p>JPG / PNG / WebP · 2–9 张 · 单张不超过 20MB</p>
            </div>
            {images.length > 0 && !project && (
              <button className="btn" type="button" onClick={() => fileRef.current?.click()} disabled={uploading}>
                重新选择
              </button>
            )}
          </div>
          <input
            ref={fileRef}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            multiple
            hidden
            onChange={(event) => {
              const files = Array.from(event.target.files ?? [])
              if (files.length) upload(files)
              event.target.value = ''
            }}
          />
          {images.length === 0 ? (
            <button
              type="button"
              className={`vlog-dropzone ${uploading ? 'loading' : ''}`}
              disabled={uploading}
              onClick={() => fileRef.current?.click()}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault()
                upload(Array.from(event.dataTransfer.files))
              }}
            >
              <span className="upload-mark" aria-hidden="true">＋</span>
              <strong>{uploading ? '正在处理图片…' : '选择或拖入图片'}</strong>
              <span>上传顺序就是 Vlog 叙事顺序</span>
            </button>
          ) : (
            <div className="vlog-image-grid">
              {images.map((image, index) => (
                <figure
                  key={image.image_path}
                  className={`vlog-image-item ${dragIndex === index ? 'dragging' : ''}`}
                  draggable={!project}
                  onDragStart={() => setDragIndex(index)}
                  onDragEnd={() => setDragIndex(null)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={() => {
                    if (dragIndex !== null) moveImage(dragIndex, index)
                    setDragIndex(null)
                  }}
                >
                  <img src={image.url} alt={`素材 ${index + 1}`} />
                  <figcaption>
                    <span>{String(index + 1).padStart(2, '0')}</span>
                    {!project && (
                      <span className="image-actions">
                        <button type="button" title="前移" aria-label={`前移素材 ${index + 1}`} onClick={() => moveImage(index, index - 1)}>↑</button>
                        <button type="button" title="后移" aria-label={`后移素材 ${index + 1}`} onClick={() => moveImage(index, index + 1)}>↓</button>
                        <button type="button" title="删除" aria-label={`删除素材 ${index + 1}`} onClick={() => updatePlan(images.filter((_, itemIndex) => itemIndex !== index))}>×</button>
                      </span>
                    )}
                  </figcaption>
                </figure>
              ))}
            </div>
          )}
        </section>

        <section className="vlog-section vlog-options">
          <div className="field">
            <label htmlFor="vlog-style">风格</label>
            <select id="vlog-style" value={style} onChange={(event) => setStyle(event.target.value)} disabled={!!project}>
              {styles.map((item) => <option key={item.id} value={item.name}>{item.name}</option>)}
            </select>
          </div>
          <div className="field">
            <label htmlFor="vlog-description">一句话补充 <span>可选</span></label>
            <textarea
              id="vlog-description"
              rows={3}
              maxLength={500}
              value={description}
              disabled={!!project}
              placeholder="例如：记录这次高原骑行，从清晨到傍晚"
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>
        </section>

        {project && (
          <section className="vlog-section">
            <div className="vlog-section-head">
              <div>
                <h2>生成进度</h2>
                <p>项目 #{project.id} · {STATUS_TEXT[project.status] ?? project.status}</p>
              </div>
              {project.status === 'completed' && <button className="btn" type="button" onClick={reset}>新建 Vlog</button>}
            </div>
            <div className="clip-progress-list">
              {project.clips.map((clip) => (
                <div className={`clip-progress status-${clip.status}`} key={clip.id}>
                  <span className="clip-index">{clip.sequence}</span>
                  <div>
                    <strong>场景 {clip.sequence} · {clip.duration}s</strong>
                    <span>{STATUS_TEXT[clip.status] ?? clip.status}</span>
                    {clip.error && <small>{clip.error}</small>}
                  </div>
                  {clip.status === 'generating_video' && <span className="spinner" />}
                  {clip.status === 'failed' && clip.retry_count < 1 && (
                    <button className="btn" type="button" onClick={() => retryClip(clip.id)}>重试一次</button>
                  )}
                </div>
              ))}
            </div>
            {mergeProgress && (
              <div className="merge-status">
                <div><span>{mergeProgress.detail}</span><span>{Math.round((mergeProgress.ratio ?? 0) * 100)}%</span></div>
                <progress max="1" value={mergeProgress.ratio ?? 0} />
              </div>
            )}
            {project.status === 'ready_to_merge' && !mergeProgress && error && (
              <button className="btn primary" type="button" onClick={() => mergeProject(project)}>重新合成</button>
            )}
            {project.status === 'completed' && project.final_video_url && (
              <div className="vlog-result">
                <video controls playsInline src={project.final_video_url} />
                <a className="btn primary" href={project.final_video_url} download={`vlog-${project.id}.mp4`}>下载 Vlog</a>
              </div>
            )}
          </section>
        )}
      </div>

      <aside className="vlog-summary">
        <div className="summary-head">
          <span>自动分镜</span>
          {planning && <span className="spinner" />}
        </div>
        {plan ? (
          <>
            <dl className="vlog-metrics">
              <div><dt>场景</dt><dd>{plan.clips.length} 段</dd></div>
              <div><dt>时长</dt><dd>约 {plan.target_duration}s</dd></div>
              <div><dt>画幅</dt><dd>{plan.ratio}</dd></div>
              <div><dt>模型调用</dt><dd>{plan.clips.length} 次</dd></div>
            </dl>
            <div className="scene-plan">
              {plan.clips.map((clip, index) => (
                <div className="scene-row" key={`${clip.reference_paths.join('-')}-${index}`}>
                  <span className="scene-number">{index + 1}</span>
                  <div className="scene-thumbs">
                    {clip.reference_paths.map((path) => {
                      const image = imageByPath.get(path)
                      return image ? <img key={path} src={image.url} alt="" /> : null
                    })}
                  </div>
                  <strong>{clip.duration}s</strong>
                </div>
              ))}
            </div>
          </>
        ) : (
          <div className="summary-empty">上传图片后显示场景与成本</div>
        )}
        {!project && (
          <button className="btn primary full vlog-submit" type="button" disabled={!canGenerate} onClick={submit}>
            {submitting ? '正在创建…' : plan ? `生成约 ${plan.target_duration}s Vlog` : '生成 Vlog'}
          </button>
        )}
        {!config?.video_model.toLowerCase().includes('seedance-2') && (
          <p className="summary-warning">请选择 Seedance 2.0 模型配置</p>
        )}
      </aside>
      {!project && (
        <div className="vlog-mobile-action">
          <button className="btn primary full" type="button" disabled={!canGenerate} onClick={submit}>
            {submitting ? '正在创建…' : plan ? `生成约 ${plan.target_duration}s Vlog` : '生成 Vlog'}
          </button>
        </div>
      )}
    </main>
  )
}
