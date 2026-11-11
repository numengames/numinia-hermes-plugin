#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 Numen Games S.L.
# SPDX-License-Identifier: MIT

"""Deterministic generator of the stick-figure GLB ("muneco-palo.glb").

Stdlib only. Same bytes on every run (no timestamps, no randomness).

Budget (set before modelling): <= 300 triangles, <= 16 kB, 2 materials (white, black), 1 mesh per node,
height 1.70 m, feet at y = 0, origin between the feet, faces +z (the engine's own-model convention).
Clips are named so the engine picks them up by name (packages/render/src/vida.ts):
"Walking" (contains "walk") and "Idle" (contains "idle").

Authorship / licence: made by Galatea (Numinia house), no third-party asset. The figure this script generates is
CC0 1.0 (see assets/muneco-palo.LICENCIA.md); the script itself is MIT (header above).

Usage: python3 scripts/hacer_muneco.py [out.glb]
"""

from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

WHITE = 0
BLACK = 1


def box(cx, cy, cz, sx, sy, sz):
    """Axis-aligned box -> (positions, normals, indices) 24 verts, 12 tris, flat normals."""
    hx, hy, hz = sx / 2, sy / 2, sz / 2
    faces = [
        ((0, 0, 1), [(-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]),
        ((0, 0, -1), [(1, -1, -1), (-1, -1, -1), (-1, 1, -1), (1, 1, -1)]),
        ((1, 0, 0), [(1, -1, 1), (1, -1, -1), (1, 1, -1), (1, 1, 1)]),
        ((-1, 0, 0), [(-1, -1, -1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1)]),
        ((0, 1, 0), [(-1, 1, 1), (1, 1, 1), (1, 1, -1), (-1, 1, -1)]),
        ((0, -1, 0), [(-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1)]),
    ]
    pos, nor, idx = [], [], []
    for n, vs in faces:
        base = len(pos)
        for vx, vy, vz in vs:
            pos.append((cx + vx * hx, cy + vy * hy, cz + vz * hz))
            nor.append(n)
        idx += [base, base + 1, base + 2, base, base + 2, base + 3]
    return pos, nor, idx


def merge(parts):
    pos, nor, idx = [], [], []
    for p, n, i in parts:
        b = len(pos)
        pos += p
        nor += n
        idx += [b + k for k in i]
    return pos, nor, idx


def quat_x(deg):
    a = math.radians(deg) / 2
    return (round(math.sin(a), 6), 0.0, 0.0, round(math.cos(a), 6))


# Measurements (metres). Hips at 0.88, shoulders at 1.42, head centre at 1.58.
HIP_Y, SHOULDER_Y = 0.88, 1.42
LEG = HIP_Y  # leg length
ARM = 0.55
T = 0.07  # stick thickness (thicker: reads from far)


def build():
    bufs = bytearray()
    views, accessors, meshes, nodes = [], [], [], []

    def add_view(data: bytes, target=None):
        while len(bufs) % 4:
            bufs.append(0)
        off = len(bufs)
        bufs.extend(data)
        v = {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
        if target:
            v["target"] = target
        views.append(v)
        return len(views) - 1

    def add_acc(view, ctype, count, atype, mn=None, mx=None):
        a = {"bufferView": view, "componentType": ctype, "count": count, "type": atype}
        if mn is not None:
            a["min"], a["max"] = mn, mx
        accessors.append(a)
        return len(accessors) - 1

    def add_mesh(name, parts_by_mat):
        prims = []
        for mat, (pos, nor, idx) in parts_by_mat:
            pv = add_view(b"".join(struct.pack("<3f", *p) for p in pos), 34962)
            nv = add_view(b"".join(struct.pack("<3f", *n) for n in nor), 34962)
            iv = add_view(struct.pack(f"<{len(idx)}H", *idx), 34963)
            pa = add_acc(
                pv,
                5126,
                len(pos),
                "VEC3",
                [round(min(p[i] for p in pos), 5) for i in range(3)],
                [round(max(p[i] for p in pos), 5) for i in range(3)],
            )
            na = add_acc(nv, 5126, len(nor), "VEC3")
            ia = add_acc(iv, 5123, len(idx), "SCALAR")
            prims.append({"attributes": {"POSITION": pa, "NORMAL": na}, "indices": ia, "material": mat, "mode": 4})
        meshes.append({"name": name, "primitives": prims})
        return len(meshes) - 1

    # --- meshes (local to each pivot) -----------------------------------------------------------
    m_torso = add_mesh("torso", [(WHITE, merge([box(0, (SHOULDER_Y - HIP_Y) / 2, 0, T, SHOULDER_Y - HIP_Y + 0.04, T)]))])
    m_cadera = add_mesh("cadera_barra", [(WHITE, merge([box(0, 0, 0, 0.26, T, T)]))])
    m_hombros = add_mesh("hombros_barra", [(WHITE, merge([box(0, 0, 0, 0.40, T, T)]))])
    m_cabeza = add_mesh(
        "cabeza",
        [
            (WHITE, merge([box(0, 0.12, 0, 0.22, 0.22, 0.22)])),
            (BLACK, merge([box(0, 0.13, 0.115, 0.14, 0.05, 0.02)])),  # visor: shows where it faces
        ],
    )
    m_brazo = add_mesh(
        "brazo", [(WHITE, merge([box(0, -ARM / 2, 0, T, ARM, T)])), (WHITE, merge([box(0, -ARM - 0.02, 0, 0.08, 0.06, 0.08)]))]
    )
    m_pierna = add_mesh(
        "pierna", [(WHITE, merge([box(0, -LEG / 2, 0, T, LEG, T)])), (WHITE, merge([box(0, -LEG + 0.03, 0.07, 0.08, 0.06, 0.2)]))]
    )

    # --- node tree ------------------------------------------------------------------------------
    def node(name, mesh=None, t=None, children=None):
        n = {"name": name}
        if mesh is not None:
            n["mesh"] = mesh
        if t:
            n["translation"] = list(t)
        if children:
            n["children"] = children
        nodes.append(n)
        return len(nodes) - 1

    pierna_i = node("pierna_i", m_pierna, (-0.11, 0, 0))
    pierna_d = node("pierna_d", m_pierna, (0.11, 0, 0))
    brazo_i = node("brazo_i", m_brazo, (-0.22, SHOULDER_Y - HIP_Y, 0))
    brazo_d = node("brazo_d", m_brazo, (0.22, SHOULDER_Y - HIP_Y, 0))
    hombros = node("hombros", m_hombros, (0, SHOULDER_Y - HIP_Y, 0))
    cabeza = node("cabeza", m_cabeza, (0, SHOULDER_Y - HIP_Y + 0.03, 0))
    torso = node("torso", m_torso, (0, 0, 0))
    barra_cadera = node("cadera_barra", m_cadera, (0, 0, 0))
    cadera = node(
        "cadera",
        None,
        (0, HIP_Y, 0),
        [torso, barra_cadera, hombros, cabeza, brazo_i, brazo_d, pierna_i, pierna_d],
    )
    root = node("muneco", None, None, [cadera])

    # --- animations (rotation about X only; translation of cadera for the walk bob) --------------
    anims = []

    def clip(name, duration, tracks):
        samplers, channels = [], []
        for node_i, path, times, values in tracks:
            tv = add_view(struct.pack(f"<{len(times)}f", *times))
            ta = add_acc(tv, 5126, len(times), "SCALAR", [min(times)], [max(times)])
            flat = [c for v in values for c in v]
            vv = add_view(struct.pack(f"<{len(flat)}f", *flat))
            va = add_acc(vv, 5126, len(values), "VEC4" if path == "rotation" else "VEC3")
            samplers.append({"input": ta, "output": va, "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": node_i, "path": path}})
        anims.append({"name": name, "samplers": samplers, "channels": channels})

    def swing(amp, times):
        return [quat_x(amp), quat_x(0), quat_x(-amp), quat_x(0), quat_x(amp)] if len(times) == 5 else []

    t5 = [0.0, 0.25, 0.5, 0.75, 1.0]
    A = 32
    clip(
        "Walking",
        1.0,
        [
            (pierna_i, "rotation", t5, swing(A, t5)),
            (pierna_d, "rotation", t5, swing(-A, t5)),
            (brazo_i, "rotation", t5, swing(-A * 0.8, t5)),
            (brazo_d, "rotation", t5, swing(A * 0.8, t5)),
            (
                cadera,
                "translation",
                t5,
                [(0, HIP_Y, 0), (0, HIP_Y + 0.03, 0), (0, HIP_Y, 0), (0, HIP_Y + 0.03, 0), (0, HIP_Y, 0)],
            ),
        ],
    )
    t3 = [0.0, 2.0, 4.0]
    clip(
        "Idle",
        4.0,
        [
            (brazo_i, "rotation", t3, [quat_x(0), quat_x(4), quat_x(0)]),
            (brazo_d, "rotation", t3, [quat_x(0), quat_x(-4), quat_x(0)]),
            (cadera, "translation", t3, [(0, HIP_Y, 0), (0, HIP_Y - 0.012, 0), (0, HIP_Y, 0)]),
        ],
    )

    while len(bufs) % 4:
        bufs.append(0)
    gltf = {
        "asset": {"version": "2.0", "generator": "hacer_muneco.py (Galatea)", "copyright": "CC0 1.0 - Numinia house, Galatea"},
        "scene": 0,
        "scenes": [{"name": "muneco-palo", "nodes": [root]}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": [
            {
                "name": "blanco",
                "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "metallicFactor": 0, "roughnessFactor": 1},
                "emissiveFactor": [1, 1, 1],
            },
            {
                "name": "negro",
                "pbrMetallicRoughness": {"baseColorFactor": [0, 0, 0, 1], "metallicFactor": 0, "roughnessFactor": 1},
                "emissiveFactor": [0, 0, 0],
            },
        ],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(bufs)}],
        "animations": anims,
    }
    js = json.dumps(gltf, separators=(",", ":"), sort_keys=True).encode()
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(bufs)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<I4s", len(js), b"JSON")
        + js
        + struct.pack("<I4s", len(bufs), b"BIN\0")
        + bytes(bufs)
    )


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "assets" / "muneco-palo.glb")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(build())
    print(out, out.stat().st_size, "bytes")
