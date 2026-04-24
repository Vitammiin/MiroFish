import { spawn } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const RESTART_DELAY_MS = 1500
const RESTART_RESET_MS = 20000
const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const backendDir = path.join(rootDir, 'backend')
const frontendDir = path.join(rootDir, 'frontend')
const backendPython =
  process.platform === 'win32'
    ? path.join(backendDir, '.venv', 'Scripts', 'python.exe')
    : path.join(backendDir, '.venv', 'bin', 'python')
const frontendViteEntry = path.join(frontendDir, 'node_modules', 'vite', 'bin', 'vite.js')

const colors = {
  backend: '\x1b[32m',
  frontend: '\x1b[36m',
  reset: '\x1b[0m'
}

const services = [
  {
    name: 'backend',
    cwd: backendDir,
    command: backendPython,
    args: ['run.py'],
    env: {
      ...process.env,
      PYTHONUNBUFFERED: '1'
    }
  },
  {
    name: 'frontend',
    cwd: frontendDir,
    command: process.execPath,
    args: [frontendViteEntry, '--host'],
    env: {
      ...process.env,
      MIROFISH_OPEN_BROWSER: process.env.MIROFISH_OPEN_BROWSER || '0'
    }
  }
]

const runtime = new Map()
let shuttingDown = false

function log(name, message) {
  const color = colors[name] || ''
  process.stdout.write(`${color}[${name}]${colors.reset} ${message}\n`)
}

function streamOutput(name, stream) {
  let buffer = ''

  stream.on('data', chunk => {
    buffer += chunk.toString()
    const lines = buffer.split(/\r?\n/)
    buffer = lines.pop() || ''
    for (const line of lines) {
      log(name, line)
    }
  })

  stream.on('end', () => {
    if (buffer) {
      log(name, buffer)
      buffer = ''
    }
  })
}

function stopChild(child, signal = 'SIGTERM') {
  if (!child || child.killed) return
  child.kill(signal)
}

function scheduleRestart(service) {
  const state = runtime.get(service.name)
  if (!state || shuttingDown) return

  const now = Date.now()
  state.restartCount = now - state.lastStartAt > RESTART_RESET_MS ? 1 : state.restartCount + 1
  const backoffMs = Math.min(RESTART_DELAY_MS * state.restartCount, 10000)

  log(service.name, `process stopped, restarting in ${backoffMs}ms`)
  state.restartTimer = setTimeout(() => startService(service), backoffMs)
}

function startService(service) {
  if (shuttingDown) return

  const previous = runtime.get(service.name)
  if (previous?.restartTimer) {
    clearTimeout(previous.restartTimer)
  }

  const child = spawn(service.command, service.args, {
    cwd: service.cwd,
    env: service.env,
    stdio: ['inherit', 'pipe', 'pipe']
  })

  runtime.set(service.name, {
    child,
    lastStartAt: Date.now(),
    restartCount: previous?.restartCount || 0,
    restartTimer: null
  })

  log(service.name, `started with pid ${child.pid}`)
  streamOutput(service.name, child.stdout)
  streamOutput(service.name, child.stderr)

  child.on('exit', (code, signal) => {
    const state = runtime.get(service.name)
    if (!state || state.child !== child) return

    runtime.set(service.name, {
      ...state,
      child: null
    })

    if (shuttingDown) return

    log(
      service.name,
      `exited with ${signal ? `signal ${signal}` : `code ${code ?? 0}`}`
    )
    scheduleRestart(service)
  })
}

function shutdown(signal) {
  if (shuttingDown) return
  shuttingDown = true

  for (const state of runtime.values()) {
    if (state.restartTimer) {
      clearTimeout(state.restartTimer)
    }
  }

  for (const { child } of runtime.values()) {
    stopChild(child, signal)
  }

  setTimeout(() => {
    for (const { child } of runtime.values()) {
      stopChild(child, 'SIGKILL')
    }
    process.exit(0)
  }, 2000).unref()
}

process.on('SIGINT', () => shutdown('SIGINT'))
process.on('SIGTERM', () => shutdown('SIGTERM'))

for (const service of services) {
  startService(service)
}
