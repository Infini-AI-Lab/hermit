import { expect, mock, test } from 'claude-code/testing'
import { readResult } from '../hooks/register.js'

// Claude Code's answers for the session.start hook, which registers /evolve.
async function startSession($: any, on: any) {
  on('session.start', () => ({ cwd: '/work' }))
  on('command.register', () => ({ value: undefined }))
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
}

test('without configuration, /evolve refuses and starts no process', async ($, on) => {
  mock.clock(on)
  const ran: unknown[] = []
  on('process.run', ($: any, e: any) => {
    ran.push(e.argv)
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  on('session.cwd', () => ({ value: '/work' }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))

  await startSession($, on)
  const answer = await $.command.run({ command: 'evolve', args: '' })

  // The mod has no adapter or learning material, so it must not run anything.
  expect(answer.text).toContain('needs its adapter and learning_material')
  expect(ran).toEqual([])
})

test('readResult returns nothing until the framework has written result.json', async ($, on) => {
  const $fake: any = {
    fs: {
      exists: async (path: string) => path === '/out' || path === '/out/evolve-a',
      list: async () => [{ name: 'evolve-a', kind: 'directory' }],
      read: async () => { throw new Error('not reached') },
    },
  }
  // The session directory exists but control/result.json does not yet.
  expect(await readResult($fake, '/out')).toBe(null)
})

test('readResult parses a finished session', async ($, on) => {
  const $fake: any = {
    fs: {
      exists: async () => true,
      list: async () => [{ name: 'evolve-a', kind: 'directory' }],
      read: async () => ({ text: '{"status":"candidate_ready","candidate":"/out/evolve-a/adapter"}' }),
    },
  }
  const found: any = await readResult($fake, '/out')
  expect(found.status).toBe('candidate_ready')
  expect(found.candidate).toBe('/out/evolve-a/adapter')
})

test('readResult tolerates a half-written result.json', async ($, on) => {
  const $fake: any = {
    fs: {
      exists: async () => true,
      list: async () => [{ name: 'evolve-a', kind: 'directory' }],
      read: async () => ({ text: '{"status":"candi' }),
    },
  }
  // A partial file must not throw; the poller tries again on the next tick.
  expect(await readResult($fake, '/out')).toBe(null)
})
