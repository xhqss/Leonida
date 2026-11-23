# BVH archives: measured composition

**Status: measured.** This is a read-only inventory of the `*_bvh4.rpf` packs that ship with the Xbox Series
package. No archive, payload, carved file or key is in the repository; the tables below are aggregate counts,
offsets and the few header bytes needed to describe the format. The packs were indexed with the fixed
`tools/rpf9_carve.py` from PR #90 (`--index-only`), whose decompressor advances by the bytes consumed instead of
the 1 MiB read size.

The groups measured were `core_bvh4.rpf` (8,297,921,520 bytes), `shared_minigame000_bvh4.rpf` (61,987,040 bytes)
and `story000_bvh4.rpf` (5,632 bytes).

## 1. Index result

`core_bvh4.rpf` was indexed in full, not bounded; the run took 843 s on the development machine.

| archive | file bytes | data-start (`u32@4`) | zlib streams | stored | inflated | zero padding | unread | readable | elapsed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `core_bvh4.rpf` | 8,297,921,520 | 65,024 | 565,596 | 7,685,366,227 | 129,929,580,001 | 577,512,352 | 34,977,917 | 92.6% | 843 s |
| `shared_minigame000_bvh4.rpf` | 61,987,040 | 1,264 | 2,560 | 59,142,787 | 970,044,012 | 2,672,073 | 170,916 | 95.4% | 3.3 s |
| `story000_bvh4.rpf` | 5,632 | 832 | 4 | 1,795 | 16,677 | 2,849 | 156 | 37.4% | 0.13 s |

"Readable" is stored bytes divided by the span from the data-start word to EOF. "Unread" is the non-zero gaps: the
encrypted table of contents and a small number of alignment bytes. The TOC is not decrypted here and no names were
recovered; the index is positional.

`core_bvh4.rpf` inflates to about 121 GiB, so the payload census below uses two levels of evidence:

* every one of the 565,596 chunks was classified from its first 0x140 inflated bytes. The carver had already
  proved that each stream inflates cleanly to its end, so the header read is sufficient;
* 270 BVH streams (522,595,968 bytes of 64-byte node area) were fully inflated for the fill census. All 2,560
  streams of `shared_minigame000_bvh4.rpf` (970 MB) were fully inflated.

## 2. Container layer (RPF9)

Verified:

* The file starts with the four bytes `39 46 50 52` (`9FPR`, the little-endian rendering of the RPF9 tag).
* `u32@4` is the offset the carver starts from; for `story000_bvh4.rpf` the first zlib stream starts exactly there.
  In the two larger packs a high-entropy table follows it before the first stream (`core_bvh4.rpf`: first stream
  at 69,648; `shared_minigame000_bvh4.rpf`: 16,400). The table is encrypted and stays opaque. No key or decryption
  work was done.
* The data area is zlib streams (`78 01/5e/9c/da`) separated by zero padding. There is no Bink 2 data in these
  three packs (`KB2*` count: 0).
* Large payloads are split into 8 MiB chunks. Ten files in `core_bvh4.rpf` start with a valid BVH header whose
  declared node range is larger than the 8 MiB chunk; the continuation chunk follows as the next stream and has no
  header of its own. A decoder must concatenate the chunks before parsing nodes. The index counts chunks, not
  files.

## 3. Payload families

| kind | streams (core) | share of streams | inflated bytes | share of inflated |
|---|---:|---:|---:|---:|
| `bvh4` complete | 271,627 | 48.02% | 129,769,988,608 | 99.877% |
| `bvh4-chunk` (first 8 MiB of a split file) | 10 | 0.002% | 83,886,080 | 0.065% |
| continuation chunk of a split file | 10 | 0.002% | 10,485,760 | 0.008% |
| `instance208` descriptor | 269,106 | 47.58% | 55,974,048 | 0.043% |
| other fixed-size descriptor | 24,832 | 4.39% | 8,770,816 | 0.007% |
| XML / content-manifest chunk | 11 | 0.002% | 474,689 | 0.000% |

`shared_minigame000_bvh4.rpf` has the same families: 1,237 `bvh4`, 1,233 `instance208`, 85 other descriptors and
5 XML chunks. `story000_bvh4.rpf` contains only 4 XML chunks (section 5).

Counting the 10 chunked first chunks and their 10 continuation chunks with the complete buffers, 99.95% of all
inflated bytes belong to the BVH family. The archive is not a mixed asset pack.

### 3.1 BVH4 stream header

**Verified** on 271,627 complete streams and 10 chunked streams (271,637/271,637):

* The 64-byte `XboxBvh4Header` begins at payload offset `0x100`. Its `flags` word is `0x81020006` in these
  packs.
* `u32@0x20` = `0x140 + 64 * totalSizeIn64BNodes` (the declared end of the 64-byte node address space).
* `u32@0x28` = `u32@0x20 - 0x100`. These two words sit before the header and are not header fields.
* `0x110` holds six floats for `sceneBounds` (`min xyz`, `max xyz`). In `core_bvh4.rpf` all 271,627 complete
  streams are ordered (`min <= max` on every axis); in `shared_minigame000_bvh4.rpf` 1,184 of 1,237 are ordered
  and 53 are not (sentinel or non-finite values).

The field names, types and offsets match the public `XboxBvh4Header` used by the Xbox DXR traversal shaders. The
source is [`XDXRStandaloneTraverseShortStackCPU.hlsli`](https://github.com/GaijinEntertainment/DagorEngine/blob/9639942de5a8e5b01d91977f60ed82cda22d5f41/prog/gameLibs/bvh/shaders/XDXRStandaloneTraverseShortStackCPU.hlsli)
at commit `9639942de5a8e5b01d91977f60ed82cda22d5f41`: `XboxBvh4Header` at L2531 and the flag masks at L2551.

| header offset | field | type | sample value |
|---|---|---|---:|
| `0x100` | `flags` | `u32` | `0x81020006` |
| `0x104` | `totalSizeIn64BNodes` | `u32` | 9,943 |
| `0x108` | `crc32` | `u32` | `0xeab7667e` |
| `0x10c` | `numElements` | `u32` | 5,447 |
| `0x110` | `sceneBounds.min` | 3 x `f32` | `-47.416, -25.742, -5.624` |
| `0x11c` | `sceneBounds.max` | 3 x `f32` | `47.416, 25.742, 5.624` |
| `0x128` | `offsetToNodes` | `u32` | `0x59480` |
| `0x12c` | `offsetToPrimRemap` | `i32` | 0 |
| `0x130` | `rootNodePtr` | `u32` | `0x136ac` |
| `0x134` | `offsetToElements` | `u32` | `0x4280` |
| `0x138` | `offsetToParents` | `u32` | `0xffffab40` |
| `0x13c` | `compressionParams` | `u32` | `0x808c8a89` |

**Probe provenance.** The sample column is the 64-byte header at payload offset `0x100` of the complete `bvh4`
stream at archive offset `0x4050` (16,464) in `shared_minigame000_bvh4.rpf`, inflated size 655,360 bytes. The
pack is read-only and is not in the repository; only these header fields and the four root-record words quoted
below are reproduced. `tools/bvh_index.py` reports the same stream as `node_count=9943` and
`tail_offset=636416`.

The offsets are relative to the header at `0x100`, not to the start of the payload. In the sample,
`offsetToNodes` therefore addresses `0x59580` and `offsetToElements` addresses `0x4380`. The node and element
regions are not necessarily the simple `0x140` onward array that the first indexer assumed. `offsetToPrimRemap`
is source type `int32_t`, so the indexer keeps it signed. `offsetToParents` is source type `uint32_t`, so the
indexer keeps its raw value; it is a signed offset from the start of the structure (RT-151: rehydrated console BLASes
have it negative), and only the entries from the header end to `offsetToElements` are stored.

The `flags` word has version `6` in its low 16 bits. It also sets `0x80000000` (bottom level), `0x00020000`
(offline) and `0x01000000` (dehydrated). It does not set `0x00010000` (compressed). The sample is therefore a
bottom-level, offline, dehydrated BVH, not a ready-to-traverse runtime tree.

The name "BVH4" is the builder's own: `XboxBvh4Header`, `ConstructBVH4`, `RefitBVH4`, `PT_ConstructBVH4` and
`totalSizeIn64BNodes` appear in `XboxBvhBuilder.dll`. The measured field at `0x104` is a count of 64-byte nodes
that does not include the 64-byte header: a structure is `64 * (totalSizeIn64BNodes + 1)` bytes, which lands exactly on
the end pointer in 271,627/271,627 complete streams (RT-151; the field was read as the whole size before). That agrees
with the builder's naming.

#### Node pointers and the runtime node records

A `rootNodePtr` or child pointer packs the node type in the low three bits and a 64-byte unit index in the high
29 bits:

```
type = ptr & 7
node_address = header + (ptr >> 3) * 64
```

The type values are `0..3` = `TRI0..TRI3`, `4` = `BOX16`, `5` = `BOX32`, `6` = `INSTANCE`, `7` =
`AABB_GEOMETRY`. `0xffffffff` is the invalid pointer; the indexer reports it as `root_node_invalid: true` with
the type name `invalid`, rather than naming it `AABB_GEOMETRY`. A `BOX32` node occupies 128 bytes, so it consumes
two 64-byte units; all other node records occupy 64 bytes.

The runtime records, which are what the traversal shaders read, are defined in the same file at L2285 (`Node16`),
L2302 (`Node32`) and L2342 (`Leaf`). The sample is dehydrated, so these source layouts are not instantiated by
it; the measured sample only confirms the node pointer and the header.

| record | size | layout |
|---|---:|---|
| `Node16` | 64 | four `u32` children at `+0`; four 12-byte half-float bounds at `+16` |
| `Node32` | 128 | four `u32` children at `+0`; four 24-byte float bounds at `+16`; four padding words at `+112` |
| `Leaf` | 64 | four `float3` vertices at `+0`; `GeometryIndexAndFlags` at `+48`; `BasePrimitiveIndex` at `+52`; `PrimitiveIndex2` at `+56`; `baryRemap` at `+60` |

The `Node16` bounds are six IEEE half values packed into three `u32` words. The shader expands them with
`XdxrDecompressBox16`; the first word holds `min.x`/`min.y`, the second `min.z`/`max.x`, and the third
`max.y`/`max.z`.

For the sample, `rootNodePtr = 0x136ac` decodes to index 9941, type `BOX16`, at payload address `0x9b640`.
That record begins `0x04000000, 0x13694, 0x1369c, 0x117b5`, so its first child is not a valid runtime
pointer. This is the dehydrated form, not a runtime `Node16`.

#### Packed dehydrated forms

The same file defines the packed records: `XboxBvh4NodePacked` at L2326, `XboxBvh4BVVDHeader` at L2334 and
`XboxBvh4LeafPacked` at L2376.

| record | size | layout |
|---|---:|---|
| `NodePacked` | 16 | four `u32` children |
| `LeafPacked` | 12 | `GeometryIndexAndFlags`, `BasePrimitiveIndex`, `PrimitiveIndex2AndBaryRemap` |
| `BVVDHeader` | 8 | `variantNodeIndex` plus a six-bit mask and two-bit child selector |

The layouts above are source definitions. The dehydrated stream is not a contiguous array of runtime `Node16`
records. Its element and box packing is measured in §4a: a CPU model that reads it reproduces the library's
rehydrated output byte for byte. The `BVVDHeader` table is not used by the samples there and stays **Open**. A
decoder must not feed the dehydrated root to the runtime traversal path; it has to rehydrate first.

### 3.2 Instance descriptor (208 bytes)

There are exactly 269,106 in `core_bvh4.rpf`, and every one is immediately followed by a `bvh4` stream. The layout
is fixed and was identical in all 269,106 records (and in all 1,233 records of the shared pack):

| offset | contents |
|---|---|
| `0x00` | zero `u64` |
| `0x08` | `0x50000080`, zero high word |
| `0x10` | `1` |
| `0x18` | `0x500000A0` |
| `0x20` | `0x60000000` |
| `0x28` | `0x500000B0` |
| `0x30` | `0x500000C0` |
| `0x38` | `0x50000050` |
| `0x40` | 64-bit object key (235,533 distinct values in core) |
| `0x50` | 3x4 float transform (last column zero; identity in 178,867 of 269,106): the geometry transform the BLAS was built with, §4a |
| `0x88` | `0x00000101` |
| `0xA0` | 64-byte-aligned offset within the following chunk, equal to the declared node end plus 0, 64, 128 or 192 |
| `0xA4` | two `u16` counts, high and low; multiples of 256 and 64 in the sample |

The `0x5000xxxx` words are constant across records and point at offsets inside the same 208-byte template
(`0x50`, `0x80`, `0xa0`, `0xb0`, `0xc0`). They behave like serialized GPU virtual addresses built on a fixed
`0x50000000` base, but that reading is **speculative**. They are not per-object pointers: the same constants occur
in every record.

The 24,832 "other descriptor" streams are the same family at other sizes (160, 192, 256, 272, 368, ... bytes):
first eight bytes zero, a `0x5000xxxx` word at `0x08`, and zero at `0x0C`. Some carry a full 4x4 matrix instead of
the 3x4 transform; the larger ones (e.g. 272 and 368 bytes) contain several 8-byte keys followed by a table of
`0x5000xxxx` self-pointers. 2,528 of them are immediately followed by a BVH buffer. Their exact grouping semantics
are **Open**.

### 3.3 Fill and node occupancy

The declared node area in `core_bvh4.rpf` is 1,896,784,254 nodes (121,394,192,256 bytes), 93.4% of all inflated
bytes. A 270-stream sample covering 8,165,562 whole 64-byte nodes found:

| byte value | share of node area | whole-node count |
|---|---:|---:|
| `0x00` | 90.15% | 6,887,776 |
| `0x81` | 0.70% | 53,799 |
| `0x94` | 0.20% | 3,355 |
| `0x29` | 0.08% | 31 |
| other | 9.85% | — |

So about 90% of the declared node area is zero, and a further ~1% is one of three repeated fill values; the
populated content is roughly 12 GB. The same pattern holds in the shared pack (90.4% zero of 944,604,352 node
bytes). The fill bytes are not a container magic; they look like unpopulated/reserved node slots.

### 3.4 What is absent

Scans of the inflated payloads found no `RSC7`, `RSC8`, `BVH4`, `BVH`, `0DRP`, `PSIN`, `RBF0`, `DXBC`, `DXIL`,
`PK\x03\x04` or PNG signature. The only ASCII format markers in the data area are `<?xml` (11 chunks in core, 5 in
shared, 4 in story) and the strings inside those manifests, including the substring `bvh` in the per-tile
`*_bvh.rpf` entries. No DXBC/DXIL shader container starts any chunk in the header census.

## 4. Format family

**Verified by bytes:** the archives are prebuilt BVH4-style acceleration data, not shader payloads and not a
generic geometry container. The dominant bytes are 64-byte node units under a header whose declared node range
matches the 64-byte-unit count; the DLL that builds the runtime structures exposes `XboxBvh4Header`,
`ConstructBVH4`, `RefitBVH4`, `GetBVHCompactedSize` and the D3D12XBOX `DEHYDRATE`/`Rehydrate...` entry points.

**Verified (§4a):** these are the offline-built, dehydrated inputs for `XboxBvhBuilder`'s rehydrate path. Fed with
their source geometry, `RehydrateRaytracingAccelerationStructureX` returns `S_OK` on them, and an independent CPU
model of the packing gives the same bytes. Whether the game's runtime calls that path or rebuilds from source
geometry is still **Open**; `docs/d3d12_vulkan.md` §4.8/§7.2 lists that call-path question.

**Not present as a whole:** source vertex/index meshes. The only geometry-like structure is the `u16` table at
header `0x130`, whose size is even and which is often monotonic; it is a candidate remap/primitive table. No
vertex-format header, index-format header or RSC resource container was found. The leaves carry primitive
references only; the triangles come from the drawables in `levels.rpf`, in the frame of the instance descriptor's
transform (§4a).

## 4a. Rehydration and source geometry (measured)

**Status: measured on 10 BLAS** of the first nested pack in `core_bvh4.rpf` (one interior cell), on Windows 11 with
no GPU involved. The tools that did it live in a fork of another project (`americas-recomp`, GPL-2.0-only,
`wildheathen/americas-recomp`, branch `feat/bvh-rehydrate-tools`); none of their code is in this repository. Only
layouts, offsets and counts are reproduced here.

**The library runs on a PC.** `RehydrateRaytracingAccelerationStructureX` in `XboxBvhBuilder.dll` reads the
source, vertex, index, destination and scratch buffers as CPU pointers (console unified memory) and makes no device
calls. Mapping memory at each buffer's GPU virtual address (`VirtualAlloc`) and copying the data there is enough for
it to return `S_OK`. It decodes standard DXGI vertex and index formats only, and it needs one geometry descriptor per
geometry index the leaves use; fewer makes it read past the array.

**The packing (completes §3.1).** A CPU model that reads the dehydrated stream as below writes the same bytes as the
library on the 8 samples whose geometry is identified (0 differing bytes; 384 to 2,512,512 bytes per structure, 2 to
21,814 leaves).

* *Elements* start at `offsetToElements`, in chunks of 2,048 leaves at a 128 KiB stride. A chunk is five arrays,
  one entry per leaf: geometry index (3 bytes), flags (1 byte), base primitive (3 bytes), second primitive (3 bytes,
  `0xffffff` for none) and a vertex-order byte. Rehydration writes one 64-byte `Leaf` per entry from the geometry:
  the triangle's vertices in the slots the vertex-order byte selects, the fourth vertex for a paired second
  primitive, `GeometryIndexAndFlags = index << 8 | flags`, and the two primitive indices.
* *Boxes* start at `offsetToNodes` and are stored by level, bottom-up. The structure ends with the level table: the
  last `u32` is the level count, preceded by one (BOX32 end, BOX16 end) pair per level. A level holds BOX32 groups
  (8 KiB per 64 nodes), then BOX16 groups (4 KiB per 64). A group starts with one word per node, `child count << 24 |
  24-bit override mask`, then every child pointer, then the override values (`f32` in BOX32, half in BOX16), one per
  mask bit. Bit `6 * slot + c` covers component `c` of a slot (`min xyz`, then `max xyz`). Rehydration recomputes each
  slot's box from the rehydrated child and then writes the stored values over the masked components. BOX16 rounds
  min down and max up; a bound past the half range becomes `+-inf` (`0x7c00`/`0xfc00`), not `+-65504`.
* *Leaf frame.* Leaf vertices are `(R * v - t) * s`, from `compressionParams`: bit 31 enables a per-axis scale
  `2^(e - 127)` with `e` the bytes 0, 1 and 2; bit 30 enables a translation per axis, the centre of `sceneBounds`
  where the bounds do not straddle zero; bits 24-29 select one of 64 rotations, `Rz * Ry * Rx` in steps of pi/8
  about each axis (index 0, the identity, on every sample).

**The instance transform.** The 3x4 at descriptor `+0x50` is applied to the geometry, `x' = M * x`, before the BLAS
is built. The samples carry the identity or an axis-aligned 90-degree rotation. Without it, most leaves of the
rotated samples fall outside `sceneBounds` (175 of 181 and 207 of 273); with it, none do. The transposed matrix does
not fit.

**Where the triangles come from.** A leaf's geometry index is a position in the geometry list of a drawable's
primary-LOD model in `levels.rpf`, counting only the geometries whose shader the BVH keeps. On the 8 BLAS whose
drawable is a single resource, a geometry is left out exactly when its shader is one of two shaders: 48 geometries,
8 distinct shaders, no exception. The two name hashes stay out of this repository (§8). They are the shader-group
hashes (first word of the shader record; shader group at drawable `+0x10`, pointer array at group `+0x10`, `u16`
count at `+0x18`, shader index per geometry in the model's `u16` array at `+0x20`) that only skipped geometries
carry, so any identified BLAS that skips a geometry lets a user recover them locally. The positions in the samples
are the RAGE9 quantized-8 form: a 32-byte origin/axis/scale header at the vertex-format position offset minus 32,
then one contiguous stream of 8-byte records; the other vertex attributes follow in their own streams, so the bytes
per vertex of the whole block are not a position stride. The leaves address triangles in primitive order of the
meshlet index buffer.

**How a match is verified.** A byte-identical library result checks the packing, not the geometry: the model and
the library agree on any input. These checks use what the dehydrated stream recorded from the true geometry:

1. every rehydrated leaf lies inside `sceneBounds` mapped through the leaf frame, and the union of the leaves equals
   it (error 0 to 2e-7 of the extent for the right geometry; 5.7e-4 to 0.5 for a wrong decode);
2. every stored box override lies inside the range of its rehydrated slot and is no looser than it, within 4 float32
   ulps (the bake rounds some overrides one or two ulps away from the rehydrated value);
3. the internal box area, as a tie-break between candidates (3.6-7.4 % of a wrong decode's area);
4. paired leaves: the builder pairs two primitives that share an edge, so in the true index buffer the two triangles
   share two vertex indices. The true geometry scores 1.00, others 0.3-0.6 once an index has 30 or more pairs. This
   test needs no positions.

With these, 8 of the 10 BLAS each have exactly one matching drawable among all candidates of their triangle-count
signature (up to 467), and the library output equals the model on all 8. The other two are interior rooms that
aggregate props from several drawables in room-local coordinates: test 4 finds three of their pieces exactly, but
placing them needs the interior's entity list (`CMloArchetypeDef`, see #1981), which the BLAS does not store.

**Open:** what the two excluded shaders are; the placement of aggregated rooms; whether the runtime rehydrates
these packs at all (above).

## 5. Cross-check of the small packs

Both small packs are ordinary RPF9 files with the same header, zlib streams and zero padding.

* `story000_bvh4.rpf` is **metadata-only**: 4 XML chunks (a `SSetupData`-shaped setup file, a content file and two
  changesets). It has no descriptors and no BVH streams. It registers per-tile `*_bvh.rpf` archives but contains
  none of their data.
* `shared_minigame000_bvh4.rpf` is **not empty**: 1,237 BVH streams, 1,233 instance records, 85 other descriptors
  and 5 XML chunks. Its data is the same two-family layout as core. It is sparse in the same way: 90.4% of the
  declared node area is zero.
* Both use the same `0x81020006` tag, the same `0x140` node start and the same `0x5000xxxx` descriptor template.

`core_bvh4.rpf` ends with 11 XML chunks: one setup file, one content file and three copies of a changeset split
into three 64 KiB/26 KiB chunks. These list the per-tile BVH packs; they are the only readable text in the pack.

## 6. What a decoder or builder path needs

1. **Names are not available.** The RPF9 TOC is encrypted. Index by offset, or build a name map from the XML
   changesets and the tile pack convention; do not attempt decryption.
2. **Concatenate split files.** A `bvh4-chunk` (10 in core) is the first 8 MiB of a larger payload; append the
   following continuation chunks until the declared `u32@0x20` end is reached before reading nodes.
3. **Pair descriptor to buffer.** The 208-byte descriptor precedes its BVH chunk in stream order in
   269,106/269,106 cases. The other descriptor sizes account for 2,528 more BVH buffers; 3 more are preceded by
   other streams.
4. **Parse the header, then stop guessing.** The exact header table, node pointer encoding and runtime
   `Node16`/`Node32`/`Leaf` records are in section 3.1, and the dehydrated element and box packing in §4a; do not
   infer a runtime child array from the `0x140` slot space.
5. **Skip fill.** Ignore all-zero nodes and the `0x81`/`0x94`/`0x29` repeated blocks; they are not primitives.
6. **Do not feed these nodes to the AMD traversal.** The translator's `image_bvh_intersect_ray` path expects the
   AMD hardware node format (256-byte base units), while these payloads use the builder's 64-byte units. The
   sample is also dehydrated, so its root is not a runtime child array. A rehydrator, converter or rebuild is
   required; see `docs/d3d12_vulkan.md` §4.8. §4a has the rehydration inputs: the drawable geometry, the instance
   transform and the packing.
7. **Result format.** Any decoder should emit world-space bounds plus primitive references, and validate the
   wrapper (`0x140 + 64*n == end`, `u32@0x28 == end - 0x100`) and ordered AABB before use.

## 7. Tooling

`tools/bvh_index.py` (standard library only) scans an RPF9 archive or a directory of `.rpf`/`.bin` payloads and
writes JSON and/or CSV with offset, stored size, inflated size, kind, signature, node count, tail offset, the
`XboxBvh4Header` fields, root pointer/index/type/invalid flag, declared bytes, bounds, descriptor key/transform
and fill counts. It reads only headers and emits no payload bytes. The row's `version` is the low 16 bits of
`flags` as an integer; `flags` keeps the full 32-bit word.

```powershell
python -I tools/bvh_index.py <archive.rpf> --out <temp>/index.json --csv <temp>/index.csv
python -I tools/bvh_index.py <carve-dir> --out <temp>/index.json
python -I tests/test_bvh_index.py
```

`--start` must be 0 or more, and `--limit` and `--max-inflate` at least 1. A value outside that, or a `--start` past
the end of the archive, stops the tool with a one-line error and exit code 2. An empty archive, or one shorter
than its 8-byte header, has no streams. In directory mode an archive that cannot be read is reported on stderr as
`skipped` and the other archives are still indexed.

The `bvh4` signature is reported only when `flags == 0x81020006`, the `0x140 + 64*n == end` formula and the
`u32@0x28 == end - 0x100` wrapper relation all hold; a larger payload whose declared end is past the chunk but
which is a 64 KiB multiple is reported as `bvh4-chunk`. The synthetic tests cover the tag, the formula rejection,
the exact header fields and root pointer/type, the chunk case, the 208-byte template, the other descriptor
template, XML and XML fragments, the two-stream advance bug and CSV/JSON output.

Measured with the tool on `shared_minigame000_bvh4.rpf`: 2,560 streams, 59,142,787 stored, 970,044,012 inflated,
1,237 complete `bvh4`, 1,233 `instance208`, 85 descriptors, 5 XML. That matches the carver summary exactly.

`tools/bvh_rehydrate.py` (standard library only) turns a dehydrated BLAS back into the runtime layout on the CPU,
from float3 vertices, `uint16`/`uint32` or no indices and an optional 3x4 geometry transform, following the packing
in §4a. On the 8 measured samples whose geometry is identified, its output is byte-identical to what
`XboxBvhBuilder.dll`'s `RehydrateRaytracingAccelerationStructureX` writes (384 to 2,512,512 bytes per structure). It
is the reference for checking the builder's `Box16Rehydration`, `Box32Rehydration` and `LeafRehydration` programs on
a GPU ([embedded_shaders.md](embedded_shaders.md)). Its tests are synthetic.

The output keeps the console's size convention: `64 * (totalSizeIn64BNodes + 1)` bytes from the header, which is the
`0x140 + 64 * n` end of §3.1 with the header at `0x100`. `tools/xdxr_bvh.py validate` and the GPU builder use
`64 * totalSizeIn64BNodes` for the whole structure instead, so `validate` reports a rehydrated console structure as
not covering its buffer.

```powershell
python -I tools/bvh_rehydrate.py <dehydrated.bin> --geom <vertices.f32>[,<indices.u32>] --out <rehydrated.bin>
python -I tools/bvh_rehydrate.py <bvh4 stream> --header-offset 256 --geom ... --transform m00,...,m23 --out ...
python -I tests/test_bvh_rehydrate.py
```

`tools/bvh_geometry_check.py` says whether the geometry given to a rehydration is the geometry the BLAS was built
from, using what the dehydrated stream stores (§4a): leaves inside and equal to `sceneBounds`, stored box overrides
consistent with the rehydrated slots, and paired leaves sharing an edge in the index buffer, which needs no positions.
On the 8 samples the right geometry passes every check and a wrong decode fails (175 of 181 leaves outside, or 11 of
23 overrides inconsistent).

```powershell
python -I tools/bvh_geometry_check.py <dehydrated.bin> --geom <vertices.f32>[,<indices.u32>] [--json report.json]
python -I tests/test_bvh_geometry_check.py
```

`tools/bvh_match.py` finds the drawable geometry a dehydrated BLAS was built from, on top of `rpf9dump`'s model
export rather than a second decoder. It reads the leaf signature (triangles per geometry index, a leaf that holds two
primitives counting as two), keeps every (drawable, model) of a `rpf9dump models` manifest whose per-geometry triangle
counts contain that signature as an ordered subsequence, and checks the survivors with
`tools/bvh_geometry_check.py` from the model's exported OBJ and the 3x4 at `+0x50` of the instance descriptor. The
geometries of the two shaders the BVH leaves out are filtered with `--skip-shaders`: those name hashes are game data,
as §4a says, so the rule is off unless the user passes them. A geometry whose shader index the manifest cannot resolve
keeps its place and the checks decide. Measured on the ten BLAS of the sample pack, against the first 1 GiB window of
`levels.rpf` cut at 3 GB (exported as a whole slice): 1,228 models, 1 to 374 candidates from the signature per BLAS,
and exactly one consistent candidate each. A window holds one region's drawables, so a BLAS whose drawable is
elsewhere in the archive finds no candidate in it at all, and the tool says so instead of reporting a rejection.

`rpf9dump models` and `rpf9dump textures` take `--start`/`--length` to export a window of a file. A window need not be a
valid archive - a slice of `levels.rpf` cut at an offset has no `9FPR` header - because both modes read every resource
from its own compression blocks; a sub-range is never written to the index cache, which describes a whole file.

```powershell
dotnet build tools/rpf9dump/rpf9dump.csproj -c Release
dotnet tools/rpf9dump/bin/Release/net8.0/rpf9dump.dll models <levels.rpf> <out> --start 3221225472 --length 1073741824
python -I tools/bvh_match.py <dehydrated.bin> <out>/levels/models/manifest.json --descriptor <instance208.bin> [--skip-shaders H,H]
python -I tests/test_bvh_match.py
```

The sample set §4a was measured on can be rebuilt from any copy of the package, and nothing of it enters this
repository:

1. `tools/bvh_index.py` on `core_bvh4.rpf` lists each `bvh4` stream and its descriptor row; the 3x4 at `+0x50` is
   the transform the matching needs, and a carved stream is passed to the next tools with `--header-offset 256`
   (or `0` when the structure was carved from its own header);
2. export a window of `levels.rpf` that holds that region's drawables (`rpf9dump models`, `--start`/`--length`);
3. `tools/bvh_match.py` identifies the drawable's geometries against it, and `--write <dir>` stores exactly those
   geometries as the float3 and uint32 buffers the next step takes, with the transform recorded in `export.json`;
4. `tools/bvh_rehydrate.py` on the same stream with those buffers and `--transform` gives the runtime layout,
   byte-identical to what `XboxBvhBuilder.dll` writes on these samples (§4a).

With the two excluded shader hashes the run answers with one consistent candidate per sample; without them every
candidate is checked and the checks decide. Measured on the window above: 1,228 models in the export, 1 to 374
candidates from the signature per sample, and a rehydrated structure of `64 * (totalSizeIn64BNodes + 1)` bytes from
the header. Its root parent-table entry is `0xFFFFFFFF` in all eight identified samples (the entry at
`offsetToParents` read signed, plus `4 * ((root - offsetToElements) / 64)`), which is what the GPU builder writes
for a root and what `tools/xdxr_bvh.py validate` requires since #2625.

## 7a. Building runtime BVHs: `tools/xdxr_bvh.py`

The Vulkan layer has to write acceleration structures in this runtime layout, because the game's traversal shaders
read the nodes themselves (`docs/d3d12_vulkan.md` §4.8). `tools/xdxr_bvh.py` (standard library only) is the CPU
reference for that builder. It encodes a BLAS from triangles or a TLAS from instances, runs one traversal step
with the semantics of the hardware instruction, walks a whole BVH to the closest hit, and checks a buffer's
structure. Its fixtures are synthetic.

What it writes, and where each part comes from (line numbers in the public `.hlsli` quoted in §3.1):

| Part | Layout | Source |
|---|---|---|
| Header | §3.1 table; `flags = 0x80000006` (BLAS) or `0x00000006` (TLAS); `crc32` and `compressionParams` 0 | `XboxBvh4Header` L2531, flag masks L2551, `XDXR_VERSION_2403 = 6` |
| BOX32 / BOX16 | four children, unused slots `0xffffffff` with an empty box (min > max); BOX16 rounds min down and max up to half | `Node32` L2302, `Node16` L2285, `XdxrDecompressBox16` |
| Leaf | vertex slots 0–3, then `GeometryIndexAndFlags` (index << 8 \| flags), `BasePrimitiveIndex` (TRI0), `PrimitiveIndex2` (TRI1), one remap byte per triangle node type | `Leaf` L2342, `XDXR_Runtime_Callback_GetPrimitiveIndex` |
| TRI0 / TRI1 | TRI0 reads slots (0, 1, 2), TRI1 reads (1, 3, 2); TRI2/TRI3 read slot 4, which overlaps the metadata, and are not emitted | `Read3Verts`, `XdxrGetVertexIndicesForNodeType` |
| Barycentric remap | byte `8 * type` of leaf `+60`: bits [1:0] pick which edge function is the first barycentric (0 U, 1 V, 2 W), [3:2] the second; U weighs the first vertex read | `TestXboxBvh4Leaf` |
| INSTANCE | 128 bytes: object-to-world 3×4, ID \| mask << 24, contribution \| flags << 24, BLAS root pointer with bit 31 = identity rotation, BLAS address >> 8, world-to-object 3×4, instance index, parent | `Instance` L2436, `XDXR_INSTANCE_INTERNAL_FLAGS_*` |
| Parent table | one dword per 64-byte unit from `offsetToElements` on; the entry for unit `u` is at dword `u - offsetToElements/64 + offsetToParents/4` | `XboxBvh4Node_GetParentTableOffset` |

**ASSUMPTION:** the region order (header, parent table, elements, nodes), the median-split build and the greedy
pairing of triangles that share an edge into TRI0/TRI1 are this tool's choices. The console builder's own choices
are not public. A shader reaches every region through the header, so these choices don't change what it reads.

The traversal step (`intersect_node`) follows `TestXboxBvh4Node` and `TestXboxBvh4Leaf`. A box child is kept when
its entry distance is at most the ray extent, and the children are sorted with the network (0,2) (1,3) (0,1) (2,3)
(1,2). A triangle returns `(t_num, t_denom, i_num, j_num)`, or `(inf, 1, 0, 0)` on a miss. A box miss stays a miss
for an unbounded ray: the public code's `<=` against an infinite extent would keep it.

**Translator.** `image_bvh_intersect_ray` in `tools/rdna2spirv.py` used to follow RADV's software path on triangle
nodes: slots (0, 1, 2) for every type and (V, W) as the barycentrics, because RADV builds only one triangle per
node. It now reads the slots of the node type and applies the remap byte. AMD GPURT
(`CalcTriangleCompressionVertexIndices`, `SwizzleBarycentrics`) and the XDXR path agree on both. TRI1 nodes
therefore traverse to the right triangle (docs/design.md, Ray tracing row; `tests/semantic/test_image_bvh_tri.py`).

**GPU builder.** `runtime/src/xdxr_build.comp` with `xdxr_build.h` writes the same layout on the GPU, straight into
the destination buffer:

1. one thread per triangle (TRI0 leaves, remap `0x09`) or per instance (128-byte records, the world-to-object
   inverse, the BLAS root and identity-rotation flag);
2. a 30-bit Morton key of each box centre, then a stable sort: up to 1,024 keys in one workgroup's shared memory
   (one dispatch), more with a radix sort of 8 passes of 4-bit digits over 256-element tiles (24 dispatches
   whatever the count, no padding to a power of two);
3. BOX32 nodes, built level by level from four consecutive sorted children, with the parent table and each
   instance's `ParentNode`;
4. the header.

The radix sort's scan writes the prefix sums of the digit counts to their own scratch array, not over the counts.
No invocation of the builder stores to a word and then uses a value it loaded from that word earlier. SPIRV-Cross
`6c09849f`, in MoltenVK 1.4.2, breaks that pattern for a pointer made by `OpBitcast` from a `uvec2`
(`GL_EXT_buffer_reference_uvec2`): it moves the load to where the value is used, after the store, and the load
returns the new value. MoltenVK `main` at `5189f8de` pins SPIRV-Cross `aa217aeb`, which has the same bug.
SPIRV-Cross [`38abe362`](https://github.com/KhronosGroup/SPIRV-Cross/commit/38abe362c57311a77808b7ec4d1d0d9ceb8d9370)
fixes it. With the old in-place scan, the radix path (more than 1,024 elements) lost and duplicated elements on
MoltenVK: `xdxr_build_check` found about 130 of 200 rays wrong in `blas_three_sort_tiles` and 210 of 300 in
`blas_refit_in_place`, which refits a radix-sorted tree. Measured on an Apple M3 Max, the separate arrays pass
10 runs out of 10 on Homebrew MoltenVK 1.4.2, on MoltenVK `5189f8de` built with SPIRV-Cross `57e4efcc`, on
KosmicKrisp and on llvmpipe. A pointer from `OpConvertUToPtr`, which the translator uses, kept its load before the
store in the same test.

The tree's shape depends only on the element count. `XdxrLayout` therefore computes the result and scratch sizes
exactly when the build is recorded, and the same numbers can answer `GetRaytracingAccelerationStructurePrebuildInfo`.
A triangle with a NaN vertex gets an empty box (D3D12 treats it as inactive). So do an instance with mask 0, one with
no BLAS and one with a singular transform. An instance record holds its BLAS address in 256-byte units in 32 bits,
so BLAS addresses must be below 2^40, as console GPU addresses are. Where device addresses are wider (llvmpipe's are
host pointers), the builder takes a bias that is added to the addresses read from instance descs.

The Vulkan layer uses it for `BuildRaytracingAccelerationStructure` unless `LEONIDA_RT_AS=khr`
(`docs/d3d12_vulkan.md` §4.8); `d3d12_xdxr_rt_test <dir>` builds a BLAS and a TLAS through the D3D12 API and writes
them the same way.

`vk_xdxr_build_test <dir>` builds nine synthetic cases on the GPU and writes each structure with a description of its
scene. `xdxr_bvh.py check-build <dir>` validates them and traces rays against brute force. `ctest` runs both as
`xdxr_build` and `xdxr_build_check`.

**Updates** (D3D12 `PERFORM_UPDATE`) refit: the source structure is copied to the destination unless it is the
destination, the elements are rewritten at their slots, and each node's boxes are recomputed bottom-up from the
children it already points to. The tree is kept, so a refit is valid for any motion and skips the Morton pass and
the sort. It takes 0.07 ms for 1,000 triangles and 9.8 ms for 1,000,000 on an RTX 2050, against 0.19 ms and 22 ms for
a build, but the tree traverses more slowly as the geometry moves away from where it was built.

**Copies and postbuild info.** `CopyRaytracingAccelerationStructure` with `CLONE` or `COMPACT` copies the
structure's bytes. The records are position-independent: node pointers count from the structure's start, and a TLAS
names its BLASes by address, which a clone keeps. The size comes from the source header (`totalSizeIn64BNodes`), so
a copy can follow a build in the same command list, and it is bounded by the destination resource. A build is
already exactly its layout's size, so `COMPACTED_SIZE` and `CURRENT_SIZE` (from
`EmitRaytracingAccelerationStructurePostbuildInfo` or the postbuild descs of a build) are both
`64 * (totalSizeIn64BNodes + 1)`, and compaction saves nothing. Serialization, deserialization and the tools-visualization
mode are refused at replay as unsupported, not faked. In `LEONIDA_RT_AS=khr` mode the postbuild descs stay ignored,
as before.

Not done yet: pairing triangles into TRI1 leaves, BOX16 nodes, serialization. A higher-quality build (SAH) would also
come later.

```powershell
python -I tools/xdxr_bvh.py selftest --seed 1 --rays 400   # build BLASes, compare closest hits with brute force
python -I tools/xdxr_bvh.py validate <dumped BVH buffer>    # structural check of a runtime buffer
python -I tests/test_xdxr_bvh.py
```

## 8. No-sharing rule

Nothing from the packs may be committed: no `.rpf`, carved stream, decompressed buffer, XML text, tile list,
name hash, key or TOC dump. This document and the tool carry only aggregate counts, offsets, field layouts and a
handful of header bytes. The tests build their inputs by hand. Any future decoder must keep its fixtures synthetic
and write real output only to the user's private work folder.
