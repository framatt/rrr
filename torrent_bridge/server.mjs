#!/usr/bin/env node
/**
 * Private Mirror WebTorrent bridge.
 *
 * A small loopback-only HTTP service that owns one WebTorrent client. Flask
 * proxies /api/torrents/* to this process so the browser never talks to the
 * BitTorrent swarm directly and the bridge port stays private.
 *
 * Endpoints (all JSON unless noted):
 *   GET  /health
 *   POST /add              { magnet, fileIndex?, fileHint? } -> torrent + files
 *   GET  /torrent/:id
 *   GET  /status/:id
 *   GET  /stream/:id/:index        range-aware video stream
 *   POST /select           { id, index }
 *   POST /remove           { id }
 *   GET  /list
 */

import http from 'node:http'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import WebTorrent from 'webtorrent'
import parseTorrent from 'parse-torrent'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const PORT = Number(process.env.TORRENT_BRIDGE_PORT || 0)
const HOST = process.env.TORRENT_BRIDGE_HOST || '127.0.0.1'
const TOKEN = process.env.TORRENT_BRIDGE_TOKEN || ''
const DATA_DIR = path.resolve(
  process.env.TORRENT_BRIDGE_DATA || path.join(os.tmpdir(), 'private-mirror-torrents'),
)
const MAX_TORRENTS = Math.max(1, Number(process.env.TORRENT_MAX || 24))
const IDLE_MS = Math.max(60_000, Number(process.env.TORRENT_IDLE_MS || 30 * 60_000))
const EXIT_MS = Math.max(0, Number(process.env.TORRENT_BRIDGE_IDLE_EXIT || 2 * 60 * 60_000))
const METADATA_TIMEOUT_MS = Math.max(5_000, Number(process.env.TORRENT_METADATA_TIMEOUT || 45_000))
const KEEP_DATA = String(process.env.TORRENT_KEEP_DATA || '').toLowerCase() === 'true'
const TORRENT_PORT = Number(process.env.TORRENT_PORT || 0)
const TORRENT_DHT_PORT = Number(process.env.TORRENT_DHT_PORT || 0)

const VIDEO_RE = /\.(mp4|m4v|mkv|webm|mov|avi|ts|m2ts|flv|wmv|ogv)$/i
const AUDIO_RE = /\.(mp3|m4a|aac|flac|ogg|opus|wav)$/i
const SUBTITLE_RE = /\.(srt|vtt|ass|ssa|sub|idx)$/i

const TRACKERS = [
  'udp://tracker.opentrackr.org:1337/announce',
  'udp://open.stealth.si:80/announce',
  'udp://tracker.torrent.eu.org:451/announce',
  'udp://tracker.dler.org:6969/announce',
  'udp://open.dstud.io:6969/announce',
  'udp://exodus.desync.com:6969/announce',
  'udp://tracker.openbittorrent.com:6969/announce',
  'udp://tracker.tiny-vps.com:6969/announce',
  'udp://explodie.org:6969/announce',
  'udp://tracker1.bt.moack.co.kr:80/announce',
  'udp://tracker.qu.ax:6969/announce',
  'udp://tracker.leech.ie:1337/announce',
  'udp://tracker.ololosh.space:6969/announce',
  'udp://tracker.theoks.net:6969/announce',
  'wss://tracker.openwebtorrent.com',
  'wss://tracker.btorrent.xyz',
  'wss://tracker.webtorrent.dev',
]

fs.mkdirSync(DATA_DIR, { recursive: true })

const client = new WebTorrent({
  maxConns: Number(process.env.TORRENT_MAX_CONNS || 80),
  dht: true,
  tracker: { announce: TRACKERS },
  lsd: true,
  utPex: true,
  path: DATA_DIR,
  torrentPort: TORRENT_PORT,
  dhtPort: TORRENT_DHT_PORT,
})

/** @type {Map<string, {infoHash:string,magnet:string,torrent:any,files:any[],selected:number,addedAt:number,lastAccess:number,ready:boolean,error:string,waiters:Set<Function>}>} */
const entries = new Map()
let lastRequest = Date.now()

function json(res, status, payload) {
  if (res.writableEnded || res.destroyed) return
  const body = Buffer.from(`${JSON.stringify(payload)}\n`, 'utf8')
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    'Cache-Control': 'no-store',
  })
  res.end(body)
}

function fail(res, status, message, extra = {}) {
  json(res, status, { ok: false, error: message, ...extra })
}

function readJson(req, limit = 128 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > limit) {
        reject(new Error('Request body is too large.'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      if (!chunks.length) return resolve({})
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')))
      } catch {
        reject(new Error('Request body is not valid JSON.'))
      }
    })
    req.on('error', reject)
  })
}

function normalize(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim()
}

function isVideo(file) {
  return VIDEO_RE.test(file.name || file.path || '')
}

function contentTypeFor(name, type) {
  if (type && type !== 'application/octet-stream') return type
  const ext = (path.extname(name || '') || '').replace('.', '').toLowerCase()
  const types = {
    mp4: 'video/mp4',
    m4v: 'video/mp4',
    webm: 'video/webm',
    mkv: 'video/x-matroska',
    mov: 'video/quicktime',
    avi: 'video/x-msvideo',
    ts: 'video/mp2t',
    m2ts: 'video/mp2t',
    ogv: 'video/ogg',
    flv: 'video/x-flv',
    wmv: 'video/x-ms-wmv',
    mp3: 'audio/mpeg',
    m4a: 'audio/mp4',
    aac: 'audio/aac',
    flac: 'audio/flac',
    ogg: 'audio/ogg',
    opus: 'audio/ogg',
    wav: 'audio/wav',
  }
  return types[ext] || type || 'application/octet-stream'
}

function fileRecord(file, index) {
  const name = file.name || path.basename(file.path || '')
  const ext = (path.extname(name) || '').replace('.', '').toLowerCase()
  return {
    index,
    name,
    path: file.path || name,
    length: Number(file.length || 0),
    type: contentTypeFor(name, file.type),
    ext,
    isVideo: isVideo(file),
    isAudio: AUDIO_RE.test(name),
    isSubtitle: SUBTITLE_RE.test(name),
  }
}

function chooseFile(torrent, fileIndex, fileHint) {
  const files = torrent.files || []
  if (!files.length) return -1
  const hint = normalize(fileHint)
  let chosen = -1

  if (Number.isInteger(fileIndex) && fileIndex >= 0 && fileIndex < files.length) {
    chosen = fileIndex
  }

  if (hint) {
    let best = -1
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index]
      if (!isVideo(file)) continue
      const target = normalize(`${file.name} ${file.path}`)
      if (target.includes(hint)) {
        if (best < 0 || file.length > files[best].length) best = index
      }
    }
    if (best >= 0) chosen = best
  }

  if (chosen < 0 || !isVideo(files[chosen])) {
    let best = -1
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index]
      if (!file.length || !isVideo(file)) continue
      if (best < 0 || file.length > files[best].length) best = index
    }
    if (best >= 0) chosen = best
  }

  if (chosen < 0) {
    for (let index = 0; index < files.length; index += 1) {
      if (files[index].length && (chosen < 0 || files[index].length > files[chosen].length)) chosen = index
    }
  }

  if (chosen >= 0) selectOnly(torrent, chosen)
  return chosen
}

function selectOnly(torrent, index) {
  if (!torrent.ready || !torrent.files || !torrent.files[index]) return
  for (let i = 0; i < torrent.files.length; i += 1) {
    try {
      torrent.files[i].deselect()
    } catch {
      /* selection is best-effort */
    }
  }
  try {
    torrent.files[index].select()
  } catch {
    /* ignore */
  }
}

function entrySnapshot(entry) {
  const torrent = entry.torrent
  const files = entry.files || []
  const selected = files[entry.selected] || null
  const selectedFile =
    torrent && entry.selected >= 0 && Array.isArray(torrent.files) ? torrent.files[entry.selected] : null
  const fileProgress = selectedFile ? Number(selectedFile.progress || 0) : 0
  const fileLength = selectedFile ? Number(selectedFile.length || 0) : 0
  return {
    ok: true,
    id: entry.infoHash,
    infoHash: entry.infoHash,
    name: (torrent && torrent.name) || selected?.name || '',
    ready: Boolean(entry.ready),
    error: entry.error || '',
    selected: entry.selected,
    file: selected,
    files,
    downloaded: Number(torrent?.downloaded || 0),
    uploaded: Number(torrent?.uploaded || 0),
    length: Number(torrent?.length || 0),
    progress: Number(torrent?.progress || 0),
    fileProgress,
    fileDownloaded: selectedFile ? Number(selectedFile.downloaded || 0) : 0,
    downloadSpeed: Number(torrent?.downloadSpeed || 0),
    uploadSpeed: Number(torrent?.uploadSpeed || 0),
    numPeers: Number(torrent?.numPeers || 0),
    timeRemaining: Number(torrent?.timeRemaining || 0),
    ratio: Number(torrent?.ratio || 0),
    done: Boolean(torrent?.done) || Boolean(fileLength && fileProgress >= 0.999),
    dataDir: DATA_DIR,
    addedAt: entry.addedAt,
  }
}

function waitForEntry(entry, timeoutMs) {
  if (entry.ready) return Promise.resolve(entry)
  if (entry.error) return Promise.reject(new Error(entry.error))
  return new Promise((resolve, reject) => {
    let settled = false
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      entry.waiters.delete(onReady)
      entry.waiters.delete(onError)
      reject(new Error('Timed out waiting for torrent metadata. The swarm may have no reachable peers.'))
    }, timeoutMs)

    const onReady = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      entry.waiters.delete(onReady)
      entry.waiters.delete(onError)
      resolve(entry)
    }
    const onError = (error) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      entry.waiters.delete(onReady)
      entry.waiters.delete(onError)
      reject(error instanceof Error ? error : new Error(String(error)))
    }
    entry.waiters.add(onReady)
    entry.waiters.add(onError)
  })
}

function settle(entry, error) {
  const waiters = [...entry.waiters]
  entry.waiters.clear()
  for (const waiter of waiters) {
    try {
      if (error) waiter(error)
      else waiter()
    } catch {
      /* watchers handle their own failures */
    }
  }
}

function attachTorrent(entry, torrent) {
  entry.torrent = torrent
  if (torrent.ready) {
    onReady()
    return
  }
  torrent.once('ready', onReady)
  torrent.once('error', (error) => {
    entry.error = error?.message || String(error)
    settle(entry, new Error(entry.error))
  })

  function onReady() {
    if (entry.ready) return
    entry.ready = true
    entry.error = ''
    entry.files = (torrent.files || []).map(fileRecord)
    entry.selected = chooseFile(torrent, entry.requestedIndex, entry.requestedHint)
    settle(entry)
  }
}

async function addTorrent(magnet, fileIndex, fileHint) {
  const parsed = await parseTorrent(magnet)
  if (!parsed || !parsed.infoHash) throw new Error('The torrent magnet is invalid.')
  const infoHash = String(parsed.infoHash).toLowerCase()

  let entry = entries.get(infoHash)
  if (entry) {
    entry.lastAccess = Date.now()
    if (!entry.ready) {
      if (Number.isInteger(fileIndex)) entry.requestedIndex = fileIndex
      if (fileHint) entry.requestedHint = fileHint
    }
    if (entry.ready && Number.isInteger(fileIndex) && fileIndex !== entry.selected) {
      entry.selected = chooseFile(entry.torrent, fileIndex, fileHint)
    } else if (entry.ready && fileHint) {
      entry.selected = chooseFile(entry.torrent, entry.selected, fileHint)
    }
    return waitForEntry(entry, METADATA_TIMEOUT_MS)
  }

  const existing = await client.get(magnet)
  const torrent = existing || client.add(magnet, { path: DATA_DIR, announce: TRACKERS })
  entry = {
    infoHash,
    magnet,
    torrent,
    files: [],
    selected: -1,
    addedAt: Date.now(),
    lastAccess: Date.now(),
    ready: false,
    error: '',
    waiters: new Set(),
    requestedIndex: Number.isInteger(fileIndex) ? fileIndex : -1,
    requestedHint: fileHint || '',
  }
  entries.set(infoHash, entry)
  attachTorrent(entry, torrent)
  evictIfNeeded()
  return waitForEntry(entry, METADATA_TIMEOUT_MS)
}

function touch(entry) {
  entry.lastAccess = Date.now()
}

function destroyEntry(entry, keepData = KEEP_DATA) {
  entries.delete(entry.infoHash)
  for (const waiter of entry.waiters) {
    try {
      waiter(new Error('Torrent was removed.'))
    } catch {
      /* ignore */
    }
  }
  entry.waiters.clear()
  try {
    entry.torrent.destroy({ destroyStore: !keepData }, () => {})
  } catch {
    /* already destroyed */
  }
}

function evictIfNeeded() {
  if (entries.size <= MAX_TORRENTS) return
  const ordered = [...entries.values()].sort((a, b) => a.lastAccess - b.lastAccess)
  for (const entry of ordered) {
    if (entries.size <= MAX_TORRENTS) break
    // Eviction is not a user delete: keep bytes so a download can resume or play later.
    destroyEntry(entry, true)
  }
}

function parseRange(header, size) {
  if (!header) return { start: 0, end: Math.max(0, size - 1), partial: false }
  const match = /^bytes=(\d*)-(\d*)/i.exec(String(header))
  if (!match) return null
  let start
  let end
  if (match[1] === '' && match[2] === '') return null
  if (match[1] === '') {
    const suffix = Number(match[2])
    if (!Number.isFinite(suffix) || suffix <= 0) return null
    start = Math.max(0, size - suffix)
    end = size - 1
  } else {
    start = Number(match[1])
    end = match[2] === '' ? size - 1 : Number(match[2])
  }
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || start >= size) return null
  end = Math.min(size - 1, Math.max(start, end))
  return { start, end, partial: true }
}

async function handleStream(req, res, entry, index) {
  touch(entry)
  await waitForEntry(entry, METADATA_TIMEOUT_MS)
  const file = entry.torrent.files[index]
  if (!file) return fail(res, 404, 'Torrent file index was not found.')
  if (!file.length) return fail(res, 416, 'Torrent file is empty.')
  if (entry.selected !== index) {
    entry.selected = chooseFile(entry.torrent, index, '')
    entry.files = (entry.torrent.files || []).map(fileRecord)
  }

  const range = parseRange(req.headers.range, file.length)
  if (!range) {
    res.writeHead(416, {
      'Content-Range': `bytes */${file.length}`,
      'Accept-Ranges': 'bytes',
    })
    return res.end()
  }

  const { start, end, partial } = range
  const headers = {
    'Content-Type': contentTypeFor(file.name, file.type),
    'Accept-Ranges': 'bytes',
    'Content-Length': String(end - start + 1),
    'Cache-Control': 'no-store',
    'X-Torrent-Info-Hash': entry.infoHash,
    'X-Torrent-File-Index': String(index),
  }
  if (partial) headers['Content-Range'] = `bytes ${start}-${end}/${file.length}`

  if (req.method === 'HEAD') {
    res.writeHead(partial ? 206 : 200, headers)
    return res.end()
  }

  res.writeHead(partial ? 206 : 200, headers)
  const stream = file.createReadStream({ start, end })
  const heartbeat = setInterval(() => touch(entry), 60_000)
  let closed = false
  const close = () => {
    if (closed) return
    closed = true
    clearInterval(heartbeat)
    try {
      stream.destroy()
    } catch {
      /* ignore */
    }
  }
  res.on('close', close)
  req.on('aborted', close)
  stream.on('error', () => {
    close()
    if (!res.writableEnded) res.destroy()
  })
  stream.pipe(res)
}

async function route(req, res) {
  lastRequest = Date.now()
  if (TOKEN && req.headers['x-bridge-token'] !== TOKEN) {
    return fail(res, 401, 'Bridge token was rejected.')
  }
  const url = new URL(req.url || '/', `http://${HOST}:${PORT || 1}`)
  const parts = url.pathname.split('/').filter(Boolean)

  if (req.method === 'GET' && url.pathname === '/health') {
    return json(res, 200, {
      ok: true,
      service: 'private-mirror-torrent-bridge',
      version: 1,
      webtorrent: WebTorrent.VERSION || '',
      torrents: entries.size,
      maxTorrents: MAX_TORRENTS,
      dataDir: DATA_DIR,
      listen: `${HOST}:${PORT || 'ephemeral'}`,
      torrentPort: client.torrentPort || TORRENT_PORT,
      dhtPort: client.dhtPort || TORRENT_DHT_PORT,
      uptime: Math.round(process.uptime()),
    })
  }

  if (req.method === 'POST' && url.pathname === '/add') {
    const body = await readJson(req)
    const magnet = String(body.magnet || '').trim()
    if (!magnet.startsWith('magnet:?')) return fail(res, 400, 'A magnet URI is required.')
    const fileIndex = Number.isInteger(body.fileIndex) ? body.fileIndex : -1
    const fileHint = typeof body.fileHint === 'string' ? body.fileHint.slice(0, 300) : ''
    const entry = await addTorrent(magnet, fileIndex, fileHint)
    return json(res, 200, entrySnapshot(entry))
  }

  if (req.method === 'GET' && parts[0] === 'torrent' && parts[1]) {
    const entry = entries.get(String(parts[1]).toLowerCase())
    if (!entry) return fail(res, 404, 'Torrent is not active.')
    touch(entry)
    return json(res, 200, entrySnapshot(entry))
  }

  if (req.method === 'GET' && parts[0] === 'status' && parts[1]) {
    const entry = entries.get(String(parts[1]).toLowerCase())
    if (!entry) return fail(res, 404, 'Torrent is not active.')
    touch(entry)
    return json(res, 200, entrySnapshot(entry))
  }

  if ((req.method === 'GET' || req.method === 'HEAD') && parts[0] === 'stream' && parts[1]) {
    const entry = entries.get(String(parts[1]).toLowerCase())
    if (!entry) return fail(res, 404, 'Torrent is not active.')
    const index = Number(parts[2])
    if (!Number.isInteger(index) || index < 0) return fail(res, 400, 'A valid file index is required.')
    return handleStream(req, res, entry, index)
  }

  if (req.method === 'POST' && url.pathname === '/select') {
    const body = await readJson(req)
    const entry = entries.get(String(body.id || '').toLowerCase())
    if (!entry) return fail(res, 404, 'Torrent is not active.')
    await waitForEntry(entry, METADATA_TIMEOUT_MS)
    const index = Number(body.index)
    if (!Number.isInteger(index) || !entry.torrent.files[index]) return fail(res, 400, 'File index was not found.')
    entry.selected = chooseFile(entry.torrent, index, '')
    entry.files = (entry.torrent.files || []).map(fileRecord)
    touch(entry)
    return json(res, 200, entrySnapshot(entry))
  }

  if (req.method === 'POST' && url.pathname === '/remove') {
    const body = await readJson(req)
    const entry = entries.get(String(body.id || '').toLowerCase())
    if (!entry) return json(res, 200, { ok: true, removed: false })
    destroyEntry(entry, Boolean(body.keepData) || KEEP_DATA)
    return json(res, 200, { ok: true, removed: true })
  }

  if (req.method === 'GET' && url.pathname === '/list') {
    return json(res, 200, { ok: true, torrents: [...entries.values()].map(entrySnapshot) })
  }

  return fail(res, 404, 'Unknown bridge endpoint.')
}

const server = http.createServer((req, res) => {
  route(req, res).catch((error) => {
    if (!res.headersSent) fail(res, 500, error?.message || String(error))
    else res.destroy()
  })
})

setInterval(() => {
  const now = Date.now()
  for (const entry of [...entries.values()]) {
    if (now - entry.lastAccess > IDLE_MS) {
      const file = entry.torrent?.files?.[entry.selected]
      const complete =
        Boolean(entry.torrent?.done) || Boolean(file && file.length && file.progress >= 0.999)
      destroyEntry(entry, complete)
    }
  }
  if (EXIT_MS && entries.size === 0 && now - lastRequest > EXIT_MS) {
    console.log('[bridge] idle exit')
    shutdown('idle')
  }
}, 60_000).unref()

function shutdown(signal) {
  console.log(`[bridge] ${signal}; shutting down`)
  server.close(() => {
    try {
      client.destroy(() => process.exit(0))
    } catch {
      process.exit(0)
    }
  })
  setTimeout(() => process.exit(0), 3_000).unref()
}

process.on('SIGINT', () => shutdown('SIGINT'))
process.on('SIGTERM', () => shutdown('SIGTERM'))
process.on('uncaughtException', (error) => console.error('[bridge] uncaught', error))
process.on('unhandledRejection', (error) => console.error('[bridge] unhandled', error))

server.listen(PORT, HOST, () => {
  const address = server.address()
  console.log(
    `[bridge] listening on http://${HOST}:${address.port} · data ${DATA_DIR} · webtorrent ${WebTorrent.VERSION || 'unknown'}`,
  )
})
