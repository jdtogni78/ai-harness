import React from 'react'

// The honesty layer, made visible. Rendered wherever a section is `partial` or
// `unavailable` — NEVER a zero-value chart. This is the core validating-manager
// requirement: "unknown" must not look like "zero".
export default function NoData({ section, availability, warnings = [] }) {
  const why = availability === 'partial'
    ? 'Some of this signal was reachable, but the numbers are incomplete — not shown to avoid a misleading read.'
    : `No data was collected for "${section}". This is NOT zero — the source could not be reached.`
  return (
    <div className="nodata" role="status" aria-label={`no data for ${section}`}>
      <div className="k">⚠ {availability === 'partial' ? 'partial data' : 'no data'} · {section}</div>
      <div className="msg">{why}</div>
      {warnings.length > 0 && (
        <div className="why">{warnings.join(' · ')}</div>
      )}
      <div className="why">Say the word and I'll try to collect it — e.g. run the suite.</div>
    </div>
  )
}
