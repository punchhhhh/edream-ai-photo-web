import { useEffect, useMemo, useRef, useState } from 'react'
import {
  abandonVlog,
  completeVlog,
  createVlog,
  getLatestVlog,
  getVlog,
  listCreations,
  retryVlogClip,
  saveMergedVideo,
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
  type Creation,
  type ModelConfig,
  type StylePreset,
  type VlogImage,
  type VlogMotionTemplate,
  type VlogProject,
  type VlogTransition,
  VLOG_MOTION_TEMPLATES,
  VLOG_TRANSITIONS,
} from '../types'

const LAST_VLOG_KEY = 'edream_last_vlog_id'
const VLOG_TIMELINE_KEY = 'edream_vlog_timeline_draft'
const ACTIVE_PROJECT_STATUSES = ['pending', 'generating_video', 'ready_to_merge'] as const
const LOCAL_ASSET_DB = 'edream-vlog-local-assets'
const LOCAL_ASSET_STORE = 'files'

type GroupMode = 'ai' | 'motion' | 'video'
type VideoKind = 'local' | 'history'

interface LocalImage {
  id: string
  file: File
  url: string
  width: number
  height: number
}

interface VideoAsset {
  id: string
  name: string
  url: string
  duration: number
  kind: VideoKind
  mime?: string
  forceEncode?: boolean
  creation?: Creation
}

interface LocalResult {
  url: string
  duration: number
  saved: boolean
  saving: boolean
  saveTarget: 'Vlog 项目' | '历史记录'
}

interface SceneGroup {
  id: string
  mode: GroupMode
  images: VlogImage[]
  description: string
  localImage: LocalImage | null
  localAssetKey: string | null
  video: VideoAsset | null
  motionTemplate: VlogMotionTemplate
  duration: number
}

interface Props {
  config: ModelConfig | null
  styles: StylePreset[]
}

interface PersistedTimelineGroup {
  id: string
  mode: GroupMode
  images?: VlogImage[]
  description?: string
  duration?: number
  motionTemplate?: VlogMotionTemplate
  localAssetKey?: string
  historyCreationId?: number
}

interface PersistedTimelineDraft {
  projectId: number
  style: string
  description: string
  transitionStyle: VlogTransition
  ratio: '9:16' | '16:9'
  groups: PersistedTimelineGroup[]
}

const MODE_LABELS: Record<GroupMode, string> = {
  ai: 'AI 图生视频',
  motion: '单图动效',
  video: '视频素材',
}

const MODE_HINTS: Record<GroupMode, string> = {
  ai: '1–9 张图片生成一个连续 AI 片段',
  motion: '1 张图片在浏览器本地生成动态效果',
  video: '直接使用历史视频或本地视频',
}

function newGroup(mode: GroupMode = 'ai'): SceneGroup {
  return {
    id: crypto.randomUUID(),
    mode,
    images: [],
    description: '',
    localImage: null,
    localAssetKey: null,
    video: null,
    motionTemplate: 'kenburns_in',
    duration: mode === 'motion' ? 4 : 5,
  }
}

function openLocalAssetDb(): Promise<IDBDatabase> {
  if (typeof indexedDB === 'undefined') return Promise.reject(new Error('当前浏览器不支持本地素材持久化'))
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(LOCAL_ASSET_DB, 1)
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(LOCAL_ASSET_STORE)) {
        request.result.createObjectStore(LOCAL_ASSET_STORE)
      }
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error ?? new Error('无法打开本地素材存储'))
  })
}

async function saveLocalAsset(key: string, file: File): Promise<void> {
  const db = await openLocalAssetDb()
  await new Promise<void>((resolve, reject) => {
    const request = db.transaction(LOCAL_ASSET_STORE, 'readwrite').objectStore(LOCAL_ASSET_STORE).put(file, key)
    request.onsuccess = () => resolve()
    request.onerror = () => reject(request.error ?? new Error('无法保存本地素材'))
  }).finally(() => db.close())
}

async function loadLocalAsset(key: string): Promise<File | null> {
  const db = await openLocalAssetDb()
  return new Promise<File | null>((resolve, reject) => {
    const request = db.transaction(LOCAL_ASSET_STORE, 'readonly').objectStore(LOCAL_ASSET_STORE).get(key)
    request.onsuccess = () => {
      resolve((request.result as File | undefined) ?? null)
      db.close()
    }
    request.onerror = () => {
      reject(request.error ?? new Error('无法读取本地素材'))
      db.close()
    }
  })
}

async function removeLocalAsset(key: string | null): Promise<void> {
  if (!key) return
  const db = await openLocalAssetDb()
  await new Promise<void>((resolve, reject) => {
    const request = db.transaction(LOCAL_ASSET_STORE, 'readwrite').objectStore(LOCAL_ASSET_STORE).delete(key)
    request.onsuccess = () => resolve()
    request.onerror = () => reject(request.error ?? new Error('无法清理本地素材'))
  }).finally(() => db.close())
}

function aiDurationForCount(count: number): number {
  return count === 1 ? 5 : 10
}

function inferRatioFromImages(images: VlogImage[]): '9:16' | '16:9' {
  const portrait = images.filter((image) => image.height > image.width).length
  const landscape = images.filter((image) => image.width > image.height).length
  return portrait > landscape ? '9:16' : '16:9'
}

function isMp4Url(url: string): boolean {
  return /\.mp4(?:[?#]|$)/i.test(url)
}

function isActiveProject(project: VlogProject | null): boolean {
  return !!project && ACTIVE_PROJECT_STATUSES.includes(project.status as typeof ACTIVE_PROJECT_STATUSES[number])
}

function readTimelineDraft(): PersistedTimelineDraft | null {
  try {
    const raw = localStorage.getItem(VLOG_TIMELINE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as PersistedTimelineDraft
    if (!parsed || typeof parsed.projectId !== 'number' || !Array.isArray(parsed.groups)) return null
    return parsed
  } catch {
    return null
  }
}

function saveTimelineDraft(draft: PersistedTimelineDraft) {
  localStorage.setItem(VLOG_TIMELINE_KEY, JSON.stringify(draft))
}

function toPersistedGroups(groups: SceneGroup[]): PersistedTimelineGroup[] {
  return groups.map((group) => ({
    id: group.id,
    mode: group.mode,
    images: group.mode === 'ai' ? group.images : undefined,
    description: group.mode === 'ai' ? group.description : undefined,
    duration: group.duration,
    motionTemplate: group.motionTemplate,
    localAssetKey: group.mode !== 'ai' ? group.localAssetKey ?? undefined : undefined,
    historyCreationId: group.mode === 'video' && group.video?.kind === 'history' ? group.video.creation?.id : undefined,
  }))
}

function sameAiTimeline(draft: PersistedTimelineDraft, project: VlogProject): boolean {
  const draftPaths = draft.groups
    .filter((group) => group.mode === 'ai')
    .map((group) => group.images?.map((image) => image.image_path) ?? [])
  const projectPaths = project.clips.map((clip) => clip.reference_paths)
  return JSON.stringify(draftPaths) === JSON.stringify(projectPaths)
}

function groupsFromProject(project: VlogProject): SceneGroup[] {
  return project.clips.map((clip) => ({
    ...newGroup('ai'),
    images: clip.reference_paths.map((path, index) => ({
      image_path: path,
      url: clip.reference_urls[index] ?? '',
      width: project.ratio === '9:16' ? 720 : 1280,
      height: project.ratio === '9:16' ? 1280 : 720,
      order: index,
    })),
    duration: clip.duration,
  }))
}

function hydrateGroups(project: VlogProject, history: Creation[]): SceneGroup[] {
  const draft = readTimelineDraft()
  if (!draft || draft.projectId !== project.id || !sameAiTimeline(draft, project)) return groupsFromProject(project)

  const historyById = new Map(history.map((item) => [item.id, item]))
  let aiIndex = 0
  return draft.groups.map((saved) => {
    if (saved.mode === 'ai') {
      const clip = project.clips[aiIndex++]
      if (!clip) return newGroup('ai')
      return {
        ...newGroup('ai'),
        id: saved.id,
        description: saved.description ?? '',
        images: clip.reference_paths.map((path, index) => ({
          image_path: path,
          url: clip.reference_urls[index] ?? '',
          width: project.ratio === '9:16' ? 720 : 1280,
          height: project.ratio === '9:16' ? 1280 : 720,
          order: index,
        })),
        duration: clip.duration,
      }
    }

    if (saved.mode === 'motion') {
      return {
        ...newGroup('motion'),
        id: saved.id,
        localAssetKey: saved.localAssetKey ?? saved.id,
        duration: saved.duration ?? 4,
        motionTemplate: saved.motionTemplate ?? 'kenburns_in',
      }
    }

    const creation = saved.historyCreationId ? historyById.get(saved.historyCreationId) : undefined
    return {
      ...newGroup('video'),
      id: saved.id,
      localAssetKey: saved.localAssetKey ?? null,
      duration: saved.duration ?? 5,
      video: creation?.video_url ? {
        id: `history-${creation.id}`,
        name: creation.input_text || `历史视频 ${creation.id}`,
        url: creation.video_url,
        duration: creation.duration,
        kind: 'history',
        forceEncode: !isMp4Url(creation.video_url),
        creation,
      } : null,
    }
  })
}

function readImage(file: File): Promise<LocalImage> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const image = new Image()
    image.onload = () => resolve({ id: crypto.randomUUID(), file, url, width: image.naturalWidth, height: image.naturalHeight })
    image.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new Error(`${file.name} 不是可读取的图片`))
    }
    image.src = url
  })
}

function readVideo(file: File): Promise<VideoAsset> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const video = document.createElement('video')
    video.preload = 'metadata'
    video.onloadedmetadata = () => resolve({
      id: crypto.randomUUID(),
      name: file.name,
      url,
      duration: Math.max(1, video.duration || 5),
      kind: 'local',
      mime: file.type,
      forceEncode: file.type !== 'video/mp4' || !/\.mp4$/i.test(file.name),
    })
    video.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new Error(`${file.name} 不是可读取的视频`))
    }
    video.src = url
  })
}

export default function VlogPanel({ config, styles }: Props) {
  const [groups, setGroups] = useState<SceneGroup[]>([newGroup()])
  const [style, setStyle] = useState('写实纪录')
  const [description, setDescription] = useState('')
  const [transitionStyle, setTransitionStyle] = useState<VlogTransition>('fade')
  const [ratio, setRatio] = useState<'9:16' | '16:9'>('16:9')
  const [history, setHistory] = useState<Creation[]>([])
  const [project, setProject] = useState<VlogProject | null>(null)
  const [result, setResult] = useState<LocalResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [resultSaving, setResultSaving] = useState(false)
  const [error, setError] = useState('')
  const [progress, setProgress] = useState<MergeProgress | null>(null)
  const [openHistoryGroup, setOpenHistoryGroup] = useState<string | null>(null)
  const inputRefs = useRef<Record<string, HTMLInputElement | null>>({})
  const mountedRef = useRef(false)
  const localImageUrls = useRef<string[]>([])
  const localVideoUrls = useRef<string[]>([])
  const resultUrl = useRef<string | null>(null)
  const resultSaveId = useRef(0)
  const resultSaveInFlight = useRef(false)
  const appendAiImagesRef = useRef<string | null>(null)

  const aiGroups = useMemo(() => groups.filter((group) => group.mode === 'ai'), [groups])
  const hasAiGroups = aiGroups.length > 0
  const allValid = groups.every((group) => {
    if (group.mode === 'ai') return group.images.length >= 1
    if (group.mode === 'motion') return !!group.localImage
    return !!group.video
  })
  const totalDuration = groups.reduce((sum, group) => {
    const duration = group.mode === 'video' ? group.video?.duration ?? 0 : group.duration
    return sum + duration
  }, 0) - VLOG_TRANSITION_SECONDS * Math.max(0, groups.length - 1)
  const aiModelReady = !!config?.video_model.toLowerCase().includes('seedance-2')
  const canGenerate = !busy && !resultSaving && !project && allValid && groups.length > 0 && (!hasAiGroups || (!!config && aiModelReady))
  const activeProject = isActiveProject(project)
  const needsLocalReselect = !!project && groups.some((group) => (group.mode === 'motion' && !group.localImage) || (group.mode === 'video' && !group.video))
  const canMergeReadyProject = !!project && project.status === 'ready_to_merge' && allValid && !busy && !resultSaving

  const persistDraft = (projectId: number, sourceGroups = groups) => {
    saveTimelineDraft({
      projectId,
      style,
      description: description.trim(),
      transitionStyle,
      ratio,
      groups: toPersistedGroups(sourceGroups),
    })
  }

  const clearResult = () => {
    if (resultSaveInFlight.current) return
    resultSaveId.current += 1
    if (resultUrl.current) URL.revokeObjectURL(resultUrl.current)
    resultUrl.current = null
    setResult(null)
  }

  const revokeGroupAssets = (sourceGroups: SceneGroup[] = groups) => {
    sourceGroups.forEach((group) => {
      if (group.localImage) URL.revokeObjectURL(group.localImage.url)
      if (group.video?.kind === 'local') URL.revokeObjectURL(group.video.url)
    })
  }

  const removePersistedGroupAssets = async (sourceGroups: SceneGroup[] = groups) => {
    await Promise.all(sourceGroups
      .filter((group) => group.mode !== 'ai')
      .map((group) => removeLocalAsset(group.localAssetKey ?? group.id).catch(() => {})))
  }

  const restoreLocalAssets = async (sourceGroups: SceneGroup[]) => {
    const restored = await Promise.all(sourceGroups.map(async (group) => {
      if (group.mode === 'ai' || !group.localAssetKey) return null
      const file = await loadLocalAsset(group.localAssetKey).catch(() => null)
      if (!file) return null
      try {
        if (group.mode === 'motion') {
          const image = await readImage(file)
          if (!mountedRef.current) {
            URL.revokeObjectURL(image.url)
            return null
          }
          localImageUrls.current.push(image.url)
          return { id: group.id, mode: group.mode, image }
        }
        const video = await readVideo(file)
        if (!mountedRef.current) {
          URL.revokeObjectURL(video.url)
          return null
        }
        localVideoUrls.current.push(video.url)
        return { id: group.id, mode: group.mode, video }
      } catch {
        return null
      }
    }))
    if (!mountedRef.current) return
    setGroups((current) => current.map((group) => {
      const item = restored.find((candidate) => candidate?.id === group.id && candidate.mode === group.mode)
      if (!item) return group
      if (item.mode === 'motion') {
        if (group.localImage) return group
        return { ...group, localImage: item.image, localAssetKey: group.localAssetKey ?? group.id }
      }
      if (group.video) return group
      return { ...group, video: item.video, localAssetKey: group.localAssetKey ?? group.id }
    }))
  }

  useEffect(() => {
    mountedRef.current = true
    let disposed = false
    const loadProject = async () => {
      const id = Number(localStorage.getItem(LAST_VLOG_KEY))
      if (id) {
        try {
          return await getVlog(id)
        } catch {
          localStorage.removeItem(LAST_VLOG_KEY)
        }
      }
      return getLatestVlog().catch(() => null)
    }

    const load = async () => {
      const reusableHistory = (await listCreations(100).catch(() => []))
        .filter((item) => item.status === 'completed' && item.video_url)
      if (disposed) return
      setHistory(reusableHistory)

      const saved = await loadProject()
      if (disposed || !saved) return
      setProject(saved)
      setStyle(saved.style)
      setDescription(saved.description)
      setTransitionStyle(saved.transition_style ?? 'fade')
      setRatio(saved.ratio === '9:16' ? '9:16' : '16:9')
      const restoredGroups = hydrateGroups(saved, reusableHistory)
      setGroups(restoredGroups)
      void restoreLocalAssets(restoredGroups)
      localStorage.setItem(LAST_VLOG_KEY, String(saved.id))
    }

    load()
    return () => {
      disposed = true
      mountedRef.current = false
      localImageUrls.current.forEach((url) => URL.revokeObjectURL(url))
      localVideoUrls.current.forEach((url) => URL.revokeObjectURL(url))
      resultSaveId.current += 1
      resultSaveInFlight.current = false
      if (resultUrl.current) URL.revokeObjectURL(resultUrl.current)
    }
  }, [])

  useEffect(() => {
    if (!project || !isActiveProject(project)) return
    persistDraft(project.id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.id, project?.status, groups, style, description, transitionStyle, ratio])

  useEffect(() => {
    if (!project || !['pending', 'generating_video'].includes(project.status)) return
    const timer = window.setInterval(() => getVlog(project.id).then(setProject).catch(() => {}), 3000)
    return () => window.clearInterval(timer)
  }, [project])

  const updateGroup = (id: string, updater: (group: SceneGroup) => SceneGroup) => {
    clearResult()
    setGroups((current) => current.map((group) => group.id === id ? updater(group) : group))
  }

  const changeMode = (id: string, mode: GroupMode) => {
    const old = groups.find((group) => group.id === id)
    if (!old || old.mode === mode) return
    if (old?.localImage && mode !== 'motion') URL.revokeObjectURL(old.localImage.url)
    if (old?.video?.kind === 'local' && mode !== 'video') URL.revokeObjectURL(old.video.url)
    void removePersistedGroupAssets([old])
    updateGroup(id, (group) => ({ ...newGroup(mode), id: group.id }))
  }

  const addGroup = (mode: GroupMode) => {
    clearResult()
    setGroups((current) => [...current, newGroup(mode)])
  }

  const removeGroup = (index: number) => {
    const group = groups[index]
    if (!group) return
    if (group.localImage) URL.revokeObjectURL(group.localImage.url)
    if (group.video?.kind === 'local') URL.revokeObjectURL(group.video.url)
    void removePersistedGroupAssets([group])
    setGroups((current) => current.filter((_, itemIndex) => itemIndex !== index))
    clearResult()
  }

  const moveGroup = (from: number, to: number) => {
    if (to < 0 || to >= groups.length) return
    setGroups((current) => {
      const next = [...current]
      const [moved] = next.splice(from, 1)
      next.splice(to, 0, moved)
      return next
    })
    clearResult()
  }

  const openAiImagePicker = (id: string, append: boolean) => {
    appendAiImagesRef.current = append ? id : null
    inputRefs.current[`${id}-ai`]?.click()
  }

  const uploadAiImages = async (id: string, files: File[], append = false) => {
    const current = groups.find((group) => group.id === id)
    const imageCount = append ? (current?.images.length ?? 0) + files.length : files.length
    if (files.length < 1 || imageCount > 9) return setError('每个 AI 图生视频组最多 9 张图片')
    setBusy(true)
    setError('')
    try {
      const uploaded = await uploadVlogImages(files)
      const images = append
        ? [...(current?.images ?? []), ...uploaded.images.map((image, index) => ({ ...image, order: (current?.images.length ?? 0) + index }))]
        : uploaded.images
      updateGroup(id, (group) => {
        return { ...group, images, duration: aiDurationForCount(images.length) }
      })
      setRatio(inferRatioFromImages(images))
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const chooseMotionImage = async (id: string, file?: File) => {
    if (!file || !file.type.startsWith('image/')) return setError('图片动效组请选择 PNG、JPEG 或 WebP 图片')
    setBusy(true)
    setError('')
    try {
      const image = await readImage(file)
      try {
        await saveLocalAsset(id, file)
      } catch (cause) {
        URL.revokeObjectURL(image.url)
        throw cause
      }
      localImageUrls.current.push(image.url)
      const old = groups.find((group) => group.id === id)?.localImage
      if (old) URL.revokeObjectURL(old.url)
      const oldAssetKey = groups.find((group) => group.id === id)?.localAssetKey
      if (oldAssetKey && oldAssetKey !== id) void removeLocalAsset(oldAssetKey).catch(() => {})
      updateGroup(id, (group) => ({ ...group, localImage: image, localAssetKey: id }))
      if (image.height > image.width) setRatio('9:16')
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const chooseLocalVideo = async (id: string, file?: File) => {
    if (!file || !file.type.startsWith('video/')) return setError('请选择 MP4、WebM 等浏览器可读取的视频')
    setBusy(true)
    setError('')
    try {
      const video = await readVideo(file)
      const old = groups.find((group) => group.id === id)?.video
      try {
        await saveLocalAsset(id, file)
      } catch (cause) {
        URL.revokeObjectURL(video.url)
        throw cause
      }
      localVideoUrls.current.push(video.url)
      if (old?.kind === 'local') URL.revokeObjectURL(old.url)
      const oldAssetKey = groups.find((group) => group.id === id)?.localAssetKey
      if (oldAssetKey && oldAssetKey !== id) void removeLocalAsset(oldAssetKey).catch(() => {})
      updateGroup(id, (group) => ({ ...group, video, localAssetKey: id }))
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const clearVideoAsset = (id: string) => {
    const old = groups.find((group) => group.id === id)
    if (!old) return
    if (old.video?.kind === 'local') {
      URL.revokeObjectURL(old.video.url)
    }
    const assetKey = old.localAssetKey ?? (old.video?.kind === 'local' ? old.id : null)
    if (assetKey) void removeLocalAsset(assetKey).catch(() => {})
    updateGroup(id, (group) => ({ ...group, video: null, localAssetKey: null }))
  }

  const selectHistoryVideo = (id: string, creation: Creation) => {
    if (!creation.video_url) return
    const videoUrl = creation.video_url
    const old = groups.find((group) => group.id === id)
    if (old?.video?.kind === 'local') {
      URL.revokeObjectURL(old.video.url)
    }
    const assetKey = old?.localAssetKey ?? (old?.video?.kind === 'local' ? old.id : null)
    if (assetKey) void removeLocalAsset(assetKey).catch(() => {})
    updateGroup(id, (group) => ({
      ...group,
      localAssetKey: null,
      video: {
        id: `history-${creation.id}`,
        name: creation.input_text || `历史视频 ${creation.id}`,
        url: videoUrl,
        duration: creation.duration,
        kind: 'history',
        forceEncode: !isMp4Url(videoUrl),
        creation,
      },
    }))
    setOpenHistoryGroup(null)
  }

  const retryClip = async (clipId: number) => {
    if (!project) return
    setError('')
    try { setProject(await retryVlogClip(project.id, clipId)) } catch (cause) { setError((cause as Error).message) }
  }

  const buildSources = async (current: VlogProject | null) => {
    const aiClips = current?.clips ?? []
    let aiIndex = 0
    const sources: Array<{ url: string; duration: number; mime?: string; forceEncode?: boolean }> = []
    const temporaryUrls: string[] = []
    try {
      for (const group of groups) {
        if (group.mode === 'ai') {
          const clip = aiClips[aiIndex++]
          if (!clip?.video_url) throw new Error(`第 ${aiIndex} 个 AI 片段还没有可读取的视频地址`)
          sources.push({ url: clip.video_url, duration: clip.duration })
        } else if (group.mode === 'video' && group.video) {
          sources.push({ url: group.video.url, duration: group.video.duration, mime: group.video.mime, forceEncode: group.video.forceEncode })
        } else if (group.mode === 'motion' && group.localImage) {
          const motion = await mergeImageMotionVlog(
            [{ url: group.localImage.url, mime: group.localImage.file.type }],
            ratio,
            group.motionTemplate,
            group.duration,
            'fade',
            setProgress,
          )
          const motionUrl = URL.createObjectURL(motion.blob)
          temporaryUrls.push(motionUrl)
          sources.push({ url: motionUrl, duration: motion.duration ?? group.duration })
        }
      }
      return { sources, temporaryUrls }
    } catch (cause) {
      temporaryUrls.forEach((url) => URL.revokeObjectURL(url))
      throw cause
    }
  }

  const mergeFinal = async (current: VlogProject | null) => {
    if (resultSaveInFlight.current) return
    setBusy(true)
    setError('')
    clearResult()
    let temporaryUrls: string[] = []
    try {
      if (!allValid) throw new Error('请先补齐所有片段组素材')
      const built = await buildSources(current)
      temporaryUrls = built.temporaryUrls
      const sources = built.sources
      const merged = await mergeVlogClips(sources, ratio, transitionStyle, setProgress)
      const url = URL.createObjectURL(merged.blob)
      resultUrl.current = url
      const duration = merged.duration ?? totalDuration
      const saveId = resultSaveId.current + 1
      resultSaveId.current = saveId
      resultSaveInFlight.current = true
      setResultSaving(true)

      // 本地 Blob 已经可以播放，不要让云端保存阻塞预览。保存状态单独反馈给用户。
      setResult({
        url,
        duration,
        saved: false,
        saving: true,
        saveTarget: current ? 'Vlog 项目' : '历史记录',
      })
      void (async () => {
        try {
          if (current) {
            const completed = await completeVlog(current.id, merged.blob, duration)
            if (resultSaveId.current !== saveId) return
            setProject(completed)
            localStorage.removeItem(VLOG_TIMELINE_KEY)
          } else {
            await saveMergedVideo(merged.blob, {
              title: description.trim() || 'Vlog 剪辑合成',
              sourceIds: groups.flatMap((group) => group.video?.creation?.id ? [group.video.creation.id] : []),
              totalDuration: duration,
            })
          }
          if (resultSaveId.current !== saveId) return
          setResult((previous) => previous ? { ...previous, saved: true, saving: false } : previous)
        } catch (cause) {
          if (resultSaveId.current !== saveId) return
          const target = current ? 'Vlog 项目' : '历史记录'
          setError(`成片已生成，但保存到${target}失败：${(cause as Error).message}`)
          setResult((previous) => previous ? { ...previous, saving: false } : previous)
        } finally {
          if (resultSaveId.current === saveId) {
            resultSaveInFlight.current = false
            setResultSaving(false)
          }
        }
      })()
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      temporaryUrls.forEach((url) => URL.revokeObjectURL(url))
      setProgress(null)
      setBusy(false)
    }
  }

  const submit = async () => {
    if (!canGenerate) return
    if (!hasAiGroups) return mergeFinal(null)
    if (!config) {
      setError('AI 图生视频组需要选择 Seedance 2.0 模型配置')
      return
    }
    const aiImages = aiGroups.flatMap((group) => group.images.map((image) => image.image_path))
    setBusy(true)
    setError('')
    try {
      const created = await createVlog({
        config_id: config.id,
        image_paths: aiImages,
        image_groups: aiGroups.map((group) => group.images.map((image) => image.image_path)),
        image_group_descriptions: aiGroups.map((group) => group.description.trim()),
        ratio,
        style,
        description: description.trim(),
        transition_style: transitionStyle,
      })
      setProject(created)
      localStorage.setItem(LAST_VLOG_KEY, String(created.id))
      persistDraft(created.id, groups)
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const reset = async () => {
    if (resultSaveInFlight.current) return
    if (project && ['pending', 'generating_video', 'ready_to_merge'].includes(project.status)) {
      try { await abandonVlog(project.id) } catch { /* local reset is still useful if the network is unavailable */ }
    }
    revokeGroupAssets()
    await removePersistedGroupAssets()
    setProject(null)
    setGroups([newGroup()])
    setDescription('')
    clearResult()
    localStorage.removeItem(LAST_VLOG_KEY)
    localStorage.removeItem(VLOG_TIMELINE_KEY)
  }

  const abandonProject = async () => {
    if (!project || !isActiveProject(project) || resultSaveInFlight.current) return
    const confirmed = window.confirm('放弃后会释放当前 Vlog 名额，已生成但未合成的片段会作废。确定放弃？')
    if (!confirmed) return
    setBusy(true)
    setError('')
    try {
      await abandonVlog(project.id)
      revokeGroupAssets()
      await removePersistedGroupAssets()
      setProject(null)
      setGroups([newGroup()])
      setDescription('')
      clearResult()
      localStorage.removeItem(LAST_VLOG_KEY)
      localStorage.removeItem(VLOG_TIMELINE_KEY)
    } catch (cause) {
      setError((cause as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const canEditGroupStructure = !busy && !resultSaving && !project
  const canChooseGroupAsset = (group: SceneGroup) => !busy && !resultSaving && (!project || (activeProject && group.mode !== 'ai'))

  const renderProgress = progress && (
    <div className="progress">
      <span className="spinner" />
      <span>{progress.detail ?? '正在处理素材'}{progress.stage === 'encode' && typeof progress.ratio === 'number' ? ` · ${Math.round(progress.ratio * 100)}%` : ''}</span>
    </div>
  )

  return (
    <main className="vlog-workspace unified-vlog" aria-busy={busy || resultSaving}>
      <div className="vlog-main-column">
        <section className="vlog-hero">
          <div>
            <span className="surface-kicker">VLOG WORKSPACE</span>
            <h2>先决定每一段怎么来</h2>
            <p>手动添加片段组，混合 AI、本地动效、历史视频和本地视频，再用一套转场合成完整 Vlog。</p>
          </div>
          <div className="group-add-actions">
            {(Object.keys(MODE_LABELS) as GroupMode[]).map((mode) => <button key={mode} className="btn" type="button" onClick={() => addGroup(mode)} disabled={!canEditGroupStructure}>＋ {MODE_LABELS[mode]}组</button>)}
          </div>
        </section>

        {error && <div className="alert error" role="alert"><span>{error}</span><button className="icon-btn" type="button" onClick={() => setError('')}>×</button></div>}

        <section className="scene-groups" aria-label="Vlog 片段组">
          {groups.map((group, index) => (
            <article className={`scene-group scene-group-${group.mode}`} key={group.id}>
              <header className="scene-group-head">
                <div className="scene-group-index">{String(index + 1).padStart(2, '0')}</div>
                <div className="scene-group-title"><strong>{MODE_LABELS[group.mode]}组</strong><span>{MODE_HINTS[group.mode]}</span></div>
                <div className="scene-group-actions"><button className="icon-btn" type="button" title="上移" aria-label="上移片段组" disabled={index === 0 || !canEditGroupStructure} onClick={() => moveGroup(index, index - 1)}>↑</button><button className="icon-btn" type="button" title="下移" aria-label="下移片段组" disabled={index === groups.length - 1 || !canEditGroupStructure} onClick={() => moveGroup(index, index + 1)}>↓</button><button className="icon-btn danger-icon" type="button" title="删除" aria-label="删除片段组" disabled={groups.length === 1 || !canEditGroupStructure} onClick={() => removeGroup(index)}>×</button></div>
              </header>

              <div className="mode-picker" role="group" aria-label={`第 ${index + 1} 组来源类型`}>
                {(Object.keys(MODE_LABELS) as GroupMode[]).map((mode) => <button key={mode} className={group.mode === mode ? 'active' : ''} type="button" disabled={!canEditGroupStructure} onClick={() => changeMode(group.id, mode)}>{MODE_LABELS[mode]}</button>)}
              </div>

              {group.mode === 'ai' && <div className="group-content"><input ref={(element) => { inputRefs.current[`${group.id}-ai`] = element }} type="file" accept="image/png,image/jpeg,image/webp" multiple hidden onChange={(event) => { const files = Array.from(event.target.files ?? []); if (files.length) uploadAiImages(group.id, files, appendAiImagesRef.current === group.id); appendAiImagesRef.current = null; event.target.value = '' }} />{group.images.length === 0 ? <button className="group-dropzone" type="button" disabled={!canEditGroupStructure} onClick={() => openAiImagePicker(group.id, false)}><span>＋</span><strong>选择这一组的 1–9 张图片</strong><small>这些图片只会生成一个 AI 视频片段</small></button> : <div className="group-image-grid">{group.images.map((image, imageIndex) => <figure key={image.image_path} className="group-image"><img src={image.url} alt={`第 ${index + 1} 组图片 ${imageIndex + 1}`} /><figcaption><span>{imageIndex + 1}</span><button className="icon-btn" type="button" title="删除图片" aria-label="删除图片" disabled={!canEditGroupStructure} onClick={() => updateGroup(group.id, (current) => { const images = current.images.filter((_, itemIndex) => itemIndex !== imageIndex); return { ...current, images, duration: aiDurationForCount(images.length) } })}>×</button></figcaption></figure>)}{canEditGroupStructure && group.images.length < 9 && <button className="add-image-tile" type="button" onClick={() => openAiImagePicker(group.id, true)}>＋<span>继续添加图片</span></button>}</div>} {group.images.length >= 1 && <><label className="field group-description-field"><span>本组描述 <em>可选</em></span><textarea rows={2} maxLength={500} disabled={!canEditGroupStructure} value={group.description} onChange={(event) => updateGroup(group.id, (current) => ({ ...current, description: event.target.value }))} placeholder="例如：镜头从山脚出发，逐渐靠近骑行者" /></label><div className="group-meta"><span>{group.images.length} 张图片 · 1 次 AI 调用 · 约 {aiDurationForCount(group.images.length)} 秒</span><button className="link-btn" type="button" disabled={!canEditGroupStructure} onClick={() => openAiImagePicker(group.id, false)}>重新选择</button></div></>}</div>}

              {group.mode === 'motion' && <div className="group-content"><input ref={(element) => { inputRefs.current[`${group.id}-motion`] = element }} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={(event) => { chooseMotionImage(group.id, event.target.files?.[0]); event.target.value = '' }} />{!group.localImage ? <button className="group-dropzone compact" type="button" disabled={!canChooseGroupAsset(group)} onClick={() => inputRefs.current[`${group.id}-motion`]?.click()}><span>＋</span><strong>选择一张图片</strong><small>只在当前浏览器生成动态效果，不创建 AI 任务</small></button> : <div className="motion-source"><img src={group.localImage.url} alt={`第 ${index + 1} 组动效图片`} /><div><strong>{group.localImage.file.name}</strong><span>本地处理 · 不上传服务器</span><button className="link-btn" type="button" disabled={!canChooseGroupAsset(group)} onClick={() => inputRefs.current[`${group.id}-motion`]?.click()}>更换图片</button></div></div>}<div className="group-settings"><label className="field"><span>动效</span><select value={group.motionTemplate} disabled={!canEditGroupStructure} onChange={(event) => updateGroup(group.id, (current) => ({ ...current, motionTemplate: event.target.value as VlogMotionTemplate }))}>{VLOG_MOTION_TEMPLATES.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}</select></label><label className="field"><span>时长</span><select value={group.duration} disabled={!canEditGroupStructure} onChange={(event) => updateGroup(group.id, (current) => ({ ...current, duration: Number(event.target.value) }))}><option value="3">3 秒</option><option value="4">4 秒</option><option value="5">5 秒</option><option value="6">6 秒</option></select></label></div></div>}

              {group.mode === 'video' && <div className="group-content"><input ref={(element) => { inputRefs.current[`${group.id}-video`] = element }} type="file" accept="video/*" hidden onChange={(event) => { chooseLocalVideo(group.id, event.target.files?.[0]); event.target.value = '' }} />{!group.video ? <div className="video-source-choices"><button className="group-choice" type="button" disabled={!canChooseGroupAsset(group)} onClick={() => inputRefs.current[`${group.id}-video`]?.click()}><strong>上传本地视频</strong><span>只在当前浏览器读取</span></button><button className="group-choice" type="button" disabled={!canChooseGroupAsset(group)} onClick={() => setOpenHistoryGroup(openHistoryGroup === group.id ? null : group.id)}><strong>选择历史视频</strong><span>复用已经生成的成片</span></button></div> : <div className="video-source-selected"><span className="video-source-icon">▶</span><div><strong>{group.video.name}</strong><span>{group.video.kind === 'history' ? '历史生成视频' : '本地视频'} · {group.video.duration.toFixed(1)} 秒</span></div><button className="link-btn" type="button" disabled={!canChooseGroupAsset(group)} onClick={() => clearVideoAsset(group.id)}>更换</button></div>}{openHistoryGroup === group.id && <div className="history-picker">{history.length === 0 ? <span className="muted small">暂无可复用的历史视频</span> : history.map((item) => <button key={item.id} type="button" className="history-picker-item" onClick={() => selectHistoryVideo(group.id, item)}><span className="history-picker-thumb">{item.image_url ? <img src={item.image_url} alt="" /> : '▶'}</span><span><strong>{item.input_text || `视频 ${item.id}`}</strong><small>{item.duration}s · {item.image_source === 'merged' ? '剪辑成片' : 'AI 生成'}</small></span></button>)}</div>}</div>}
            </article>
          ))}
        </section>

        <section className="vlog-section unified-options"><div className="vlog-section-head"><div><h2>成片设置</h2><p>这些设置应用到整条时间线；一句话补充会传给每个 AI 片段。</p></div></div><div className="unified-options-grid"><label className="field"><span>画幅</span><select value={ratio} disabled={!!project || resultSaving} onChange={(event) => setRatio(event.target.value as '9:16' | '16:9')}><option value="9:16">竖屏 9:16</option><option value="16:9">横屏 16:9</option></select></label><label className="field"><span>AI 风格</span><select value={style} disabled={!!project || resultSaving} onChange={(event) => setStyle(event.target.value)}>{styles.map((item) => <option key={item.id} value={item.name}>{item.name}</option>)}</select></label><label className="field field-wide"><span>一句话补充 <em>可选</em></span><textarea rows={2} maxLength={500} disabled={!!project || resultSaving} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="例如：记录这次高原骑行，从清晨到傍晚" /></label></div><div className="field transition-field"><span>统一转场模板</span><div className="transition-grid">{VLOG_TRANSITIONS.map((item) => <button key={item.key} className={`transition-option ${transitionStyle === item.key ? 'selected' : ''}`} type="button" disabled={!!project || busy || resultSaving} onClick={() => { clearResult(); setTransitionStyle(item.key) }}><strong>{item.label}</strong><span>{item.description}</span></button>)}</div></div></section>

        {renderProgress}
        {project && <section className="vlog-section project-status"><div className="vlog-section-head"><div><h2>AI 片段进度</h2><p>项目 #{project.id} · {STATUS_TEXT[project.status] ?? project.status}</p></div><div className="scene-group-actions">{activeProject && <button className="btn" type="button" disabled={busy || resultSaving} onClick={abandonProject}>放弃项目</button>}{['completed', 'failed', 'cancelled'].includes(project.status) && <button className="btn" type="button" disabled={resultSaving} onClick={reset}>新建 Vlog</button>}</div></div>{needsLocalReselect && <div className="alert error">刷新后本地素材不会保留，请重新选择缺失的图片或视频后再合成。</div>}{project.clips.map((clip) => <div className={`clip-progress status-${clip.status}`} key={clip.id}><span className="clip-index">{clip.sequence}</span><div><strong>AI 片段 {clip.sequence} · {clip.duration}s</strong><span>{STATUS_TEXT[clip.status] ?? clip.status}</span>{clip.error && <small>{clip.error}</small>}</div>{clip.status === 'generating_video' && <span className="spinner" />}{clip.status === 'failed' && clip.retry_count < 1 && <button className="btn" type="button" disabled={resultSaving} onClick={() => retryClip(clip.id)}>重试</button>}</div>)}{project.status === 'ready_to_merge' && <button className="btn primary big full" type="button" disabled={!canMergeReadyProject} onClick={() => mergeFinal(project)}>{busy ? '正在合成…' : needsLocalReselect ? '请补齐本地素材' : '生成最终 Vlog'}</button>}{project.status === 'completed' && project.final_video_url && <div className="vlog-result"><video controls playsInline src={project.final_video_url} /><a className="btn primary" href={project.final_video_url} download={`vlog-${project.id}.mp4`}>下载 Vlog</a></div>}</section>}
        {result && <section className="vlog-section local-result-section"><div className="vlog-section-head"><div><h2>成片已生成</h2><p>约 {result.duration.toFixed(1)} 秒 · <span className={`result-save-status ${result.saving ? 'is-saving' : result.saved ? 'is-saved' : 'is-failed'}`} aria-live="polite">{result.saving ? `正在保存到${result.saveTarget}…` : result.saved ? `已保存到${result.saveTarget}` : `未保存到${result.saveTarget}`}</span></p></div><button className="btn" type="button" disabled={resultSaving} onClick={clearResult}>关闭预览</button></div><div className="vlog-result"><video controls playsInline src={result.url} /><a className="btn primary" href={result.url} download="vlog-edit.mp4">下载 Vlog</a></div></section>}
      </div>

      <aside className="vlog-summary unified-summary"><div className="summary-head"><span>时间线摘要</span><span className="summary-count">{groups.length} 组</span></div><dl className="vlog-metrics"><div><dt>AI 片段</dt><dd>{aiGroups.length}</dd></div><div><dt>视频素材</dt><dd>{groups.filter((group) => group.mode === 'video').length}</dd></div><div><dt>预计时长</dt><dd>{Math.max(0, totalDuration).toFixed(1)}s</dd></div><div><dt>转场</dt><dd>{VLOG_TRANSITIONS.find((item) => item.key === transitionStyle)?.label}</dd></div></dl><div className="summary-timeline">{groups.map((group, index) => <div className={`summary-timeline-row summary-${group.mode}`} key={group.id}><span>{index + 1}</span><div><strong>{MODE_LABELS[group.mode]}组</strong><small>{group.mode === 'ai' ? `${group.images.length} 张图片` : group.mode === 'motion' ? (group.localImage ? `${group.duration}s 本地动效` : '待选图片') : (group.video ? `${group.video.duration.toFixed(1)}s 视频` : '待选视频')}</small></div></div>)}</div>{hasAiGroups && !aiModelReady && <p className="summary-warning">AI 组需要 Seedance 2.0 视频模型配置</p>}<button className="btn primary full vlog-submit" type="button" disabled={!canGenerate} onClick={submit}>{busy ? '处理中…' : hasAiGroups ? '开始生成 AI 片段' : '生成最终 Vlog'}</button><p className="summary-note">{hasAiGroups ? 'AI 片段生成完成后，再合并本地与历史素材。' : '全部素材会在浏览器本地完成转场合成。'}</p></aside>
    </main>
  )
}
