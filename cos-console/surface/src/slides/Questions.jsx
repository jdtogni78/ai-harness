import React from 'react'

// Open questions are free-text prompts for the operator. Always renderable (no
// availability gate — an empty list is a true "nothing to ask", not a fabrication).
export default function Questions({ report }) {
  const qs = report.open_questions || []
  const warnings = report.warnings || []

  return (
    <>
      <div className="eyebrow">Over to you</div>
      <h1>Open questions</h1>
      <h2>{qs.length ? 'These need your call' : 'Nothing outstanding'}</h2>

      {qs.length === 0 ? (
        <div className="card"><h3>No open questions</h3>
          <p style={{ margin: 0, color: 'var(--text-secondary)' }}>The probe surfaced nothing needing a decision right now.</p>
        </div>
      ) : (
        <div className="list grow">
          {qs.map((q, i) => (
            <div className="row" key={i}>
              <span className="pill" style={{ background: 'color-mix(in srgb, var(--warning) 25%, transparent)', color: 'var(--warning)' }}>Q{i + 1}</span>
              <span className="title" style={{ whiteSpace: 'normal' }}>{q}</span>
            </div>
          ))}
        </div>
      )}

      {warnings.length > 0 && (
        <div className="card">
          <h3>Collection warnings</h3>
          <div className="list">
            {warnings.map((w, i) => (
              <div key={i} style={{ color: 'var(--text-muted)', fontSize: 13 }}>· {w}</div>
            ))}
          </div>
        </div>
      )}
    </>
  )
}
