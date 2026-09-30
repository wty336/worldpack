/**
 * Vite 配置（Stage B）。
 *
 * 两个关键点：
 *
 * 1. **`base: '/static/'`** —— FastAPI 用 `app.mount("/static", StaticFiles(dist))`
 *    托管构建产物，所以 index.html 里必须是 `/static/assets/xxx.js` 这样的绝对路径。
 *    Vite 默认输出 `/assets/...`，会让页面在浏览器里 404（而服务端与测试都看不到，
 *    因为文件确实存在——只是路径不对）。这是本配置里最容易被忽略的一行。
 *
 * 2. **`build-manifest.json`（buildManifest 插件）** —— CI **不跑 npm**，
 *    所以"源码改了但忘了重新构建"必须由 pytest 侧的守卫发现。
 *    守卫不能靠 mtime：全新 clone 里所有文件的 mtime 都是检出时间，
 *    比不出先后。故构建时把 `src/**` + `index.html` + 本配置的内容哈希写进
 *    `dist/build-manifest.json`，守卫重算源哈希比对——跨机、跨 clone 都成立。
 */

import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join, posix, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

const ROOT = fileURLToPath(new URL('.', import.meta.url))
const OUT_DIR = 'dist'
/** 参与哈希的源码集合：改这些而没重建 = dist 过期。 */
const SOURCE_GLOBS = ['index.html', 'vite.config.js', 'package.json', 'src']

function walk(entry, acc = []) {
  const abs = join(ROOT, entry)
  const st = statSync(abs)
  if (st.isDirectory()) {
    for (const name of readdirSync(abs).sort()) walk(posix.join(entry, name), acc)
  } else {
    acc.push(entry)
  }
  return acc
}

function hashSources() {
  const files = SOURCE_GLOBS.flatMap((g) => walk(g)).sort()
  const h = createHash('sha256')
  const per = {}
  for (const rel of files) {
    const norm = rel.split(sep).join('/')
    const digest = createHash('sha256')
      .update(readFileSync(join(ROOT, rel)))
      .digest('hex')
      .slice(0, 16)
    per[norm] = digest
    h.update(norm)
    h.update('\0')
    h.update(digest)
    h.update('\0')
  }
  return { files: per, sourceDigest: h.digest('hex').slice(0, 16), count: files.length }
}

function buildManifest() {
  return {
    name: 'game-agent-build-manifest',
    apply: 'build',
    closeBundle() {
      const m = hashSources()
      writeFileSync(
        join(ROOT, OUT_DIR, 'build-manifest.json'),
        JSON.stringify({ ...m, generatedBy: 'vite.config.js' }, null, 2) + '\n',
        'utf8',
      )
      // eslint-disable-next-line no-console
      console.log(`\n[build-manifest] ${m.count} 个源文件 → ${m.sourceDigest}`)
    },
  }
}

export default defineConfig({
  base: '/static/',
  plugins: [vue(), buildManifest()],
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
    // 不压缩标识符：产物要能被"结构守卫"读到可辨认的函数名与中文文案
    // （守卫读 dist 的 JS 做子串断言，压缩后会全部失效）。
    minify: false,
    chunkSizeWarningLimit: 800,
  },
  server: {
    port: 5173,
    // 开发时把 /api 转给 uvicorn，前后端各自热更新、无 CORS
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
