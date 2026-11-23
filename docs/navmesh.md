# Navmesh archive (`navmeshes.rpf`)

**Status: decoded at container, header, schema and spatial-table level; stdlib decoder in `tools/navmesh_decode.py`.**
All 90,295 carved streams in `navmeshes.rpf` are classified with **0 unclassified streams** into four top-level
resource classes (`0DRP` cover-point resources, `0x00010015` navmesh tiles, Band-3 `grid` sparse-table resources and
`PSIN` metadata containers) plus their single continuation streams. Every number below is aggregate metadata from a
full read-only scan of one Xbox Series 1.0.2.1 package (`6,707,512,496` bytes). No payload, key, name or carved file
is in this document or the repository.

## Method

- The package archive is **6,707,512,496 bytes**; its RPF9 data area starts at `0x780`, leaving a span of
  `6,707,510,576` bytes.
- Walked sequentially with the fixed zlib carver rule (`ZHDR = \x78[\x01\x5e\x9c\xda]`, advance by bytes actually
  consumed). The encrypted RPF9 table of contents and encrypted sub-RPF headers were never touched.
- Stream totals match the carver threshold (`inflated >= 64` bytes) for exact parity with `tools/navmesh_index.py`.
  `tools/navmesh_decode.py` also accepts `< 64`-byte zlib streams strictly as continuations when a resource's block
  map declares a second stream, recovering **10** tiny continuations (`3` `0DRP`, `7` `0x00010015`) that the carver's
  64-byte cutoff skipped.
- Full-archive `--deep` verification (`tools/navmesh_decode.py navmeshes.rpf --deep`) ran in 236 s on Windows x64
  (Python 3.13, stdlib only) with **0 check failures** across all `64,729` `pgBase` resources (`9,603` `0DRP`,
  `27,644` `0x00010015` navmesh tiles, `27,482` Band-3 `grid` resources).

## Composition and 3-band layout

| Metric | Value |
|---|---:|
| File bytes | 6,707,512,496 |
| Scanned span (file minus `0x780`) | 6,707,510,576 |
| Carved zlib streams (`>= 64` B) | 90,295 |
| Stored (compressed) bytes | 6,540,238,392 (97.51% of span) |
| Inflated bytes | 20,551,068,624 (3.14x stored) |
| Zero padding | 161,191,191 (2.40%) |
| Unread (non-zero, non-stream) | 6,080,993 (0.09%) |
| Tiny continuations recovered (`16â€“48` B) | 10 (`3` `0DRP`, `7` `0x00010015`) |
| Bink 2 streams | 0 |

Every stream in the earlier 53,026 "other" bucket is now classified into continuations or the Band-3 `grid` class
(**0 unclassified streams**):

| Stream class | Role | Streams (`>= 64` B) | Stored bytes | Inflated bytes | Median inflated |
|---|---|---:|---:|---:|---:|
| `0DRP` | `0x7a` cover-point resource (`0DRP` at `+0x10`) | 9,603 | 745,601,504 | 2,764,382,624 | 196,608 |
| `0DRP` continuation *(was "other")* | 2nd stream of a `0DRP` (+3 tiny `< 64` B) | 2,478 | 19,473,516 | 37,778,384 | 10,944 |
| `0x00010015` | Navmesh tile (`u32 0x00010015` at `+0x14`) | 27,644 | 5,346,712,784 | 15,281,523,008 | 131,072 |
| `0x00010015` continuation *(was "other")* | 2nd stream of a navmesh tile (+7 tiny `< 64` B) | 23,039 | 51,000,438 | 139,234,640 | 1,840 |
| `grid` *(was "other")* | Band-3 sparse-grid SwissTable resource (`0x60` header) | 27,482 | 375,870,578 | 2,325,442,800 | 41,912 |
| `grid` continuation *(was "other")* | 2nd stream of a `grid` resource | 6 | 19,805 | 339,456 | 55,968 |
| `PSIN` | `PSIN` metadata container (`PSIN` at `+0x00`) | 22 | 628,344 | 1,413,376 | 65,536 |
| `PSIN` continuation *(was "other")* | 2nd stream of a `PSIN` container | 21 | 931,423 | 954,336 | 44,440 |
| **Total** | **0 unclassified streams** | **90,295** | **6,540,238,392** | **20,551,068,624** | |

The archive is strictly partitioned into three contiguous, non-overlapping offset bands separated by 29 encrypted
sub-RPF header/TOC gaps (`5,947,550` bytes total) plus `117,745` non-zero bytes at EOF:

| Band | Archive offset range | Contents |
|---|---|---|
| **Band 1** (`0DRP`) | `0x0000037010` â€“ `0x002ed51619` | All `9,603` `0DRP` cover-point resources (`7,122` 1-stream, `2,481` 2-stream) |
| **Band 2** (`0x00010015`) | `0x002ed87000` â€“ `0x0174b93f8f` | All `27,644` navmesh tiles (`4,598` 1-stream, `23,046` 2-stream) + `11` `PSIN` (`10` regional + `1` non-grid) |
| **Band 3** (`grid`) | `0x0174bbc000` â€“ `0x018fcab8bf` | All `27,482` `grid` resources (`27,476` 1-stream, `6` 2-stream) + `11` `PSIN` (`10` identical regional pairs + `1` EOF v79) |

Within Bands 2 and 3, static tiles are packed into 10 regional sub-RPF partitions (`2,448` unique tiles each, headed by
10 byte-for-byte identical `PSIN` version-256 pairs), plus an overlay partition of `3,002` variant tiles (`940` unique
sector IDs, subset of the base grid) and (in Band 2 only) a non-grid partition of `161` dynamic local-space navmeshes
plus `1` whole-world coarse navmesh.

## Shared `pgBase` prefix and block map (`+0x00..+0x0F`)

All three resource classes (`0DRP`, `0x00010015`, `grid`) share the same 16-byte prefix and `0x50000000` virtual
pointer base (`offset = u64_ptr - 0x50000000` when the upper 32 bits are zero and `0x50000000 <= ptr < 0x60000000`):

- `+0x00` (`u64`): always `0` (`64,729 / 64,729`).
- `+0x08` (`u64`): pointer to a block-map descriptor (`24` bytes on `0DRP`, `32` bytes on `0x00010015` and `grid`):
  - `+0x00` (`u64`): `0`
  - `+0x08` (`u64`): **exact logical zlib stream count (`1` or `2`)** â€” matches actual stream membership on
    `9,603 / 9,603` `0DRP`, `27,644 / 27,644` `0x00010015`, and `27,482 / 27,482` `grid` resources (`0` resources span
    more than 2 streams).
  - `+0x10` (`u64`): `0` (and `+0x18` `u64` is `0` on `100%` of `0x00010015` and `grid`; on `105` `0DRP` pages the next
    object begins at `+0x18`, confirming the `0DRP` block map is 24 bytes).

## `0DRP` directory pages (`9,603` pages â€” `0x7a` cover-point resources)

`0DRP` pages in `navmeshes.rpf` are **self-describing `0x7a` resources** (the same container format documented in
[world.md](world.md) and [page_directory.md](page_directory.md)). Reconciling the full `9,603`-page corpus settles the
fields previously marked "not decoded" (`+0x48` eight `u16` values, `+0x60` "key" and `+0x70` repeating records).

### Fixed header (`0x60` bytes, measured on all `9,603` pages)

| Offset | Size | Observed | Verified meaning |
|---:|---:|---|---|
| `+0x00` | 8 | zero (`9,603 / 9,603`) | `pgBase` vtable slot |
| `+0x08` | 8 | `0x50xxxxxx`, 16-byte aligned | pointer to 24-byte block map (`u64@+8 == stream_count` on `9,603 / 9,603`) |
| `+0x10` | 4 | `30 44 52 50` (`0DRP`) | `0x7a` container magic |
| `+0x14` | 4 | `122` (`0x7A`, `9,603 / 9,603`) | format version |
| `+0x18` | 8 | zero (`9,603 / 9,603`) | reserved |
| `+0x20` | 4 | `1â€“6` (`3`: 7,650, `4`: 932, `2`: 779, `6`: 219, `5`: 22, `1`: 1) | **1-based index into `q38` naming the root object block** (`1 <= u20 <= n_blocks` and `blocks[u20-1].hash` is in `q28` on `9,603 / 9,603`) |
| `+0x24` | 4 | zero (`9,603 / 9,603`) | high half of `+0x20` |
| `+0x28` | 8 | `0x50xxxxxx`, 8-byte aligned | pointer to `q28` struct schema table (`32` B/record) |
| `+0x30` | 8 | `0x50xxxxxx` (`9,600`) or `0` (`3`) | pointer to `q30` enum schema table (`24` B/record) |
| `+0x38` | 8 | `0x50xxxxxx`, 16-byte aligned | pointer to `q38` instance block table (`16` B/record) |
| `+0x40` | 8 | `0x50xxxxxx`, 8-byte aligned | pointer to `q40` auxiliary table (`{u32 count, count x {u32 tag, u32 size}, payloads}`) |
| `+0x48` | 2 | `2â€“12` | `n_structs`: number of `q28` struct definitions |
| `+0x4a` | 2 | `0â€“4` | `n_enums`: number of `q30` enum definitions |
| `+0x4c` | 2 | `2â€“6` | `n_blocks`: number of `q38` instance blocks |
| `+0x4e` | 2 | `0` (`2,578`) or `1..602` (`7,025`) | **`pages_4k`:** `0` or `ceil(data_end / 4096)` (`9,603 / 9,603`; see rule below) |
| `+0x50` | 16 | zero (`9,603 / 9,603`) | reserved (covers the trailing four `u16`s of `+0x48..+0x57` plus `+0x58..+0x5f`) |
| `+0x60` | â€” | first `q38` instance block (`9,603 / 9,603`) | **payload, not a header field** |

#### Decoding `u16@0x4e` (`pages_4k`)

Define the runtime data end before schema tables as
`data_end = max(max(block.target + block.size), q38 + 16 * n_blocks, p08 + 24)` and `pg = ceil(data_end / 4096)`.
Across all **9,603 / 9,603** pages (`0` exceptions), `u16@0x4e` is either `0` or `pg`:

- When `pg * 4096 < stream_end` (`stream_end = len(stream0)` if `data_end <= len(stream0)` else `len(joined)`):
  `u16@0x4e == pg` on **100%** of pages.
- When `pg * 4096 > stream_end` (Stream 1 holds `data_end` without reaching the next 4 KiB multiple) or on single-stream
  pages where `pg * 4096 == len(stream0)`: `u16@0x4e == 0` on **100%** of pages.
- On the **307** two-stream pages where `data_end <= len(stream0)` and `pg * 4096 == len(stream0)` (runtime data ends
  in the last 4 KiB of Stream 0 while `q28`/`q30`/`q40` spill into Stream 1): `93` have `u16@0x4e == pg` and `214` have
  `u16@0x4e == 0`. What breaks the tie on that exact 4 KiB boundary is **open** (likely the virtual page split in the
  encrypted TOC).

### Schema tables (`q28`, `q30`, `q38`, `q40`)

- **`q28` struct table (`32` B/record):** `{u32 name_hash, u32 signature, u64 flags, ptr64 members, u32 size, u16 0, u16 member_count}`. Each member is `16` bytes: `{u32 name_hash, u32 offset, u8 type, u8 subtype, u16 ref, u32 extra}`.
- **`q30` enum table (`24` B/record):** `{u32 name_hash, u32 signature, ptr64 values, u64 count}`. Each enum value is `16` bytes: `{i64 value, u32 name_hash, u32 0}`. Values are dense `0 .. count - 1` on `9,603 / 9,603` pages.
- **`q38` instance blocks (`16` B/record):** `{u32 struct_or_type_hash, u32 byte_size, ptr64 target}`. Disjoint with **0 overlaps** across all `9,603` pages (`54,898` total records, including `18` on the 3 tiny-continuation pages); every page has a block starting at `+0x60`.
- **`q40` auxiliary table:** `{u32 count, count x {u32 tag, u32 size}, payload bytes}`. Fits cleanly in-bounds on `9,603 / 9,603` pages. On `8,275` world-grid `CCoverPointRegion` pages it holds a single tag `0x101` (`32/48/64/80` B = `2..5` 16-byte entries); on all `137` non-grid `CCoverPointRegion` pages and all `1,187` `0x4712787b` pages `count == 0`; the 4 singleton pages carry larger `0x101`, `0x207` or `0x211` tables.

Only **10 distinct schemas** (6 root structs) occur across all `9,603` `0DRP` pages â€” **all cover-point data, not
navmesh polygons**. Struct/enum/member hashes match standard RAGE cover names under **case-sensitive joaat32**
(verified from hand-typed clean-room candidates in `tools/navmesh_decode.py`):

| Root struct (joaat32) | Pages | Schemas | Summary |
|---|---:|---:|---|
| `CCoverPointRegion` (`0x2283b2cb`, `304` B) | 8,412 | 2 (`11â€“12` structs) | `8,275` 128 m Ã— 128 m world-grid cover tiles + `137` smaller local/interior tiles; `15,474,729` `CCoverPointNode` (`0x0bcb00e4`, `160` B) instances (`+0xA0` `u16` array count `== block_count` on `8,412 / 8,412`; `+0x04` `i32` point count matches on `8,411 / 8,412`, with 1 local tile at `0x2e329010` having `23` vs `8` serialized nodes); world bounds (`+0x10`/`+0x20`) `X âˆˆ [-10,368, +10,411.9]`, `Y âˆˆ [-8,448, +9,472]`, `Z âˆˆ [-500, +1,350]` m; `256` sub-regions (`0xde5f1345`, `4,096` B) + `CCoverSpatialTreeNode` (`0xbb5a70d8`, `64` B) |
| `0x4712787b` (`48` B, name open) | 1,187 | 4 (`3â€“9` structs) | Local-space dynamic/entity cover sets (`155,250` `CCoverPointNode` instances on `1,173` pages, `0` nodes on `14` pages; node `+0x38` `f32[3]` positions in `[-63.1, +67.0] Ã— [-36.0, +35.4] Ã— [-72.1, +6.9]` m; `q40` always empty) |
| `CCoverPointRegionManifest` (`0xe842b0d6`, `48` B) | 1 | 1 (`4` structs) | Singleton at `0xca9d010`: `6,446` `CCoverPointRegionDef` (`0xbea9889e`, `64` B) entries |
| `0xb6dbe11b` (`16` B, name open) | 1 | 1 (`2` structs) | Singleton at `0x24c9a010`: `1,829` `0x0904902e` (`24` B) entries mapping hashes/strings to `RegionIndex` |
| `0x339accda` / `0x2bf76252` (`24` B, names open) | 2 | 2 (`2` structs each) | Two singleton map tables (`0x2e43e010`, `0x2ed47010`) |

Verified case-sensitive joaat32 matches (`22` total): `CCoverPointRegion` (`0x2283b2cb`), `CCoverPointNode`
(`0x0bcb00e4`), `CCoverNodeId` (`0xa33600ff`), `CCoverPointUsageInfo` (`0x7f072f43`), `CCoverSpatialTree`
(`0x9b39705a`), `CCoverSpatialTreeNode` (`0xbb5a70d8`), `CCoverPointRegionManifest` (`0xe842b0d6`),
`CCoverPointRegionDef` (`0xbea9889e`), `rage__spdAABB` (`0xf377e8c8`), `eCoverFlags` (`0x42338022`, 54 values),
`eCoverHeight` (`0x5c3b2e01`, 4 values), `eCoverUsage` (`0x7245ba24`, 6 values), `eCoverRegionFlags` (`0xb1cf92b0`, 3
values), `Uid` (`0x1863ce8e`), `SpatialTreeNodes` (`0xabe9944f`), `Entries` (`0xd566f590`), `RegionIndex`
(`0xd0a257d2`), `COVHEIGHT_PRONE` (`0x4d20005e`), `COVUSE_WALLTOLEFT` (`0xfdd96e08`), `COVUSE_WALLTORIGHT`
(`0xb00bfdf7`), `COVUSE_WALLTOBOTH` (`0xeae2591d`), `COVUSE_WALLTONEITHER` (`0x399bbf46`).

### Explaining the `+0x60` "key" and `+0x70` repeating records

The earlier report observed an opaque `u64` at `+0x60` (`9,409` nonzero, `194` zero), `u32_68` (`1,381` zeros), zero at
`+0x6c`, and repeating `key / u32 / 0xffffffff` patterns at `+0x70`. Every one of those counts comes directly from the
first `q38` payload block starting at `+0x60`:

| Block placed at `+0x60` | Pages | `+0x60..+0x8f` byte layout |
|---|---:|---|
| `CCoverPointNode[]` (`0x0bcb00e4`, `160` B/node) | **9,378** (`8,205` region + `1,173` dynamic) | Node 0 starts with **three 16-byte `CCoverNodeId` (`0xa33600ff`) structs** at `+0x60`, `+0x70`, `+0x80`, each `{u64 id_key, u32 field_08, u32 local_index}`. `+0x60..+0x67` is `Uid.id_key` (always nonzero); `+0x68..+0x6b` is `0` on all `1,173` local/dynamic `0x4712787b` pages; `+0x6c..+0x6f` is `Uid.local_index` (`0` on all `9,378` pages because it is node 0). **`+0x70..+0x7f`** is the second `CCoverNodeId` (repeating `Uid.id_key` on `1,186` pages and holding sentinel `0xffffffff` at `+0x7c` on `3,966` pages and at `+0x78` on `1,912` pages â€” the `+0x70` pattern). **`+0x80..+0x8f`** is the third `CCoverNodeId` (repeating `id_key` on `2,237` pages, `local_index == 1` on `9,105` pages, `0xffffffff` on `203` pages). |
| `0xde5f1345[]` (`256 Ã— 16` B sub-regions) | **207** (all `CCoverPointRegion`) | First `atArray<u16>` descriptor. On **`194`** pages the first sub-region is empty (`{0, 0, 0}` -> exact match for all **194** zero `+0x60` values and `194` of the `u32_68 == 0` pages); on `13` pages it is non-empty. |
| `0x00000100` (map-pair array) | **16** (`14` empty `0x4712787b` + `2` singletons) | Map entry array; contributes the remaining `14` `u32_68 == 0` pages (`1,173 + 194 + 14 = 1,381` exact). |
| `CCoverPointRegionDef[]` / `0x0904902e[]` | **2** (`2` singletons) | First manifest/index record (accounts for 2 of the 3 nonzero `+0x6c` words; `0x339accda` accounts for the 3rd). |

## Typed data pages (`27,644` pages â€” `0x00010015` navmesh tiles)

The 27,644 pages with `u32 0x00010015` at `+0x14` are the actual **navmesh geometry tiles** (RAGE `CNavMesh`-family
layout, fixed `0xF0`-byte header, `14.36 GiB` / `15,420,757,856` bytes inflated including continuations).

### Fixed header (`0x000..0x0EF`, 240 bytes, verified on all `27,644` pages)

| Offset | Size | Type | Verified meaning | Full-corpus check (`27,644` pages) |
|---:|---:|---|---|---|
| `+0x00` | 8 | `u64` | `pgBase` vtable slot | `0` on `27,644 / 27,644` |
| `+0x08` | 8 | `ptr64` | Pointer to 32-byte block map | `bm[1] == stream_count` (`4,598` Ã— 1, `23,046` Ã— 2) on `27,644 / 27,644` |
| `+0x10` | 4 | `u32` | Navmesh flags bitmask | `{9: 18791, 11: 5507, 3: 3149, 7: 161, 1: 35, 69: 1}` |
| `+0x14` | 4 | `u32` | Format word (`0x00010015`, low16 = `21`) | `0x00010015` on `27,644 / 27,644` |
| `+0x20` | 64 | `f32[16]` | Transform matrix (padded 3Ã—4) | Identity `(1,0,0,0 / 0,1,0,0 / 0,0,1,0 / 0,0,0,0)` on `27,644 / 27,644` |
| `+0x60` | 12 | `f32[3]` | 3D tile extents `(dx, dy, dz)` | Matches `quadtree_root.vMax - vMin` (`< 0.02` m) on `27,644 / 27,644`; `(128, 128)` on all `27,482` static tiles |
| `+0x70` | 8 | `ptr64` | `aiSplitArray` of compressed vertices (`6` B: `u16[3]`) | Valid & total `== u32@0xC0` (`138,041,746` verts, max `52,608`) on `27,644 / 27,644` |
| `+0x78` | 8 | `ptr64` | Uncompressed vertex array (unused; bit 0 always set) | `0` on `27,644 / 27,644` |
| `+0x80` | 8 | `ptr64` | `aiSplitArray` of vertex indices (`2` B: `u16`) | Valid, total `== u32@0x90`, all `< u32@0xC0` on `27,644 / 27,644` |
| `+0x88` | 8 | `ptr64` | `aiSplitArray` of adjacent-poly edges (`12` B) | Valid & total `== u32@0x90` on `27,644 / 27,644` |
| `+0x90` | 4 | `u32` | Index / adjacency pool size | Sum `493,024,188`, max `235,411`; equals `sum(poly.num_verts)` on `27,644 / 27,644` |
| `+0x98` | 8 | `ptr64` | `aiSplitArray` of polygons (`44` B) | Valid, total `== u32@0xC4` (`124,768,148` polys, max `71,803`), contiguous chain, `poly+0 == u32@0xC8` and `poly+4 == (index start << 4) \| count` on `27,644 / 27,644` |
| `+0xA0` | 8 | `ptr64` | Spatial quadtree root (`96` B nodes, `32` B leaves) | Full tree (`2,337,904` nodes, `1,760,339` leaves, max depth `9`) valid & all leaf indices `< u32@0xC4` on `27,644 / 27,644` |
| `+0xA8` | 8 | `ptr64` | Nullable special-link array (`32` B, count `u32@0xD4`) | Non-null iff `flags & 2` (`8,817` pages); in-bounds `27,644 / 27,644` |
| `+0xB0` | 8 | `ptr64` | Nullable special-link index table (`u16`, count `u32@0xD8`) | Non-null iff `flags & 2`; in-bounds & all entries `< u32@0xD4` on `27,644 / 27,644` (`2` tiles have `d8 < d4`) |
| `+0xB8` | 8 | `ptr64` | Nullable 96-byte obstacle footprint array (count `u32@0xDC`) | Present on `9,425` pages (`6,246,890` records = `3,123,445` outer/inner contour pairs); in-bounds & valid `27,644 / 27,644` |
| `+0xC0` | 4 | `u32` | Vertex count | Min `4`, max `52,608`, sum `138,041,746` |
| `+0xC4` | 4 | `u32` | Polygon count | Min `1`, max `71,803`, sum `124,768,148` |
| `+0xC8` | 4 | `u32` | 512 Ã— 512 sector ID (`0` if `flags & 4`) | `0` iff `flags & 4` (`162` pages); exact `136 Ã— 180 = 24,480` static grid |
| `+0xCC` | 4 | `u32` | Core mesh data byte size | `6*c0 + 14*pool + 44*c4 + 32*d4 == u32@0xCC` with **0 residual** on `27,644 / 27,644` |
| `+0xD4` / `+0xD8` / `+0xDC` | 12 | `u32[3]` | Special-link, link-index, and `+0xB8` record counts | Sums `26,679,246` / `26,903,247` / `6,246,890` |
| `+0xE8` | 4 | `u8[4]` | Build stamp (`0` if `flags & 4`): second, minute, hour, day of month | `7` distinct values (`0` on `162`, `0x09090516` on `24,474`, `0x160e3132` on `3,003`, 4 rare on `5`); see [Build stamp](#build-stamp-0xe8) |

Reserved slots `+0x18..+0x1f`, `+0x6c..+0x6f`, `+0x78..+0x7f`, `+0x94..+0x97`, `+0xd0..+0xd3`, `+0xe0..+0xe7`, and
`+0xec..+0xef` are zero on `27,644 / 27,644` pages.

### Build stamp (`+0xE8`)

Read as four bytes from the lowest address, every nonzero value is a second, a minute, an hour and a day of month,
the first four fields of C's `struct tm` in their order. No month or year is stored. The numbers are for the
1.0.2.1 package (ASSET-282):

| `u32@0xE8` | Bytes | Reads as | Tiles | Grid positions |
|---|---|---|---:|---|
| `0x09090516` | 22, 5, 9, 9 | day 9, 09:05:22 | 24,474 | 24,474 of 24,480: 23,536 alone, 938 shared |
| `0x160e3132` | 50, 49, 14, 22 | day 22, 14:49:50 | 3,003 | 940, never alone |
| `0x0f0e1b37` | 55, 27, 14, 15 | day 15, 14:27:55 | 2 | 2 adjacent, alone |
| `0x1d07151c` | 28, 21, 7, 29 | day 29, 07:21:28 | 1 | alone |
| `0x18101739` | 57, 23, 16, 24 | day 24, 16:23:57 | 1 | shared with 2 day-22 tiles |
| `0x0b0a2f1a` | 26, 47, 10, 11 | day 11, 10:47:26 | 1 | alone |
| `0` | | not a grid tile | 162 | the `flags & 4` meshes |

- **Only the date and time reading fits all six nonzero values.** In each one, the low two bytes are below 60, the
  third is below 24 and the top byte is 1 to 31. Six independent random words land in those ranges with a
  probability of about 6 × 10⁻²⁰. The low two bytes could in principle be the other way round (minute first). The
  `struct tm` order is the natural reading, but the data can't tell them apart.
- **The day-22 stamp marks tiles added on top of the base grid.** The 27,482 static tiles sit on 24,480 grid
  positions (the `u32@0xC8` sector, above). The 3,002 extra tiles are all on 940 positions that hold 2 to 40 tiles,
  and every one of those positions has day-22 tiles. 2,984 of the 3,003 day-22 tiles share their position with a
  day-9 tile, and on all 938 positions with both, the day-22 tiles come later in the archive. The other 19 are on two
  positions without a day-9 tile: 17 at sector (255, 248), and 2 with the day-24 tile at (248, 246). On 450 of the 938, the first day-22 tile has the same vertex and polygon
  counts as the day-9 tile. What the stacked tiles are for is not measured.
- **Neighbouring tiles share a stamp.** Of the 48,644 pairs of grid neighbours (one tile per position), 98.12% have
  the same stamp, against 92.58% for stamps placed at random with the same frequencies.

Method: `tools/navmesh_decode.py`'s own scan of `navmeshes.rpf`, with each tile's `u32@0xE8`, `u32@0xC8`, flags,
counts and archive offset recorded per tile. No geometry was kept.

The `+4` word of a polygon packs `(index start << 4) | count`; the runs tile the index pool in order
(`tools/navmesh_decode.py --deep` checks it on all `124,768,148` polygons, 0 failures), which caps a polygon at 15
vertices. 6stars independently reads the same packing. Its polygon `+0x28` bit 17 and `+0x18` bit 27
road/pavement labels were not adopted: a 1,027,984-polygon sample over 238 tiles sets both bits on 258 polygons, so
they are not a partition and neither meaning is confirmed. No 6stars code was copied; the repository has no
licence.

### Flags (`u32@0x10`) and 512 Ã— 512 world grid

- **Bit 0 (`0x01`)**: set on `27,644 / 27,644` (compressed 6-byte vertices at `+0x70`, `+0x78 == 0`).
- **Bit 1 (`0x02`)**: exact bijection (`27,644 / 27,644`) with special links (`+0xA8/+0xB0` non-null, `u32@0xD4/0xD8 > 0`; `8,817` tiles).
- **Bit 2 (`0x04`)**: exact bijection (`27,644 / 27,644`) with non-grid meshes (`u32@0xC8 == 0`, `u32@0xE8 == 0`: `161` local-space dynamic meshes with `flags = 7` + `1` whole-world coarse mesh with `flags = 69`).
- **Bit 3 (`0x08`)**: set on `24,298` tiles (`9` or `11`), clear on `3,346`. `0 / 3,346` bit-3-clear tiles contain any polygon with `poly+0x18 & 0x80000` (water candidate), whereas `24,009 / 24,298` (`98.81%`) bit-3-set tiles do; the remaining `289` bit-3-set tiles have an unresolved secondary trigger.
- **Bit 6 (`0x40`)**: set on `1 / 27,644` (`flags = 69`, the single global coarse mesh).

For all `27,482` static tiles (`(flags & 4) == 0`), `u32@0xC8` encodes the tile origin bit-for-bit against the quadtree
root AABB (`27,482 / 27,482`):

```text
sector_x    = u32@0xC8 & 0x1FF          # 160 .. 295 (136 columns)
sector_y    = u32@0xC8 >> 9             # 179 .. 358 (180 rows)
world_min_x = (sector_x - 256) * 128.0  # -12,288 .. +4,992 m (max +5,120 m)
world_min_y = (sector_y - 256) * 128.0  #  -9,856 .. +13,056 m (max +13,184 m)
```

The `24,480` unique static sector IDs form a complete, gap-free `136 Ã— 180` rectangle (`Z âˆˆ [-78.55, +403.84]` m).

### Adjacent-polygon edges (`+0x88`, `493,024,188` records) and special links (`+0xA8`/`+0xB0`, `26,679,246` records)

- **12-byte adjacent-polygon edges (`+0x88`, count `u32@0x90`, `493,024,188` records across `27,644` tiles):**
  Each record is `{u32 adj_sector, u16 adj_poly, u16 edge_meta, u32 edge_flags}`. Across all `493,024,188 / 493,024,188`
  records (`0` exceptions):
  - **`458,728,869`** (`93.04%`) are intra-tile neighbors with `adj_sector == u32@0xC8` and `adj_poly < u32@0xC4`
    (`0` out-of-bounds polygon indices);
  - **`34,295,319`** (`6.96%`) are open or tile-border edges with `adj_sector == 0xFFFFFFFF` and `adj_poly == 0xFFFF`
    (cross-tile border stitching is performed at runtime rather than serialized in `+0x88`).
- **Polygon `+0x20` special-link prefix-sum chain (`124,768,148` polygons across `27,644` tiles):**
  In every 44-byte polygon at `+0x98`, `u32@0x20` packs `link_count = w20 & 0x3F` (bits `0..5`, range `0..63`),
  `link_start = (w20 >> 6) & 0x1FFFF` (bits `6..22`, up to `17,459`), and a 9-bit polygon flag field `w20 >> 23`
  (bits `23..31`). Across all `27,644 / 27,644` tiles (including polygons with `link_count == 0` and linkless tiles
  with `u32@0xD8 == 0`), `link_start` equals the exact running prefix sum of `link_count` across preceding polygons,
  and the tile total after polygon `u32@0xC4 - 1` equals `u32@0xD8` (`0` failures).
- **32-byte special links (`+0xA8`, count `u32@0xD4`, `26,679,246` records across `8,817` tiles) and `+0xB0` index table
  (`u16[u32@0xD8]`, `26,903,247` entries):**
  Each 32-byte record at `+0xA8` is `{u16[3] pos0, u16[3] pos1, u16 poly0, u16 poly1, u32 sector0, u32 sector1,
  u16 flags0, u16 flags1, u16 heading, u16 pad}`:
  - `+0x00..+0x05` (`u16[3]`) and `+0x06..+0x0B` (`u16[3]`): quantized 3D link endpoints `pos0` and `pos1`.
  - `+0x0C..+0x0F` (`u16 poly0, u16 poly1`) and `+0x10..+0x17` (`u32 sector0, u32 sector1`): endpoint polygon indices
    and sector IDs. Across all `26,679,246 / 26,679,246` links, at least one endpoint sector equals the owning tile's
    `u32@0xC8` (`26,398,281` intra-tile with `sector0 == sector1 == u32@0xC8`; `143,913` cross-tile with
    `sector0 == u32@0xC8`; `137,052` cross-tile with `sector1 == u32@0xC8`; `0` with neither). Whenever
    `sector0 == u32@0xC8`, `poly0 < u32@0xC4` (`26,542,194 / 26,542,194`); whenever `sector1 == u32@0xC8`,
    `poly1 < u32@0xC4` (`26,535,333 / 26,535,333`).
  - `+0x18..+0x1B` (`u16 flags0, u16 flags1`): link attribute and traversal type words (`flags1` top values: `0x0050`
    on `11,492,614`, `0x0040` on `9,705,698`, `0x0030` on `4,281,168`, `0x0060` on `425,369`).
  - `+0x1C..+0x1D` (`u16 heading`): 8-bit quantized heading angle (`0..254`, `< 256` on `26,679,246 / 26,679,246`).
  - `+0x1E..+0x1F` (`u16 pad`): zero padding (`0` on `26,679,246 / 26,679,246`).
  - Across all `26,903,247 / 26,903,247` entries of `+0xB0`, every entry `li = u16@(b0 + 2*k)` in polygon `pi`'s slice
    `[link_start .. link_start + link_count)` indexes a 32-byte link at `+0xA8` satisfying
    `(poly0 == pi or poly1 == pi) and (sector0 == u32@0xC8 or sector1 == u32@0xC8)` (`0` failures).

### Split arrays and quadtree layout

- Each `aiSplitArray` descriptor (`48` B: `{u64 vtable, u64 total, ptr64 blocks, ptr64 starts, u64 nblocks, u64 0}`)
  chunks elements into `floor(16384 / stride)` per non-final block (`6` B vertices: max `2,730`; `2` B vertex indices:
  max `8,192`; `12` B adjacent edges: max `1,365`; `44` B polygons: max `372`). The header checksum
  `6*c0 + 14*pool + 44*c4 + 32*d4 == u32@0xCC` independently proves all five core record strides on `27,644 / 27,644`.
- Each `96`-byte quadtree node stores min bounds (`+0x00` `f32[3]`), max bounds (`+0x10` `f32[3]`), `i16[6]` bounds (`+0x20`), a 4-byte-aligned
  nullable leaf pointer (`+0x2C`), four 4-byte-aligned child pointers (`+0x34..+0x53`), and 12 trailing zero
  bytes (`+0x54..+0x5F`). Each `32`-byte leaf holds `{u64 0, ptr64 poly_indices, u64 0, u64 num_polys}`.

### Paired 96-byte obstacle footprint records (`+0xB8`, `6,246,890` records)

On all `9,425` tiles where `u32@0xDC > 0`, `u32@0xDC` is even (`9,425 / 9,425`) and the array at `+0xB8` holds
`3,123,445` consecutive `(outer, inner)` pairs (`6,246,890` 96-byte records). Every pair `(rec[2*p], rec[2*p + 1])`
shares the identical 20-byte prefix `+0x00..+0x13` (`3,123,445 / 3,123,445`):

| Offset | Size | Type | Verified meaning (`6,246,890 / 6,246,890` records) |
|---:|---:|---|---|
| `+0x00` | 8 | `u64` | 64-bit obstacle/object UID (identical across `rec[2*p]` and `rec[2*p + 1]` on `3,123,445 / 3,123,445` pairs; repeated across adjacent tiles when a footprint crosses a 128 m tile border) |
| `+0x08` | 12 | `f32[3]` | Finite 3D world-space anchor `(x, y, z)` (`6,246,890 / 6,246,890`, identical across each pair) |
| `+0x14` | 1 | `u8` | Contour role/flags: `0x00` (`2,750,542`) or `0x04` (`372,903`) on `rec[2*p]` (expanded outer contour), and `0x08` (`3,123,445`) on `rec[2*p + 1]` (inner core contour) on `3,123,445 / 3,123,445` pairs |
| `+0x15` | 1 | `u8` | 2D polygon vertex count `N` (`4` on `3,123,878` records, `8` on `3,123,012` records). `(rec[2*p].N, rec[2*p + 1].N)` is `(8, 4)` on `3,123,012` pairs (chamfered octagon around an inner quad, typically expanded by `128` fixed-point units) and `(4, 4)` on `433` pairs |
| `+0x16` | 64 | `i16[32]` | Up to 16 relative 2D `(dx, dy)` footprint vertices in signed 16-bit fixed point around `(x, y)`; trailing unused slots `+0x16 + 4*N .. +0x55` are zero on `6,246,890 / 6,246,890` |
| `+0x56` / `+0x58` | 4 | `i16`, `u16` | Vertical/extent words (`u16@0x56` range `0..65,535`, high bit set on `1,846,885`; `u16@0x58` range `3..65,534`, high bit set on `13,629`) |
| `+0x5A` | 6 | `u8[6]` | Trailing zero padding (`0` on `6,246,890 / 6,246,890`) |

## Band-3 `grid` resources (`27,482` resources)

The final 433 MiB of `navmeshes.rpf` holds `27,482` `pgBase` resources (`+6` continuations) paired **1-to-1** with all
`27,482` static navmesh tiles (`24,480` unique sectors, **0 symmetric difference** against Band 2).

| Offset | Size | Type | Verified meaning (`27,482 / 27,482`) |
|---:|---:|---|---|
| `+0x00` / `+0x08` | 16 | `u64`, `ptr64` | `0`, pointer to 32-byte block map (`bm[1] == stream_count`: `27,476` Ã— 1, `6` Ã— 2) |
| `+0x10` / `+0x14` | 8 | `u32`, `u32` | Power-of-2 group count (`8..2048`), active entry count `N` (`85..14,903`; sum `53,835,068`) |
| `+0x18` / `+0x20` | 16 | `ptr64`, `ptr64` | Hash-table pointer (always `+0x60`, `groups Ã— 272` B), payload pointer (`u32[N]`) |
| `+0x28` / `+0x34` | 16 | `u32[3]`, `u32` | Cubic volume extent (`128Â³ / 256Â³ / 512Â³ / 1024Â³ / 4096Â³` m), 512 Ã— 512 sector ID |
| `+0x40` | 12 | `f32[3]` | World origin `(ox, oy, oz)` (`ox, oy` match sector ID on `27,482 / 27,482`; `oz âˆˆ {-2048..+256}`) |
| `+0x50` | 4 | `u32` | Mirror of entry count (`u32@0x50 == u32@0x14` on `27,482 / 27,482`) |

Reserved slots `+0x38..+0x3f`, `+0x4c..+0x4f`, and `+0x54..+0x5f` are zero on `27,482 / 27,482`. Each 272-byte group at
`+0x60` is a 16-way SwissTable block (`16` control bytes where `0x80` = empty and `< 0x80` = occupied tag, followed by
`16 Ã— {u64 key, u64 value}`). Across all `27,482 / 27,482` resources, occupied slots equal `N` and their `value` fields
form an exact bijection onto `[0 .. N - 1]`: `value` indexes the `u32[N]` payload.

**Keys (ASSET-287).** The `u64` keys are sentinel-bit linear octree keys over the cubic volume: `1` is the root and a
child is `(parent << 3) | octant`, so the highest set bit sits at bit `3 × depth`. Measured on `27,482 / 27,482`
resources: the root is present, no key appears in two slots, every key's highest set bit is a multiple of 3, and every non-root key's parent (`key >>
3`) is in the same table. Octant bit 2 is the vertical axis (cells stack on it); which of bits 0/1 is x and where the
cube sits against the sector are **open**. Depths run `0..10` (`53,835,068` keys). The cell size at a resource's deepest
level (`extent >> depth`) is 16 m on `12,694` resources (`12,693` 128 m tiles at depth 3, one 256 m tile), 8 m on `859`
and 4 m on `13,929`.

**`u32[N]` words.** `51` distinct values over the whole package. Every inner node (a key with at least one child:
`8,617,353`) holds `0x0000ff00`. Leaves (`45,217,715`) hold `0x0000ff00` (`1,791,096`, never at a resource's deepest
level), a word with low byte `0x01` (`26,659,574`; bits `8-11` take `0, 5, 7, 9, B, D, F`; bits `17-18` are a 2-bit
field), or a word with low byte `0x03` (`16,767,045`; bit 8 is a flag, bits `11-14` a 4-bit value `0..15`). What the two
leaf kinds and their fields mean, and the engine subsystem name, are **open**. `navmesh_decode.py --deep` checks the key
and inner-word rules (`keys_octree`, `inner_payload_ff00`) and reports `key_depth`, `inner_nodes` and `leaf_payload`
counts.

## Remaining unknowns

1. **RPF9 TOC and sub-RPF headers.** The outer TOC and the 29 internal sub-RPF boundary gaps are encrypted and were not
   touched; asset paths and official resource type IDs require the TOC or executable.
2. **`0DRP` `u16@0x4e` boundary tie-breaker.** On the `307` two-stream pages where `data_end <= len(stream0)` and
   `ceil(data_end / 4096) * 4096 == len(stream0)`, `93` store `pg` and `214` store `0`; what selects between them is
   open.
3. **`0DRP` unnamed structs and fields.** Several `0x7a` struct/member hashes (including `0x4712787b`, `0x57ad0b12`,
   `0xdd53676a`, `0xf2dd6348`, `0xde5f1345`, and 3 of the 4 singletons) are not yet matched to engine names.
4. **Per-record bitfields inside `0x00010015` tiles.** `u32@0x10` bit 3's secondary trigger on `289` tiles and bit 6's
   name, `u32@0xE8` build-stamp encoding, polygon/adjacent-edge/special-link flag bitfields, and the exact C++ class name
   and `+0x56/+0x58` vertical unit of the paired 96-byte `+0xB8` footprint records remain open.
5. **Band-3 `grid` subsystem and EOF `PSIN` v79.** The Band-3 subsystem identity, the x/y order of the octree key's
   octant bits 0/1, the cube's placement against its sector and the meaning of the `u32[N]` leaf fields are open
   (the key encoding and the inner-node word are decoded above), as is why the final `PSIN` (version `79`)
   terminates before `LSNS` ends.

## Vehicle navmesh settings (`CVehiclePoints`, `common.rpf`)

Navmeshes on vehicles that peds walk around on are configured outside this archive, by 32 `CVehiclePoints` XML
documents in `common.rpf` (see [gamedata.md](gamedata.md)). One document per vehicle; counts only, no names.

| Field | Documents | Values |
|---|---:|---|
| `DefinitionName`, `ModelName` | 32 | equal in all 32; 31 distinct (one model has two different documents) |
| `Flags`, `iNumBoardingPoints` | 32 | one value each across all 32; `iNumBoardingPoints` is 0 |
| `Points`, `Props`, `AuthoredMeshName` | 32 | empty in all 32 |
| `fNavOverrideRecastCellSize` | 31 | 4 distinct values: 0, 0.03125, 0.0325, 0.0625 |
| `fNavOverrideRecastCellHeight` | 26 | 2 distinct values, 0 or 0.04 |
| `fNavOverrideAgentRadius` | 30 | 4 distinct values, 0 to 0.15 |
| `fNavOverrideAgentHeight` | 30 | 0 or 1.2 |
| `fNavMaxHeightChange`, `fNavMinZDist` | 30 | constant (0.35 and 0.5) |
| `fMinPatchAreaBeforeCulling` | 31 | 6 distinct values, 0.1 to 4 |
| `ModDefs/Item` | 6 documents, 49 items | `ChassisModName`, `UtilityModName`, `PropsetExteriorModName`, `InteriorModName`, `CargoModName` (always empty), `RoofModName` (on 17) |

The field names are Recast's (cell size and height, agent radius and height, a minimum patch area), so a vehicle
navmesh is built with Recast-style parameters per model, where 0 reads as "no override". The cell sizes are
fine (at most 0.0625 world units) compared with a world navmesh, as a deck or wagon bed would need.

Cross-checks against the local archive catalog: 25 of the 31 model names are `.yft` base names (6 are not in
the catalog), and all 25 are also among the 319 models of the vehicle distant-light recipes; 4 are among the 252
rated vehicles. Of the mod part names, only the 5 `RoofModName` values that are set resolve to a `.yft`; the
49 chassis, 32 utility, 35 exterior-propset and 4 interior names match no archive entry, so they are probably
fragment-part or kit names rather than models.

Not measured here (hypothesis): whether the vehicle navmeshes are generated at runtime from these parameters or
stored elsewhere, and which vehicles the 31 are (wagons, boats and trains would fit the field set).

## Tools

- `tools/navmesh_index.py` (stdlib-only) performs the quick `0DRP` header inventory (`offset`, sizes, version, `+0x20`,
  raw `+0x60`/`+0x68`).
- `tools/navmesh_decode.py` (stdlib-only) decodes all four resource classes (`0DRP`, `0x00010015`, `grid`, `PSIN`),
  joins continuations (including tiny `< 64`-byte continuations), validates structural invariants, and writes aggregate
  JSON and optional numeric CSV metadata. It never writes payload bytes and never touches the TOC.
- `tools/navmesh_links.py` (stdlib-only) decodes and verifies the 12-byte `+0x88` adjacent-polygon edges, the polygon
  `+0x20` special-link prefix-sum chain, and the 32-byte `+0xA8` special links and `+0xB0` index table.

```powershell
python -I tools/navmesh_decode.py <package>\navmeshes.rpf --out <temp>/navmesh-decode.json --csv <temp>/navmesh-decode.csv
python -I tools/navmesh_decode.py <package>\navmeshes.rpf --deep --out <temp>/navmesh-decode-deep.json
python -I tools/navmesh_decode.py <carve-dir> --out <temp>/carved-decode.json
python -I tools/navmesh_links.py <package>\navmeshes.rpf --out <temp>/navmesh-links.json
python -I -W error::ResourceWarning tests/test_navmesh_decode.py -v
python -I tests/test_navmesh_links.py -v
python -I tests/test_navmesh_index.py
```

For both tools, `--start` must be 0 or more and `--limit` at least 1; anything else is refused with exit code 2 before the scan.

Validation on the full real archive (`navmeshes.rpf`, `6,707,512,496` bytes) with `--deep`: `90,295` carver streams +
`10` tiny continuations; `9,603` `0DRP` (`15,629,979` cover nodes), `27,644` `0x00010015` navmesh tiles (`138,041,746`
vertices, `124,768,148` polygons, `2,337,904` quadtree nodes), `27,482` `grid` resources (`53,835,068` entries, `0`
sector mismatches, `keys_octree` and `inner_payload_ff00` pass on all), `22` `PSIN` (`21` complete), **0 check
failures**, in 236 s. Both test suites build synthetic archives in memory/tempdir and need no game files.

## No-sharing rule

The package is read-only. Do not commit, upload or share the archive, any carved page, a key, an extracted name,
or a report that quotes payload bytes. Only aggregate counts, offsets, verified layouts and four-byte magics belong in
this repository.
