/**
 * 浏览器端视频合成(FFmpeg.wasm,引擎自托管于 public/ffmpeg,不依赖 CDN)。
 *
 * 客户端走 UMD(index.html 引入 /ffmpeg/ffmpeg.js,自带经典 worker);
 * 直接用 ESM 包在 Vite 下创建 blob module worker 会报 Cannot find module。
 *
 * 合成策略:
 * 1. 流拷贝拼接(-c copy):素材编码参数一致时秒出、无损 —— AI 同模型出片是常态
 * 2. 自动回退 x264 重编码:统一到首段分辨率(等比缩放 + 黑边居中),兼容混合素材
 *
 * 单线程 core,不依赖 SharedArrayBuffer/跨域隔离头;重编码长片较慢,适合短视频剪辑。
 */
import type { FFmpeg } from '@ffmpeg/ffmpeg'
import { toBlobURL } from '@ffmpeg/util'

declare global {
  interface Window {
    FFmpegWASM?: { FFmpeg: new () => FFmpeg }
  }
}

const ENGINE_BASE = '/ffmpeg'

export type MergeStage = 'loading-engine' | 'downloading' | 'copy' | 'probing' | 'encode'

export interface MergeProgress {
  stage: MergeStage
  detail?: string
  /** 0~1,仅 encode 阶段有意义 */
  ratio?: number
}

export interface MergeResult {
  blob: Blob
  mode: 'copy' | 'encode'
  logTail: string[]
}

let ffmpegInstance: FFmpeg | null = null
let loadPromise: Promise<FFmpeg> | null = null
let logLines: string[] = []
let progressCb: ((ratio: number) => void) | null = null

function tail(): string[] {
  return logLines.slice(-6)
}

function toMp4Blob(data: Uint8Array | string): Blob {
  const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : new Uint8Array(data)
  return new Blob([bytes], { type: 'video/mp4' })
}

async function getFFmpeg(): Promise<FFmpeg> {
  if (ffmpegInstance) return ffmpegInstance
  if (!loadPromise) {
    loadPromise = (async () => {
      const ff = new window.FFmpegWASM!.FFmpeg()
      ff.on('log', ({ message }) => {
        logLines.push(message)
        if (logLines.length > 200) logLines = logLines.slice(-100)
      })
      ff.on('progress', ({ progress }) => progressCb?.(Math.max(0, Math.min(1, progress))))
      const coreURL = await toBlobURL(`${ENGINE_BASE}/ffmpeg-core.js`, 'text/javascript')
      const wasmURL = await toBlobURL(`${ENGINE_BASE}/ffmpeg-core.wasm`, 'application/wasm')
      await ff.load({ coreURL, wasmURL })
      ffmpegInstance = ff
      return ff
    })().catch((e) => {
      loadPromise = null
      throw e
    })
  }
  return loadPromise
}

/** 从 ffmpeg 日志里解析首段视频分辨率,失败回退 1280x720。 */
function probeResolution(log: string[]): { w: number; h: number } {
  for (let i = log.length - 1; i >= 0; i--) {
    const m = log[i].match(/Video:.*?,\s(\d{2,5})x(\d{2,5})[\s,]/)
    if (m) return { w: Number(m[1]), h: Number(m[2]) }
  }
  return { w: 1280, h: 720 }
}

export async function mergeClips(urls: string[], onProgress: (p: MergeProgress) => void): Promise<MergeResult> {
  if (urls.length < 2) throw new Error('至少选择 2 段视频才能合成')

  onProgress({ stage: 'loading-engine', detail: '首次使用需加载约 32MB 引擎' })
  const ff = await getFFmpeg()

  // 下载素材写入虚拟文件系统
  for (let i = 0; i < urls.length; i++) {
    onProgress({ stage: 'downloading', detail: `加载素材 ${i + 1}/${urls.length}` })
    const resp = await fetch(urls[i])
    if (!resp.ok) throw new Error(`素材下载失败(${resp.status}):${urls[i]}`)
    const buf = new Uint8Array(await resp.arrayBuffer())
    await ff.writeFile(`clip${i}.mp4`, buf)
  }
  const list = urls.map((_, i) => `file 'clip${i}.mp4'`).join('\n')
  await ff.writeFile('list.txt', new TextEncoder().encode(list))

  const cleanup = async () => {
    for (let i = 0; i < urls.length; i++) await ff.deleteFile(`clip${i}.mp4`).catch(() => {})
    await ff.deleteFile('list.txt').catch(() => {})
    await ff.deleteFile('out.mp4').catch(() => {})
  }

  try {
    // 首选:流拷贝拼接(同参数素材,无损且几乎不耗时)
    onProgress({ stage: 'copy', detail: '无损拼接中' })
    let rc = await ff.exec(['-f', 'concat', '-safe', '0', '-i', 'list.txt', '-c', 'copy', 'out.mp4'])
    if (rc === 0) {
      const data = await ff.readFile('out.mp4')
      onProgress({ stage: 'copy', detail: '拼接完成' })
      return { blob: toMp4Blob(data), mode: 'copy', logTail: tail() }
    }

    // 回退:重编码统一到首段分辨率
    const logStart = logLines.length
    await ff.exec(['-hide_banner', '-i', 'clip0.mp4'])
    const { w, h } = probeResolution(logLines.slice(logStart))
    onProgress({ stage: 'encode', detail: `素材参数不一致,转码合成中(${w}x${h})`, ratio: 0 })
    const filter = `scale=${w}:${h}:force_original_aspect_ratio=decrease,pad=${w}:${h}:-1:-1,format=yuv420p`
    progressCb = (ratio) => onProgress({ stage: 'encode', ratio })
    try {
      rc = await ff.exec([
        '-f', 'concat', '-safe', '0', '-i', 'list.txt',
        '-vf', filter,
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
        '-c:a', 'aac', '-movflags', '+faststart',
        'out.mp4',
      ])
    } finally {
      progressCb = null
    }
    if (rc !== 0) {
      const last = tail().filter((l) => l.includes('Error') || l.includes('error')).slice(-1)
      throw new Error(`合成失败:${last[0] ?? '请检查素材格式'}`)
    }
    const data = await ff.readFile('out.mp4')
    onProgress({ stage: 'encode', ratio: 1, detail: '转码完成' })
    return { blob: toMp4Blob(data), mode: 'encode', logTail: tail() }
  } finally {
    await cleanup()
  }
}
