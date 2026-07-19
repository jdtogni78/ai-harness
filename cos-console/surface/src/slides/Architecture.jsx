import React, { useEffect, useRef, useState } from 'react'

// Optional Mermaid slide: the four-plane cos-console architecture. Mermaid is
// lazy-imported so it never blocks the rest of the deck; if it fails we fall
// back to a plain text version rather than a broken chart.
const GRAPH = `flowchart LR
  subgraph Data["Data plane (W0 · FROZEN v1.0)"]
    P["status_mcp.probe"] --> R["StatusReport JSON"]
  end
  subgraph Brain["Brain — Claude"]
    B["Agent SDK\\ntool-use: say + show"]
  end
  subgraph Voice["Voice loop (W1/W2/W3)"]
    V["mic → STT → brain → TTS"]
  end
  subgraph Surface["Surface — this PWA (W4)"]
    S["server-driven deck\\ndesktop + phone"]
  end
  R --> B
  V <--> B
  B -- "goto / show intents" --> DS["driver server"]
  DS -- "websocket" --> S
  R --> DS
`

export default function Architecture() {
  const ref = useRef(null)
  const [err, setErr] = useState(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const mermaid = (await import('mermaid')).default
        mermaid.initialize({
          startOnLoad: false,
          theme: 'dark',
          themeVariables: {
            background: '#101014',
            primaryColor: '#232328',
            primaryTextColor: '#ffffff',
            primaryBorderColor: '#37373f',
            lineColor: '#3987e5',
            fontSize: '15px'
          }
        })
        const { svg } = await mermaid.render('cos-arch', GRAPH)
        if (!cancelled && ref.current) ref.current.innerHTML = svg
      } catch (e) {
        if (!cancelled) setErr(e.message)
      }
    })()
    return () => { cancelled = true }
  }, [])

  return (
    <>
      <div className="eyebrow">How it fits together</div>
      <h1>Architecture</h1>
      <div className="card grow" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'auto' }}>
        {err ? (
          <div style={{ color: 'var(--text-secondary)' }}>
            <p>Data → Brain (Claude) → decides say + show.</p>
            <p>Voice loop ↔ Brain. Brain → driver server → (websocket) → this surface.</p>
            <p style={{ color: 'var(--text-muted)', fontSize: 12 }}>(mermaid failed to load: {err})</p>
          </div>
        ) : (
          <div ref={ref} style={{ width: '100%', display: 'flex', justifyContent: 'center' }} />
        )}
      </div>
    </>
  )
}
