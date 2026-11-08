// hermes-palace — desktop half. Plain ESM, no build step, no JSX.
// Imports only @hermes/plugin-sdk, react and react/jsx-runtime (catalog rule 8).
// Talks only to its own backend namespace (/api/plugins/hermes-palace/) through ctx.rest, and
// opens the world page through the curated OS door (ctx.os.openExternal).
import { Badge, Button, Codicon, ROUTES_AREA, SIDEBAR_NAV_AREA, SandboxedFrame, StatusDot, host } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const POLL_MS = 5000
// Path A: the world INSIDE Desktop. The page is a SandboxedFrame (opaque origin, no allow-same-origin); this file is the parent
// and the only messenger: the engine trusts a short-lived credential in the frame's URL fragment, never `event.origin`.
const EVENT_PREFIX = 'plugin.hermes-palace.'
const FRAME_SANDBOX = 'allow-scripts allow-pointer-lock'
const MAX_MESSAGE_BYTES = 16 * 1024 // the engine's cap for one message, either way
const REFRESH_DEBOUNCE_MS = 1500
const WORLD_POLL_MS = 15000
const MAX_CHANGES = 40 // more changes than this are sent as a fresh snapshot
const MAX_INFLIGHT = 8 // requests from the world waiting for Hermes' approval
const REQUEST_BURST = 5
const REQUEST_PER_SECOND = 2
const ACTION_TIMEOUT_MS = 35000 // a little over the 25 s the backend waits for an approver

// Strings live in one table per locale. English is the default and the fallback for any missing key.
const LOCALES = {
  en: {
    salud: 'Health — the state of Hermes as a place',
    cron: 'Cron — scheduled jobs (pause/resume from the world, with approval)',
    sesiones: 'Sessions — each session is a place',
    agentes: 'Agents — every Hermes profile as an agent (read-only)',
    gasto: 'Spend — tokens and estimated cost per profile (read-only)',
    habilidades: 'Skills — every skill as a tablet in the workshop (read-only)',
    kanban: 'Kanban — the work board: cards per column (read-only)',
    documentos: 'Documentation — curated pages in the library (needs `hermes palace docs`)',
    connected: 'connected', bridgeNoEngine: 'bridge, no engine', off: 'off',
    chipError: 'World · no answer', chipConnected: 'World · connected', chipNoEngine: 'World · no engine', chipOff: 'World · off',
    togglePanel: 'Show or hide the world panel', openBrowser: 'Open the world in the browser', notConnected: 'The world is not connected',
    notRunning: 'The world is not running. Run `hermes palace up`.', couldNotOpen: 'Could not open the world',
    loading: 'Loading…',
    errorPrefix: 'No answer from the backend: ', errorSuffix: '. Is the plugin enabled in config.yaml?',
    disconnected: 'The world is not connected. Start it with `hermes palace up`.',
    empty: 'The world is connected but reports no functions yet.',
    functionsHint: 'Functions (change them in Settings → Plugins)', active: 'active', waiting: 'waiting for the world',
    openWorld: 'Open the world', readOnly: 'read-only',
    navLabel: 'Palace', frameTitle: 'Hermes Palace world', reload: 'Reload', retry: 'Try again',
    embedLoading: 'Opening the world…', embedWaiting: 'Waiting for the world to say hello…', embedConnected: 'Connected',
    embedDown: 'The world is not running.', embedNoKey: 'The plugin has no link key yet.', embedOff: 'This world does not accept an embedded page yet.',
    embedBad: 'The plugin settings are not valid.', embedError: 'Could not open the world: ', runCommand: 'Run',
    tooMany: 'Too many requests at once.', noAnswer: 'Hermes did not answer.'
  },
  es: {
    salud: 'Salud — el estado de Hermes como un lugar',
    cron: 'Cron — tareas programadas (pausar o reanudar desde el mundo, con aprobación)',
    sesiones: 'Sesiones — cada sesión es un lugar',
    agentes: 'Agentes — cada perfil de Hermes como agente (solo lectura)',
    gasto: 'Gasto — tokens y coste estimado por perfil (solo lectura)',
    habilidades: 'Habilidades — cada habilidad como una tablilla en el taller (solo lectura)',
    kanban: 'Tablero — el tablero de trabajo: tarjetas por columna (solo lectura)',
    documentos: 'Documentación — páginas elegidas en la biblioteca (necesita `hermes palace docs`)',
    connected: 'conectado', bridgeNoEngine: 'puente, sin motor', off: 'apagado',
    chipError: 'Mundo · sin respuesta', chipConnected: 'Mundo · conectado', chipNoEngine: 'Mundo · sin motor', chipOff: 'Mundo · apagado',
    togglePanel: 'Mostrar u ocultar el panel del mundo', openBrowser: 'Abrir el mundo en el navegador', notConnected: 'El mundo no está conectado',
    notRunning: 'El mundo no está en marcha. Ejecute `hermes palace up`.', couldNotOpen: 'No se pudo abrir el mundo',
    loading: 'Cargando…',
    errorPrefix: 'Sin respuesta del servidor: ', errorSuffix: '. ¿Está activado el complemento en config.yaml?',
    disconnected: 'El mundo no está conectado. Arránquelo con `hermes palace up`.',
    empty: 'El mundo está conectado pero aún no informa de ninguna función.',
    functionsHint: 'Funciones (cámbielas en Ajustes → Complementos)', active: 'activa', waiting: 'esperando al mundo',
    openWorld: 'Abrir el mundo', readOnly: 'solo lectura',
    navLabel: 'Palace', frameTitle: 'Mundo de Hermes Palace', reload: 'Recargar', retry: 'Reintentar',
    embedLoading: 'Abriendo el mundo…', embedWaiting: 'Esperando a que el mundo salude…', embedConnected: 'Conectado',
    embedDown: 'El mundo no está en marcha.', embedNoKey: 'El complemento aún no tiene clave de enlace.', embedOff: 'Este mundo aún no admite una página incrustada.',
    embedBad: 'Los ajustes del complemento no son válidos.', embedError: 'No se pudo abrir el mundo: ', runCommand: 'Ejecute',
    tooMany: 'Demasiadas peticiones a la vez.', noAnswer: 'Hermes no ha respondido.'
  }
}

// Locale mechanism: the host's locale if it offers one, else the browser's, else English.
function pickLocale(ctx) {
  const raw = (ctx && ctx.locale) || (typeof navigator !== 'undefined' && navigator.language) || 'en'
  const short = String(raw).toLowerCase().split(/[-_]/)[0]
  return LOCALES[short] ? short : 'en'
}
function tr(ctx, key) {
  return (LOCALES[pickLocale(ctx)] || LOCALES.en)[key] ?? LOCALES.en[key] ?? key
}

// The pane is always in exactly one of these states (D4): loading, error, disconnected, empty or ready.
function viewState({ loading, status, switches, error }) {
  if (error) return 'error'
  if (loading && !status) return 'loading'
  if (!(status && status.bridge && status.connected)) return 'disconnected'
  const rows = (switches && switches.functions) || []
  return rows.length ? 'ready' : 'empty'
}

// ---- the messenger between Hermes and the frame (pure: everything it touches is passed in, so it is tested without a browser) ----
const sizeOf = value => {
  try { return new TextEncoder().encode(JSON.stringify(value)).length } catch { return Infinity }
}
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const text = (value, max) => (typeof value === 'string' && value.length <= max ? value : null)
const objectKey = obj => String(obj.tipo) + '/' + String(obj.id)

export function createBridge({ rest, getWindow, now = Date.now, schedule, onState = () => {} }) {
  let known = new Map() // tipo/id -> JSON of what the world already holds
  let functions = ''
  let hola = false
  let disposed = false
  let busy = false
  let again = false
  let pending = null
  let tokens = REQUEST_BURST
  let stamp = now()
  const inflight = new Set()

  const send = msg => {
    const target = getWindow()
    if (!target || sizeOf(msg) > MAX_MESSAGE_BYTES) return false
    target.postMessage(msg, '*') // an opaque-origin frame can only be addressed with '*'; the frame side checks event.source
    return true
  }
  const event = (evento, objeto) => send({ v: 1, kind: 'event', evento, objeto })

  async function pull(full) {
    const snap = await rest('/world-snapshot')
    if (!isObject(snap) || snap.ok !== true || !Array.isArray(snap.objetos)) return
    const all = [...snap.objetos, ...(Array.isArray(snap.extra) ? snap.extra : [])].filter(o => isObject(o) && o.tipo && o.id)
    const next = new Map(all.map(o => [objectKey(o), JSON.stringify(o)]))
    const changes = []
    for (const o of all) {
      const was = known.get(objectKey(o))
      if (was === undefined) changes.push(['alta', o])
      else if (was !== next.get(objectKey(o))) changes.push(['cambio', o])
    }
    for (const key of known.keys()) {
      if (!next.has(key)) { const [tipo, ...id] = key.split('/'); changes.push(['baja', { tipo, id: id.join('/') }]) }
    }
    if (full || changes.length > MAX_CHANGES) {
      send({ v: 1, kind: 'snapshot', objetos: snap.objetos })
      for (const o of Array.isArray(snap.extra) ? snap.extra : []) event('alta', o)
    } else {
      for (const [kind, obj] of changes) event(kind, obj)
    }
    known = next
    const names = Array.isArray(snap.funciones) ? snap.funciones.filter(n => typeof n === 'string') : []
    const joined = names.join(',')
    if (full || joined !== functions) { functions = joined; send({ v: 1, kind: 'funciones', funciones: names }) }
    onState({ objects: all.length, readOnly: snap.read_only === true })
  }

  // One pull at a time; a request that arrives meanwhile makes exactly one more.
  async function refresh(full = false) {
    if (disposed || !hola) return
    if (busy) { again = full || again === 'full' ? 'full' : true; return }
    busy = true
    try { await pull(full) } catch (err) { onState({ note: String((err && err.message) || err).slice(0, 120) }) } finally { busy = false }
    if (again) { const f = again === 'full'; again = false; await refresh(f) }
  }

  async function ask(m) {
    const id = text(m.id, 80)
    const objeto = text(m.objeto, 120)
    const verbo = text(m.verbo, 40)
    if (!id || !objeto || !verbo) return
    const answer = (ok, texto) => send({ v: 1, kind: 'event', evento: 'resultado', peticion: id, ok, texto })
    const t = now()
    tokens = Math.min(REQUEST_BURST, tokens + ((t - stamp) / 1000) * REQUEST_PER_SECOND)
    stamp = t
    if (tokens < 1 || inflight.size >= MAX_INFLIGHT || inflight.has(id)) return answer(false, 'Demasiadas peticiones a la vez.')
    tokens -= 1
    inflight.add(id)
    const quien = isObject(m.quien) ? { id: text(m.quien.id, 80) || '', nombre: text(m.quien.nombre, 60) || '' } : undefined
    const body = { id, objeto, verbo, seguridad: text(m.seguridad, 20) || '', ...(quien ? { quien } : {}), ...(typeof m.caduca === 'number' ? { caduca: m.caduca } : {}) }
    try {
      const res = await rest('/action', { method: 'POST', body, timeoutMs: ACTION_TIMEOUT_MS })
      answer(!!(res && res.ok === true), String((res && (res.texto || res.text)) || '').slice(0, 200))
    } catch {
      answer(false, 'Hermes no ha respondido.')
    } finally {
      inflight.delete(id)
    }
  }

  return {
    /** A `message` event from the window. Only the frame's own window is heard; everything else is dropped without a word. */
    fromFrame(ev) {
      const target = getWindow()
      if (disposed || !target || !ev || ev.source !== target) return undefined
      const m = ev.data
      if (!isObject(m) || m.v !== 1 || typeof m.kind !== 'string' || sizeOf(m) > MAX_MESSAGE_BYTES) return undefined
      if (m.kind === 'hola') { hola = true; known = new Map(); functions = ''; onState({ hola: true }); return refresh(true) }
      if (m.kind === 'funciones') return refresh(true)
      if (m.kind === 'peticion') return ask(m)
      if (m.kind === 'error') onState({ note: String(m.motivo || '').slice(0, 120) })
      return undefined
    },
    /** A gateway event. Only this plugin's own are looked at; any of them means "the picture may have changed". */
    onGatewayEvent(ev) {
      if (disposed || pending || !ev || typeof ev.type !== 'string' || !ev.type.startsWith(EVENT_PREFIX)) return
      pending = schedule(() => { pending = null; refresh(false) }, REFRESH_DEBOUNCE_MS)
    },
    refresh,
    dispose() { disposed = true; if (pending) pending(); pending = null },
  }
}

// One shared, tiny poller so the chip and the pane never double-request.
function usePalace(ctx) {
  const [state, setState] = useState({ loading: true, status: null, switches: null, error: '' })
  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const [status, switches] = await Promise.all([ctx.rest('/status'), ctx.rest('/switches')])
        if (alive) setState({ loading: false, status, switches, error: '' })
      } catch (err) {
        // Backend half off (plugin not enabled in config.yaml) or gateway restarting.
        if (alive) setState(prev => ({ ...prev, loading: false, error: String((err && err.message) || err).slice(0, 120) }))
      }
    }
    tick()
    const stop = ctx.setInterval(tick, POLL_MS)
    return () => { alive = false; stop() }
  }, [])
  return state
}

async function openWorld(ctx) {
  try {
    const { url } = await ctx.rest('/world')
    // The backend only ever answers a loopback http URL (127.0.0.1 or localhost); refuse anything else defensively.
    if (typeof url === 'string' && /^http:\/\/(127\.0\.0\.1|localhost):\d+(\/|$)/.test(url)) {
      await ctx.os.openExternal(url)
    } else {
      host.notify({ kind: 'info', message: tr(ctx, 'notRunning') })
    }
  } catch (err) {
    host.notifyError(err, tr(ctx, 'couldNotOpen'))
  }
}

function Chip({ ctx }) {
  const { status, error } = usePalace(ctx)
  const bridgeUp = !!(status && status.bridge)
  const connected = !!(status && status.connected)
  const label = tr(ctx, error ? 'chipError' : connected ? 'chipConnected' : bridgeUp ? 'chipNoEngine' : 'chipOff')
  return jsxs('span', {
    className: 'flex items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    title: error || (status && status.last_error) || label,
    children: [
      // The label is a button: it toggles the right sidebar, where the World pane lives (the same handler as the shortcut).
      jsxs('button', {
        type: 'button',
        className: 'flex items-center gap-1 rounded hover:text-(--ui-accent)',
        'aria-label': tr(ctx, 'togglePanel'),
        title: tr(ctx, 'togglePanel'),
        onClick: () => { if (ctx.runAction) ctx.runAction('view.toggleRightSidebar') },
        children: [
          jsx(StatusDot, { tone: connected ? 'good' : bridgeUp ? 'warn' : 'muted' }),
          jsx('span', { children: label })
        ]
      }),
      jsx('button', {
        type: 'button',
        className: 'ml-1 rounded px-1 hover:text-(--ui-accent) disabled:opacity-40',
        disabled: !connected,
        'aria-label': tr(ctx, 'openBrowser'),
        title: connected ? tr(ctx, 'openBrowser') : tr(ctx, 'notConnected'),
        onClick: () => openWorld(ctx),
        children: jsx(Codicon, { name: 'link-external' })
      })
    ]
  })
}

function Pane({ ctx }) {
  const { loading, status, switches, error } = usePalace(ctx)
  const rows = (switches && switches.functions) || []
  const view = viewState({ loading, status, switches, error })
  const note = {
    error: error ? tr(ctx, 'errorPrefix') + error + tr(ctx, 'errorSuffix') : '',
    loading: tr(ctx, 'loading'),
    disconnected: tr(ctx, 'disconnected'),
    empty: tr(ctx, 'empty'),
    ready: ''
  }[view]
  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 overflow-auto p-3 text-sm',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between',
        children: [
          jsx('div', { className: 'font-medium', children: 'Hermes Palace' }),
          jsxs('span', {
            className: 'flex items-center gap-1',
            children: [
              status && status.read_only ? jsx(Badge, { children: tr(ctx, 'readOnly') }) : null,
              jsx(Badge, { children: tr(ctx, status && status.connected ? 'connected' : status && status.bridge ? 'bridgeNoEngine' : 'off') })
            ]
          })
        ]
      }),
      note ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: note }) : null,
      view === 'loading' ? null : jsx('div', { className: 'text-(--ui-text-secondary)', children: tr(ctx, 'functionsHint') }),
      view === 'loading' ? null : jsx('ul', {
        className: 'flex flex-col gap-1',
        children: rows.map(row => jsxs('li', {
          className: 'flex items-center justify-between gap-2',
          children: [
            jsx('span', { children: LOCALES[pickLocale(ctx)][row.name] || LOCALES.en[row.name] || row.name }),
            jsx(Badge, { children: row.effective ? tr(ctx, 'active') : row.local ? tr(ctx, 'waiting') : tr(ctx, 'off') })
          ]
        }, row.name))
      }),
      jsx(Button, {
        disabled: !(status && status.connected),
        onClick: () => openWorld(ctx),
        children: tr(ctx, 'openWorld')
      })
    ]
  })
}

// ---- the page: the world inside Desktop ----
function commandOf(hint) {
  const m = /`([^`]+)`/.exec(String(hint || ''))
  return m ? m[1] : 'hermes palace up'
}

// loading -> (down | error | ready); ready is "connected" once the world has said hello.
function pageView(state) {
  if (state.phase === 'ready') return state.hola ? 'connected' : 'waiting'
  return state.phase
}

function PalacePage({ ctx }) {
  const [state, setState] = useState({ phase: 'loading', url: '', code: '', hint: '', error: '', hola: false, readOnly: false, note: '', n: 0 })
  const frameRef = useRef(null)
  const bridgeRef = useRef(null)

  const load = async () => {
    setState(prev => ({ ...prev, phase: 'loading', url: '', hola: false, error: '', code: '' }))
    try {
      const res = await ctx.rest('/embed-credential', { method: 'POST', body: {} })
      if (res && res.ok === true && typeof res.url === 'string') {
        setState(prev => ({ ...prev, phase: 'ready', url: res.url, readOnly: res.read_only === true, hola: false, n: prev.n + 1 }))
      } else {
        const code = String((res && res.error) || 'error')
        setState(prev => ({ ...prev, phase: code === 'engine-down' || code === 'no-link-key' || code === 'embed-off' ? 'down' : 'error', code, hint: String((res && res.hint) || ''), error: String((res && res.message) || code) }))
      }
    } catch (err) {
      setState(prev => ({ ...prev, phase: 'error', code: 'rest', error: String((err && err.message) || err).slice(0, 120) }))
    }
  }

  useEffect(() => {
    const bridge = createBridge({
      rest: ctx.rest,
      getWindow: () => (frameRef.current ? frameRef.current.contentWindow : null),
      schedule: ctx.setTimeout,
      onState: patch => setState(prev => ({ ...prev, ...patch }))
    })
    bridgeRef.current = bridge
    const stops = [
      ctx.addEventListener(window, 'message', ev => bridge.fromFrame(ev)),
      ctx.onEvent('*', ev => bridge.onGatewayEvent(ev)),
      ctx.setInterval(() => bridge.refresh(false), WORLD_POLL_MS)
    ]
    load()
    return () => { bridge.dispose(); stops.forEach(stop => stop()) }
  }, [])

  const view = pageView(state)
  const reason = { 'engine-down': 'embedDown', 'no-link-key': 'embedNoKey', 'embed-off': 'embedOff', 'bad-settings': 'embedBad' }[state.code]
  const head = jsxs('div', {
    className: 'flex items-center justify-between gap-2 border-b border-(--ui-border) px-3 py-1.5 text-sm',
    children: [
      jsx('div', { className: 'font-medium', children: 'Hermes Palace' }),
      jsxs('span', {
        className: 'flex items-center gap-1',
        children: [
          state.readOnly ? jsx(Badge, { children: tr(ctx, 'readOnly') }) : null,
          view === 'connected' || view === 'waiting' ? jsx(Badge, { children: tr(ctx, view === 'connected' ? 'embedConnected' : 'embedWaiting') }) : null,
          jsx(Button, { onClick: load, children: tr(ctx, view === 'down' || view === 'error' ? 'retry' : 'reload') })
        ]
      })
    ]
  })
  let body
  if (view === 'loading') {
    body = jsx('div', { className: 'p-3 text-(--ui-text-tertiary)', children: tr(ctx, 'embedLoading') })
  } else if (view === 'down') {
    body = jsxs('div', {
      className: 'flex flex-col gap-2 p-3 text-sm',
      children: [
        jsx('div', { children: tr(ctx, reason || 'embedDown') }),
        jsxs('div', { className: 'text-(--ui-text-secondary)', children: [tr(ctx, 'runCommand') + ' ', jsx('code', { children: commandOf(state.hint) })] })
      ]
    })
  } else if (view === 'error') {
    body = jsx('div', { className: 'p-3 text-sm', children: tr(ctx, reason || 'embedError') + (reason ? '' : state.error) })
  } else {
    body = jsx(SandboxedFrame, {
      ref: frameRef,
      src: state.url,
      title: tr(ctx, 'frameTitle'),
      sandbox: FRAME_SANDBOX,
      className: 'h-full w-full flex-1 border-0'
    }, state.n)
  }
  return jsxs('div', { className: 'flex h-full flex-col', children: [head, state.note ? jsx('div', { className: 'px-3 text-xs text-(--ui-text-tertiary)', children: state.note }) : null, body] })
}

export default {
  id: 'hermes-palace', // must equal the folder name
  name: 'Hermes Palace',
  defaultEnabled: false, // opt-in, like the Python half
  register(ctx) {
    ctx.registerMany([
      { id: 'chip', area: 'statusBar.right', order: 140, render: () => jsx(Chip, { ctx }) },
      {
        id: 'pane', area: 'panes', title: 'World',
        data: { placement: 'right', width: '300px' },
        render: () => jsx(Pane, { ctx })
      },
      { id: 'page', area: ROUTES_AREA, data: { path: '/palace' }, render: () => jsx(PalacePage, { ctx }) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/palace', label: tr(ctx, 'navLabel'), codicon: 'globe' } }
    ])
  }
}
