// Data access for the surface server.
//
// Source of truth is W0's StatusReport probe:
//   python3 -m status_mcp.probe <project> --pretty
// We shell out to it (no secrets, pure stdlib per W0). If the probe can't run
// (python missing, path moved), we fall back to a captured fixture so the deck
// still renders offline for demos. Either way the object honours the FROZEN
// v1.0 schema, including the `availability` honesty layer.

import { execFile } from 'node:child_process'
import { readFile, access } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const FIXTURES = join(__dirname, '..', 'fixtures')

// Where W0's status-mcp package lives, relative to this surface dir.
const STATUS_MCP_DIR = resolve(__dirname, '..', '..', 'status-mcp')

function runProbe(project) {
  return new Promise((resolvePromise, reject) => {
    execFile(
      'python3',
      ['-m', 'status_mcp.probe', project, '--pretty'],
      { cwd: STATUS_MCP_DIR, timeout: 20000, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) return reject(new Error(stderr || err.message))
        try {
          resolvePromise(JSON.parse(stdout))
        } catch (e) {
          reject(new Error('probe returned non-JSON: ' + e.message))
        }
      }
    )
  })
}

async function readFixture(project) {
  const path = join(FIXTURES, `${project}.json`)
  await access(path)
  const raw = await readFile(path, 'utf8')
  return JSON.parse(raw)
}

// Returns { report, meta:{ source: 'probe'|'fixture', warnings:[] } }
export async function getStatus(project, { preferFixture = false } = {}) {
  const warnings = []
  if (!preferFixture) {
    try {
      const report = await runProbe(project)
      return { report, meta: { source: 'probe', warnings } }
    } catch (e) {
      warnings.push(`live probe failed (${e.message.split('\n')[0]}); served captured fixture`)
    }
  }
  const report = await readFixture(project)
  return { report, meta: { source: 'fixture', warnings } }
}

export async function listProjects() {
  // Probe supports --list; fall back to fixtures we have on disk.
  try {
    const out = await new Promise((res, rej) =>
      execFile('python3', ['-m', 'status_mcp.probe', '--list'],
        { cwd: STATUS_MCP_DIR, timeout: 10000 },
        (err, stdout) => (err ? rej(err) : res(stdout)))
    )
    const names = out.split('\n').map(s => s.trim()).filter(Boolean)
    if (names.length) return names
  } catch {
    /* fall through to fixtures */
  }
  return ['dstrader', 'familyfund']
}
