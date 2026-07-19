// Shared helpers. The most important one is availability(): the anti-fabrication
// gate every slide consults BEFORE rendering a number.

export function availability(report, section) {
  return report?.availability?.[section] || 'unavailable'
}

// True only when we can trust the numbers for a section.
export function isLive(report, section) {
  return availability(report, section) === 'live'
}

export function fmtDate(iso, { time = false } = {}) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const date = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
  if (!time) return date
  return `${date} ${d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}`
}

export function relTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const diff = (Date.now() - d.getTime()) / 1000
  const days = Math.floor(diff / 86400)
  if (days > 1) return `${days}d ago`
  const hrs = Math.floor(diff / 3600)
  if (hrs >= 1) return `${hrs}h ago`
  const mins = Math.max(1, Math.floor(diff / 60))
  return `${mins}m ago`
}

// number or null -> string; null renders as an explicit unknown mark, never 0.
export function num(v, fallback = '—') {
  return v == null ? fallback : v
}
