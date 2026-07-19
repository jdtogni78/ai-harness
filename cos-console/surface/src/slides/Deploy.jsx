import React from 'react'
import { isLive, availability, fmtDate, relTime } from '../util.js'
import NoData from '../components/NoData.jsx'

export default function Deploy({ report }) {
  if (!isLive(report, 'deploy')) {
    return (
      <>
        <div className="eyebrow">Deployment</div>
        <h1>Deploy status</h1>
        <NoData section="deploy" availability={availability(report, 'deploy')} warnings={report.warnings} />
      </>
    )
  }
  const d = report.deploy || {}
  const tone = d.status === 'ok' ? 'good' : d.status === 'failed' ? 'bad' : ''
  const toneColor = d.status === 'ok' ? 'var(--good)' : d.status === 'failed' ? 'var(--critical)' : 'var(--neutral)'

  return (
    <>
      <div className="eyebrow">Deployment</div>
      <h1 style={{ color: toneColor }}>{(d.status || 'unknown').toUpperCase()}</h1>
      <h2>{d.env || d.target || 'target unknown'} · {relTime(d.last_deployed_at)}</h2>

      <div className="tiles">
        <div className={`tile ${tone}`}>
          <div className="label">Status</div>
          <div className="value" style={{ fontSize: 'clamp(22px,3.6vw,36px)' }}>{d.status || 'unknown'}</div>
          <div className="sub">{d.target || d.env || ''}</div>
        </div>
        <div className="tile">
          <div className="label">Last deployed</div>
          <div className="value" style={{ fontSize: 'clamp(16px,2.6vw,24px)' }}>{fmtDate(d.last_deployed_at, { time: true })}</div>
          <div className="sub">{relTime(d.last_deployed_at)}</div>
        </div>
        <div className="tile">
          <div className="label">Commit</div>
          <div className="value" style={{ fontSize: 'clamp(18px,3vw,28px)', fontFamily: 'ui-monospace, monospace' }}>
            {d.commit ? d.commit.slice(0, 10) : '—'}
          </div>
          <div className="sub">{d.duration_s != null ? `${d.duration_s}s to deploy` : ''}</div>
        </div>
      </div>

      {d.source && (
        <div className="card"><h3>Source</h3>
          <code style={{ color: 'var(--text-secondary)', fontSize: 13 }}>{d.source}</code>
          <p style={{ color: 'var(--text-muted)', fontSize: 12, marginBottom: 0 }}>Reporting layer only — this surface never deploys.</p>
        </div>
      )}
    </>
  )
}
