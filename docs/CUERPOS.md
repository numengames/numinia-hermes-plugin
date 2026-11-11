# Bodies for the bots (stick figures)

One black-and-white stick figure per Hermes profile (up to 14), walking a short round in the control tower (z6).

**Status: PARTIAL.** Done: the 14 figures exist, are told apart by spot and by name label, and walk (verified, see below).
Not done: "a working agent goes to its post" and "an idle agent rests in its place".

## How it works
- Figure: `assets/muneco-palo.glb` (CC0, 108 triangles, 15 kB, clips `Walking` and `Idle`; record in `assets/muneco-palo.LICENCIA.md`, generator `scripts/hacer_muneco.py`).
- `hermes_3d_lib/cuerpos.py` writes `<world>/aventura/pnj/cuerpo-<profile>.json` (engine format `pnj@1` with `ronda`) and copies the figure to `<world>/datos/activos/<sha256>.glb`. It runs just before the engine starts (the engine reads `aventura/` only then). It touches only `cuerpo-*.json`.
- No engine change: it reuses the engine's own NPC piece.

## Needs engine release newer than the pinned one
The `ronda` of `pnj@1` landed in engine PR #318, after the pinned release `motor-7c6c9a2` (`engine.lock.json`). The validator of that checkout rejects it ("ronda: campo desconocido"). The pin must move to a release that contains #318 (e.g. `motor-31640cb` or newer) with its sha256 and size: moved to `motor-5b2427f` on branch `pin/motor-5b2427f` (commit fd78e84, separate from this one; no push permission to the public repo from here).

## Verified / not verified
- Verified: 5 unit tests (`tests/test_cuerpos.py`, run with a small runner, pytest is not installed here); `node tools/aventura.ts` on the world: "Todo en orden"; engine at origin/main 5b2427f6 on a scratch world (port 8890): `/a/<sha>.glb` answers 200, the page lists 14 persons, I walked to them and saw the figures with the walk pose, at different positions between two screenshots, with their name label (`docs/images/hermes-3d-bodies.png`).
- Not verified: bronte-xs-ca-003 (no access from here); the figure against a lit/bright floor; the Idle clip (the round pauses 3-6 s per end; I did not capture a stopped figure); the figures never cross walls or the profile things (stretches are 2.4 m in the free front aisle; not checked against the objects of z6 when 14 profiles exist).
- Contrast (t_ca92a923): the first version rendered dim grey on the black floor. The white material is now emissive (`emissiveFactor` 1,1,1) so it stays white whatever the light, and the sticks are 7 cm thick (were 5). The black visor is kept (on the white head). Seen only in a previewer, NOT yet re-checked inside the engine.

## Request to the engine (piece (a)) - card t_32b69dbf on board motor-vr
Pablo said yes on 2026-11-11 (R-1). The card is assigned to galatea.

Reusable piece: a `si` condition of `pnj@1` (and of `ronda`) on the **live state of a mirror object/profile** (e.g. `{ "objeto": "perfil/<id>", "estado": "activo" }`), or a way for the plugin to move/place the `perfil@z6` thing. With it: a working agent's figure goes to its work post (a `ronda` point, or a fixed `posicion`), an idle one stays resting there (`reposo`). Today `si` only reads per-player quests, and `ronda` has points, not areas.
