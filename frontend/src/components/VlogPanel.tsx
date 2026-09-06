import { useCallback, useEffect, useRef, useState } from 'react'
import {
  abandonVlog,
  completeVlog,
  createVlog,
  getLatestVlog,
  getVlog,
  planVlog,
  retryVlogClip,
  uploadVlogImages,
} from '../api'
import {
  mergeImageMotionVlog,
  mergeVlogClips,
  VLOG_TRANSITION_SECONDS,
  type MergeProgress,
} from '../services/ffmpegClient'
import {
  STATUS_TEXT,
  type ModelConfig,
  type StylePreset,
  type VlogMotionTemplate,
  type VlogImage,
  type VlogProject,
  type VlogTransition,
  type VlogUploadPlan,
  VLOG_MOTION_TEMPLATES,
  VLOG_TRANSITIONS,
} from '../types'

const LAST_VLOG_KEY = 'edream_last_vlog_id'

interface Props {
  config: ModelConfig | null
  styles: StylePreset[]
}

type Plan = Omit<VlogUploadPlan, 'images'>
type VlogSourceMode = 'ai' | 'motion'

interface LocalImage {
  id: string
  file: File
  url: string
  width: number
  height: number
  order: number
}

export default function VlogPanel({ config, styles }: Props) {
  const [sourceMode, setSourceMode] = useState<VlogSourceMode>('ai')
  const [images, setImages] = useState<VlogImage[]>([])
  const [localImages, setLocalImages] = useState<LocalImage[]>([])
  const [plan, setPlan] = useState<Plan | null>(null)
  const [style, setStyle] = useState('写实纪录')
  const [transitionStyle, setTransitionStyle] = useState<VlogTransition>('fade')
  const [motionTemplate, setMotionTemplate] = useState<VlogMotionTemplate>('kenburns_in')
  const [imageDuration, setImageDuration] = useState(4)
  const [description, setDescription] = useState('')
  const [project, setProject] = useState<VlogProject | null>(null)
  const [localResult, setLocalResult] = useState<{ url: string; duration: number } | null>(null)
  const [uploading, setUploading] = useState(false)
  const [planning, setPlanning] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [localRendering, setLocalRendering] = useState(false)
  const [merging, setMerging] = useState(false)
  const [mergeProgress, setMergeProgress] = useState<MergeProgress | null>(null)
  const [error, setError] = useState('')
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [localDragIndex, setLocalDragIndex] = useState<number | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const localFileRef = useRef<HTMLInputElement>(null)
  const localImagesRef = useRef<LocalImage[]>([])
  const localResultRef = useRef<{ url: string; duration: number } | null>(null)
  const planRequest = useRef(0)
  const mergingProject = useRef<number | null>(null)

  useEffect(() => {
    localImagesRef.current = localImages
  }, [localImages])

  useEffect(() => {
    localResultRef.current = localResult
  }, [localResult])

  useEffect(() => () => {
    localImagesRef.current.forEach((image) => URL.revokeObjectURL(image.url))
    if (localResultRef.current) URL.revokeObjectURL(localResultRef.current.url)
  }, [])

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
        setTransitionStyle(saved.transition_style ?? 'fade')
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
      getVlog(project.id)
        .then((latest) => {
          setProject(latest)
          if (!['pending', 'generating_video'].includes(latest.status)) mergingProject.current = null
        })
        .catch(() => {})
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
    setMerging(true)
    setError('')
    try {
      const merged = await mergeVlogClips(sources, current.ratio, current.transition_style, setMergeProgress)
      setMergeProgress({ stage: 'encode', ratio: 1, detail: '正在保存成片' })
      const saved = await completeVlog(current.id, merged.blob, merged.duration ?? current.target_duration)
      setProject(saved)
      setMergeProgress(null)
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setMergeProgress(null)
      mergingProject.current = null
      setMerging(false)
    }
  }, [])

  const abandonProject = async (current: VlogProject) => {
    const message = ['pending', 'generating_video'].includes(current.status)
      ? '放弃后将停止该项目，正在生成的片段会作废。确定放弃？'
      : '片段已生成但尚未合成，放弃后需要重新创建项目。确定放弃？'
    if (!window.confirm(message)) return
    setError('')
    try {
      await abandonVlog(current.id)
      reset()
    } catch (cause) {
      setError((cause as Error).message)
    }
  }

  const clearLocalResult = () => {
    if (localResultRef.current) URL.revokeObjectURL(localResultRef.current.url)
    localResultRef.current = null
    setLocalResult(null)
  }

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

  const readLocalImage = (file: File, order: number): Promise<LocalImage> =>
    new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file)
      const image = new Image()
      image.onload = () => resolve({ id: crypto.randomUUID(), file, url, width: image.naturalWidth, height: image.naturalHeight, order })
      image.onerror = () => {
        URL.revokeObjectURL(url)
        reject(new Error(`${file.name} 不是可读取的图片`))
      }
      image.src = url
    })

  const uploadLocal = async (files: File[]) => {
    if (files.length < 1 || files.length > 9) {
      setError('一次请选择 1–9 张图片')
      return
    }
    const allowed = files.every((file) => ['image/jpeg', 'image/png', 'image/webp'].includes(file.type))
    if (!allowed) {
      setError('本地动效仅支持 PNG / JPEG / WebP 图片')
      return
    }
    if (files.some((file) => file.size > 20 * 1024 * 1024)) {
      setError('单张图片不能超过 20MB')
      return
    }
    setUploading(true)
    setError('')
    setProject(null)
    // 本地动效不依赖服务端项目，清掉记录避免旧项目在刷新后复活
    localStorage.removeItem(LAST_VLOG_KEY)
    clearLocalResult()
    const next: LocalImage[] = []
    try {
      for (let index = 0; index < files.length; index++) {
        next.push(await readLocalImage(files[index], index))
      }
      localImagesRef.current.forEach((image) => URL.revokeObjectURL(image.url))
      setLocalImages(next)
    } catch (cause) {
      next.forEach((image) => URL.revokeObjectURL(image.url))
      setError((cause as Error).message)
    } finally {
      setUploading(false)
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

  const moveLocalImage = (from: number, to: number) => {
    if (to < 0 || to >= localImages.length || from === to || localResult) return
    const next = [...localImages]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    clearLocalResult()
    setLocalImages(next.map((image, order) => ({ ...image, order })))
  }

  const removeLocalImage = (index: number) => {
    const removed = localImages[index]
    if (removed) URL.revokeObjectURL(removed.url)
    clearLocalResult()
    setLocalImages(localImages.filter((_, itemIndex) => itemIndex !== index).map((image, order) => ({ ...image, order })))
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
        transition_style: transitionStyle,
      })
      setProject(created)
      localStorage.setItem(LAST_VLOG_KEY, String(created.id))
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  const renderLocal = async () => {
    if (localImages.length < 1 || mergeProgress || localRendering) return
    const ratio = inferLocalRatio()
    setError('')
    clearLocalResult()
    setLocalRendering(true)
    try {
      const merged = await mergeImageMotionVlog(
        localImages.map((image) => ({ url: image.url, mime: image.file.type })),
        ratio,
        motionTemplate,
        imageDuration,
        transitionStyle,
        setMergeProgress,
      )
      setLocalResult({ url: URL.createObjectURL(merged.blob), duration: merged.duration ?? 0 })
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setMergeProgress(null)
      setLocalRendering(false)
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
    localImagesRef.current.forEach((image) => URL.revokeObjectURL(image.url))
    if (localResultRef.current) URL.revokeObjectURL(localResultRef.current.url)
    setImages([])
    setPlan(null)
    setProject(null)
    setLocalImages([])
    setLocalResult(null)
    setDescription('')
    setError('')
    setMergeProgress(null)
    mergingProject.current = null
    localStorage.removeItem(LAST_VLOG_KEY)
  }

  const busy =
    uploading || planning || submitting || localRendering || !!mergeProgress ||
    !!project && ['pending', 'generating_video'].includes(project.status)
  // 只有进行中的项目才锁定来源切换；failed/completed/cancelled 是终态，可随时改用本地动效
  const activeProject = !!project && ['pending', 'generating_video', 'ready_to_merge'].includes(project.status)
  const canGenerate =
    !busy && !project && (sourceMode === 'motion'
      ? localImages.length >= 1
      : !!config?.video_model.toLowerCase().includes('seedance-2') && !!plan && images.length >= 2)
  const imageByPath = new Map(images.map((image) => [image.image_path, image]))
  const inferLocalRatio = () => {
    const portrait = localImages.filter((image) => image.height > image.width).length
    return portrait > localImages.length / 2 ? '9:16' : '16:9'
  }
  const localRatio = localImages.length ? inferLocalRatio() : '16:9'
  const localTotalDuration = Math.max(
    1,
    imageDuration * localImages.length - VLOG_TRANSITION_SECONDS * Math.max(0, localImages.length - 1),
  )

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
              <h2>{sourceMode === 'ai' ? 'AI 视频片段' : '本地图片动效'}</h2>
              <p>{sourceMode === 'ai' ? '图片会提交给视频模型生成连续场景' : '图片只在浏览器本地处理，不上传服务器'}</p>
            </div>
            <div className="vlog-source-switch" role="group" aria-label="选择片段来源">
              <button
                className={sourceMode === 'ai' ? 'selected' : ''}
                type="button"
                aria-pressed={sourceMode === 'ai'}
                disabled={busy || activeProject}
                onClick={() => setSourceMode('ai')}
              >
                AI 视频片段
              </button>
              <button
                className={sourceMode === 'motion' ? 'selected' : ''}
                type="button"
                aria-pressed={sourceMode === 'motion'}
                disabled={busy || activeProject}
                onClick={() => setSourceMode('motion')}
              >
                本地图片动效
              </button>
            </div>
          </div>
          {sourceMode === 'ai' && images.length > 0 && !project && (
            <button className="btn" type="button" onClick={() => fileRef.current?.click()} disabled={uploading}>
              重新选择
            </button>
          )}
          {sourceMode === 'motion' && localImages.length > 0 && !project && (
            <button className="btn" type="button" onClick={() => localFileRef.current?.click()} disabled={uploading}>
              重新选择
            </button>
          )}
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
          <input
            ref={localFileRef}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            multiple
            hidden
            onChange={(event) => {
              const files = Array.from(event.target.files ?? [])
              if (files.length) uploadLocal(files)
              event.target.value = ''
            }}
          />
          {sourceMode === 'ai' && images.length === 0 ? (
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
              <strong>{uploading ? '正在上传图片…' : '选择或拖入图片'}</strong>
              <span>上传顺序就是 Vlog 叙事顺序 · 2–9 张 · 单张不超过 20MB</span>
            </button>
          ) : sourceMode === 'motion' && localImages.length === 0 ? (
            <button
              type="button"
              className={`vlog-dropzone ${uploading ? 'loading' : ''}`}
              disabled={uploading}
              onClick={() => localFileRef.current?.click()}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault()
                if (uploading) return
                uploadLocal(Array.from(event.dataTransfer.files))
              }}
            >
              <span className="upload-mark" aria-hidden="true">＋</span>
              <strong>{uploading ? '正在读取图片…' : '选择或拖入图片'}</strong>
              <span>本地生成推拉、平移和漂移动效 · 1–9 张</span>
            </button>
          ) : sourceMode === 'ai' ? (
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
          ) : (
            <div className="vlog-image-grid">
              {localImages.map((image, index) => (
                <figure
                  key={image.id}
                  className={`vlog-image-item ${localDragIndex === index ? 'dragging' : ''}`}
                  draggable={!localResult}
                  onDragStart={() => setLocalDragIndex(index)}
                  onDragEnd={() => setLocalDragIndex(null)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={() => {
                    if (localDragIndex !== null) moveLocalImage(localDragIndex, index)
                    setLocalDragIndex(null)
                  }}
                >
                  <img src={image.url} alt={`素材 ${index + 1}`} />
                  <figcaption>
                    <span>{String(index + 1).padStart(2, '0')}</span>
                    {!localResult && (
                      <span className="image-actions">
                        <button type="button" title="前移" aria-label={`前移素材 ${index + 1}`} onClick={() => moveLocalImage(index, index - 1)}>↑</button>
                        <button type="button" title="后移" aria-label={`后移素材 ${index + 1}`} onClick={() => moveLocalImage(index, index + 1)}>↓</button>
                        <button type="button" title="删除" aria-label={`删除素材 ${index + 1}`} onClick={() => removeLocalImage(index)}>×</button>
                      </span>
                    )}
                  </figcaption>
                </figure>
              ))}
            </div>
          )}
        </section>

        <section className="vlog-section vlog-options">
          {sourceMode === 'ai' ? (
            <>
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
            </>
          ) : (
            <>
              <div className="field motion-template-field">
                <label>图片动效模板</label>
                <div className="motion-grid" role="radiogroup" aria-label="选择图片动效模板">
                  {VLOG_MOTION_TEMPLATES.map((item) => (
                    <button
                      key={item.key}
                      className={`motion-option ${motionTemplate === item.key ? 'selected' : ''}`}
                      type="button"
                      role="radio"
                      aria-checked={motionTemplate === item.key}
                      disabled={!!mergeProgress}
                      onClick={() => setMotionTemplate(item.key)}
                    >
                      <span className={`motion-preview motion-${item.key}`} aria-hidden="true" />
                      <strong>{item.label}</strong>
                      <span>{item.description}</span>
                    </button>
                  ))}
                </div>
              </div>
              <div className="field">
                <label htmlFor="vlog-image-duration">每张图片时长</label>
                <select
                  id="vlog-image-duration"
                  value={imageDuration}
                  disabled={!!mergeProgress}
                  onChange={(event) => setImageDuration(Number(event.target.value))}
                >
                  <option value="3">3 秒</option>
                  <option value="4">4 秒</option>
                  <option value="5">5 秒</option>
                  <option value="6">6 秒</option>
                </select>
              </div>
            </>
          )}
          <div className="field transition-field">
            <label>转场模板</label>
            <div className="transition-grid" role="radiogroup" aria-label="选择转场模板">
              {VLOG_TRANSITIONS.map((item) => (
                <button
                  key={item.key}
                  className={`transition-option ${transitionStyle === item.key ? 'selected' : ''}`}
                  type="button"
                  role="radio"
                  aria-checked={transitionStyle === item.key}
                  disabled={!!project || !!mergeProgress}
                  onClick={() => {
                    if (sourceMode === 'motion') clearLocalResult()
                    setTransitionStyle(item.key)
                  }}
                >
                  <strong>{item.label}</strong>
                  <span>{item.description}</span>
                </button>
              ))}
            </div>
          </div>
        </section>

        {project && (
          <section className="vlog-section">
            <div className="vlog-section-head">
              <div>
                <h2>生成进度</h2>
                <p>
                  项目 #{project.id} · {STATUS_TEXT[project.status] ?? project.status} ·
                  {' '}{VLOG_TRANSITIONS.find((item) => item.key === project.transition_style)?.label ?? project.transition_style}
                </p>
              </div>
              {['completed', 'cancelled', 'failed'].includes(project.status) && (
                <button className="btn" type="button" onClick={reset}>新建 Vlog</button>
              )}
              {['pending', 'generating_video'].includes(project.status) && (
                <button
                  className="btn"
                  type="button"
                  disabled={merging}
                  onClick={() => abandonProject(project)}
                >
                  放弃项目
                </button>
              )}
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
            {project.error && project.status === 'ready_to_merge' && (
              <div className="merge-error" role="alert">{project.error}</div>
            )}
            {project.status === 'ready_to_merge' && !mergeProgress && (
              <div className="merge-choice">
                <div className="merge-choice-head">
                  <strong>片段已就绪</strong>
                  <span>点击后在当前浏览器本地完成转场合成</span>
                </div>
                <div className="merge-choice-actions">
                  <button
                    className="btn primary"
                    type="button"
                    disabled={merging}
                    onClick={() => mergeProject(project)}
                  >
                    浏览器合成
                  </button>
                  <button
                    className="btn"
                    type="button"
                    disabled={merging}
                    onClick={() => abandonProject(project)}
                  >
                    放弃项目
                  </button>
                </div>
                <p className="merge-choice-note">
                  首次合成需要加载约 32MB 的 FFmpeg.wasm；之后会在当前页面缓存。
                </p>
              </div>
            )}
            {project.status === 'completed' && project.final_video_url && (
              <div className="vlog-result">
                <video controls playsInline src={project.final_video_url} />
                <a className="btn primary" href={project.final_video_url} download={`vlog-${project.id}.mp4`}>下载 Vlog</a>
              </div>
            )}
          </section>
        )}

        {sourceMode === 'motion' && localResult && (
          <section className="vlog-section local-result-section">
            <div className="vlog-section-head">
              <div>
                <h2>本地 Vlog 已生成</h2>
                <p>{localRatio} · 约 {localResult.duration.toFixed(1)} 秒 · 未上传服务器</p>
              </div>
              <button className="btn" type="button" onClick={renderLocal} disabled={localRendering || !!mergeProgress}>重新生成</button>
            </div>
            <div className="vlog-result">
              <video controls playsInline src={localResult.url} />
              <a className="btn primary" href={localResult.url} download="local-vlog.mp4">下载 Vlog</a>
            </div>
          </section>
        )}
      </div>

      <aside className="vlog-summary">
        <div className="summary-head">
          <span>{sourceMode === 'ai' ? '自动分镜' : '本地动效预览'}</span>
          {planning && <span className="spinner" />}
        </div>
        {sourceMode === 'ai' && plan ? (
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
        ) : sourceMode === 'motion' && localImages.length > 0 ? (
          <>
            <dl className="vlog-metrics">
              <div><dt>图片</dt><dd>{localImages.length} 张</dd></div>
              <div><dt>成片</dt><dd>约 {localTotalDuration.toFixed(1)}s</dd></div>
              <div><dt>画幅</dt><dd>{localRatio}</dd></div>
              <div><dt>处理</dt><dd>本地浏览器</dd></div>
            </dl>
            <div className="scene-plan local-scene-plan">
              {localImages.map((image, index) => (
                <div className="scene-row" key={image.id}>
                  <span className="scene-number">{index + 1}</span>
                  <div className="scene-thumbs"><img src={image.url} alt="" /></div>
                  <strong>{imageDuration}s</strong>
                </div>
              ))}
            </div>
          </>
        ) : (
          <div className="summary-empty">上传图片后显示场景与成本</div>
        )}
        {!project && sourceMode === 'ai' && (
          <button className="btn primary full vlog-submit" type="button" disabled={!canGenerate} onClick={submit}>
            {submitting ? '正在创建…' : plan ? `生成约 ${plan.target_duration}s Vlog` : '生成 Vlog'}
          </button>
        )}
        {!project && sourceMode === 'motion' && (
          <button className="btn primary full vlog-submit" type="button" disabled={!canGenerate} onClick={renderLocal}>
            {localRendering ? '正在生成…' : localImages.length >= 1 ? `生成约 ${localTotalDuration.toFixed(1)}s 本地 Vlog` : '生成本地 Vlog'}
          </button>
        )}
        {sourceMode === 'ai' && !config?.video_model.toLowerCase().includes('seedance-2') && (
          <p className="summary-warning">请选择 Seedance 2.0 模型配置</p>
        )}
      </aside>
      {!project && sourceMode === 'ai' && (
        <div className="vlog-mobile-action">
          <button className="btn primary full" type="button" disabled={!canGenerate} onClick={submit}>
            {submitting ? '正在创建…' : plan ? `生成约 ${plan.target_duration}s Vlog` : '生成 Vlog'}
          </button>
        </div>
      )}
      {!project && sourceMode === 'motion' && (
        <div className="vlog-mobile-action">
          <button className="btn primary full" type="button" disabled={!canGenerate} onClick={renderLocal}>
            {localRendering ? '正在生成…' : localImages.length >= 1 ? `生成约 ${localTotalDuration.toFixed(1)}s 本地 Vlog` : '生成本地 Vlog'}
          </button>
        </div>
      )}
    </main>
  )
}
