import React from 'react'
import { isLive, availability, num } from '../util.js'
import NoData from '../components/NoData.jsx'

const SEG = [
  { key: 'done',        label: 'Done',        color: 'var(--good)' },
  { key: 'in_progress', label: 'In progress', color: 'var(--series-1)' },
  { key: 'todo',        label: 'To do',       color: 'var(--neutral)' },
  { key: 'blocked',     label: 'Blocked',     color: 'var(--critical)' }
]

function pillClass(state) {
  const s = (state || '').toLowerCase().replace(/\s+/g, '')
  if (s.includes('done')) return 'done'
  if (s.includes('progress')) return 'inprogress'
  if (s.includes('block')) return 'blocked'
  return 'todo'
}

// Ticket burn. Board Status counts as a stacked bar (identity = state, so a
// small fixed categorical/status set), plus the item list.
export default function Tickets({ report }) {
  if (!isLive(report, 'tickets')) {
    return (
      <>
        <div className="eyebrow">Ticket burn</div>
        <h1>Board</h1>
        <NoData section="tickets" availability={availability(report, 'tickets')} warnings={report.warnings} />
      </>
    )
  }
  const t = report.tickets
  const total = t.total || SEG.reduce((a, s) => a + (t[s.key] || 0), 0) || 1
  const items = (t.items || []).slice().sort((a, b) => Number(a.id) - Number(b.id))

  return (
    <>
      <div className="eyebrow">Ticket burn</div>
      <h1>{num(t.done)} done · {num(t.todo)} to do</h1>

      <div className="card">
        <h3>{num(t.total)} tickets · {report.tickets.source ? report.tickets.source.split('(')[0].trim() : 'board'}</h3>
        <div className="burn">
          {SEG.map(s => {
            const v = t[s.key] || 0
            if (!v) return null
            return (
              <span key={s.key} style={{ width: `${(v / total) * 100}%`, background: s.color }}
                    title={`${s.label}: ${v}`}>
                {(v / total) > 0.06 ? v : ''}
              </span>
            )
          })}
        </div>
        <div className="legend">
          {SEG.map(s => (
            <div className="item" key={s.key}>
              <span className="swatch" style={{ background: s.color }} />
              {s.label} · <strong style={{ color: 'var(--text-primary)' }}>{t[s.key] || 0}</strong>
            </div>
          ))}
        </div>
      </div>

      <div className="list grow">
        {items.map(it => (
          <div className="row" key={it.id}>
            <span className={`pill ${pillClass(it.state)}`}>{it.state}</span>
            <span className="title">#{it.id} · {it.title}</span>
            {it.url && <a href={it.url} target="_blank" rel="noreferrer">open ↗</a>}
          </div>
        ))}
      </div>
    </>
  )
}
