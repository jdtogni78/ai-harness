import React from 'react'
import { isLive, availability, fmtDate } from '../util.js'
import NoData from '../components/NoData.jsx'

export default function Decisions({ report }) {
  if (!isLive(report, 'decisions')) {
    return (
      <>
        <div className="eyebrow">Decisions timeline</div>
        <h1>Decisions</h1>
        <NoData section="decisions" availability={availability(report, 'decisions')} warnings={report.warnings} />
      </>
    )
  }
  const decisions = (report.decisions || [])
    .slice()
    .sort((a, b) => new Date(b.when) - new Date(a.when))

  return (
    <>
      <div className="eyebrow">Decisions timeline</div>
      <h1>Recent decisions</h1>
      <h2>{decisions.length} on record · newest first</h2>

      {decisions.length === 0 ? (
        <NoData section="decisions" availability="partial" warnings={['no merge commits or close-work briefs found']} />
      ) : (
        <div className="timeline grow">
          {decisions.map((d, i) => (
            <div className="tl-item" key={i}>
              <div className="when">
                {fmtDate(d.when)}
                <div className="src">{d.source}{d.ref ? ` · ${d.ref}` : ''}</div>
              </div>
              <div className="what">{d.summary}</div>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
