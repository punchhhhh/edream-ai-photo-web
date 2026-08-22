import { useEffect, useMemo, useRef, useState } from 'react'
import { listCreations, saveMergedVideo } from '../api'
import { mergeClips, type MergeProgress } from '../services/ffmpegClient'
import type { Creation } from '../types'

interface Props {
  onClose: () => void
}

const STAGE_TEXT: Record<MergeProgress['stage'], string> = {
  'loading-engine': '加载剪辑引擎(首次约 32MB,之后有缓存)',
  downloading: '加载素材',
  copy: '无损拼接中(同参数素材,秒级完成)',
  probing: '分析素材参数',
  encode: '转码合成中(素材参数不一致时自动统一)',
}

export default function EditPanel({ onClose }: Props) {
  const [creations, setCreations] = useState<Creation[]>([])
  const [order, setOrder] = useState<number[]>([])
  const [title, setTitle] = useState('')
  const [merging, setMerging] = useState(false)
  const [progress, setProgress] = useState<MergeProgress | null>(null)
  const [result, setResult] = useState<{ url: string; mode: 'copy' | 'encode'; saved: boolean } | null>(null)
  const [error, setError] = useState('')
  const dragFrom = useRef<number | null>(null)
  const resultUrlRef = useRef<string | null>(null)

  useEffect(() => {
    listCreations(100)
      .then((list) => setCreations(list.filter((c) => c.status === 'completed' && c.video_url)))
      .catch((e) => setError((e as Error).message))
  }, [])

  useEffect(() => {
    return () => {
      if (resultUrlRef.current) URL.revokeObjectURL(resultUrlRef.current)
    }
  }, [])

  const byId = useMemo(() => new Map(creations.map((c) => [c.id, c])), [creations])
  const selected = order.map((id) => byId.get(id)).filter((c): c is Creation => !!c)
  const totalDuration = selected.reduce((s, c) => s + c.duration, 0)

  const toggle = (c: Creation) => {
    setResult(null)
    setOrder((o) => (o.includes(c.id) ? o.filter((id) => id !== c.id) : [...o, c.id]))
    if (!title) setTitle(c.input_text.slice(0, 30))
  }

  const move = (from: number, to: number) => {
    setOrder((o) => {
      const next = [...o]
      const [id] = next.splice(from, 1)
      next.splice(to, 0, id)
      return next
    })
  }

  const run = async () => {
    if (selected.length < 2) return
    setMerging(true)
    setError('')
    setResult(null)
    try {
      const { blob, mode } = await mergeClips(selected.map((c) => c.video_url!), setProgress)
      const url = URL.createObjectURL(blob)
      resultUrlRef.current = url
      let saved = false
      try {
        await saveMergedVideo(blob, {
          title: title.trim() || '剪辑合成',
          sourceIds: selected.map((c) => c.id),
          totalDuration,
        })
        saved = true
      } catch (e) {
        setError(`成片已生成,但保存到历史记录失败:${(e as Error).message}`)
      }
      setResult({ url, mode, saved })
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setMerging(false)
      setProgress(null)
    }
  }

  const fileName = `${(title.trim() || '剪辑合成').replace(/[\\/:*?"<>|#&\s]+/g, '_').slice(0, 40)}.mp4`

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal edit-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>视频剪辑台</h2>
          <button className="icon-btn" onClick={onClose}>
            ✕
          </button>
        </div>
        <p className="modal-tip">
          选择多段已完成的视频,拖拽调整顺序后合成。合成在你的浏览器内完成(FFmpeg.wasm),同参数素材走无损拼接;成片自动保存到历史记录。
        </p>

        {error && (
          <div className="alert error">
            {error}
            <button className="icon-btn" onClick={() => setError('')}>
              ✕
            </button>
          </div>
        )}

        <div className="edit-sources">
          <h3>素材库({creations.length} 段可用的成片)</h3>
          {creations.length === 0 && <div className="empty">还没有已完成的视频,先去生成几段吧</div>}
          {creations.map((c) => (
            <label key={c.id} className={`edit-source ${order.includes(c.id) ? 'active' : ''}`}>
              <input type="checkbox" checked={order.includes(c.id)} onChange={() => toggle(c)} />
              <span className="edit-source-title">
                {c.image_source === 'merged' && <span className="tag default-tag">剪辑</span>}
                {c.input_text}
              </span>
              <span className="muted small">
                {c.duration}s · {c.video_model || '剪辑合成'}
              </span>
            </label>
          ))}
        </div>

        {selected.length > 0 && (
          <div className="edit-timeline">
            <h3>
              时间线(拖拽排序)· 共 {selected.length} 段 / 约 {totalDuration}s
            </h3>
            <div className="timeline-strip">
              {selected.map((c, i) => (
                <div
                  key={c.id}
                  className="timeline-chip"
                  draggable
                  onDragStart={() => (dragFrom.current = i)}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={() => {
                    if (dragFrom.current !== null && dragFrom.current !== i) move(dragFrom.current, i)
                    dragFrom.current = null
                  }}
                >
                  <span className="timeline-num">{i + 1}</span>
                  <span className="timeline-title">{c.input_text.slice(0, 12)}</span>
                  <button
                    className="icon-btn"
                    onClick={() => setOrder((o) => o.filter((id) => id !== c.id))}
                    title="移除"
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
            <label className="field">
              <span>成片标题</span>
              <input value={title} placeholder="给合成视频起个名字" onChange={(e) => setTitle(e.target.value)} />
            </label>
          </div>
        )}

        {merging && progress && (
          <div className="progress">
            <span className="spinner" />
            <div className="progress-body">
              <div>
                {STAGE_TEXT[progress.stage]}
                {progress.detail ? ` · ${progress.detail}` : ''}
              </div>
              {typeof progress.ratio === 'number' && progress.stage === 'encode' && (
                <div className="progress-bar">
                  <div className="progress-fill" style={{ width: `${Math.round(progress.ratio * 100)}%` }} />
                </div>
              )}
            </div>
          </div>
        )}

        {result && (
          <div className="video-result">
            <video src={result.url} controls />
            <div className="edit-result-actions">
              <a className="btn" href={result.url} download={fileName}>
                下载成片
              </a>
              <span className={`tag ${result.saved ? 'status-completed' : 'status-failed'}`}>
                {result.saved ? '已保存到历史记录' : '未保存(可重新合成)'}
              </span>
              <span className="muted small">{result.mode === 'copy' ? '无损拼接' : '已转码统一参数'}</span>
            </div>
          </div>
        )}

        <button className="btn primary big full" disabled={selected.length < 2 || merging} onClick={run}>
          {merging ? '合成中…' : selected.length < 2 ? `再选 ${2 - selected.length} 段即可合成` : `🎬 合成 ${selected.length} 段视频`}
        </button>
      </div>
    </div>
  )
}
