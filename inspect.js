// JavaScript port of handoff/core/glb.py's inspect(). Keep the two in step.

const GLB_MAGIC = 0x46546c67;
const CHUNK_JSON = 0x4e4f534a;
const CHUNK_BIN = 0x004e4942;

export const SUPPORTED_EXTENSIONS = new Set([
  'KHR_materials_clearcoat', 'KHR_materials_emissive_strength', 'KHR_materials_ior',
  'KHR_materials_sheen', 'KHR_materials_specular', 'KHR_materials_transmission',
  'KHR_materials_unlit', 'KHR_materials_volume', 'KHR_mesh_quantization',
  'KHR_texture_transform', 'KHR_lights_punctual', 'EXT_texture_webp',
]);

export class GlbError extends Error {}

export function readGlb(buffer) {
  const view = new DataView(buffer);
  if (buffer.byteLength < 12) throw new GlbError('file is shorter than the 12-byte GLB header');
  const magic = view.getUint32(0, true);
  const version = view.getUint32(4, true);
  const length = view.getUint32(8, true);
  if (magic !== GLB_MAGIC) throw new GlbError("missing 'glTF' magic; not a binary glTF (a .gltf JSON file, or a renamed FBX?)");
  if (version !== 2) throw new GlbError(`GLB container version ${version}; only version 2 is supported`);
  if (length !== buffer.byteLength) throw new GlbError(`header declares ${length} bytes but the file is ${buffer.byteLength} bytes (truncated transfer?)`);

  let doc = null;
  let binLength = 0;
  let offset = 12;
  while (offset < length) {
    if (offset + 8 > length) throw new GlbError('truncated chunk header');
    const chunkLength = view.getUint32(offset, true);
    const chunkType = view.getUint32(offset + 4, true);
    const start = offset + 8;
    const end = start + chunkLength;
    if (end > length) throw new GlbError('a chunk runs past the end of the file');
    if (chunkType === CHUNK_JSON && doc === null) {
      try {
        doc = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, start, chunkLength)));
      } catch (e) {
        throw new GlbError(`JSON chunk is not valid JSON: ${e.message}`);
      }
    } else if (chunkType === CHUNK_BIN && !binLength) {
      binLength = chunkLength;
    }
    offset = end;
  }
  if (!doc) throw new GlbError('no JSON chunk');
  return { doc, binLength };
}

export function inspect(buffer) {
  const { doc, binLength } = readGlb(buffer);
  const issues = [];
  const asset = doc.asset || {};
  if (asset.version !== '2.0') issues.push(`asset.version is ${JSON.stringify(asset.version)}, expected '2.0'`);

  const accessors = doc.accessors || [];
  const firstBuffer = (doc.buffers || [{}])[0] || {};
  (doc.bufferViews || []).forEach((v, i) => {
    const isGlbBuffer = (v.buffer ?? 0) === 0 && !('uri' in firstBuffer);
    if (isGlbBuffer && (v.byteOffset || 0) + (v.byteLength || 0) > binLength) {
      issues.push(`bufferView ${i} reads past the end of the BIN chunk`);
    }
  });

  (doc.meshes || []).forEach((mesh, mi) => {
    const name = mesh.name || `mesh ${mi}`;
    for (const prim of mesh.primitives || []) {
      const pos = prim.attributes?.POSITION;
      if (pos === undefined) issues.push(`'${name}' has a primitive with no POSITION attribute`);
      else if (!accessors[pos]?.min || !accessors[pos]?.max) issues.push(`'${name}' POSITION accessor is missing min/max (required by the spec)`);
    }
  });

  const required = doc.extensionsRequired || [];
  for (const ext of required) {
    if (!SUPPORTED_EXTENSIONS.has(ext)) issues.push(`requires extension ${ext}, which this receiver cannot decode`);
  }

  const { triangles, primitives, bounds } = walkScene(doc, accessors);
  const pbrMaps = (m) => {
    const pbr = m.pbrMetallicRoughness || {};
    return [
      ['baseColor', pbr.baseColorTexture], ['metallicRoughness', pbr.metallicRoughnessTexture],
      ['normal', m.normalTexture], ['occlusion', m.occlusionTexture], ['emissive', m.emissiveTexture],
    ].filter(([, t]) => t).map(([n]) => n);
  };

  return {
    generator: asset.generator,
    version: asset.version,
    bytes: buffer.byteLength,
    counts: {
      nodes: (doc.nodes || []).length,
      meshes: (doc.meshes || []).length,
      primitives,
      materials: (doc.materials || []).length,
      textures: (doc.textures || []).length,
      images: (doc.images || []).length,
      animations: (doc.animations || []).length,
    },
    triangles,
    bounds,
    extensionsUsed: doc.extensionsUsed || [],
    extensionsRequired: required,
    images: (doc.images || []).map((img) => img.mimeType || 'external uri'),
    materials: (doc.materials || []).map((m, i) => ({
      name: m.name || `material ${i}`,
      alphaMode: m.alphaMode || 'OPAQUE',
      doubleSided: !!m.doubleSided,
      maps: pbrMaps(m),
    })),
    issues,
  };
}

function walkScene(doc, accessors) {
  const nodes = doc.nodes || [];
  const meshes = doc.meshes || [];
  let roots;
  if (doc.scenes?.length) {
    roots = doc.scenes[doc.scene ?? 0].nodes || [];
  } else {
    const children = new Set(nodes.flatMap((n) => n.children || []));
    roots = nodes.map((_, i) => i).filter((i) => !children.has(i));
  }

  const lo = [Infinity, Infinity, Infinity];
  const hi = [-Infinity, -Infinity, -Infinity];
  let triangles = 0;
  let primitives = 0;
  let guard = 0;
  const stack = roots.map((i) => [i, IDENTITY]);
  while (stack.length && guard++ < 100000) {
    const [index, parent] = stack.pop();
    const node = nodes[index];
    if (!node) continue;
    const world = mul(parent, localMatrix(node));
    if (node.mesh !== undefined) {
      for (const prim of meshes[node.mesh]?.primitives || []) {
        const pos = prim.attributes?.POSITION;
        if (pos === undefined) continue;
        primitives++;
        const acc = accessors[pos];
        const count = prim.indices !== undefined ? accessors[prim.indices].count : acc.count;
        const mode = prim.mode ?? 4;
        if (mode === 4) triangles += Math.floor(count / 3);
        else if (mode === 5 || mode === 6) triangles += Math.max(count - 2, 0);
        if (acc.min && acc.max) {
          for (const x of [acc.min[0], acc.max[0]])
            for (const y of [acc.min[1], acc.max[1]])
              for (const z of [acc.min[2], acc.max[2]]) {
                const p = transform(world, [x, y, z]);
                for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], p[k]); hi[k] = Math.max(hi[k], p[k]); }
              }
        }
      }
    }
    for (const c of node.children || []) stack.push([c, world]);
  }
  const bounds = lo[0] === Infinity ? null : { min: lo, max: hi, size: hi.map((h, k) => h - lo[k]) };
  return { triangles, primitives, bounds };
}

const IDENTITY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]];

function localMatrix(node) {
  if (node.matrix) {
    const m = node.matrix; // column-major
    return [0, 1, 2, 3].map((r) => [0, 1, 2, 3].map((c) => m[c * 4 + r]));
  }
  const [tx, ty, tz] = node.translation || [0, 0, 0];
  const [x, y, z, w] = node.rotation || [0, 0, 0, 1];
  const [sx, sy, sz] = node.scale || [1, 1, 1];
  return [
    [(1 - 2 * (y * y + z * z)) * sx, 2 * (x * y - z * w) * sy, 2 * (x * z + y * w) * sz, tx],
    [2 * (x * y + z * w) * sx, (1 - 2 * (x * x + z * z)) * sy, 2 * (y * z - x * w) * sz, ty],
    [2 * (x * z - y * w) * sx, 2 * (y * z + x * w) * sy, (1 - 2 * (x * x + y * y)) * sz, tz],
    [0, 0, 0, 1],
  ];
}

function mul(a, b) {
  return [0, 1, 2, 3].map((r) => [0, 1, 2, 3].map((c) => a[r][0] * b[0][c] + a[r][1] * b[1][c] + a[r][2] * b[2][c] + a[r][3] * b[3][c]));
}

function transform(m, p) {
  return [0, 1, 2].map((r) => m[r][0] * p[0] + m[r][1] * p[1] + m[r][2] * p[2] + m[r][3]);
}
