import { useEffect, useState } from 'react'
import { deleteCreation, listCreations } from '../api'
import { STATUS_TEXT, type Creation } from '../types'
import { downloadName } from '../utils'

interface Props {
  onClose: () => void
  onChanged?: () => void
  onDeleted?: (id: number) => void
  onReuse?: (c: Creation) => void
  onEditVlog?: (creation: Creation) => void
}

export default function HistoryPanel({ onClose, onChanged, onDeleted, onReuse, onEditVlog }: Props) {
  const [creations, setCreations] = useState<Creation[]>([])
  const [error, setError] = useState('')
  const [playing, setPlaying] = useState<Creation | null>(null)

  const reload = async () => {
    try {
      setCreations(await listCreations())
    } catch (e) {
      setError((e as Error).message)
    }
  }

  useEffect(() => {
    reload()
  }, [])

  const remove = async (c: Creation) => {
    if (!confirm(`确定删除「${c.input_text.slice(0, 20)}」这条记录吗?相关文件会一并删除。`)) return
    try {
      await deleteCreation(c.id)
      await reload()
      onChanged?.()
      onDeleted?.(c.id)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const fmt = (iso: string) => new Date(iso).toLocaleString('zh-CN', { hour12: false })

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal history-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>历史产出</h2>
          <button className="icon-btn" onClick={onClose}>
            ✕
          </button>
        </div>
        {error && <div className="alert error">{error}</div>}
        {creations.length === 0 ? (
          <div className="empty">还没有生成过视频,去创作第一条吧 🎬</div>
        ) : (
          <div className="history-list">
            {creations.map((c) => (
              <div key={c.id} className="history-item">
                <div className="history-thumb" onClick={() => c.video_url && setPlaying(c)}>
                  {c.image_url ? (
                    <img src={c.image_url} alt="首帧" />
                  ) : (
                    <span className="thumb-placeholder">🎬</span>
                  )}
                  <span className={`tag status-${c.status}`}>{STATUS_TEXT[c.status] ?? c.status}</span>
                </div>
                <div className="history-main">
                  <div className="history-title">{c.input_text}</div>
                  <div className="muted small">
                    {c.style ? `${c.style} · ` : ''}
                    {c.image_source === 'none' ? '文生视频' : c.image_source === 'merged' ? '剪辑合成' : '图生视频'} · {c.duration}s ·{' '}
                    {c.config_name || '浏览器合成'} · {c.video_model || '—'}
                  </div>
                  <div className="muted small">{fmt(c.created_at)}</div>
                  {c.status === 'failed' && c.error && <div className="muted small error-text">{c.error}</div>}
                  <div className="history-actions">
                    {c.video_url && (
                      <>
                        <button className="link-btn" onClick={() => setPlaying(c)}>
                          播放
                        </button>
                        <a className="link-btn" href={c.video_url} download={downloadName(c)} target="_blank" rel="noreferrer">
                          下载
                        </a>
                      </>
                    )}
                    {c.image_source === 'merged' && onEditVlog ? (
                      <button className="link-btn" onClick={() => onEditVlog(c)}>
                        编辑 Vlog
                      </button>
                    ) : onReuse && (
                      <button className="link-btn" onClick={() => onReuse(c)}>
                        再创作
                      </button>
                    )}
                    <button className="danger-link" onClick={() => remove(c)}>
                      删除
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}

        {playing && (
          <div className="player-overlay" onClick={() => setPlaying(null)}>
            <div className="player-box" onClick={(e) => e.stopPropagation()}>
              <div className="player-title">{playing.input_text}</div>
              <video src={playing.video_url ?? undefined} controls autoPlay />
              <div className="muted small">
                {playing.expanded_prompt.slice(0, 120)}
                {playing.expanded_prompt.length > 120 ? '…' : ''}
              </div>
              <button className="btn" onClick={() => setPlaying(null)}>
                关闭
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
