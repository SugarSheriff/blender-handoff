# Troubleshooting

Every preflight finding has an entry here. The **?** button next to a finding in the Blender panel opens its entry directly. Publish errors are listed further down.

Each entry covers what you see, why it matters once the asset leaves Blender, and how to fix it.

---

## Preflight findings

### units.scale_length
**You see:** *Scene unit scale is 0.01…*
**Why:** Blender's Unit Scale only changes how the viewport labels distances. The glTF exporter writes raw Blender units, and glTF defines one unit as one meter. A scene that reads "180 cm" in the viewport can arrive as 180 meters, or as 1.8, depending on how it was modeled. The receiver can't tell which.
**Fix:** Set *Scene Properties → Units → Unit Scale* to `1.0` and model at real-world size.

### units.suspicious_size
**You see:** *Chair is 9,000.0 m across…*
**Why:** Real products are rarely larger than a building or smaller than a grain of rice. Sizes like that usually come from an FBX authored in centimeters and imported as meters (100x), or the reverse. Downstream, this breaks physics, AR placement, and any viewer that frames the camera from the bounds.
**Fix:** Re-import with the correct scale factor, then *Object → Apply → Scale*.

### transform.negative_scale
**You see:** *…has negative scale, which mirrors the mesh…*
**Why:** Mirroring flips triangle winding. Blender compensates in the viewport. Runtimes that cull back faces can render the object inside-out, and lighting can go wrong where the normals are flipped.
**Fix:** *Ctrl+A → Scale*, then *Mesh → Normals → Recalculate Outside* (Shift+N) in Edit Mode.

### transform.shear_risk
**You see:** *…non-uniform scale and child objects…*
**Why:** When a parent is scaled unevenly and a child is rotated, the child ends up with shear. glTF nodes store translation, rotation, and scale only, so shear can't be represented. The exporter bakes an approximation, and children end up in slightly different places.
**Fix:** Apply scale on the parent before export.

### mesh.missing_uvs
**You see:** *…uses image textures but has no UV map…* (blocks publish)
**Why:** Without UVs, every vertex samples the same texel, so the object ships as a flat color.
**Fix:** Unwrap the mesh (*U → Smart UV Project* is a fast start) or add a UV map.

### budget.triangles
**You see:** *612,000 triangles after modifiers, over the 500,000 budget…*
**Why:** The count is taken *after* modifiers (Subdivision, Array, Mirror), because the export applies them. A 20k-triangle base mesh with Subdivision level 3 exports at around 1.3M.
**Fix:** Lower the modifier levels, decimate, or delete hidden interior geometry. The budget is set in *Preferences → Add-ons → Handoff*.

### material.not_principled
**You see:** *…not built on a Principled BSDF…*
**Why:** The glTF exporter reads PBR values from a Principled BSDF. Other shaders (Diffuse, Glossy, Toon, custom node groups) have no glTF equivalent, so the material exports as a flat default.
**Fix:** Rebuild the material on a Principled BSDF, or bake the look to image textures.

### material.procedural
**You see:** *…uses a Noise Texture node…*
**Why:** glTF materials only reference image textures. Procedural nodes are computed at render time inside Blender and are dropped on export. *Bump* is listed too, because only a *Normal Map* node exports.
**Fix:** Bake the result to an image (*Render Properties → Bake* in Cycles) and connect the image in place of the procedural node.

### texture.missing
**You see:** *…points at a file that does not exist…* (blocks publish)
**Why:** The texture path is broken. The exporter has nothing to embed, and the material ships without that map.
**Fix:** *File → External Data → Find Missing Files*, or pack the images into the .blend.

### texture.udim
**You see:** *…is a UDIM tiled image…*
**Why:** glTF has no concept of UDIM tiles. Only one tile's worth of texture survives export.
**Fix:** Bake the tiles down to a single texture set per material.

### texture.colorspace
**You see:** *…feeds base color but is tagged Non-Color…* or *…feeds normal data but is tagged sRGB…*
**Why:** This is the most common reason an asset looks right in Blender and wrong everywhere else. Color textures (base color, emission) are sRGB-encoded. Data textures (normal, roughness, metallic, occlusion) are raw numbers. glTF viewers decode by slot, whatever the tag in Blender says. If the tag is wrong, Blender previews the asset incorrectly, and the artist approves a look that the delivered file won't have.
**Fix:** In the *Image Texture* node, set *Color Space* to **sRGB** for base color and emission, and to **Non-Color** for everything else.

### texture.oversize
**You see:** *…is 8192x8192…*
**Why:** An 8K RGBA texture uses about 256 MB of GPU memory uncompressed, and more with mipmaps. That's more than many phones and standalone headsets can spare, and the detail rarely shows at typical viewing distances.
**Fix:** Downscale to 4096px or less. Most web targets are fine at 2048.

### texture.npot
**You see:** *…is 1000x1000, not a power of two…* (note only)
**Why:** Modern runtimes handle any size. GPU texture compression (KTX2/Basis Universal) and mipmap generation work best at power-of-two sizes, and some pipelines resize for you, which costs a little quality.
**Fix:** Resize to 1024, 2048, and so on.

---

## Publish errors

| Message | Cause | Fix |
|---|---|---|
| `Online access is off` | Blender's global network switch is disabled | *Preferences → System → Network → Allow Online Access* |
| `-> 401: missing or invalid bearer token` | Token is empty, wrong, or expired | Paste a fresh token into *Preferences → Add-ons → Handoff* |
| `-> 413: file exceeds the … limit` | The GLB is too large for the ingest service | Reduce texture sizes first; they are usually most of the bytes |
| `failed after 5 attempts (503 …)` | The service was unavailable for the whole retry window | Try again later. The export is kept in the temp folder shown in the log |
| `ingest rejected the asset: requires extension KHR_draco_mesh_compression` | The export used Draco compression, which the receiver doesn't decode | Turn off Draco in the export settings. Compression happens server-side |
| `ingest rejected the asset: … exceeds the … limit` | Triangle count is over the service's hard limit | See [budget.triangles](#budgettriangles) |
| `checksum mismatch` | The bytes changed between hashing and upload (proxy, antivirus, flaky network) | Retry. If it repeats, check for a corporate proxy that rewrites uploads |
| `still 'processing' after 180s` | The server accepted the file but hasn't finished processing it | Not a failure on your side. Share the asset id with support |

When reporting a problem, include the log lines from the Publish panel and the asset id if there is one. The manifest sent with every upload already records your Blender version, exporter version, and preflight results.
