# muneco-palo.glb — registro

- **Qué es**: figura de palo en blanco y negro (cabeza cúbica con visor negro que marca el frente, tronco, brazos y piernas como barras). 1,68 m de alto, pies en y=0, origen entre los pies, mira a +z.
- **Quién**: Galatea (casa Numinia), generado por `scripts/hacer_muneco.py` (Python stdlib, determinista: dos ejecuciones dan el mismo sha256).
- **Licencia**: CC0 1.0. Obra propia de la casa; sin activos de terceros, sin persona real.
- **sha256**: b6fbdaea4f949aaf773b2cbc1d70d3e0f9cc507d1422db00c817941c29b47224
- **Medido**: 15 296 bytes; 108 triángulos; 2 materiales (blanco, negro); 10 nodos; 2 clips: `Walking` (1 s, bucle) e `Idle` (4 s). Cuadro delimitador x ±0,26, y 0–1,68, z −0,11…0,17 (leído con @gltf-transform).
- **Nombres de clip**: elegidos para que el motor los coja por nombre (esClipCamina: walk|camina|andar; esClipReposo: idle|reposo|respira, según la comprobación t_92431e6e).
- **Visto**: previsualizado en three.js (`assets/preview/visor.html`, captura `muneco-palo-lado-paso.png`: zancada vista de lado) y reposo de tres cuartos.
- **Probado en el motor** (origin/main 5b2427f6, mundo de prueba local): carga (200) y anda con el clip Walking, 14 a la vez. **No comprobado**: el clip Idle visto; bronte-xs-ca-003; peso en un mundo con 14 figuras; validador oficial glTF (no instalado; solo lectura con gltf-transform sin error).
