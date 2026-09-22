// Scripted replay of the add-on's publish flow. Messages, retry rules, and status codes
// mirror handoff/core/preflight.py, handoff/core/client.py, and server/mock_ingest.py.
(() => {
  const consoleEl = document.getElementById('console');
  const rail = document.getElementById('rail');
  const btnPublish = document.getElementById('btn-publish');
  const btnPreflight = document.getElementById('btn-preflight');
  const toggles = [...document.querySelectorAll('[data-fault]')];

  const STAGES = ['preflight', 'export', 'create', 'upload', 'complete', 'process', 'ready'];
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let t0 = 0;
  let running = false;

  const faults = () => Object.fromEntries(toggles.map((t) => [t.dataset.fault, t.checked]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, reduceMotion ? Math.min(ms, 60) : ms));
  const hex = (n) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('');

  function log(text, cls = '') {
    const line = document.createElement('div');
    line.className = `line ${cls}`;
    const t = document.createElement('span');
    t.className = 't';
    t.textContent = `+${((performance.now() - t0) / 1000).toFixed(2)}s`;
    line.append(t, text);
    consoleEl.append(line);
    consoleEl.scrollTop = consoleEl.scrollHeight;
  }

  function stage(name, state) {
    const li = rail.querySelector(`[data-stage="${name}"]`);
    li.className = state;
  }

  function resetRail() {
    rail.querySelectorAll('li').forEach((li) => (li.className = ''));
  }

  function skipRest(from) {
    STAGES.slice(STAGES.indexOf(from) + 1).forEach((s) => stage(s, 'skip'));
  }

  // Findings for each scene toggle, in the order preflight.run() sorts them (errors first).
  function preflightFindings(f) {
    const out = [];
    if (f.missing) out.push({ sev: 'error', code: 'texture.missing', target: 'Brass / brass_rough.png',
      msg: 'brass_rough.png points at a file that does not exist, so the exporter has nothing to embed.',
      fix: 'Relink it (File > External Data > Find Missing Files) or pack it into the .blend.' });
    if (f.procedural) out.push({ sev: 'warn', code: 'material.procedural', target: 'Coral Clay',
      msg: 'Coral Clay uses a Noise Texture node. glTF has no procedural textures, so that part of the look is dropped on export.',
      fix: 'Bake the procedural result to an image texture and plug that in instead.' });
    if (f.colorspace) out.push({ sev: 'warn', code: 'texture.colorspace', target: 'Brass / brass_normal.png',
      msg: 'brass_normal.png feeds normal data but is tagged sRGB. The viewport lies; runtimes read it as raw data, so shading downstream will not match what was approved.',
      fix: "Set the Image Texture node's Color Space to Non-Color." });
    if (f.units) out.push({ sev: 'warn', code: 'units.suspicious_size', target: 'Plinth',
      msg: 'Plinth is 140.0 m across. That is usually a centimeter asset imported as meters (100x too big).',
      fix: 'Check the source units; if it came from FBX, re-import with the right scale and apply it.' });
    return out;
  }

  async function runPreflight(f) {
    stage('preflight', 'active');
    log('handoff.preflight  scope=SELECTED  objects=2  budget=500,000 tris', 'req');
    await sleep(500);
    const findings = preflightFindings(f);
    for (const x of findings) {
      log(`${x.sev.toUpperCase().padEnd(5)} ${x.code}  ${x.target}`, x.sev === 'error' ? 'err' : 'warn');
      log(x.msg, 'fix');
      log(`Fix: ${x.fix}`, 'fix');
      await sleep(260);
    }
    const errors = findings.filter((x) => x.sev === 'error').length;
    const warns = findings.length - errors;
    log(findings.length ? `Preflight: ${errors} error(s), ${warns} warning(s)` : 'Preflight clean', errors ? 'err' : findings.length ? 'warn' : 'ok');
    stage('preflight', errors ? 'fail' : 'done');
    return errors;
  }

  // One HTTP call with the client's retry policy. `script` is the list of responses the
  // server gives on successive attempts; the last entry repeats.
  async function call(stageName, label, script) {
    const maxAttempts = 5;
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      const res = script[Math.min(attempt - 1, script.length - 1)];
      log(`→ ${label}`, 'req');
      await sleep(380);
      if (res.ok) {
        log(`← ${res.status} ${res.note || ''}`.trim(), 'ok');
        stage(stageName, 'done');
        return res;
      }
      const base = 0.5 * 2 ** (attempt - 1);
      const delay = res.retryAfter ?? +(base / 2 + Math.random() * base / 2).toFixed(1);
      stage(stageName, 'retry');
      log(`← ${res.status} ${res.note}`, 'err');
      log(`retry ${attempt}/${maxAttempts - 1} in ${delay.toFixed(1)}s${res.retryAfter ? ' (server sent Retry-After)' : ' (backoff + jitter)'}`, 'warn');
      await sleep(delay * 1000);
      stage(stageName, 'active');
    }
  }

  async function publish() {
    running = true;
    btnPublish.disabled = btnPreflight.disabled = true;
    consoleEl.replaceChildren();
    resetRail();
    t0 = performance.now();
    const f = faults();

    const errors = await runPreflight(f);
    if (errors) {
      log('Publish blocked. Errors ship broken assets; fix them first.', 'err');
      skipRest('preflight');
      return;
    }

    stage('export', 'active');
    log("bpy.ops.export_scene.gltf(export_format='GLB', use_selection=True, export_apply=True, export_yup=True)", 'req');
    await sleep(700);
    const bytes = f.draco ? 18432 : 44968;
    log(`wrote plinth_orb.glb  ${bytes.toLocaleString()} bytes`, 'dim');
    log(`self-check: glTF 2.0 · 2 meshes · 2,220 tris · extensionsRequired: [${f.draco ? 'KHR_draco_mesh_compression' : ''}]`, 'ok');
    stage('export', 'done');

    const sha = hex(32);
    const key = crypto.randomUUID();
    const upl = `upl_${hex(6)}`;
    log(`sha256 ${sha.slice(0, 12)}  Idempotency-Key ${key.slice(0, 8)}…`, 'dim');

    stage('create', 'active');
    const createScript = [];
    if (f.s503) createScript.push({ status: 503, note: 'injected: service unavailable' }, { status: 503, note: 'injected: service unavailable' });
    if (f.s429) createScript.push({ status: 429, note: 'rate limited', retryAfter: 1 });
    if (f.ackloss) createScript.push({ status: 502, note: `upstream dropped the response (server already created ${upl})` });
    createScript.push({ ok: true, status: f.ackloss ? 200 : 201,
      note: f.ackloss ? `${upl}: same key, original upload replayed. No duplicate.` : `${upl} reserved` });
    await call('create', 'POST /v1/uploads', createScript);

    stage('upload', 'active');
    await call('upload', `PUT /v1/uploads/${upl}/content?sig=…  (${bytes.toLocaleString()} bytes)`,
      [{ ok: true, status: 200, note: 'size and sha256 verified by server' }]);

    stage('complete', 'active');
    const ast = `ast_${hex(6)}`;
    await call('complete', `POST /v1/uploads/${upl}/complete  {manifest: handoff.manifest/1}`,
      [{ ok: true, status: 202, note: `${ast} processing` }]);

    stage('process', 'active');
    for (let i = 0; i < 2; i++) {
      log(`→ GET /v1/assets/${ast}`, 'req');
      await sleep(450);
      log('← 200 status=processing', 'dim');
      await sleep(400);
    }
    log(`→ GET /v1/assets/${ast}`, 'req');
    await sleep(450);
    if (f.draco) {
      log('← 200 status=failed', 'err');
      log('ingest rejected the asset: requires extension KHR_draco_mesh_compression, which this receiver cannot decode', 'err');
      log('export kept for debugging: %TEMP%\\handoff_x8k2\\plinth_orb.glb', 'dim');
      stage('process', 'fail');
      skipRest('process');
      return;
    }
    log('← 200 status=ready', 'ok');
    stage('process', 'done');
    stage('ready', 'done');
    log(`asset ${ast} ready · 2,220 tris · bounds 1.40 × 1.50 × 1.40 m`, 'ok');
  }

  async function preflightOnly() {
    btnPublish.disabled = btnPreflight.disabled = true;
    consoleEl.replaceChildren();
    resetRail();
    t0 = performance.now();
    await runPreflight(faults());
    skipRest('preflight');
    btnPublish.disabled = btnPreflight.disabled = false;
  }

  btnPublish.addEventListener('click', async () => {
    if (running) return;
    try {
      await publish();
    } finally {
      running = false;
      btnPublish.disabled = btnPreflight.disabled = false;
    }
  });
  btnPreflight.addEventListener('click', preflightOnly);
})();
