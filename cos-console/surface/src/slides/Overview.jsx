import React from 'react'
import { isLive, availability, fmtDate, num } from '../util.js'

// Project overview: the "where does this stand" headline. KPI tiles each guard
// their own availability so one dead signal doesn't fabricate the others.
export default function Overview({ report }) {
  const t = report.tickets || {}
  const ticketsLive = isLive(report, 'tickets')
  const testsLive = isLive(report, 'tests')
  const deployLive = isLive(report, 'deploy')
  const dep = report.deploy || {}
  const tests = report.tests || {}
  const questions = report.open_questions || []

  const deployTone = dep.status === 'ok' ? 'good' : dep.status === 'failed' ? 'bad' : ''

  return (
    <>
      <div className="eyebrow">Project overview</div>
      <h1>{report.project}</h1>
      <h2>Status assembled {fmtDate(report.generated_at, { time: true })} · schema v{report.schema_version}</h2>

      <div className="tiles">
        <div className="tile good">
          <div className="label">Tickets done</div>
          <div className="value">{ticketsLive ? num(t.done) : '—'}</div>
          <div className="sub">{ticketsLive ? `of ${num(t.total)} on the board` : 'board unavailable'}</div>
        </div>
        <div className="tile">
          <div className="label">Still to do</div>
          <div className="value">{ticketsLive ? num(t.todo) : '—'}</div>
          <div className="sub">{ticketsLive ? `${num(t.in_progress)} in progress` : 'board unavailable'}</div>
        </div>
        <div className={`tile ${(t.blocked > 0) ? 'bad' : ''}`}>
          <div className="label">Blocked</div>
          <div className="value">{ticketsLive ? num(t.blocked) : '—'}</div>
          <div className="sub">{ticketsLive && t.blocked === 0 ? 'nothing blocked' : ''}</div>
        </div>
        <div className="tile">
          <div className="label">Tests</div>
          <div className="value">{testsLive ? num(tests.passing) : 'n/a'}</div>
          <div className="sub">{testsLive ? `of ${num(tests.count)} passing` : `not available (${availability(report, 'tests')})`}</div>
        </div>
        <div className={`tile ${deployTone}`}>
          <div className="label">Prod deploy</div>
          <div className="value" style={{ fontSize: 'clamp(20px,3.4vw,34px)' }}>
            {deployLive ? (dep.status || 'unknown') : '—'}
          </div>
          <div className="sub">{deployLive ? fmtDate(dep.last_deployed_at) : 'deploy log unavailable'}</div>
        </div>
        <div className="tile warn">
          <div className="label">Open questions</div>
          <div className="value">{questions.length}</div>
          <div className="sub">{questions.length ? 'need your call' : 'none'}</div>
        </div>
      </div>
    </>
  )
}
