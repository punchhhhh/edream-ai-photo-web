// 把 ffmpeg.wasm 的核心与 worker 文件复制到 public/,实现引擎自托管(不依赖 CDN)。
import { copyFileSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const outDir = join(root, 'public', 'ffmpeg')
mkdirSync(outDir, { recursive: true })

const files = [
  ['@ffmpeg/core/dist/umd/ffmpeg-core.js', 'ffmpeg-core.js'],
  ['@ffmpeg/core/dist/umd/ffmpeg-core.wasm', 'ffmpeg-core.wasm'],
  // 保留 webpack chunk 原名,UMD 客户端按相对路径 new Worker('814.ffmpeg.js') 加载
  ['@ffmpeg/ffmpeg/dist/umd/814.ffmpeg.js', '814.ffmpeg.js'],
  ['@ffmpeg/ffmpeg/dist/umd/ffmpeg.js', 'ffmpeg.js'],
]

for (const [src, name] of files) {
  const from = join(root, 'node_modules', src)
  const to = join(outDir, name)
  copyFileSync(from, to)
  console.log(`copied ${src} -> public/ffmpeg/${name}`)
}
