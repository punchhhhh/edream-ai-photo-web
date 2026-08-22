import type { Creation } from './types'

/** 由创意文本生成安全的下载文件名。 */
export function downloadName(c: Creation): string {
  const base = (c.input_text || 'video').replace(/[\\/:*?"<>|#&\s]+/g, '_').replace(/_+/g, '_').slice(0, 40)
  return `${base || 'video'}.mp4`
}

/** 浏览器系统通知(权限已授予时生效,失败静默)。 */
export function notify(title: string, body: string): void {
  try {
    if ('Notification' in window && Notification.permission === 'granted') {
      new Notification(title, { body })
    }
  } catch {
    /* 通知不可用时忽略 */
  }
}

export async function requestNotifyPermission(): Promise<void> {
  try {
    if ('Notification' in window && Notification.permission === 'default') {
      await Notification.requestPermission()
    }
  } catch {
    /* 忽略 */
  }
}
