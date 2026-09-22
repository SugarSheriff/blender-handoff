# Handoff

A Blender add-on that checks a scene for the problems that break 3D assets downstream, exports it as glTF, and publishes it to a REST ingest API.

**[Live demo →](https://sugarsheriff.github.io/blender-handoff/)** · a simulation of the publish flow with toggleable failures, plus a real in-browser `.glb` inspector

---

## Why this exists

An asset can look right in Blender and wrong everywhere else. Usually the cause isn't a bug in either system. It's in the handoff between them: a normal map tagged as color data, a centimeter FBX that arrives 100x too big, a procedural texture that simply doesn't exist in glTF. The artist approved something the file doesn't actually contain.

Handoff sits at that boundary:

```
 Blender scene
     │
     ▼
 Preflight ─── errors block, warnings explain ─── "?" opens TROUBLESHOOTING.md
     │
     ▼
 GLB export (bpy.ops.export_scene.gltf)
     │
     ▼
 Self-check: parse our own output before the network sees it
     │
     ▼
 POST /v1/uploads ──► PUT bytes ──► POST /complete ──► poll GET /v1/assets/{id}
 (Idempotency-Key)    (sha256)      (manifest)         (ready | failed + reasons)
```

## What's here

| Path | What it is |
|---|---|
| `handoff/` | The add-on. Sidebar panel, operators, and preferences. Installs on Blender 4.2+ as an extension, or on 3.6–4.1 as a legacy add-on. |
| `handoff/snapshot.py` | The only preflight code that reads `bpy`. Turns the live scene into plain data. |
| `handoff/core/` | Everything else, with no `bpy` imports: preflight rules, GLB inspector, manifest, HTTP client. Unit-tested in plain Python. |
| `server/mock_ingest.py` | A local ingest API with fault injection, so the add-on has something real to talk to. |
| `tools/make_sample_glb.py` | Builds `sample.glb` from raw bytes. Used as a test fixture and as the demo's model. |
| `tests/` | 32 unit tests (preflight rules, GLB parsing and transforms, and the client against the live mock server with injected failures), plus `blender_smoke.py`, an end-to-end run inside real Blender. |
| `TROUBLESHOOTING.md` | One entry per finding code and per publish error. |
| `index.html`, `sim.js`, `viewer.js`, `inspect.js` | The GitHub Pages demo. `inspect.js` is a port of `core/glb.py`. |

## Design decisions

**The preflight rules don't touch `bpy`.** `snapshot.py` flattens the scene into dicts, and `core/preflight.py` judges them. The rules are the part most likely to change, and this way they can be tested in milliseconds without launching Blender.

**Color space is checked by role, not by name.** The rule doesn't ask whether an image is called `*_normal.png`. It follows each Image Texture node's links downstream (through Mix, Math, and Reroute nodes) until they reach a Principled BSDF socket, a Normal Map node, or a Separate Color node, then checks the color space against what that slot expects. It only walks nodes that actually reach the active Material Output, so stray nodes don't raise false alarms.

**The add-on checks its own output.** After export, the add-on parses the GLB it just wrote with the same inspector the server uses. An exporter surprise is caught on the artist's machine, with a clear message, rather than on the server as a generic rejection.

**Every step is safe to retry.** The two POSTs carry an `Idempotency-Key`. If the server did the work and the response was lost (the mock's `--lose-ack`), the retry gets the original upload back instead of a duplicate. Backoff is exponential with jitter, `Retry-After` is honored on 429, and 4xx errors fail immediately instead of wasting retries.

**Blender stays responsive.** `bpy` isn't thread-safe. Export runs on the main thread, where it has to, and the upload runs on a worker thread that only writes to a locked log buffer. A `bpy.app.timers` callback redraws the panel. Cancel is a `threading.Event` that also cuts short any backoff wait.

**Credentials stay put.** The API token lives in add-on preferences, not in scene properties, so it never ends up inside a `.blend` that gets emailed to a vendor. The client attaches it only to requests going to the API's own origin. A presigned `upload_url` on a storage host never sees it.

**Stdlib only.** Blender's bundled Python doesn't guarantee `requests`. An add-on that pip-installs into someone's Blender creates support tickets.

**Support is built in.** Each finding has a stable code, a **?** button that opens its troubleshooting entry, and a select button that jumps to the object involved. A test fails if a new code is added without documentation. Failed uploads keep the exported file and log its path. The manifest records Blender and exporter versions, so a bug report arrives with the context it needs.

## Running it

### Tests and the mock server (no Blender needed)

```bash
python -m unittest discover -s tests -v
python server/mock_ingest.py --token dev-token --fail-first 1 --rate-limit 1 --lose-ack
```

### In Blender

1. Zip the add-on: `./package.ps1` writes `dist/handoff-extension.zip` (4.2+) and `dist/handoff-legacy.zip` (3.6–4.1).
2. In Blender 4.2+, go to *Edit → Preferences → Get Extensions → ▾ → Install from Disk…* and pick the extension zip. On older versions, use *Add-ons → Install…* with the legacy zip.
3. In the add-on's preferences, set the endpoint to `http://127.0.0.1:8765` and the token to `dev-token`.
4. In the 3D Viewport, press **N** and open the **Handoff** tab. Select something, then press **Run Preflight** and **Publish**.

### Headless test in real Blender

```powershell
./tests/run_blender_smoke.ps1 -Blender "C:\path\to\blender.exe"
```

This installs the packaged extension zip into a throwaway user folder and builds a scene with one known problem per preflight category. It then checks the exact findings, confirms errors block publishing, and publishes to the mock server through a 503, a lost acknowledgement, and a cancel. Verified on **Blender 4.2.23 LTS** and **5.2.2 LTS**.

### The demo page

```bash
python -m http.server 8000
```

Then open <http://localhost:8000>. It has to be served over HTTP, not opened from disk, because it fetches `sample.glb`.

## Known limitations and next steps

- **Node groups aren't entered** during material traversal. A Principled BSDF inside a group is reported as `material.not_principled`.
- **Only GLB is exported.** OpenUSD (`bpy.ops.wm.usd_export`) is the obvious next target, with the same preflight and client, and a manifest recording `metersPerUnit` and `upAxis` from the stage.
- **Textures ship as authored.** A KTX2/Basis step, or a check that recommends one, would matter for mobile and XR targets.
- **Auth is a pasted token.** A real deployment should use an OAuth device-code flow, so artists never handle long-lived secrets.
- **One asset per publish.** Batch publishing (one GLB per selected collection) is a small change to the operator.

## License

The add-on imports `bpy`, so it's licensed **GPL-3.0-or-later**, as Blender requires.
