// The deck manifest — shared vocabulary between the driver (agent) and the UI.
// The agent pushes intents referencing these slide ids / widget aliases; the UI
// renders slides in this order. Keep in sync with src/slides/registry.js.

export const SLIDES = [
  { id: 'overview',     widget: 'overview' },
  { id: 'tickets',      widget: 'tickets' },
  { id: 'tests',        widget: 'tests' },
  { id: 'deploy',       widget: 'deploy' },
  { id: 'visuals',      widget: 'visual_review' },
  { id: 'decisions',    widget: 'decisions' },
  { id: 'questions',    widget: 'open_questions' },
  { id: 'architecture', widget: 'architecture' }
]

// Aliases the agent might say/emit -> canonical slide id.
const WIDGET_ALIAS = {
  overview: 'overview', title: 'overview', summary: 'overview',
  tickets: 'tickets', board: 'tickets', burn: 'tickets',
  tests: 'tests', coverage: 'tests', testing: 'tests',
  deploy: 'deploy', deployment: 'deploy', prod: 'deploy',
  visual_review: 'visuals', visuals: 'visuals', 'visual-review': 'visuals', artifacts: 'visuals',
  decisions: 'decisions', timeline: 'decisions',
  open_questions: 'questions', questions: 'questions',
  architecture: 'architecture', diagram: 'architecture', mermaid: 'architecture'
}

export function slideIndexFor({ slide, slideId, widget }) {
  if (Number.isInteger(slide)) {
    return Math.max(0, Math.min(SLIDES.length - 1, slide))
  }
  if (slideId) {
    const i = SLIDES.findIndex(s => s.id === slideId)
    if (i >= 0) return i
  }
  if (widget) {
    const id = WIDGET_ALIAS[String(widget).toLowerCase()]
    const i = SLIDES.findIndex(s => s.id === id)
    if (i >= 0) return i
  }
  return null
}
