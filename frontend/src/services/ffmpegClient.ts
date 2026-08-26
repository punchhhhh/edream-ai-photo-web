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
  /** 0~1，加载引擎和编码阶段会提供。 */
  ratio?: number
}

export interface MergeResult {
  blob: Blob
  mode: 'copy' | 'encode'
  logTail: string[]
  duration?: number
}

let ffmpegInstance: FFmpeg | null = null
let loadPromise: Promise<FFmpeg> | null = null
let logLines: string[] = []
let progressCb: ((ratio: number) => void) | null = null
let engineProgress = { ratio: 0, detail: '准备本地合成引擎' }
const engineProgressListeners = new Set<(ratio: number, detail: string) => void>()

const ENGINE_DOWNLOAD_TIMEOUT_MS = 120_000
const ENGINE_START_TIMEOUT_MS = 60_000

function reportEngineProgress(ratio: number, detail: string) {
  engineProgress = { ratio, detail }
  engineProgressListeners.forEach((listener) => listener(ratio, detail))
}

async function fetchEngineAsset(
  url: string,
  mimeType: string,
  onProgress: (ratio: number) => void,
): Promise<string> {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), ENGINE_DOWNLOAD_TIMEOUT_MS)
  try {
    const response = await fetch(url, { cache: 'force-cache', signal: controller.signal })
    if (!response.ok) throw new Error(`合成引擎下载失败（${response.status}）`)

    const total = Number(response.headers.get('content-length'))
    if (!response.body || !Number.isFinite(total) || total <= 0) {
      const blob = await response.blob()
      onProgress(1)
      return URL.createObjectURL(new Blob([blob], { type: mimeType }))
    }

    const reader = response.body.getReader()
    const chunks: ArrayBuffer[] = []
    let loaded = 0
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      const chunk = new Uint8Array(value.byteLength)
      chunk.set(value)
      chunks.push(chunk.buffer)
      loaded += value.byteLength
      onProgress(Math.min(1, loaded / total))
    }
    return URL.createObjectURL(new Blob(chunks, { type: mimeType }))
  } catch (cause) {
    if ((cause as Error).name === 'AbortError') {
      throw new Error('合成引擎下载超时，请检查网络后重试')
    }
    throw cause
  } finally {
    window.clearTimeout(timeout)
  }
}

async function startEngine(ff: FFmpeg, coreURL: string, wasmURL: string): Promise<void> {
  let timeout: number | undefined
  try {
    await Promise.race([
      ff.load({ coreURL, wasmURL }),
      new Promise<never>((_, reject) => {
        timeout = window.setTimeout(
          () => reject(new Error('合成引擎初始化超时，请刷新页面后重试')),
          ENGINE_START_TIMEOUT_MS,
        )
      }),
    ])
  } finally {
    if (timeout !== undefined) window.clearTimeout(timeout)
  }
}

function tail(): string[] {
  return logLines.slice(-6)
}

function toMp4Blob(data: Uint8Array | string): Blob {
  const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : new Uint8Array(data)
  return new Blob([bytes], { type: 'video/mp4' })
}

async function getFFmpeg(onProgress?: (ratio: number, detail: string) => void): Promise<FFmpeg> {
  if (ffmpegInstance) {
    onProgress?.(1, '本地合成引擎已就绪')
    return ffmpegInstance
  }
  if (onProgress) {
    engineProgressListeners.add(onProgress)
    onProgress(engineProgress.ratio, engineProgress.detail)
  }
  try {
    if (!loadPromise) {
      loadPromise = (async () => {
        const ff = new window.FFmpegWASM!.FFmpeg()
        ff.on('log', ({ message }) => {
          logLines.push(message)
          if (logLines.length > 200) logLines = logLines.slice(-100)
        })
        ff.on('progress', ({ progress }) => progressCb?.(Math.max(0, Math.min(1, progress))))

        let coreURL = ''
        let wasmURL = ''
        try {
          reportEngineProgress(0.01, '下载本地合成引擎（首次约 32MB）')
          coreURL = await fetchEngineAsset(`${ENGINE_BASE}/ffmpeg-core.js`, 'text/javascript', (ratio) => {
            reportEngineProgress(0.01 + ratio * 0.04, '下载本地合成引擎（首次约 32MB）')
          })
          wasmURL = await fetchEngineAsset(`${ENGINE_BASE}/ffmpeg-core.wasm`, 'application/wasm', (ratio) => {
            reportEngineProgress(0.05 + ratio * 0.9, '下载本地合成引擎（首次约 32MB）')
          })
          reportEngineProgress(0.96, '正在初始化本地合成引擎')
          await startEngine(ff, coreURL, wasmURL)
          reportEngineProgress(1, '本地合成引擎已就绪')
          ffmpegInstance = ff
          return ff
        } catch (cause) {
          ff.terminate()
          throw cause
        } finally {
          if (coreURL) URL.revokeObjectURL(coreURL)
          if (wasmURL) URL.revokeObjectURL(wasmURL)
        }
      })().catch((cause) => {
        loadPromise = null
        engineProgress = { ratio: 0, detail: '准备本地合成引擎' }
        throw cause
      })
    }
    return await loadPromise
  } finally {
    if (onProgress) engineProgressListeners.delete(onProgress)
  }
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

  onProgress({ stage: 'loading-engine', detail: '准备本地合成引擎', ratio: 0 })
  const ff = await getFFmpeg((ratio, detail) => onProgress({ stage: 'loading-engine', detail, ratio }))

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

interface VlogSource {
  url: string
  duration: number
}

function probeMedia(log: string[], fallbackDuration: number): { duration: number; hasAudio: boolean } {
  let duration = fallbackDuration
  for (const line of log) {
    const match = line.match(/Duration:\s*(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)/)
    if (match) duration = Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3])
  }
  return { duration, hasAudio: log.some((line) => /Audio:/.test(line)) }
}

/** Vlog 专用合成：任意片段数、固定画幅、0.6 秒音画交叉淡化。 */
export async function mergeVlogClips(
  sources: VlogSource[],
  ratio: '9:16' | '16:9',
  onProgress: (p: MergeProgress) => void,
): Promise<MergeResult> {
  if (sources.length === 0) throw new Error('没有可合成的视频片段')
  if (sources.length === 1) {
    onProgress({ stage: 'downloading', detail: '读取唯一片段' })
    const response = await fetch(sources[0].url)
    if (!response.ok) throw new Error(`片段下载失败(${response.status})`)
    return {
      blob: new Blob([await response.arrayBuffer()], { type: 'video/mp4' }),
      mode: 'copy',
      duration: sources[0].duration,
      logTail: [],
    }
  }

  onProgress({ stage: 'loading-engine', detail: '准备本地合成引擎', ratio: 0 })
  const ff = await getFFmpeg((ratio, detail) => onProgress({ stage: 'loading-engine', detail, ratio }))
  const probes: Array<{ duration: number; hasAudio: boolean }> = []
  const outputName = 'vlog-out.mp4'

  for (let index = 0; index < sources.length; index++) {
    onProgress({ stage: 'downloading', detail: `加载片段 ${index + 1}/${sources.length}` })
    const response = await fetch(sources[index].url)
    if (!response.ok) throw new Error(`片段 ${index + 1} 下载失败(${response.status})`)
    await ff.writeFile(`vlog-${index}.mp4`, new Uint8Array(await response.arrayBuffer()))
    const logStart = logLines.length
    await ff.exec(['-hide_banner', '-i', `vlog-${index}.mp4`])
    probes.push(probeMedia(logLines.slice(logStart), sources[index].duration))
  }

  const cleanup = async () => {
    for (let index = 0; index < sources.length; index++) {
      await ff.deleteFile(`vlog-${index}.mp4`).catch(() => {})
    }
    await ff.deleteFile(outputName).catch(() => {})
  }

  try {
    const transition = 0.6
    const [width, height] = ratio === '9:16' ? [720, 1280] : [1280, 720]
    const filters: string[] = probes.map(
      (_, index) =>
        `[${index}:v]scale=${width}:${height}:force_original_aspect_ratio=decrease,` +
        `pad=${width}:${height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v${index}]`,
    )

    let videoLabel = 'v0'
    let cumulativeDuration = probes[0].duration
    for (let index = 1; index < probes.length; index++) {
      const outputLabel = `vx${index}`
      const offset = Math.max(0, cumulativeDuration - transition * index)
      filters.push(
        `[${videoLabel}][v${index}]xfade=transition=fade:duration=${transition}:offset=${offset.toFixed(3)}[${outputLabel}]`,
      )
      videoLabel = outputLabel
      cumulativeDuration += probes[index].duration
    }

    const hasAnyAudio = probes.some((probe) => probe.hasAudio)
    let audioLabel = ''
    if (hasAnyAudio) {
      probes.forEach((probe, index) => {
        if (probe.hasAudio) {
          filters.push(
            `[${index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,` +
              `atrim=0:${probe.duration.toFixed(3)},asetpts=PTS-STARTPTS[a${index}]`,
          )
        } else {
          filters.push(
            `anullsrc=channel_layout=stereo:sample_rate=48000,atrim=0:${probe.duration.toFixed(3)}[a${index}]`,
          )
        }
      })
      audioLabel = 'a0'
      for (let index = 1; index < probes.length; index++) {
        const outputLabel = `ax${index}`
        filters.push(
          `[${audioLabel}][a${index}]acrossfade=d=${transition}:c1=tri:c2=tri[${outputLabel}]`,
        )
        audioLabel = outputLabel
      }
    }

    onProgress({ stage: 'encode', detail: `正在合成 ${sources.length} 个场景`, ratio: 0 })
    progressCb = (progress) => onProgress({ stage: 'encode', detail: '正在合成自然转场', ratio: progress })
    const args = [
      ...sources.flatMap((_, index) => ['-i', `vlog-${index}.mp4`]),
      '-filter_complex',
      filters.join(';'),
      '-map',
      `[${videoLabel}]`,
      ...(hasAnyAudio ? ['-map', `[${audioLabel}]`] : []),
      '-c:v',
      'libx264',
      '-preset',
      'ultrafast',
      '-crf',
      '23',
      ...(hasAnyAudio ? ['-c:a', 'aac', '-b:a', '160k'] : []),
      '-movflags',
      '+faststart',
      outputName,
    ]
    let rc: number
    try {
      rc = await ff.exec(args)
    } finally {
      progressCb = null
    }
    if (rc !== 0) throw new Error(`Vlog 合成失败：${tail().slice(-1)[0] ?? '请重试'}`)
    const data = await ff.readFile(outputName)
    const duration = probes.reduce((sum, probe) => sum + probe.duration, 0) - transition * (probes.length - 1)
    onProgress({ stage: 'encode', detail: '合成完成', ratio: 1 })
    return { blob: toMp4Blob(data), mode: 'encode', duration, logTail: tail() }
  } finally {
    await cleanup()
  }
}
