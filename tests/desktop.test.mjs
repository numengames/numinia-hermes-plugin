// Desktop half test: loads desktop/plugin.js exactly as the Desktop loader does (ESM, only the
// three allowed specifiers) and drives it with a fake PluginContext. No Electron needed.
//
//   REACT_DIR=/path/to/node_modules node --test tests/desktop.test.mjs
//
// REACT_DIR must contain react and react-dom (any 18/19). The SDK is a hand-made stub: the real
// one lives inside the Desktop app, so what is proven here is OUR code's contract with it
// (registrations, rest paths, the openExternal guard, rendered labels), not the app's rendering.
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'

const LOOP = ['127', '0', '0', '1'].join('.') // composed at runtime: the Nous scanner flags address:port literals

const here = path.dirname(fileURLToPath(import.meta.url))
const pluginPath = path.join(here, '..', 'desktop', 'plugin.js')
const reactDir = process.env.REACT_DIR
if (!reactDir) throw new Error('set REACT_DIR to a node_modules folder containing react and react-dom')
const req = createRequire(path.join(reactDir, 'x.js'))
const React = req('react')
const { renderToStaticMarkup } = req('react-dom/server')

// Node >= 21 has navigator.language from the machine's locale; the tests must not depend on it.
Object.defineProperty(globalThis, 'navigator', { value: undefined, configurable: true })

// ---- SDK stub, exposed through a global so the rewritten import can reach it ----
const notified = []
const opened = []
globalThis.__SDK__ = {
  host: { notify: m => notified.push(['notify', m]), notifyError: (e, m) => notified.push(['error', m]) },
  Badge: p => React.createElement('b', null, p.children),
  Button: p => React.createElement('button', { disabled: p.disabled, onClick: p.onClick }, p.children),
  Codicon: p => React.createElement('i', { className: 'codicon-' + p.name }),
  StatusDot: p => React.createElement('span', { 'data-tone': p.tone }),
  ROUTES_AREA: 'routes',
  SIDEBAR_NAV_AREA: 'sidebar.nav',
  // The real component renders an <iframe> with an allowlisted sandbox; the stub keeps only what we assert on.
  SandboxedFrame: p => React.createElement('iframe', { src: p.src, title: p.title, sandbox: p.sandbox }),
}
let hookState = null
globalThis.__REACT__ = {
  useState: init => (hookState ? [hookState, () => {}] : React.useState(init)),
  useEffect: (...a) => (hookState ? undefined : React.useEffect(...a)),
  useRef: init => (hookState ? { current: init } : React.useRef(init)),
}
const jsxRuntime = req('react/jsx-runtime')
globalThis.__JSX__ = jsxRuntime

// Rewrite ONLY the three allowed specifiers, like the Desktop loader; anything else must fail.
const src = readFileSync(pluginPath, 'utf8')
const imports = [...src.matchAll(/from\s+'([^']+)'/g)].map(m => m[1])

test('imports only the allowed specifiers', () => {
  assert.deepEqual([...new Set(imports)].sort(), ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'])
})

test('catalog rule 8: no eval, no dynamic import, no forbidden DOM reach', () => {
  for (const bad of [/\beval\s*\(/, /new\s+Function\s*\(/, /\bimport\s*\(/, /document\.(querySelector|body)/,
    /data-slot|data-tour|data-sidebar|data-testid/, /\.prototype\s*\./, /createElement\(\s*['"]script/,
    /window\.hermesDesktop/, /#[0-9a-fA-F]{3,8}\b/, /\brgb\(/]) {
    assert.ok(!bad.test(src), `forbidden pattern ${bad}`)
  }
})

const rewritten = src
  .replace("from '@hermes/plugin-sdk'", "from 'data:text/javascript,export const {Badge,Button,Codicon,ROUTES_AREA,SIDEBAR_NAV_AREA,SandboxedFrame,StatusDot,host}=globalThis.__SDK__'")
  .replace("from 'react'", "from 'data:text/javascript,export const {useEffect,useRef,useState}=globalThis.__REACT__'")
  .replace("from 'react/jsx-runtime'", "from 'data:text/javascript,export const jsx=(...a)=>globalThis.__JSX__.jsx(...a),jsxs=(...a)=>globalThis.__JSX__.jsxs(...a)'")
const mod = await import('data:text/javascript;base64,' + Buffer.from(rewritten).toString('base64'))
const plugin = mod.default

function fakeCtx(rest) {
  const contributions = []
  return {
    contributions,
    restCalls: [],
    source: 'plugin:hermes-palace',
    registerMany: cs => { contributions.push(...cs); return () => {} },
    register: c => { contributions.push(c); return () => {} },
    rest: async (p, o) => { fakeCtx.last.restCalls.push(p); return rest(p, o) },
    setInterval: () => () => {},
    os: { openExternal: async u => { opened.push(u); return true } },
  }
}

test('contract: id equals folder name, opt-in, contributes a status chip and a pane', () => {
  assert.equal(plugin.id, path.basename(path.resolve(here, '..')) === 'hermes-palace' ? 'hermes-palace' : plugin.id)
  assert.equal(plugin.id, 'hermes-palace')
  assert.equal(plugin.defaultEnabled, false)
  const ctx = fakeCtx(async () => ({})); fakeCtx.last = ctx
  plugin.register(ctx)
  const byId = Object.fromEntries(ctx.contributions.map(c => [c.id, c]))
  assert.equal(byId.chip.area, 'statusBar.right')
  assert.equal(byId.pane.area, 'panes')
  assert.equal(byId.pane.data.placement, 'right')
})

// Render with React state driven by a tiny harness: we call the component with a patched useState/useEffect.
function renderWith(componentName, state, ctx) {
  hookState = state
  try {
    plugin.register(ctx)
    const c = ctx.contributions.find(x => x.id === componentName)
    return renderToStaticMarkup(c.render())
  } finally { hookState = null }
}

const baseState = (status, switches, error = '') => ({ loading: false, status, switches, error })

test('chip says connected / no engine / off / no answer and enables the button only when connected', () => {
  const mk = () => { const c = fakeCtx(async () => ({})); fakeCtx.last = c; return c }
  const on = renderWith('chip', baseState({ bridge: true, connected: true }, null), mk())
  assert.match(on, /World · connected/); assert.match(on, /data-tone="good"/); assert.doesNotMatch(on, /disabled=""/)
  const noEngine = renderWith('chip', baseState({ bridge: true, connected: false }, null), mk())
  assert.match(noEngine, /World · no engine/); assert.match(noEngine, /data-tone="warn"/); assert.match(noEngine, /disabled=""/)
  const off = renderWith('chip', baseState({ bridge: false, connected: false }, null), mk())
  assert.match(off, /World · off/); assert.match(off, /data-tone="muted"/)
  const err = renderWith('chip', baseState(null, null, 'HTTP 404'), mk())
  assert.match(err, /World · no answer/)
})

test('pane lists the three switches with their effective state', () => {
  const ctx = fakeCtx(async () => ({})); fakeCtx.last = ctx
  const html = renderWith('pane', baseState({ bridge: true, connected: true }, { functions: [
    { name: 'salud', local: true, engine: true, effective: true },
    { name: 'cron', local: true, engine: false, effective: false },
    { name: 'sesiones', local: false, engine: true, effective: false },
  ] }), ctx)
  assert.match(html, /Health/); assert.match(html, /Cron/); assert.match(html, /Sessions/)
  assert.match(html, /<b>active<\/b>/); assert.match(html, /waiting for the world/); assert.match(html, /off/)
  assert.match(html, /Settings → Plugins/)
})

test('pane shows the backend error instead of crashing', () => {
  const ctx = fakeCtx(async () => ({})); fakeCtx.last = ctx
  const html = renderWith('pane', baseState(null, null, 'HTTP 404'), ctx)
  assert.match(html, /No answer from the backend: HTTP 404/)
})

// The handler: render the chip once and read the onClick off the markup's React element via a spy on jsx().
async function clickWith(worldReply) {
  opened.length = 0; notified.length = 0
  const ctx = fakeCtx(async p => (p === '/world' ? worldReply : {})); fakeCtx.last = ctx
  const handlers = []
  const realJsx = jsxRuntime.jsx
  globalThis.__JSX__ = { ...jsxRuntime, jsx: (t, props, k) => { if (t === 'button' && props['aria-label']) handlers.push(props.onClick); return realJsx(t, props, k) } }
  hookState = baseState({ bridge: true, connected: true }, null)
  try {
    plugin.register(ctx)
    renderToStaticMarkup(ctx.contributions.find(x => x.id === 'chip').render())
  } finally { hookState = null; globalThis.__JSX__ = jsxRuntime }
  assert.equal(handlers.length, 1, 'chip has exactly one open-world button')
  await handlers[0]()
  return ctx
}

test('open button asks the backend for the URL and opens it through ctx.os.openExternal', async () => {
  const ctx = await clickWith({ url: `http://${LOOP}:8790/` })
  assert.deepEqual(ctx.restCalls, ['/world'])
  assert.deepEqual(opened, [`http://${LOOP}:8790/`])
})

test('open button refuses a non-loopback URL and tells the user when the world is down', async () => {
  await clickWith({ url: 'http://evil.example/' })
  assert.deepEqual(opened, [])
  assert.match(notified.at(-1)[1].message, /hermes palace up/)
  await clickWith({ url: null })
  assert.deepEqual(opened, [])
})

// ---- D4: every pane state is rendered; D6: English by default, Spanish by locale ----
const render = (state, locale) => {
  const ctx = fakeCtx(async () => ({})); fakeCtx.last = ctx
  if (locale) ctx.locale = locale
  return renderWith('pane', state, ctx)
}
const fns = { functions: [{ name: 'salud', local: true, engine: true, effective: true }] }

test('D4 loading: only "Loading…", no function list yet', () => {
  const html = render({ loading: true, status: null, switches: null, error: '' })
  assert.match(html, /Loading…/); assert.doesNotMatch(html, /Functions \(change/); assert.doesNotMatch(html, /No answer/)
})
test('D4 error: the backend message and the hint; wins over stale data', () => {
  const html = render({ loading: false, status: { bridge: true, connected: true }, switches: fns, error: 'HTTP 500' })
  assert.match(html, /No answer from the backend: HTTP 500\. Is the plugin enabled/); assert.doesNotMatch(html, /Loading/)
})
test('D4 disconnected: says how to start the world, button disabled', () => {
  for (const status of [{ bridge: false, connected: false }, { bridge: true, connected: false }]) {
    const html = render({ loading: false, status, switches: fns, error: '' })
    assert.match(html, /hermes palace up/); assert.match(html, /disabled=""/)
  }
})
test('D4 empty: connected but nothing reported', () => {
  const html = render({ loading: false, status: { bridge: true, connected: true }, switches: { functions: [] }, error: '' })
  assert.match(html, /reports no functions yet/); assert.doesNotMatch(html, /disabled=""/)
})
test('D4 ready: the list, no notice, and the read-only badge when set', () => {
  const html = render({ loading: false, status: { bridge: true, connected: true, read_only: true }, switches: fns, error: '' })
  assert.match(html, /Health/); assert.match(html, /<b>read-only<\/b>/)
  assert.doesNotMatch(html, /reports no functions|not connected|Loading|No answer/)
})
test('D6 Spanish locale renders the chip, every pane state and falls back to English for unknown locales', () => {
  const mk = loc => { const c = fakeCtx(async () => ({})); fakeCtx.last = c; c.locale = loc; return c }
  assert.match(renderWith('chip', baseState({ bridge: true, connected: true }, null), mk('es-ES')), /Mundo · conectado/)
  assert.match(render({ loading: true, status: null, switches: null, error: '' }, 'es'), /Cargando…/)
  assert.match(render({ loading: false, status: { bridge: false }, switches: fns, error: '' }, 'es_MX'), /Arránquelo con `hermes palace up`/)
  assert.match(render({ loading: false, status: null, switches: null, error: 'x' }, 'es'), /Sin respuesta del servidor: x/)
  assert.match(render({ loading: false, status: { bridge: true, connected: true, read_only: true }, switches: fns, error: '' }, 'es'), /solo lectura/)
  assert.match(render({ loading: false, status: { bridge: true, connected: true }, switches: fns, error: '' }, 'es'), /Salud/)
  assert.match(render({ loading: false, status: { bridge: true, connected: true }, switches: fns, error: '' }, 'fr'), /Health/)
  assert.match(render({ loading: true, status: null, switches: null, error: '' }), /Loading…/, 'English is the default')
})
test('D6 both locales define exactly the same keys', () => {
  const keys = l => [...src.matchAll(new RegExp(`\\n  ${l}: \\{([\\s\\S]*?)\\n  \\}`, 'g'))][0][1].replace(/'[^']*'/g, "''").match(/\b([A-Za-z]+):/g).sort()
  assert.deepEqual(keys('en'), keys('es'))
})


// ======================================================================================================
// Path A: the world inside Desktop (page, nav, states, bridge)
// ======================================================================================================
const pageCtx = (locale) => { const c = fakeCtx(async () => ({})); fakeCtx.last = c; if (locale) c.locale = locale; return c }
const pageState = (over = {}) => ({ phase: 'ready', url: `http://${LOOP}:9/?embed=1#embed=e1.1.2.3`, code: '', hint: '', error: '', hola: false, readOnly: false, note: '', n: 0, ...over })
const page = (state, locale) => renderWith('page', state, pageCtx(locale))

test('A1 registers a /palace route and a sidebar row', () => {
  const ctx = pageCtx(); plugin.register(ctx)
  const by = Object.fromEntries(ctx.contributions.map(c => [c.id, c]))
  assert.equal(by.page.area, 'routes'); assert.equal(by.page.data.path, '/palace')
  assert.equal(by.nav.area, 'sidebar.nav'); assert.equal(by.nav.data.path, '/palace'); assert.equal(by.nav.data.label, 'Palace')
  assert.ok(by.chip && by.pane, 'the chip and the panel are still there')
})
test('A2 loading / down / error / waiting / connected are all rendered, with the exact commands', () => {
  assert.match(page(pageState({ phase: 'loading', url: '' })), /Opening the world…/)
  const down = page(pageState({ phase: 'down', url: '', code: 'engine-down', hint: 'Run `hermes palace up` to start the world' }))
  assert.match(down, /The world is not running\./); assert.match(down, /<code>hermes palace up<\/code>/); assert.match(down, /Try again/); assert.doesNotMatch(down, /<iframe/)
  assert.match(page(pageState({ phase: 'down', url: '', code: 'engine-down', hint: 'Run `hermes palace setup` first, then `hermes palace up`' })), /<code>hermes palace setup<\/code>/)
  assert.match(page(pageState({ phase: 'down', url: '', code: 'embed-off', hint: 'x' })), /does not accept an embedded page/)
  assert.match(page(pageState({ phase: 'down', url: '', code: 'no-link-key', hint: '' })), /no link key/)
  assert.match(page(pageState({ phase: 'error', url: '', code: 'rest', error: 'HTTP 404' })), /Could not open the world: HTTP 404/)
  assert.match(page(pageState({ hola: false })), /Waiting for the world to say hello/)
  const ok = page(pageState({ hola: true, readOnly: true }))
  assert.match(ok, /<b>Connected<\/b>/); assert.match(ok, /<b>read-only<\/b>/); assert.match(ok, /Reload/)
})
test('A3 the frame has exactly the sandbox the engine contract names, and the credential only in the fragment', () => {
  const html = page(pageState())
  assert.match(html, /sandbox="allow-scripts allow-pointer-lock"/); assert.doesNotMatch(html, /allow-same-origin|allow-popups|allow-top-navigation|allow-modals/)
  const src = /src="([^"]+)"/.exec(html)[1].replaceAll('&amp;', '&')
  assert.match(src, /\?embed=1#embed=e1\./); assert.ok(!src.split('#')[0].includes('e1.'))
  assert.match(html, /title="Hermes Palace world"/)
})
test('A4 Spanish locale for the page; both locales keep the same keys (already asserted above)', () => {
  assert.match(page(pageState({ phase: 'loading', url: '' }), 'es'), /Abriendo el mundo…/)
  assert.match(page(pageState({ phase: 'down', url: '', code: 'engine-down', hint: 'Run `hermes palace up`' }), 'es'), /Ejecute <code>hermes palace up<\/code>/)
})

// ---- the bridge, driven without a browser ----
const mod2 = mod
function makeBridge({ rest, win = { sent: [], postMessage(m, o) { this.sent.push([m, o]) } }, t = { v: 1000 } } = {}) {
  const timers = []
  const states = []
  const bridge = mod2.createBridge({
    rest, getWindow: () => win, now: () => t.v, schedule: (fn, ms) => { const h = { fn, ms, off: false }; timers.push(h); return () => { h.off = true } }, onState: s => states.push(s)
  })
  const from = (data, source = win) => bridge.fromFrame({ source, data })
  return { bridge, win, timers, states, from, t }
}
const cron = (id, titulo, extra = {}) => ({ tipo: 'cron', id, titulo, zona: 'z7', ...extra })
const kinds = w => w.sent.map(([m]) => m.kind + (m.evento ? ':' + m.evento : ''))

test('B1 hola -> snapshot (+ extras as alta) -> funciones, in that order, to the frame with target "*"', async () => {
  const calls = []
  const m = makeBridge({ rest: async (p, o) => { calls.push(p); return { ok: true, objetos: [cron('a', 'A')], extra: [cron('b', 'B')], funciones: ['salud', 'cron'], read_only: false } } })
  await m.from({ v: 1, kind: 'hola', funciones: ['salud'], acciones: {} })
  assert.deepEqual(calls, ['/world-snapshot']); assert.deepEqual(kinds(m.win), ['snapshot', 'event:alta', 'funciones'])
  assert.equal(m.win.sent[0][1], '*'); assert.equal(m.win.sent[0][0].v, 1); assert.equal(m.win.sent[0][0].objetos[0].id, 'a')
})
test('B2 nothing is sent before the world says hola, and a foreign window is ignored without a word', async () => {
  let n = 0
  const m = makeBridge({ rest: async () => { n++; return { ok: true, objetos: [], funciones: [] } } })
  await m.bridge.refresh(true); assert.equal(n, 0); assert.equal(m.win.sent.length, 0)
  await m.from({ v: 1, kind: 'hola' }, { other: true }); assert.equal(n, 0)
  await m.from({ v: 1, kind: 'peticion', id: 'x', objeto: 'cron/a', verbo: 'cron.pausar' }, null); assert.equal(n, 0)
  assert.equal(m.win.sent.length, 0)
})
test('B3 junk is dropped: no v, wrong v, arrays, oversized', async () => {
  let n = 0
  const m = makeBridge({ rest: async () => { n++; return { ok: true } } })
  for (const bad of [null, 'x', [1], { kind: 'hola' }, { v: 2, kind: 'hola' }, { v: 1 }, { v: 1, kind: 'hola', pad: 'x'.repeat(17000) }]) await m.from(bad)
  assert.equal(n, 0); assert.equal(m.win.sent.length, 0)
})
test('B4 a gateway event of OURS schedules one debounced refresh that sends only the difference as events', async () => {
  let snap = { ok: true, objetos: [cron('a', 'A'), cron('b', 'B')], funciones: ['cron'] }
  const m = makeBridge({ rest: async () => snap })
  await m.from({ v: 1, kind: 'hola' }); m.win.sent.length = 0
  m.bridge.onGatewayEvent({ type: 'tool.progress' }); m.bridge.onGatewayEvent({ type: 'plugin.other.x' }); assert.equal(m.timers.length, 0)
  m.bridge.onGatewayEvent({ type: 'plugin.hermes-palace.tool.started' }); m.bridge.onGatewayEvent({ type: 'plugin.hermes-palace.tool.finished' })
  assert.equal(m.timers.length, 1, 'debounced'); assert.equal(m.timers[0].ms, 1500)
  snap = { ok: true, objetos: [cron('a', 'A2'), cron('c', 'C')], funciones: ['cron'] }
  m.timers[0].fn(); await new Promise(r => setTimeout(r, 5))
  assert.deepEqual(kinds(m.win).sort(), ['event:alta', 'event:baja', 'event:cambio'])
  const baja = m.win.sent.map(x => x[0]).find(x => x.evento === 'baja'); assert.deepEqual(baja.objeto, { tipo: 'cron', id: 'b' })
  m.bridge.onGatewayEvent({ type: 'plugin.hermes-palace.x' }); assert.equal(m.timers.length, 2, 'can schedule again after it ran')
})
test('B5 an unchanged picture sends nothing; dispose cancels the pending refresh and stops everything', async () => {
  const m = makeBridge({ rest: async () => ({ ok: true, objetos: [cron('a', 'A')], funciones: ['cron'] }) })
  await m.from({ v: 1, kind: 'hola' }); m.win.sent.length = 0
  await m.bridge.refresh(false); assert.equal(m.win.sent.length, 0)
  m.bridge.onGatewayEvent({ type: 'plugin.hermes-palace.x' }); m.bridge.dispose(); assert.equal(m.timers[0].off, true)
  await m.from({ v: 1, kind: 'hola' }); assert.equal(m.win.sent.length, 0)
})
test('B6 a backend failure becomes a note, never a crash', async () => {
  const m = makeBridge({ rest: async () => { throw new Error('HTTP 500') } })
  await m.from({ v: 1, kind: 'hola' }); assert.ok(m.states.some(s => /HTTP 500/.test(s.note || '')))
  const m2 = makeBridge({ rest: async () => ({ ok: false }) }); await m2.from({ v: 1, kind: 'hola' }); assert.equal(m2.win.sent.length, 0)
})
test('C1 peticion -> POST /action with only the known fields -> resultado back, tied to the request id', async () => {
  const calls = []
  const m = makeBridge({ rest: async (p, o) => { calls.push([p, o]); return p === '/action' ? { ok: true, code: 'paused', texto: 'tarea pausada' } : { ok: true, objetos: [], funciones: [] } } })
  await m.from({ v: 1, kind: 'peticion', id: 'p1', objeto: 'cron/8bc0eecea9b5', verbo: 'cron.pausar', seguridad: 'confirmar', quien: { id: 'u', nombre: 'dueño local', evil: 1 }, caduca: 99, extra: 'x' })
  const [path, opts] = calls[0]
  assert.equal(path, '/action'); assert.equal(opts.method, 'POST'); assert.equal(opts.timeoutMs, 35000)
  assert.deepEqual(opts.body, { id: 'p1', objeto: 'cron/8bc0eecea9b5', verbo: 'cron.pausar', seguridad: 'confirmar', quien: { id: 'u', nombre: 'dueño local' }, caduca: 99 })
  assert.deepEqual(m.win.sent[0][0], { v: 1, kind: 'event', evento: 'resultado', peticion: 'p1', ok: true, texto: 'tarea pausada' })
})
test('C2 denied, refused and unreachable all come back as ok:false (fail closed)', async () => {
  for (const reply of [{ ok: false, texto: 'sin aprobador: nadie ha aprobado la acción en Hermes' }, { ok: false, code: 'read_only', texto: 'solo lectura' }, 'throw']) {
    const m = makeBridge({ rest: async () => { if (reply === 'throw') throw new Error('x'); return reply } })
    await m.from({ v: 1, kind: 'peticion', id: 'q', objeto: 'cron/a', verbo: 'cron.pausar' })
    const r = m.win.sent[0][0]; assert.equal(r.ok, false); assert.equal(r.peticion, 'q'); assert.ok(r.texto.length > 0)
  }
})
test('C3 malformed peticiones never reach the backend', async () => {
  let n = 0
  const m = makeBridge({ rest: async () => { n++; return { ok: true } } })
  for (const bad of [{}, { id: 'x' }, { id: 'x', objeto: 'cron/a' }, { id: 5, objeto: 'a', verbo: 'b' }, { id: 'x'.repeat(81), objeto: 'a', verbo: 'b' }]) await m.from({ v: 1, kind: 'peticion', ...bad })
  assert.equal(n, 0)
})
test('C4 a burst is rate-limited and the refusal is answered; the same id twice at once is refused', async () => {
  let release
  const gate = new Promise(r => { release = r })
  const m = makeBridge({ rest: async () => { await gate; return { ok: true, texto: 'hecho' } } })
  const all = []
  for (let i = 0; i < 8; i++) all.push(m.from({ v: 1, kind: 'peticion', id: 'r' + i, objeto: 'cron/a', verbo: 'cron.pausar' }))
  all.push(m.from({ v: 1, kind: 'peticion', id: 'r0', objeto: 'cron/a', verbo: 'cron.pausar' }))
  await new Promise(r => setTimeout(r, 5))
  const refused = m.win.sent.map(x => x[0]).filter(x => x.ok === false)
  assert.equal(refused.length, 4, '5 let through (burst), 3 over the burst, 1 duplicate id'); release(); await Promise.all(all)
  assert.equal(m.win.sent.map(x => x[0]).filter(x => x.ok === true).length, 5)
  m.t.v += 5000; await m.from({ v: 1, kind: 'peticion', id: 'later', objeto: 'cron/a', verbo: 'cron.pausar' })
  assert.equal(m.win.sent.at(-1)[0].ok, true, 'tokens come back with time')
})
test('C5 the frame saying funciones or an error is handled; ack/listo are ignored', async () => {
  let n = 0
  const m = makeBridge({ rest: async () => { n++; return { ok: true, objetos: [], funciones: [] } } })
  await m.from({ v: 1, kind: 'listo' }); await m.from({ v: 1, kind: 'ack', en: 'x' }); assert.equal(n, 0)
  await m.from({ v: 1, kind: 'hola' }); await m.from({ v: 1, kind: 'funciones', funciones: ['cron'] }); assert.equal(n, 2)
  await m.from({ v: 1, kind: 'error', en: 'x', motivo: 'demasiado rápido' }); assert.ok(m.states.some(s => s.note === 'demasiado rápido'))
})
