import { useCallback, useEffect, useRef, useState } from 'react'

// Connects to the driver server: subscribes to authoritative deck state over
// websocket and fetches the StatusReport for the current project. The surface
// is a MIRROR — it never decides which slide is shown, the server does. Local
// key/tap nav is just another intent sent up to the server.

function wsUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/ws`
}

export function useDeck() {
  const [deck, setDeck] = useState({
    project: 'dstrader', slideIndex: 0, slideId: 'overview', slides: [], source: null
  })
  const [connected, setConnected] = useState(false)
  const [report, setReport] = useState(null)
  const [meta, setMeta] = useState(null)
  const [loadingData, setLoadingData] = useState(true)
  const wsRef = useRef(null)
  const projectRef = useRef(deck.project)

  const fetchReport = useCallback(async (project, refresh = false) => {
    setLoadingData(true)
    try {
      const res = await fetch(`/api/status/${project}${refresh ? '?refresh=1' : ''}`)
      const body = await res.json()
      setReport(body.report)
      setMeta(body.meta)
    } catch (e) {
      setReport(null)
      setMeta({ error: e.message })
    } finally {
      setLoadingData(false)
    }
  }, [])

  // Websocket lifecycle with auto-reconnect.
  useEffect(() => {
    let closed = false
    let retry
    function connect() {
      const ws = new WebSocket(wsUrl())
      wsRef.current = ws
      ws.onopen = () => setConnected(true)
      ws.onclose = () => {
        setConnected(false)
        if (!closed) retry = setTimeout(connect, 1200)
      }
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data)
        if (msg.type === 'deck') {
          setDeck(msg)
          if (msg.project !== projectRef.current) {
            projectRef.current = msg.project
            fetchReport(msg.project)
          }
        } else if (msg.type === 'data') {
          fetchReport(msg.project, true)
        }
      }
    }
    connect()
    return () => { closed = true; clearTimeout(retry); wsRef.current?.close() }
  }, [fetchReport])

  // Initial data load.
  useEffect(() => { fetchReport(projectRef.current) }, [fetchReport])

  // Send an intent up to the server (local nav mirrors to all surfaces).
  const send = useCallback((intent) => {
    const ws = wsRef.current
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(intent))
  }, [])

  return { deck, connected, report, meta, loadingData, send, refresh: () => fetchReport(deck.project, true) }
}
