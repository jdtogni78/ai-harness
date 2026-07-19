// cos-console surface — local driver server.
//
// Two jobs:
//   1. Serve the StatusReport (from W0's probe, fixture fallback) over HTTP.
//   2. Model the SERVER-DRIVEN protocol: hold authoritative deck state
//      { project, slideIndex } and broadcast it to every connected surface
//      (desktop big-screen + phone) over a websocket. The eventual voice-brain
//      pushes the same intents this server accepts on POST /control.
//
// Intent shape (what the agent emits):
//   { type: "goto",  slide: 3 }                 // by index
//   { type: "goto",  widget: "tests" }          // by widget alias
//   { type: "show",  widget: "coverage" }       // alias of goto
//   { type: "next" } | { type: "prev" }
//   { type: "project", project: "familyfund" }  // switch data source
//   { type: "reload" }                          // re-run probe
//   { type: "ping" }

import express from 'express'
import { WebSocketServer } from 'ws'
import { createServer } from 'node:http'
import { getStatus, listProjects } from './probe.js'
import { SLIDES, slideIndexFor } from './deck.js'

const PORT = process.env.PORT || 8787
const PREFER_FIXTURE = process.env.SURFACE_FIXTURE === '1'

const app = express()
app.use(express.json())

// Authoritative deck state. The UI is a mirror of this.
const state = {
  project: process.env.SURFACE_PROJECT || 'dstrader',
  slideIndex: 0,
  slides: SLIDES.map(s => s.id),
  source: null,       // 'probe' | 'fixture'
  updatedAt: null
}

// Cache the last fetched report per project so a phone joining mid-talk gets
// the same numbers the desktop is showing.
const reportCache = new Map()

async function loadReport(project, { force = false } = {}) {
  if (!force && reportCache.has(project)) return reportCache.get(project)
  const { report, meta } = await getStatus(project, { preferFixture: PREFER_FIXTURE })
  const entry = { report, meta }
  reportCache.set(project, entry)
  state.source = meta.source
  return entry
}

// ---- HTTP API ---------------------------------------------------------------

app.get('/api/health', (_req, res) => res.json({ ok: true }))

app.get('/api/projects', async (_req, res) => {
  res.json({ projects: await listProjects() })
})

app.get('/api/status/:project', async (req, res) => {
  try {
    const { report, meta } = await loadReport(req.params.project, {
      force: req.query.refresh === '1'
    })
    res.json({ report, meta })
  } catch (e) {
    res.status(502).json({ error: e.message })
  }
})

app.get('/api/deck', (_req, res) => res.json(publicState()))

// The control endpoint the agent / a human driver / the demo CLI posts to.
app.post('/control', async (req, res) => {
  try {
    const applied = await applyIntent(req.body || {})
    res.json({ ok: true, applied, state: publicState() })
  } catch (e) {
    res.status(400).json({ ok: false, error: e.message })
  }
})

// ---- WebSocket broadcast ----------------------------------------------------

const server = createServer(app)
const wss = new WebSocketServer({ server, path: '/ws' })

function publicState() {
  return {
    type: 'deck',
    project: state.project,
    slideIndex: state.slideIndex,
    slideId: state.slides[state.slideIndex],
    slides: state.slides,
    source: state.source,
    updatedAt: state.updatedAt
  }
}

function broadcast(msg) {
  const data = JSON.stringify(msg)
  for (const client of wss.clients) {
    if (client.readyState === 1) client.send(data)
  }
}

function touch() {
  state.updatedAt = new Date().toISOString()
  broadcast(publicState())
}

async function applyIntent(intent) {
  const type = (intent.type || '').toLowerCase()
  switch (type) {
    case 'goto':
    case 'show': {
      const idx = slideIndexFor(intent)
      if (idx == null) throw new Error(`cannot resolve slide from ${JSON.stringify(intent)}`)
      state.slideIndex = idx
      touch()
      return { type: 'goto', slideIndex: idx, slideId: state.slides[idx] }
    }
    case 'next':
      state.slideIndex = Math.min(state.slides.length - 1, state.slideIndex + 1)
      touch()
      return { type: 'next', slideIndex: state.slideIndex }
    case 'prev':
      state.slideIndex = Math.max(0, state.slideIndex - 1)
      touch()
      return { type: 'prev', slideIndex: state.slideIndex }
    case 'project': {
      if (!intent.project) throw new Error('project intent needs {project}')
      state.project = intent.project
      state.slideIndex = 0
      await loadReport(state.project, { force: true })
      touch()
      broadcast({ type: 'data', project: state.project })
      return { type: 'project', project: state.project }
    }
    case 'reload':
      await loadReport(state.project, { force: true })
      touch()
      broadcast({ type: 'data', project: state.project })
      return { type: 'reload', project: state.project }
    case 'ping':
      return { type: 'pong' }
    default:
      throw new Error(`unknown intent type: ${intent.type}`)
  }
}

wss.on('connection', (ws) => {
  // New surface joins -> hand it current state immediately.
  ws.send(JSON.stringify(publicState()))
  // Surfaces may also emit intents (e.g. keyboard nav on the desktop), which we
  // treat exactly like agent intents so every screen stays in lockstep.
  ws.on('message', async (raw) => {
    try {
      const intent = JSON.parse(raw.toString())
      await applyIntent(intent)
    } catch (e) {
      ws.send(JSON.stringify({ type: 'error', error: e.message }))
    }
  })
})

server.listen(PORT, async () => {
  try {
    await loadReport(state.project)
  } catch (e) {
    console.warn('[surface] initial report load failed:', e.message)
  }
  state.updatedAt = new Date().toISOString()
  console.log(`[surface] driver server on http://localhost:${PORT}`)
  console.log(`[surface] project=${state.project} source=${state.source} slides=${state.slides.length}`)
  console.log(`[surface] drive it:  node server/drive.js goto tests`)
})
