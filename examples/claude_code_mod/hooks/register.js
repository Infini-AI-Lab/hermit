// Start an evolution session from inside a Claude Code session.
//
// The Evolver does not run in this mod. `/evolve` spawns the framework's own
// CLI as a separate process, so the session keeps the execution policy it
// always had: preflight must pass before a learner starts, only the candidate
// and scratch are writable, and the source adapter is never modified. This mod
// starts that process and reports what it produced. It adopts nothing.

const POLL_MS = 15_000

let session = null      // { dir } while one is in flight
let poll = null

export function register(on, options = {}) {
  const adapter = (options.adapter || '').trim()
  const material = (options.learning_material || '').trim()
  const output = (options.output || '.evolution').trim()
  const network = Boolean(options.network)

  on('session.start', async ($, e, next) => {
    // Register last and catch: a throwing session.start hook is skipped
    // whole, and register throws on a name a built-in already uses.
    try {
      await $.command.register({
        name: 'evolve',
        description: 'Run an evolution session on the configured adapter',
        argumentHint: '[goal]',
        immediate: true,
      })
    } catch (error) {
      $.ui.log('evolve-trigger: /evolve not registered: ' + error.message)
    }
    return next(e)
  })

  on('command.run', { command: 'evolve' }, async ($, e) => {
    if (!adapter || !material) {
      return { text: 'evolve-trigger needs its adapter and learning_material settings before it can run.' }
    }
    if (session) {
      return { text: 'An evolution session is already running in ' + session.dir + '.' }
    }

    const dir = output.startsWith('/') ? output : (await $.session.cwd()) + '/' + output
    const argv = ['harnessadapter', 'evolve', adapter,
                  '--learning-material', material,
                  '--output', dir,
                  network ? '--network' : '--no-network']
    if (e.args) argv.push('--goal', e.args)

    // Detach rather than stream. A session runs for minutes, past both a
    // hook's budget and the $.process.run timeout, and nothing here needs its
    // output: the result lands in control/result.json, which the timer reads.
    // Backgrounding with & makes the run call return at once.
    const command = argv.map(quote).join(' ') + ' >/dev/null 2>&1 &'
    const started = await $.process.run(['sh', '-c', command])
    if (started.exitCode !== 0) {
      return { text: 'Could not start the evolution session: ' + (started.stderr || 'exit ' + started.exitCode) }
    }
    session = { dir, startedAt: await $.clock.now() }
    $.ui.status('evolve: running')
    poll = $.clock.every(POLL_MS, () => report($))

    return { text: 'Evolution session started in ' + dir + '. Nothing is adopted automatically; its result will appear here.' }
  })
}

// Single-quote for sh, so a path or goal with spaces survives.
function quote(value) {
  return "'" + String(value).replace(/'/g, "'\\''") + "'"
}

async function report($) {
  const found = await readResult($, session.dir)
  if (!found) return
  if (poll) { poll.cancel(); poll = null }
  const where = session.dir
  session = null

  $.ui.status('evolve: ' + found.status)
  // A dim transcript line: this is for the person deciding, not for Claude.
  $.ui.log('evolve-trigger: ' + found.status + ' — candidate at ' + (found.candidate || where))
  if (found.status === 'candidate_ready') {
    $.ui.toast('Candidate ready for review')
  } else {
    $.ui.toast('Session finished: ' + found.status)
  }
}

// The framework writes control/result.json when a session finishes.
// Exported so a test can drive it without a session.
export async function readResult($, dir) {
  if (!(await $.fs.exists(dir))) return null
  let entries
  try {
    entries = await $.fs.list(dir)
  } catch {
    return null
  }
  for (const entry of entries) {
    if (entry.kind !== 'directory') continue
    const path = dir + '/' + entry.name + '/control/result.json'
    if (!(await $.fs.exists(path))) continue
    try {
      return JSON.parse((await $.fs.read(path)).text)
    } catch {
      return null      // still being written; try again next tick
    }
  }
  return null
}
