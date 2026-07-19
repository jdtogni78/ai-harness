import React from 'react'
import { isLive, availability, fmtDate } from '../util.js'
import NoData from '../components/NoData.jsx'

const KIND_ICON = {
  video: '🎬', demo_script: '📝', explainer: '📖', screenshot: '🖼', other: '📎'
}

export default function Visuals({ report }) {
  if (!isLive(report, 'visual_review')) {
    return (
      <>
        <div className="eyebrow">Visual review</div>
        <h1>Visual artifacts</h1>
        <NoData section="visual_review" availability={availability(report, 'visual_review')} warnings={report.warnings} />
      </>
    )
  }
  const vr = report.visual_review || {}
  const items = vr.artifacts || []

  return (
    <>
      <div className="eyebrow">Visual review</div>
      <h1>{vr.done ? 'Reviewed ✓' : 'Not yet reviewed'}</h1>
      <h2>{items.length} artifact{items.length === 1 ? '' : 's'} on record</h2>

      {items.length === 0 ? (
        <NoData section="visual_review" availability="partial" warnings={['no artifacts found — no demo/video/explainer yet']} />
      ) : (
        <div className="list grow">
          {items.map((a, i) => (
            <div className="row" key={i}>
              <span style={{ fontSize: 22 }}>{KIND_ICON[a.kind] || KIND_ICON.other}</span>
              <span className="title">
                {a.name}
                <span style={{ color: 'var(--text-muted)', marginLeft: 8, fontSize: 12 }}>{a.kind}</span>
              </span>
              <span style={{ color: 'var(--text-muted)', fontSize: 12, whiteSpace: 'nowrap' }}>{fmtDate(a.when)}</span>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
