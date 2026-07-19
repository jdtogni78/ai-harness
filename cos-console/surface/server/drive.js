#!/usr/bin/env node
// Tiny driver CLI — stands in for the voice-brain until it exists.
// It POSTs intents to the surface server's /control endpoint.
//
// Usage:
//   node server/drive.js goto tests        # jump to the tests slide
//   node server/drive.js goto 3            # jump by index
//   node server/drive.js show coverage     # alias
//   node server/drive.js next | prev
//   node server/drive.js project familyfund
//   node server/drive.js reload
//   node server/drive.js script            # run a scripted walk-through demo

const BASE = process.env.SURFACE_URL || 'http://localhost:8787'

async function send(intent) {
  const res = await fetch(`${BASE}/control`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(intent)
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(body.error || res.statusText)
  return body
}

function intentFromArgs(argv) {
  const [cmd, arg] = argv
  switch (cmd) {
    case 'goto':
    case 'show':
      if (arg == null) throw new Error(`${cmd} needs a slide (index or widget name)`)
      return /^\d+$/.test(arg)
        ? { type: 'goto', slide: Number(arg) }
        : { type: cmd, widget: arg }
    case 'next': return { type: 'next' }
    case 'prev': return { type: 'prev' }
    case 'project': return { type: 'project', project: arg }
    case 'reload': return { type: 'reload' }
    default: return null
  }
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms))

// A canned "chief-of-staff walking the operator through dstrader" sequence.
async function runScript() {
  const steps = [
    { say: 'Here is where dstrader stands.', intent: { type: 'goto', widget: 'overview' } },
    { say: 'The board: 26 done, 15 still to do, nothing blocked.', intent: { type: 'goto', widget: 'tickets' } },
    { say: "Tests — heads up, I don't actually have test data for this one.", intent: { type: 'goto', widget: 'tests' } },
    { say: 'It did deploy to prod though.', intent: { type: 'goto', widget: 'deploy' } },
    { say: 'And there was a visual review.', intent: { type: 'goto', widget: 'visuals' } },
    { say: 'Recent decisions:', intent: { type: 'goto', widget: 'decisions' } },
    { say: 'Open questions for you:', intent: { type: 'goto', widget: 'questions' } }
  ]
  for (const s of steps) {
    process.stdout.write(`\n🗣  ${s.say}\n   → ${JSON.stringify(s.intent)}\n`)
    await send(s.intent)
    await sleep(2500)
  }
  process.stdout.write('\n✅ script done\n')
}

const argv = process.argv.slice(2)
try {
  if (argv[0] === 'script') {
    await runScript()
  } else {
    const intent = intentFromArgs(argv)
    if (!intent) {
      console.error('usage: drive.js <goto|show|next|prev|project|reload|script> [arg]')
      process.exit(1)
    }
    const out = await send(intent)
    console.log(JSON.stringify(out.state, null, 2))
  }
} catch (e) {
  console.error('drive error:', e.message)
  process.exit(1)
}
