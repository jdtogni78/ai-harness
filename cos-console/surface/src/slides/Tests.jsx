import React from 'react'
import {
  BarChart, Bar, XAxis, YAxis, Cell, ResponsiveContainer, Tooltip, LabelList
} from 'recharts'
import { isLive, availability, num } from '../util.js'
import NoData from '../components/NoData.jsx'

// Tests slide. dstrader's tests are `unavailable` in the real probe, so this is
// the canonical no-data case: we render the honesty panel, NOT a "0 tests" chart.
export default function Tests({ report }) {
  const live = isLive(report, 'tests')
  const tests = report.tests || {}

  if (!live || tests.available === false) {
    return (
      <>
        <div className="eyebrow">Tests & coverage</div>
        <h1>Tests</h1>
        <NoData
          section="tests"
          availability={availability(report, 'tests')}
          warnings={report.warnings}
        />
        <div className="card">
          <h3>What "no data" means here</h3>
          <p style={{ margin: 0, color: 'var(--text-secondary)' }}>
            The probe looks for Maven <code>surefire</code> reports and a JaCoCo
            coverage file. Neither was found for <strong>{report.project}</strong>,
            so every test number is <strong>null</strong>. That is different from a
            suite that ran and reported <strong>0</strong>.
          </p>
        </div>
      </>
    )
  }

  const data = [
    { name: 'Passing', value: num(tests.passing, 0), fill: 'var(--good)' },
    { name: 'Failing', value: num(tests.failing, 0), fill: 'var(--critical)' },
    { name: 'Skipped', value: num(tests.skipped, 0), fill: 'var(--warning)' }
  ]
  const cov = tests.coverage_pct
  const covColor = cov == null ? 'var(--neutral)'
    : cov >= 80 ? 'var(--good)' : cov >= 50 ? 'var(--warning)' : 'var(--critical)'

  return (
    <>
      <div className="eyebrow">Tests & coverage</div>
      <h1>{num(tests.passing)}/{num(tests.count)} passing</h1>

      <div className="cols">
        <div className="card">
          <h3>Test outcomes</h3>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={data} margin={{ top: 16, right: 12, left: 0, bottom: 0 }}>
              <XAxis dataKey="name" tick={{ fill: 'var(--text-secondary)', fontSize: 13 }} axisLine={{ stroke: 'var(--border)' }} tickLine={false} />
              <YAxis tick={{ fill: 'var(--text-muted)', fontSize: 12 }} axisLine={false} tickLine={false} allowDecimals={false} width={34} />
              <Tooltip cursor={{ fill: 'var(--surface-2)' }}
                       contentStyle={{ background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 10, color: 'var(--text-primary)' }} />
              <Bar dataKey="value" radius={[4, 4, 0, 0]} maxBarSize={90} isAnimationActive={false}>
                <LabelList dataKey="value" position="top" fill="var(--text-primary)" fontSize={14} />
                {data.map((d, i) => <Cell key={i} fill={d.fill} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div className="card">
          <h3>Coverage</h3>
          {cov == null ? (
            <NoData section="coverage" availability="partial" />
          ) : (
            <>
              <div style={{ fontSize: 'clamp(40px,7vw,72px)', fontWeight: 800, color: covColor, lineHeight: 1 }}>
                {cov}%
              </div>
              <div className="meter" style={{ marginTop: 14 }}>
                <i style={{ width: `${Math.min(100, cov)}%`, background: covColor }} />
              </div>
              <p style={{ color: 'var(--text-muted)', fontSize: 13, marginTop: 10 }}>
                Last run {tests.last_run ? new Date(tests.last_run).toLocaleString() : '—'}
              </p>
            </>
          )}
        </div>
      </div>
    </>
  )
}
