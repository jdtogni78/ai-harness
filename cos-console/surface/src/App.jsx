import React, { useEffect } from 'react'
import { useDeck } from './useDeck.js'
import { REGISTRY } from './slides/registry.js'

export default function App() {
  const { deck, connected, report, meta, loadingData, send } = useDeck()

  // Local nav (keyboard on desktop, tap on filmstrip) is sent UP to the server
  // as an intent so every connected surface stays in lockstep — same channel
  // the voice-brain will use.
  useEffect(() => {
    function onKey(e) {
      if (e.key === 'ArrowRight' || e.key === ' ' || e.key === 'PageDown') { send({ type: 'next' }); e.preventDefault() }
      else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { send({ type: 'prev' }); e.preventDefault() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [send])

  const activeIndex = deck.slideIndex ?? 0
  const source = meta?.source || deck.source

  return (
    <div className="app">
      <div className="topbar">
        <span className="project">🎙 {deck.project}</span>
        {source && <span className={`badge ${source === 'probe' ? 'live' : 'fixture'}`}>{source === 'probe' ? 'live probe' : 'fixture'}</span>}
        <span className="spacer" />
        <span className="meta">
          <span><span className={`conn-dot ${connected ? 'up' : 'down'}`} /> {connected ? 'driven' : 'reconnecting'}</span>
          {report?.generated_at && <span>· {new Date(report.generated_at).toLocaleTimeString()}</span>}
        </span>
      </div>

      <div className="stage">
        {(!report || loadingData) && (
          <div className="slide active" style={{ alignItems: 'center', justifyContent: 'center' }}>
            <div style={{ color: 'var(--text-muted)' }}>{loadingData ? 'Loading status…' : (meta?.error || 'No report')}</div>
          </div>
        )}
        {report && REGISTRY.map((s, i) => {
          const Comp = s.Component
          return (
            <section key={s.id} className={`slide ${i === activeIndex ? 'active' : ''}`} aria-hidden={i !== activeIndex}>
              {/* Mount only nearby slides to keep charts cheap on phones. */}
              {Math.abs(i - activeIndex) <= 1 ? <Comp report={report} meta={meta} /> : null}
            </section>
          )
        })}
      </div>

      <div className="filmstrip">
        {REGISTRY.map((s, i) => (
          <button key={s.id} className={`dot ${i === activeIndex ? 'active' : ''}`}
                  onClick={() => send({ type: 'goto', slide: i })}>
            {i + 1}. {s.label}
          </button>
        ))}
        <span className="nav-hint">← → to move · driven by the agent</span>
      </div>
    </div>
  )
}
