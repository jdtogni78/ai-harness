import Overview from './Overview.jsx'
import Tickets from './Tickets.jsx'
import Tests from './Tests.jsx'
import Deploy from './Deploy.jsx'
import Visuals from './Visuals.jsx'
import Decisions from './Decisions.jsx'
import Questions from './Questions.jsx'
import Architecture from './Architecture.jsx'

// Must mirror server/deck.js SLIDES order + ids. The server owns navigation;
// this maps each slide id to its component + the label shown in the filmstrip.
export const REGISTRY = [
  { id: 'overview',     label: 'Overview',     Component: Overview },
  { id: 'tickets',      label: 'Tickets',      Component: Tickets },
  { id: 'tests',        label: 'Tests',        Component: Tests },
  { id: 'deploy',       label: 'Deploy',       Component: Deploy },
  { id: 'visuals',      label: 'Visuals',      Component: Visuals },
  { id: 'decisions',    label: 'Decisions',    Component: Decisions },
  { id: 'questions',    label: 'Questions',    Component: Questions },
  { id: 'architecture', label: 'Architecture', Component: Architecture }
]
