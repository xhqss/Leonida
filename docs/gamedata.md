# Game data in `common.rpf`

This note measures the `common.rpf` archive from the Xbox Series 1.0.2.1 package. It records aggregate
counts, sizes and schema/root names only. No archive, carved file, XML value, entry name or per-file path is
committed. Everything below comes from a local copy and can be reproduced on any copy of the same version.

`common.rpf` is 6,642,192 bytes. Its RPF9 header is `RPF9` (`9FPR` little-endian) and the data area starts at
byte 38,272 (`0x9580`). The table of contents is encrypted/opaque; nothing here decrypts it or looks for keys.
The measurements cover the data area only.

## How to carve it

Use the carver with the short-final-slice fix (advance by the bytes actually read, not by the 1 MiB request):

```powershell
python -I tools/rpf9_carve.py common.rpf <work>\carve\common
```

Its `index.json` has `off`, `stored`, `size`, `pre` and `head` per stream. The `.bin` files are named by
offset and are **zlib streams, not files**. RPF9 splits a large file into 64 KiB inflated blocks, each with
its own zlib stream. A logical XML document is the concatenation of consecutive streams until its root element
closes; 178 of the 257 header-bearing documents are one stream, and the largest document spans 218.

`tools/gamedata_index.py` groups those blocks into payloads and writes an offset/size/type/root index:

```powershell
python -I tools/gamedata_index.py <work>\carve\common --csv common.csv --json common.json
```

The CSV/JSON contain offsets, sizes, the coarse type and the XML root element. They contain no element values
or entry names, but they are still local work products and must not be committed or attached to an issue.

### Coverage

| Measure | Value | Share of data span |
|---|---:|---:|
| Data span (file size minus header) | 6,603,920 B | 100.0% |
| zlib streams | 954 | - |
| Stored stream bytes | 5,647,255 B | 85.5% |
| Inflated bytes | 50,231,123 B | 8.9x the stored bytes |
| Zero padding | 386,138 B | 5.8% |
| Unread bytes | 570,527 B | 8.6% |

The earlier 781-stream / 39.5 MB / 87.3% figure came from the carver that advanced by the 1 MiB read request.
It counted skipped regions as part of the preceding stream, so its stored-byte total was too high and its
inflated total was too low. The fixed carver finds 173 more streams and 10.7 MB more data; its stored total is
lower because it measures each zlib stream's real end. Treat the old figures as superseded.

The 8.6% unread region is the non-zero gaps between streams: high-entropy bytes that do not decode as complete
zlib streams. Some contain a zlib header but fail the Adler check or are truncated. This is why 9 XML documents
survive only from their closing half and one 64 KiB XML block has no recoverable root. The carve does not guess
their content.

## Type split

Stream-level split, using the requested classes. `other` is 2 `MHIU` payloads (38 streams), 1 `PAMH` and 1
`SPS1Z`; there are no `RBF0`, `PSIN` or Bink 2 streams in this archive.

| Type | Streams | Stored B | Inflated B | Logical payloads |
|---|---:|---:|---:|---:|
| XML (`<?xml` / `<`) | 844 | 3,764,523 | 43,284,925 | 267 (266 docs + 1 fragment) |
| `RBF0` | 0 | 0 | 0 | 0 |
| `PSIN` | 0 | 0 | 0 | 0 |
| text / INI-like | 4 | 30,162 | 142,036 | 3 |
| binary | 66 | 1,396,087 | 4,312,136 | 2 |
| other (`MHIU`/`PAMH`/`SPS1Z`) | 40 | 456,483 | 2,492,026 | 4 |
| **total** | **954** | **5,647,255** | **50,231,123** | **276** |

By inflated bytes: 86.2% XML, 8.6% binary, 5.0% `other`, 0.3% text. XML is only 66.7% of stored bytes,
which is why a 6.6 MB archive inflates to 50.2 MB. XML grouping is exact because each document closes on its
root element. Non-XML grouping follows the 64 KiB block rule; none of the nine non-XML payloads here has a size
that is an exact multiple of 64 KiB, so their counts are unambiguous too.

The four non-XML structured payloads are the only readable binary metadata here: two `MHIU` blobs (about
1.19 MB and 1.21 MB inflated), one `PAMH` blob (48.6 KB) and one `SPS1Z` shader-parameter table (43.8 KB).
The two large opaque blobs (1.04 MB and 3.27 MB) have no recognised header and are left as binary. No model,
texture or audio signature is present in `common.rpf`; the two opaque blobs are not identified.

## MHIU and PAMH heightmaps

`tools/gamedata_maps.py` decodes both payloads. They are **heightmaps**, not
generic square arrays: each has a 44-byte header, a row table and u8 heights,
and both scale a value into the header's Z box with

```
z = bounds.min.z + value / 255 * (bounds.max.z - bounds.min.z)
```

`common.rpf`'s manifest names `common:/data/levels/americas/heightmap.dat` as a
`WORLD_HEIGHTMAP_FILE`, and the `PAMH` magic is the GTA V `HMAP` magic stored
little-endian. The row-table layout matches the published GTA V
`heightmap.dat` reader (`HMAP`, 44-byte header, 8-byte row headers, max/min
u8 arrays). The measured file is version 1.2; GTA V writes 1.1.

### `MHIU`: 2048 x 2048 single-layer heightmap

| Header | Offset | Type | Measured |
|---|---|---|---|
| magic | 0x00 | 4 bytes | `MHIU` |
| version | 0x04 | `u16` | 1 |
| width | 0x06 | `u16` | 2048 |
| height | 0x08 | `u16` | 2048 |
| pad | 0x0A | `u16` | 0 |
| six bounds | 0x0C | 6 x `f32` | `(-13000, -9000, -50)` to `(7000, 11000, 500)` |
| data size | 0x24 | `u32` | bytes after the 44-byte header |
| default height | 0x28 | `u8` | 23 in both (0 m quantised) |
| pad | 0x29 | 3 bytes | differ per file; not a checksum |

At 0x2C there are `height` records of eight bytes
`{u16 start, u16 count, i32 offset}`, then the packed u8 heights. A row with
`count = 0` keeps the default height from byte 0x28 (**23** in both payloads); the other rows fill cells
`start .. start + count - 1` from `pixels[offset + start + i]`.

| Payload | Filled rows | Empty rows | Packed bytes | Height range |
|---|---:|---:|---:|---|
| `0x144000` | 1,724 | 324 | 1,172,743 | 19..209 |
| `0x16E000` | 1,727 | 321 | 1,194,048 | 0..209 |

The row spans cover the packed bytes exactly: **0 gaps, 0 overlaps** in both
payloads. The default 23 is `-0.39 m`, and 209 is `400.8 m`, so the default is
the sea surface: `(0 - min.z) / (max.z - min.z) * 255 = 23.18`, so 23 is 0 m
quantised to the header's Z box.

The encoder crops each row to its first and last non-default cell. Over the
3,451 filled rows of both payloads, **0 of the 6,902 span ends** hold 23,
although 23 is 82% of all cells and occurs 849,544 times *inside* spans. The
ends are 24 (6,402), 22 (343) and other neighbours of 23. Empty rows are rows
that are entirely default. The decoder reads the default from byte 0x28 and
reports `span_ends_at_default` so a different build can be checked the same way.

The earlier "zero-filled prefix plus square byte grid" reading (1,090 x 1,090
and 1,100 x 1,100) was an artifact. The prefix is the zeroed row headers of
the empty rows, and the grid is 2048 x 2048. The largest square that ends at
the payload end happened to fit the byte count, but it is not the format.

### `PAMH`: `HMAP` max/min heightmap (`common.rpf` 144 x 164, tiles 64 x 64)

| Header | Offset | Type | Measured |
|---|---|---|---|
| magic | 0x00 | 4 bytes | `PAMH` (little-endian `HMAP`) |
| version | 0x04 | 2 x `u8` | 1.2 in `common.rpf`; overwritten in the `levels.rpf` tiles |
| pad | 0x06 | `u16` | 0 in `common.rpf`; text in the tiles |
| flag word | 0x08 | `u32` | `0x1BBC8CED` in `common.rpf`; padding in the tiles |
| width, height | 0x0C | 2 x `u16` | 144, 164 in `common.rpf`; 64, 64 in every tile |
| six bounds | 0x10 | 6 x `f32` | `(-13312, -9728, -302)` to `(5120, 11264, 404)` |
| data size | 0x28 | `u32` | 48,544 in `common.rpf`; 8,704 in a tile |

At 0x2C there are 164 row records, then `width * height` **max** bytes and
`width * height` **min** bytes. The measured max layer runs 110..255 and the
min layer 0..238; `min <= max` in all 23,616 cells. The max layer is a flat
110 (2.5 m) over most of the map - the sea surface - and rises to 255 (404 m)
on land. The min layer is the ground or sea floor.

The earlier u16 reading interleaved the two arrays: every measured `0x6E6E`
was a `(110, 110)` max/min pair, not the value 28,270.

### The 1,476 `PAMH` tiles in `levels.rpf`

`levels.rpf` holds 1,476 more `PAMH` payloads. All of them decode with the
same rules: a 44-byte header, 64 x 64 cells, 64 full-width row records with
`start = 0`, `count = 64` and `offset = y * 64`, then 4,096 **max** bytes and
4,096 **min** bytes. That is 6,045,696 cells in total.

- The tiles are a 36 x 41 rectangle with **512 m** spacing, so they cover the
  same `(-13312, -9728)` to `(5120, 11264)` box as the `common.rpf` payload,
  with the same Z box `-302` to `404`.
- Max values span 0..255 and min values 0..254. `min <= max` in all but 62
  cells, all in the tile at `(-5120, 2048)`, where the encoder wrote a min
  above its max.
- Bytes 0x05..0x0B are leftover text, not a version minor and a flag. 0x04
  stays 1, 0x05.. hold the tile name `n_m` (column + 1, row + 1), and the
  remaining bytes keep the tail of the string `height`, right-aligned at 0x0B.
  The 28 distinct 0x08..0x0B words are all text; none is zero and none changes
  the arrays.
- The census groups by grid size, not by the 0x04 version bytes, because
  0x05 is the first digit of the name. Nothing depends on the 0x08 word.

```powershell
python -I tools/gamedata_maps.py <work>\carve\levels --json levels.json
```

The tool prints one `PAMH census 64x64: ...` line and writes the per-group
counts. Only header fields and counts are written, never the height bytes.

### Cross-check and Z mapping

MHIU and PAMH cover slightly different boxes at different resolutions.
Sampling MHIU at the PAMH cell centres over their overlap gives a Pearson
correlation of **r = 0.861** for payload `0x144000` and **r = 0.870** for
`0x16E000` against the PAMH max layer. Mirrored or transposed orientations
give `r <= 0.64`. The land masks (`MHIU > 23` and `PAMH max > 110`) overlap
with **IoU 0.61**. Row 0 is therefore `bounds.min.y`, column 0 is
`bounds.min.x`, and the linear Z formula above is the same for both.

The HMAP header and row-table rules were cross-checked against the public
[rage-formats](https://github.com/VIRUXE/rage-formats) reader (`Unlicense`)
and [gta-op/heightmap](https://github.com/gta-op/heightmap) (`MIT`); no code is
copied. The MHIU field offsets, the span checks and the Z correlation are
measured on this package.

```powershell
python -I tools/gamedata_maps.py <work>\carve\common --json maps.json
```

### Checksum and unknown fields

- `PAMH` 0x08 is GTA V's `compressed` word only in the `common.rpf` payload,
  where it holds `0x1BBC8CED`. In the 1,476 `levels.rpf` tiles bytes 0x04..0x0B
  are leftover text (tile name plus `height`), so 0x08 is padding there. The
  `0x1BBC8CED` value is not CRC32, Adler32, FNV-1a, joaat32 or a hashlib
  digest of the max/min arrays; it is reported as a flag word, not a proven
  checksum.
- `MHIU` 0x28 is a `u8` default height, not a 32-bit word (ASSET-48). Its
  value is 23 in both payloads, the same value the empty rows were measured
  to hold, and the row cropping above uses it. A full scan of all 12 archives
  of package 1.0.2.1 (every zlib stream inflated far enough to read its magic)
  finds **2 MHIU payloads**, both in `common.rpf`, so N = 2 for the header
  values; the cropping rule is measured over all 6,902 span ends.
- `MHIU` 0x29..0x2B differ between the two files and match none of CRC32,
  Adler32, FNV-1/FNV-1a, joaat32 or an MD5/SHA-1/SHA-256/SHA3/BLAKE2 digest
  (any 3-byte window, either byte order, or a 24-bit slice of the 32-bit
  hashes) over the grid, the packed bytes, the row table, the header, the
  body or the payload with 0x28..0x2B zeroed. They do not equal any count or
  sum of the grid. They are most likely uninitialised struct padding after the
  `u8` and are reported as `pad_29`; with N = 2 this is a hypothesis.

## Grid atlases

Four `GridAtlasInfo` documents describe world-space texture grids streamed by position. All four have the
same numeric fields; the rest are names (`type`, `clientSystemName`, tile prefix/suffix, fallback texture).

| Field | Measured (identical in all 4) |
|---|---|
| `width`, `height` | 16,384 x 33,792 world units |
| `tileSize` | 512, so **32 x 66 = 2,112 tiles** |
| `origin` | the grid **centre** (see below) |
| `streamRadius` | 512 (one tile) |
| `zInflation` | 1,000 |
| `hasOwnGlobalFallback`, `useGlobalFallback` | true, true |
| `sectorOverrides/Item` | 1, with one sector at start (0, 0) and span (32, 66): the whole grid |

The tile count is the only reading under which the sector override's span matches: 16,384 / 512 = 32 and
33,792 / 512 = 66, so `width`/`height` are world units, not tile or texel counts.

Which corner `origin` names was tested against entity positions from a local index of map resources: 2,036,542
placements from 23,361 resources at every LOD level, 2,011,519 of them in `levels.rpf` and the rest in the
minigame and story packs (x 0.1th to 99.9th percentile -9,955 to 4,577, y -8,096 to 9,440). This is a
different count from the 1,670,442 entities of the levels census in [world_census.md](world_census.md); it was
only used for the share below. With `origin` as the grid centre, 99.83% fall inside the grid; as the minimum
corner, 1.0%; as the top-left corner with y growing downwards, 82.5%. The `levels.rpf` subset alone gives the
same picture: 99.83%, 1.0% and 83.0%. The centre
reading also gives a minimum x of exactly -13,312, the `PAMH` heightmap's minimum x above.

Tile names: none of the four `tileTextureNamePrefix` values starts an archive entry name or a texture-dictionary
item name in the local catalog (118,925 named items, 119,090 item hashes). Neither the global nor the sector
fallback names match an item name or its joaat hash, and ten `prefix + x + y + suffix` spellings over all
2,112 tiles match nothing in three atlases and one hash in the fourth. The tile textures are therefore either
outside the scanned archives or named another way; this is unresolved.

In the 1.0.2.1 exports, one content change set lists four grid metadata registrations and three candidate
archive paths as siblings. The list gives no per-grid archive assignment. The two fog grids share a tile
prefix, while the shore-wave and water-height grids each have a distinct prefix. A case-insensitive search
of the 30,516 other plain export blobs (30,520 total, excluding the four grid documents) found none of the
three prefixes or the grids' global or sector fallback names in ASCII or UTF-16LE. None of the three
archive filenames appeared as a loose file in the local package. The single catalog hash match above
remains uncorroborated. The tile payloads and the mapping from tile coordinates to archive entries remain
unverified.

A fifth grid uses the same base fields: one `TextureGridInfo` document has `origin`, `width`, `height`,
`tileSize`, `streamRadius` and `zInflation`, plus a low- and a high-detail fallback name, but no sector
overrides. It is 32,768 x 32,768 units with the same 512 tile, so **64 x 64 tiles**, the same 512 stream
radius and the same 1,000 `zInflation`, around a different origin. Tested the same way, 100.00% of the
placements fall inside with `origin` as the centre, 0.01% as the minimum corner and 11.8% as the top-left
corner, so the centre convention holds for both document types.

Not measured here (hypothesis): what the four grids hold (their names suggest per-tile map data for separate
client systems), and how `zInflation` widens a tile's streaming box vertically.

## XML documents

257 documents start with an XML declaration and all 257 parse as well-formed XML with a standard parser.
Nine more complete documents are visible only from their closing root tag because their first block was in
the unread region; 2 of those 9 still parse from their first retained byte, and 7 are truncated at the start.
One 64 KiB XML block has no root at all. That gives 266 recoverable documents and 118 distinct root elements.

Size distribution of the 257 header-bearing documents: minimum 88 B, median 13,435 B, mean 165,603 B,
maximum 14,262,150 B. 59 are at most 1 KiB, 178 are at most 64 KiB, 3 exceed 1 MiB and the largest spans 218
streams. 41 start with a UTF-8 BOM; 255 declare UTF-8 (214 uppercase, 41 lowercase), 2 omit the encoding
attribute, 11 say `standalone="yes"`, and the 412 comments are hand-edit notes.

### Root elements

| Root element | Documents |
|---|---:|
| `timecycle_modifier_data` | 49 |
| `CVehiclePoints` | 32 |
| `timecycle_keyframe_data` | 27 |
| `CDataFileMgr__ContentChangeSetDef` | 23 |
| `CTuningFile` | 10 |
| `CDamageDataManager` | 5 |
| `GridAtlasInfo` | 4 |
| `CCoverProcessorTuning` | 3 |
| `CDataFileMgr__ContentDef` | 2 |
| `CVehiclePerformanceMaps` | 2 |
| `CVehicleBoneOffsetsArray` | 2 |
| 107 other root elements | 107 |
| **total** | **266** |

The repeated roots matter: a root element alone does not identify a file. There are 49 timecycle modifier
documents, 32 vehicle-point documents and so on. Original paths and names would have to come from the missing
table of contents.

### Functional categories

The 118 root elements group into these categories. Counts and bytes cover the 266 recoverable documents
(41.2 MiB), not the one unattributed block.

| Category | Files | Inflated B | Roots | Representative root elements |
|---|---:|---:|---:|---|
| time, weather, sky, water | 87 | 17,339,011 | 13 | `timecycle_modifier_data`, `timecycle_keyframe_data`, `CContentsOfWeatherXmlFile`, `ShoreWavePlacementData` |
| vehicles, tyres, handling data | 45 | 19,618,605 | 12 | `CVehiclePoints`, `CVehicleBoneOffsetsArray`, `CTyreDataSet`, `CVehiclePerformanceMaps` |
| peds, schedules, appearance | 29 | 2,029,321 | 29 | `CPedScheduleInfoManager`, `CPedVariationBodyTypeManager`, `CPairedPedDataBlock`, `CEnergyConfigInfos` |
| tuning, parameters, effects | 28 | 343,601 | 14 | `CTuningFile`, `CCoverProcessorTuning`, `GridAtlasInfo`, `PatchConstants` |
| mount / content manifests | 25 | 450,316 | 2 | `CDataFileMgr__ContentChangeSetDef`, `CDataFileMgr__ContentDef` |
| world, navigation, map | 19 | 1,660,133 | 19 | `GameShape__Data__LocationNameLoopkup`, `PathingStyleData`, `CTrainConfigs`, `CHazardNodes` |
| combat and AI behaviour | 13 | 809,074 | 13 | `CCombatInfoMgr`, `CFiringPatternInfoManager`, `CFleeDecisionMakerData`, `Combat__GoalData__Store` |
| animation, poses, weapons | 6 | 162,778 | 6 | `WeaponAnimations__FullbodyControllerData`, `WeaponAnimations__ConfigData`, `CAimUpperBodyDataBlock` |
| damage | 5 | 485,482 | 1 | `CDamageDataManager` |
| audio, video, speech | 4 | 13,741 | 4 | `LandingPageVideos`, `CTVPlaylistContainer`, `naSpeechEventTypeBatchRequestParams` |
| missions, scenarios, respawn | 3 | 57,327 | 3 | `CDynamicScenarioManager`, `CMissions`, `Respawn__MetaData` |
| conversation | 2 | 250,000 | 2 | `CConversationList`, `CConversationDictionary` |
| **total** | **266** | **43,219,389** | **118** | |

This archive has no root element for popcycle, weapon stats or handling. Weapon-related documents here cover
loadouts, combat references and animation configuration. Vehicle handling-style values appear under the
vehicle/tyre roots, and the remaining gameplay tables are not XML. Do not infer a full gameplay schema from
`common.rpf` alone.

## Fog volumes

Volumetric fog comes in two documents: `FogVolumeProfileList` (the media) and
`CScatteringTransmittance__FogVolumeListData` (placed volumes). Counts; no values or names.

**Profiles** (`ProfileList/Item`, 43): `Name`, `UUID`, `Anisotropy`, `WaterDropletDensity`,
`DiffusionIntensity`, `ScatteringColorBlend`, `AbsorptionColorBlend`, `Scattering` and `Absorption` (`x y z`),
`IntExtMode`, plus optional `WaterDropletVariance` (8) and `WindDensityInfluence` (15). Names and UUIDs are
unique. `Scattering` and `Absorption` are within 0..1 per channel in 41 of 43. `IntExtMode` is 0 in 31 and 1 in 12.

**Volumes** (`FogVolumeList/Item`, 14), 37 fields each: shape (`IsBox`, `Position`, `PositionOffset`, `Scale`,
`Rotation`, `Shear`, `Wedge`), falloff (`Falloff`, `FalloffCurve` and per-axis versions), two 3x3 matrices
(`RotationMtx`, `TransformMtx`, 9 numbers each), ambient/emissive intensity, noise and wind, `Scattering`, `Density`,
`Anisotropy`, `WaterDropletDensity`, `Color`, `BlendMode`, `IntExtMode`, `SortKey`, `ProfileIndex`,
`SecondaryProfileIndex`, `ProfileInterpolation`, `uuid` and `Name`.

| Check | Result |
|---|---|
| `ProfileIndex` and `SecondaryProfileIndex` in `0..42` | 14 of 14 each: profiles are referenced by **list position**, not by UUID |
| Volume `uuid` equal to a profile `UUID` | 0 of 14 (separate id spaces) |
| Distinct profiles referenced | 2 of 43; primary equals secondary in 12; `ProfileInterpolation` is 0 in all 14 |
| `RotationMtx` and `TransformMtx` | identity in all 14, while `Rotation` is zero and `Scale` ranges 0.6 to 28 |
| `Shear` | zero in all 14 |
| `IsBox` | true in 13, false in 1 |
| `BlendMode` / `IntExtMode` | 0 or 1, seven each / 0 in all 14 |
| Positions | 7 at exactly (0, 0, 0); 8 distinct; all within x -2,093..30, y 0..803, z 0..109 |

Two consequences for a loader. The stored matrices do not include `Scale`, so the volume transform has to be
built from `Position`, `Rotation` and `Scale` rather than read from `TransformMtx`. Profile indices are
positions in `ProfileList`, so its order must be kept.

Not measured here (hypothesis): with half the volumes at the origin, two profiles in use and no interpolation,
this list looks like a sample or default set; the placed fog of the map is probably elsewhere (map resources
or another archive). What `IntExtMode` (interior/exterior) and `BlendMode` select is also untested.

## Schema style

Four patterns cover almost everything:

1. **Mount/content manifests.** `CDataFileMgr__ContentChangeSetDef` has `<id>`, optional registration/map
   fields, a `<fileList>` of `<Item>` with `<filename>` and `<fileType>`, plus flags and map phases.
   `CDataFileMgr__ContentDef` is the companion content description. There are 25 such documents and 3,315
   `<filename>` elements in total. The path tokens are `$(common)`, `$(target)` and `platform:/`; no XInclude
   is used.
2. **Typed object graphs.** `CCombatInfoMgr` and the conversations/damage roots use `<Item type="...">`.
   Fields are usually child elements, scalars use `value="..."`, and enums are bare text. Across the 259
   parseable documents there are 428,611 elements, 5,293 distinct tag names, 318 distinct `type` values
   (7,181 `type` attributes) and 33 distinct attribute names. The most common are `value` (84,585), `x`/`y`
   (20,825 each), `content` (13,449), `key` (9,180), `type` (7,181), `name` (4,688) and `ref` (924).
3. **Compact numeric tables.** `CVehicleBoneOffsetsArray` stores `<packedIndices content="short_array">`
   and `<palette content="float16_array">` as whitespace-separated numbers, with `numEntries` and
   `bitsPerIndex`. `content` values seen: `short_array` (10,309), `float16_array` (2,481),
   `float_array` (536), `matrix34` (46), `vector2_array` (40), `matrix33` (28), `vec3v_array` (9).
   `CTyreDataSet` uses nested groups of named floats. These tables are why the largest single document is
   14.26 MB and spans 218 streams.
4. **Timecycle arrays.** `timecycle_modifier_data` has a version root attribute, `<modifier>` records with
   name/description/apply-tag/index/user-flag attributes, and parameter elements whose text is a
   space-separated vector (`a b`, sometimes `a b c d`). Keyframe documents add `<cycle>`/`<region>`/`<axis>`.
   This is not the `name=value` attribute style of pattern 2.

A runtime consumer needs:

- a UTF-8 XML parser that tolerates a BOM, CRLF, tabs, comments and inconsistent whitespace around `=`;
- a root-element dispatch table (118 roots, 5,293 tag names), or an RTTI/class-metadata bridge if the
  executable's class names are available;
- a polymorphic `Item` type registry for `type=`;
- enum and symbolic-reference resolution: this archive uses symbolic `ref`/`key`/`name` strings
  (924 `ref`, 9,180 `key`); only 28 `key` values look like 8-digit hex hashes, so a name registry matters more
  than a joaat table here;
- whitespace-array and float16/packed-index decoders for the compact vehicle tables;
- a mount resolver for `$(common)`, `$(target)` and `platform:/` prefixes;
- streaming rather than whole-file parsing for the 14 MB table;
- strict handling of the 9 tail-only and 1 unattributed blocks: they are incomplete evidence, not documents
  to guess at.

## Custom clouds

One `CustomClouds` document places individual weather clouds over the world. Counts and ranges; no values or
names:

| Path | Count | Content |
|---|---:|---|
| `CustomCloudGroupList/Item` | 26 | `Name` (26 distinct), `Position` (`x`, `y`), `CustomCloudList` |
| `.../CustomCloudList/Item` | 630 | `Name` (429 distinct), `Position` (`x`, `y`), `Density`, `Rain`, `Fog`, `FogHeight`, `StrikeChance`, `BurstChance`, `CoverageMinMax` (`x`, `y`), and five booleans `Nimbus0`, `Nimbus1`, `Cumulus`, `Stratus`, `CirroStratus` |

Measured over the 630 clouds:

- **Positions are absolute world x/y.** Every group `Position` is (0, 0), so the group offset cannot be
  distinguished from none, and cloud positions span x -33,515 to 28,596 and y -31,920 to 37,612. 386 of the 630
  fall inside the 2D box of the world grid atlases (x -13,312..3,072, y -9,216..24,576; #1180); 136 lie more than
  20,000 units from the origin, far outside the map placements. There is no z component.
- **Groups** hold 1 to 74 clouds (median 18).
- **Cloud types are flags, not weights.** Per cloud the number of `true` flags is 0 (91 clouds), 1 (418), 2 (55),
  3 (26), 4 (3) or 5 (37). Clouds per type: `Cumulus` 357, `Nimbus0` 158, `Nimbus1` 131, `CirroStratus` 84,
  `Stratus` 73.
- **`CoverageMinMax` is a distance range, not a fraction**: min <= max in all 630, values 0 to 30,000, medians 200
  (min) and 2,000 (max), so it reads as an inner/outer radius in world units.
- `StrikeChance` and `BurstChance` are within 0..1 in all 630. `Rain` and `Fog` are within -1..1 (median 0),
  `Density` within -100..100 (median 0.5) and `FogHeight` within -100..300: signed values, so these act as
  offsets or overrides with a sign, not plain amounts.

The cloud renderer's settings sit in one `STS_Config` document with 44 top-level sections: four cloud layers
(`Nimbus`, `Cumulus`, `Stratus`, `Alto`) that share one set of 13 fields (noise weights and blends, erosion,
density, lighting and multi-scattering density scale, shape fade, transition and altitude ranges, dilation,
noise displacement); six media (`GlobalFog`, `LocalFog`, `Rain`, `Clouds`, `CloudsCirrus`, `CloudsHaze`) that
share another set of 9; and 34 single-value or vector settings. Of the five cloud flags above, `Cumulus` and
`Stratus` match a layer name, `Nimbus0`/`Nimbus1` would both fall on the one `Nimbus` layer, and
`CirroStratus` has no layer of its own (`CloudsCirrus` is a medium, not a layer). The weather data's
`CloudType` is fractional (14 distinct values from 0.01 to 0.67 over the 25 weathers), so it is a blend
position rather than an index into these layers.

Not measured here (hypothesis): how a cloud's radius range blends with the weather's `CloudType` and cloud maps,
what a negative `Rain`/`Fog`/`Density` means (subtracting from the weather, or "unset"), and what the two
`Nimbus` flags distinguish.

## XML catalog

`tools/gamedata_xml.py` turns a carve into a per-file and per-root catalog. It is stdlib-only and rebuilds
multi-stream documents with `gamedata_index.py`, so it reads the same 267 XML payloads the index reports:

```powershell
python -I tools/gamedata_xml.py <work>\carve\common --json xml.json --csv xml.csv --check
```

The file rows carry offset/bytes/chunks, root, category, element count, maximum depth, attribute names, `$(name)`
and `platform:/` counts, include-like counts and the well-formed/fragment flags. The root rows add the
element-name and attribute histograms and the reference targets. `--check` separates a rootless fragment, a
tail-only document and a malformed document instead of treating all three as parse failures.

Measured over the 267 payloads (43,284,925 B):

| Measure | Value |
|---|---:|
| Well-formed | 259 (257 with a declaration, 2 complete without a declaration) |
| Truncated at the start | 7 |
| Rootless fragment | 1 |
| Malformed | 0 |
| Named roots / catalogued roots | 118 (+1 unattributed) / 118 |
| Elements / distinct element names | 428,611 / 5,293 |
| Attribute names / `type` values / `content` kinds | 33 / 318 / 7 |
| Maximum depth | 17 |
| Comment starts (all / declaration-bearing) | 507 / 412 |
| `filename` / `parentPath` / `parentFile` / XInclude | 3,315 / 40 / 3 / 0 |
| `platform:/` references (levels / other) | 2,674 (2,346 / 328) |
| `$(common)` / `$(root)` / `$(target)` references | 588 / 28 / 23 |

Macro counts cover all element text; 587 of the 588 `$(common)` hits are in `<filename>` elements, which is the
filename-only bucket in the table above.

The root and element distribution is the one in the tables above: the five repeated roots hold 141 of the 266
documents, and the element histogram is dominated by the general node names `Item` (118,179), `item` (16,316)
and `Name` (12,850). The long tail is per-system parameter names; the manifest and timecycle names are a small
part of it. A category is a dispatch bucket, not a schema: for the 266 documents the catalog reproduces the
file and root counts in the functional table and assigns each root one category. No root has more than one
handler.

Reference handling is narrow. Every `$(...)` and `platform:/` string is element text, not an attribute; no
document uses XInclude. The macro alphabet is `$(common)`, `$(target)` and `$(root)`, and the platform targets
split into `platform:/levels/` and everything else. `filename` is the only include-like element with a large
population; `parentPath`/`parentFile` are the only relative references outside it. A resolver therefore needs
four prefix rules and a content-set/mount walk, not a general include processor.

A runtime dispatcher needs the 118-root table (or an RTTI class bridge), the 12 category buckets, the 318-value
`type` registry, the 33-name attribute whitelist, the `content=` array decoders and a streaming reader for the
14 MB document. The mount/reference half of that layer is measured separately in
[gamedata_graph.md](gamedata_graph.md): 25 manifest documents, 30 content-set ids, 12 resolved include edges
and 3,343 reachable target references. It also needs to mark the 9 tail-only documents and the one rootless
block as incomplete evidence: `gamedata_xml.py --check` is the reference for that split. The catalog is a local work product. Only
aggregate counts and schema/root/element/attribute names belong in git; the CSV/JSON, the element values, the
`type`/`content` values and per-file paths stay on the local machine. The tests are synthetic.

## Weather types

One `CContentsOfWeatherXmlFile` document defines the weather system. Element tree (counts per path; no values
or names):

| Path | Count | Content |
|---|---:|---|
| `WeatherTimeCycleFileNames/Item` | 25 | one timecycle file path each, all with the same path prefix |
| `WeatherTypeDatas/Item` | 25 | `Name`, `TimeCycleName`, 72 numeric `value=` fields (wind, gusts, rain, snow, fog, sandstorm, ripples, ocean and shore waves, foam, puddles, accumulation), one-token `LocalizedDensityMap`, `CloudMapA` and `CloudMapB`, an empty `Variations` on all 25, and `PedDirtDefinition` / `DirtCannedColor` (set on 1 of 25) |
| `WeatherTypes/Item` | 168 | `Name`, `ShortName`, `DataName`, eight `Mod*` tags, `WeatherAppDataName` (151), `SkipDebugCycling` |
| `WeatherAppTypeDatas/Item` | 26 | `Name`, `Icon`, `IconNight` (23), `Background`, `Description` |
| `Mod*List/Item` | 73 | allowed tags: Atmosphere 17, Precipitation 16, Temperature 9, Humidity 5, Wind 11, Fetch 5, Misc 7, Niceness 3 |
| `WeatherCycles/Item` | 2 | `CycleName`, `TimeMult` |
| `WeatherTypeIndirectionList/Item` | 15 | `From`, `To` |
| `WeatherMiscData` | 1 | four `Honor*` thresholds and times |

How the pieces link, checked by name (case-insensitive) over the archive:

- **25 weather datas = 25 timecycles.** Every `WeatherTypeDatas/Item` has `TimeCycleName` equal to its own
  `Name`, the 25 are distinct, and each matches the `cycle` `name` of one of the 27 `timecycle_keyframe_data`
  documents. The 25 matched documents are exactly the ones with 1,142 variables per region; the two keyframe
  documents with 515 and 520 variables are not referenced. All 25 file paths contain one of the 25 names;
  19 are in the same order as the data list.
- **168 weather types over 25 datas.** Every `WeatherTypes/Item` has a distinct `Name` and a `DataName` that
  resolves to one of the 25 datas, and every data is used. A weather type is therefore a named variant of a
  data entry, not a separate timecycle. 151 types carry all eight `Mod*` tags and a `WeatherAppDataName`; the
  other 17 carry none, and 16 of those have `Name == DataName` (the plain base weathers).
- **Mod tags resolve to their lists**, with three exceptions: Atmosphere, Precipitation, Temperature, Humidity,
  Fetch and Niceness resolve 151/151; Wind 150/151 (the miss is two characters from a list entry); Misc only
  14/151, because 137 types use one of two values that are not in `ModMiscList`. A loader should not reject a
  type over an unknown `ModMisc`.
- `WeatherAppDataName` resolves 151/151 to `WeatherAppTypeDatas`. In the 15 indirection pairs, 14 `From`
  and 14 `To` values are weather type names, and 13 pairs have both.
- The two `WeatherCycles` names occur in other `common.rpf` documents (2 and 1 documents), not as weather types.

Not measured here (hypothesis): what the `Mod*` axes do at runtime (they read as weather-app forecast
attributes), how `TimeMult` scales a cycle, and what the `Honor*` values gate.

## `OceanSimPresets`

`tools/gamedata_xml.py` lists `OceanSimPresets` among the time/weather/water roots. There is one such document.
Its element tree (counts per path; no values or names):

| Path | Count | Content |
|---|---:|---|
| `Presets/Item` | 10 | one preset per sea region |
| `Presets/Item/Name`, `Id` | 10 each | region label and id |
| `Presets/Item/Location` | 10 | `x`, `y` attributes in world units: x -7,995 to 3,183, y -4,572 to 8,645 |
| `Presets/Item/Direction` | 10 | wave direction |
| `Presets/Item/Octaves/Item` | 30 | exactly 3 per preset |
| `Presets/Item/Octaves/Item/ControlPoints` | 30 | 4 `(x, y)` pairs each, both in 0..1 |
| `WavefrontShapeA`, `B`, `G`, `R` `/Item/ControlPoints` | 1 each | one curve each |

So the ocean is a small set of regional presets placed by world position, each with three wave octaves whose
amplitude is a 4-point curve over a 0..1 input. The input is the sea state: the timecycle keyframe documents
carry a `water_ocean_beaufort_scale` variable (26 documents, 676 values, 0 to 10), the Beaufort number per
weather and time of day.

Not measured here (hypothesis): which preset applies at a position (nearest `Location` is the obvious reading),
how the Beaufort number maps onto the 0..1 curve input (Beaufort / 12 is one choice), and what the four
`WavefrontShape` curves drive.

## Timecycle documents

Two of the three most repeated roots are the timecycle tables: 27 `timecycle_keyframe_data` and 49
`timecycle_modifier_data` documents. All 76 parse. Counts below; no values, modifier names or region names.

**Keyframe documents** (root attribute `version`, the same value in all 27):

| Path | Count | Content |
|---|---:|---|
| `cycle` | 27 | exactly one per document; attributes `name`, `description`, `regions` |
| `cycle/region` | 54 | exactly two per cycle; attribute `name` |
| `cycle/region/<variable>` | 59,170 | one element per variable, text = **13** space-separated numbers |

Every one of the 59,170 variables has 13 values, the same as GTA V's 13 time-of-day samples, so a keyframe
document reads as one weather's day curve per variable and region. In 25 documents each region has
**1,142 variables**; the other two have 515 and 520. Within every document the two regions list the same
variables in the same order. The 1,142-variable set and the two short documents together use 1,301 distinct
names: 362 of the 521 short-document names are in the 1,142 set and 159 are not, so the short documents
follow an older or separate variable list rather than a prefix of the current one. A loader should therefore
bind variables by element name, not by position.

**Modifier documents** (root attribute `version`, the same value in all 49):

| Path | Count | Content |
|---|---:|---|
| `modifier` | 4,593 | attributes `name`, `description`, `groupApplyTag`, `localApplyIndex`, `userFlags` on all; 4,592 distinct names |
| `modifier/<variable>` | 29,738 | 29,727 hold 2 numbers, 11 are empty |
| `modifier/submodifiers/item` | 8,043 | in 1,928 modifiers (4 items is the usual count: 1,252); attributes `flags` (29), `alpha` (14), `blendSpeed` (3) |
| `modifier/axis` | 405 | in the 302 modifiers that carry `numAxes` and `numKeyframes`; attribute `controlValue` on all |
| `modifier/axis/keyframe_t_value` | 302 | exactly one per axis modifier (on one of its axes), `numKeyframes` numbers |
| `modifier/axis/<variable>` | 2,713 | `numKeyframes` numbers each |

Modifiers come in two shapes:

- **Plain** (4,291): variables are direct children with two numbers each, as in GTA V's modifier files.
  Per modifier: 0 to 407 variables. 1,025 have none: 759 of those only list submodifiers and 266 are empty. The 1,054 modifier variable
  names include 947 keyframe variables and 107 that occur only in modifiers.
- **Axis-driven** (302): every variable sits inside an `axis` and has exactly `numKeyframes` values; the axis's
  `keyframe_t_value` lists the input positions (one list per modifier, even with several axes). All 2,713 axis variables match their modifier's
  `numKeyframes`, and all 302 `keyframe_t_value` lists have that length and are sorted ascending.
  `numAxes` is 1 (235), 2 (49) or 4 (18) and always equals the number of `axis` children
  (`(numAxes, numKeyframes)` most often `(1, 6)`: 93), and `numKeyframes` ranges from
  2 to 25 (2 to 7 in 271 of the 302; 6 alone in 107). `controlValue` takes 43 distinct symbolic names, so the input is a named game
  quantity rather than time of day. 61 axes add `input_min`/`input_max`, and 7 add `blendType`; 23 of the
  302 position lists fall outside `[input_min, input_max]` or, without those attributes, outside 0..1, so
  the range is not a clamp on the stored positions.

`submodifiers/item` text is one token. 8,042 of the 8,043 items equal the `name` of a modifier in the same
49 documents, so a submodifier is a by-name reference to another modifier, with its own blend attributes.
The one unresolved item points outside this archive or at a document lost to the unread region.

Not measured here (hypothesis): the hour of each of the 13 samples (GTA V's are fixed in code), how the two
regions are chosen at runtime, what the two plain-modifier numbers are (GTA V reads them as a value pair
blended by strength), and how `controlValue` maps onto game state.
## Vehicle and tyre XML documents

Four roots account for 37 of the 45 documents (4,547,932 inflated bytes) in the vehicles/tyres category:
`CVehiclePoints` (32), `CVehicleBoneOffsetsArray` (2), `CVehiclePerformanceMaps` (2), and `CTyreDataSet` (1).
All 37 parse cleanly with no truncation errors. Only counts, element/attribute names, encodings, and numeric
ranges are recorded below; no vehicle names or entry values are included.

### `CVehiclePoints` (32 documents, 33,516 B)

Each document fits in one stream (370 to 10,425 B, median 592 B) and holds one vehicle entry (31 distinct
`DefinitionName` values across the 32 documents; `DefinitionName` equals `ModelName` in all 32). Element tree:

| Path | Count | Content |
|---|---:|---|
| `DefinitionName`, `ModelName` | 32 each | text identifier (equal in all 32) |
| `Flags`, `iNumBoardingPoints` | 32 each | `value="0"` in all 32 |
| `Points`, `Props`, `AuthoredMeshName` | 32 each | empty in all 32 |
| `fNavOverrideRecastCellSize` | 32 | `value=` float `0.0000..0.0625` (median `0.0312`, 4 distinct values) |
| `fMinPatchAreaBeforeCulling` | 31 | `value=` float `0.1000..4.0000` (median `1.0000`, 6 distinct values) |
| `fNavMaxHeightChange`, `fNavMinZDist` | 30 each | `value=` constant `0.35` and `0.50` |
| `fNavOverrideAgentRadius`, `fNavOverrideAgentHeight` | 30 each | `value=` `0.00..0.15` (4 values) and `0.00..1.20` (2 values) |
| `fNavOverrideRecastCellHeight` | 26 | `value=` `0.00..0.04` (2 values) |
| `ModDefs` / `ModDefs/Item` | 6 / 49 | 1 to 32 mod entries per document (median 4) |

Every `ModDefs/Item` carries `ChassisModName` (49/49 non-empty), `PropsetExteriorModName` (35/49 non-empty),
`UtilityModName` (32/49 non-empty), `InteriorModName` (4/49 non-empty), and `CargoModName` (0/49 non-empty);
17 items also include `RoofModName` (8 non-empty). Cross-referencing `ModelName` against the two
`CVehiclePerformanceMaps` documents matches 20 keys in the watercraft map and 4 in the land/air map, matching
the navmesh-override fields used for walkable/boardable hulls and large vehicles.

### `CVehicleBoneOffsetsArray` (2 documents, 4,303,883 B)

Two documents share the `CVehicleBoneOffsetsArray` root (`boneOffsets/Item`, 2,481 entries total) with a clean
split between identifier fields:

| Document | Inflated B | Streams | `boneOffsets/Item` | Identifier field | `attachBones/Item` per entry | `numEntries` per entry |
|---|---:|---:|---:|---|---:|---:|
| Doc 0 | 4,283,195 | 66 | 2,447 | `name` (2,447/2,447; `vehicleName` empty) | 8..3,200 (median 8; 66,128 total, 9,345 non-empty) | 24..9,600 (median 24; 740,376 total) |
| Doc 1 | 20,688 | 1 | 34 | `vehicleName` (34/34; `name` empty) | 4 in all 34 (136 total, all non-empty) | 12 in all 34 (408 total) |

33 of Doc 1's 34 `vehicleName` entries match keys in `CVehiclePerformanceMaps` Doc 0 (0 in Doc 1).

Each `boneOffsets/Item/offsets` block stores a palette-compressed float16 table with four elements: `numEntries`
(`value=`), `bitsPerIndex` (`value=`), `packedIndices` (`content="short_array"`), and `palette`
(`content="float16_array"`). Measured over all **2,481 / 2,481** tables (740,784 unpacked entries):

- **`numEntries` divisibility:** exact multiple of `len(attachBones)` in all 2,481 entries (remainder 0) and exact
  multiple of 12 in all 2,481 entries. `numEntries / len(attachBones)` is `3` in 2,319 entries (including all 34
  of Doc 1 — one 3-component vector per bone slot); the remaining 162 entries take multiples of 3 from `9` to
  `1,200` (`9, 12, 27, 36, 48, 75, 108, 300, 1200`).
- **Bit width and palette:** `bitsPerIndex` is `1` (253 tables, palette length 2), `2` (1,951 tables, length
  3..4), `3` (258 tables, length 5..8), or `4` (19 tables, length 9..14) — always minimal for `len(palette)`.
  Palette values span `-30.90625` to `2.542969`, with `0.0` present in 2,119/2,481 palettes.
- **Exact packing rule:** `packedIndices` holds 107,229 non-negative unsigned 16-bit integers (`0..65535`)
  encoding a continuous **LSB-first bitstream** across 16-bit words. The array length equals
  `floor(numEntries * bitsPerIndex / 16) + 1` in **2,481 / 2,481** tables (when `numEntries * bitsPerIndex` is a
  multiple of 16 — 2,043 tables — the writer appends one trailing `0` word; all 2,043 trailing words and all
  unused tail bits across all 2,481 tables are `0`).
- **Bounds check:** unpacking all 740,784 indices produces **0 out-of-bounds** palette indices.

### `CVehiclePerformanceMaps` (2 documents, 133,848 B)

Two documents with disjoint `Map/Item[@key]` sets (285 distinct keys total, 0 overlap). Every `Map/Item` carries
the exact same 10 scalar `value=` children: `topSpeed`, `accelZeroTwenty`, `accelZeroSixty`, `brakingSixtyZero`,
`turnRadius`, `maxGforceLateral`, `maxGforceSteering`, `idlePitchAngle`, `buoyancyOffsetZ`, and `takeOffSpeed`.

| Document | Inflated B | Streams | Entries | Non-zero road braking/turn/G | Non-zero `buoyancyOffsetZ` | Non-zero `takeOffSpeed` | `topSpeed` range (median) |
|---|---:|---:|---:|---:|---:|---:|---|
| Doc 0 (land/air) | 117,808 | 2 | 251 | 203..223 | 0 | 10 (max `57.64`) | `0.00..115.96` (`45.32`) |
| Doc 1 (watercraft) | 16,040 | 1 | 34 | 0 | 34 (`-1.11..1.91`) | 1 (`21.54`) | `0.00..55.05` (`16.82`) |

### `CTyreDataSet` (1 document, 76,685 B, 2 streams)

One document containing 40 `TyreDataEntries/Item` records (40 distinct `key` attributes, max tree depth 5). Each
entry combines up to four nested parameter groups with 12 direct scalar `value=` fields:

| Path under `TyreDataEntries/Item` | Present | Schema |
|---|---:|---|
| `roadPerformance` | 40 / 40 | 8 scalars: `fPeakSlipAngle`, `fAdhesionLimitAngle`, `fLateralFrictionMult`, `fPeakSlipRatio`, `fAdhesionLimitRatio`, `fLongitudinalFrictionMult`, `fPeakForceCoefficient`, `fSlidingForceCoefficient` |
| `offRoadPerformance` | 38 / 40 | identical 8-scalar schema (omitted on 2 entries) |
| `roadPenalties`, `offRoadPenalties` | 40 / 40 each | identical 6-scalar schema: `fMaterialTractionLoss`, `fMaterialRollingDragGain`, `fMaterialSinkDragGain`, `fMaterialSurfaceDragGain`, `fWetTractionLoss`, `fWetDragGain` |
| Direct scalars (required) | 40 / 40 each | `fLoadShapeVarianceScale`, `fDamageShapeVarianceScale`, `fDamageCoefficientScale`, `fOptimumTyreTemp` (all `100.0`), `fOptimumTyreTempRange` (all `100.0`), `fGripDropOffWithTemp` (`0.01..0.05`) |
| Direct scalars (optional) | 36..39 / 40 | `fPuncturedTyreGripReduction`, `fBurstTyreGripReduction`, `fTractionStiffnessSpeedMult`, `fTractionStiffnessSpeedCap` (39 each); `fLoadStability` (37); `fLoadSensitivity` (36) |
**Sample hours come from data.** One `time_data` document (root attributes `version` and `mode`) has exactly 13
`sample` elements, each with `name`, `hour`, `uw_tc_mod` and `interpolation_type`, plus one `region`. The 13
hours are integers from 0 to 22 in strictly increasing order, with gaps of 1 hour (7 times), 2 (twice), 3 (once)
and 4 (twice); `interpolation_type` is `EASE_LINEAR_IN` on 9 samples and `EASE_QUARTIC_INOUT` on 4, and all 13
`uw_tc_mod` values are the same name, which resolves to one of the 4,592 modifier names. The `region` name equals one of the two keyframe
region names. So unlike GTA V, where the 13 sample times are compiled in, this build reads them (and the easing
between them) from `time_data`; their count matches the 13 values of every keyframe variable.

Not measured here (hypothesis): how the two regions are chosen at runtime, what the two plain-modifier numbers
are (GTA V reads them as a value pair blended by strength), and how `controlValue` maps onto game state.

## Relationship to `data.rpf` and `script.rpf`

| Archive | Size | Streams | Inflated | Dominant payloads | XML | `RBF0` | `PSIN` |
|---|---:|---:|---:|---|---:|---:|---:|
| `common.rpf` | 6.6 MB | 954 | 50.2 MB | XML/config, text, `MHIU`, `PAMH`, `SPS1Z` | 844 streams | 0 | 0 |
| `data.rpf` | 481.5 MB | 28,509 | 1,403.3 MB | models/GPU pages, 1,263 `RBF0`, 2,281 `PSIN`, 3,132 resource pages | 0 | 1,263 | 2,281 |
| `script.rpf` | 4.4 MB | 714 | 5.0 MB | 671 `PSIN` plus binary | 0 | 0 | 671 |

There is **no XML, `RBF0` or `PSIN` duplication** between them: `common.rpf` contains no `RBF0`/`PSIN`, and
`data.rpf`/`script.rpf` have no XML documents at all — no stream starts as XML and no inflated stream contains
an XML declaration (the ten `<`-leading `data.rpf` streams are binary). This is a type-level and stream-start
check, not a byte hash of all 1.4 GB of inflated `data.rpf`; without the table of contents a duplicate stored
at a different block boundary would not be recognised.

The manifests in `common.rpf` reference the other archives by path token, not by archive handle. Counted over
the 3,315 `<filename>` elements:

| Reference bucket | Count |
|---|---:|
| `platform:/levels/` | 2,346 |
| `$(common)/` | 587 |
| other `platform:/` | 328 |
| relative/other | 31 |
| `$(target)/data/` | 23 |
| `$(target)/script/` | 0 |

So `common.rpf` is the XML/config layer that mounts levels content and references 23 `data.rpf` paths; none
of its `<filename>` entries use the `script` token. It may still reference script content through non-path
content names, which this count does not cover. The 587 `$(common)/` entries are mostly movie paths (537 end
in `.bk2`), not XML. Of the 57 `.xml` path entries, 50 are under `$(common)/` and 7 are relative/other; none
are under `data/` or `script/`. A further 40 `parentPath` attributes are XPath-style
`../Item[@key='...']` steps that all resolve to sibling items inside their own document, so they need no table
of contents (see [gamedata_graph.md](gamedata_graph.md#what-the-locators-point-at)). No XInclude (`xi:include`) elements occur.

## Shore wave placement

The shore-break waves are placed by one `ShoreWavePlacementData` document, the coastline counterpart of
`OceanSimPresets`. Element tree (counts per path; no values):

| Path | Count | Content |
|---|---:|---|
| `Context` | 1 | empty |
| `Patches/item` | 434 | `loop` and `waveType` (`value=`), then `ControlPoints` |
| `Patches/item/ControlPoints/item` | 7,839 | `position` and `direction` (`x`, `y`), `lateralCoord`, `maxDistance`, `amplitude` (`value=`) |

Measured over the 434 patches and 7,839 points:

- **Patches are open 2D polylines.** 2 to 124 points per patch (median 14), median spacing 9.5 world units.
  `loop` is false on all 434; 11 patches still repeat their first point at the end. Positions span x -9,602 to
  2,335 and y -7,955 to 9,474, a larger box than the ten ocean preset locations. Positions have no z
  component.
- **`waveType`** takes 5 values, used by 181, 147, 69, 31 and 6 patches.
- **`direction` is a unit vector** at every point (length 1.000 to three decimals). It runs across the polyline
  rather than along it: the median |cos| between it and the local polyline tangent is 0.24 (quartiles 0.11 and
  0.44), consistent with a wave travelling across the line towards the shore.
- **`amplitude` is 1.0 at all 7,839 points**, so it carries no per-point variation in this build. The height
  presumably comes from elsewhere, for example the per-weather `ShoreWaveAmplitude`, `ShoreWaveMinAmplitude`,
  `ShoreWaveMaxAmplitude` and `ShoreWaveWindScale` fields of the `CContentsOfWeatherXmlFile` weather data
  (its layout is documented in #1175).
- **`maxDistance`** ranges 3.0 to 120.4 (median 74.9): how far the wave runs from the line.
- **`lateralCoord`** is a float in -2.94..3.50 with 7,820 distinct values. It is not arc length and not monotonic
  along 433 of the 434 patches, so it is not a running texture coordinate along the line.

Not measured here (hypothesis): what the five `waveType` values select, and what `lateralCoord` encodes (an
offset across the line is one reading).

## Readable vs binary

Readable: the 266 XML documents, 3 text/INI-like files, and the two `MHIU` 2048 x 2048
heightmaps plus the `PAMH` max/min heightmap and the `SPS1Z` header. The XML is ordinary text
and can be consumed by any parser. `tools/gamedata_maps.py` decodes the `MHIU` row table and
packed heights and the `PAMH` row table and max/min arrays. `SPS1Z` is a NUL-separated
table of path/key strings with per-entry type/length markers; its structure is visible but its consumer is not
implemented.

Not fully readable: the 8.6% unread span, the two opaque binary blobs, and the missing first blocks of the 9
tail-only documents (their retained tails are readable but incomplete). The encrypted table of contents is
outside this work by rule; names and exact file boundaries stay unknown until a documented table reader exists.

## Dirt and canned colours (`CGraphicsDefinitionsData`)

One `CGraphicsDefinitionsData` document holds the dirt/grime presets for peds, vehicles and props and the
colour table they draw from. Seven sections, each a list of `Item key="..."` records (counts; no values or
names):

| Section | Items | Fields per item | `key` |
|---|---:|---:|---|
| `CannedColors` | 44 | none: `x`, `y`, `z`, `w` attributes | text |
| `AlphaChannels` | 7 | `Name`, `Channels` (1 or 2 `Probability`/`Channel` pairs; 10 in total) | equals `Name` in 6 of 7 |
| `DirtDefinitions` | 24 | 25 (alpha curves and thresholds, vertex multipliers, normal/roughness/material contribution, spawn range, 6 `ClothHideDirtModifiers`) | equals `Name` in 22 of 24 |
| `VehDirtDefinitions` | 1 | 12, including a `DirtTunings` block of tile, detail-map, mud and modulate start/end values | equals `Name` |
| `VehDirtControls` | 5 | 13 | equals `Name` |
| `VehSpawnIntensities` | 31 | 5 | equals `Name` |
| `PropDirtDefinitions` | 9 | 4 | equals `Name` |

The keys are plain text, not joaat hashes, and a loader can index each section by `key`.

**Canned colours are 8-bit RGB.** In all 44, `x`, `y` and `z` are integers from 0 to 255 and `w` is -1, so
`w` is not alpha; read it as an unused component or "no alpha".

**References** (case-insensitive equality with another section's `key` or `Name`):

| Field | Resolves to | Count |
|---|---|---:|
| `DirtDefinitions/Item/ColorLookup` | `CannedColors` | 24 of 24 |
| `DirtDefinitions/Item/AlphaMasksLookup` | `AlphaChannels` | 18 of 24 (the other 6 are empty) |
| `VehDirtDefinitions/Item/ColorLookup` | `CannedColors` | 1 of 1 |
| `PropDirtDefinitions/Item/Color0Lookup`, `Color1Lookup`, `Color2Lookup` | `CannedColors` | 9 of 9 each |
| weather data `PedDirtDefinition` (see the weather document) | `DirtDefinitions` | 1 of 1 |
| weather data `DirtCannedColor` | none | 0 of 1 |

So every dirt preset gets its colour from the canned table, and one weather selects a ped dirt preset. The
weather's `DirtCannedColor` is a single non-numeric token that matches no canned-colour key, even as a
substring; where it resolves is unknown. Other `common.rpf` documents share only a few short generic words
with the `VehSpawnIntensities` keys, which is not evidence of a reference.

`AlphaChannels` probabilities do not sum to 1 in any of the 7 entries, so they are not a probability
distribution over the listed channels (independent chances or weights would fit). The `Channel` values are 0, 1 or 2.

Not measured here (hypothesis): which texture channel numbering `Channel` uses, and how `VehSpawnIntensities`
are chosen at spawn.

## Entity component sets and component LOD

Four documents describe which runtime components an entity gets and how far away each stays alive. Counts only;
no set or type names.

| Root | Content |
|---|---|
| `CEntityComponentLodManagerData` | `EntityComponentCounts/Components/Item` x 33: `Type` (33 distinct) and `DesiredCount` (0 to 250, 2,684 in total; one type is 0), plus optional distance-scale factors for not-visible (5 and 2 items), interior (1) and deep-interior (1) entities and per-frame create/remove limits (4 and 2) |
| `CVehicleComponentSetManager` | 7 `CVehicleComponentSetInfo` sets with 11 to 14 components each (90 in all): `Type`, `MinDistance`, `MaxDistance` |
| `CPedComponentSetManager` | 15 `CPedComponentSetInfo` sets with 8 to 17 components each (173 in all), plus feature flags: `HasFacial` (true in 12), `HasRagdollConstraints` (13), `HasLookAt` (10), `HasInventory` (8), `HasPhone` (2), `HasReins` (1), `HasDistraction` (2 of 3 sets that carry it), `IsRidable` (false in all 15) |
| `CObjectComponentSetManager` | 1 `CObjectComponentSetInfo` set with an empty component list |

Each manager has a `DefaultSet ref`, and all three refs name one of their own sets.

**The component types form one closed list.** Vehicle sets use 14 distinct `Type`s and ped sets 18, with none
in common, and every one of the 32 is among the 33 types of `CEntityComponentLodManagerData`; only one LOD-manager
type is used by no set. So a runtime can treat the LOD manager's list as the component registry, with
`DesiredCount` as the pool size per type.

**Distances.** `MinDistance <= MaxDistance` in all 263 components. Vehicle components reach up to 3,000 units
and ped components up to 200 (both medians 100); the natural reading is the range within which the component is kept
alive.

Not measured here (hypothesis): whether the distances are metres from the camera or the player, and what the
not-visible and interior scale factors multiply.

## Damage and tuning XML documents

Three repeated roots in the top-ten table above cover the damage and tuning tables: `CDamageDataManager` (5 documents,
485,482 B), `CTuningFile` (10 documents, 27,718 B), and `CCoverProcessorTuning` (3 documents, 34,380 B). All 18 parse
cleanly. Counts and schemas only; no entry names or parameter values.

### `CDamageDataManager` (5 documents, 485,482 B, 10 streams)

Rather than repeating one table type, the five documents partition the damage system by sub-table:

| Document | Inflated B | Streams | Top-level section | Measured structure |
|---|---:|---:|---|---|
| Doc 0 | 257,008 | 4 | `DamageCleanlinessDatas/Item` (5) | `25` `RegionData/Item` blocks (5 per entry) containing `528` keyed `WeaponData/Item` records (`key=`), with `616` `CleanlinessResults/Item` and `328` `CleanlinessResultsLegendary/Item` pairs `{MaxBulletHits, MaxDamageCleanliness}` |
| Doc 1, Doc 4 | 104,082 / 107,078 | 2 / 2 | `DamageModifiers/Item` (147 / 96) | `243` top-level modifiers across 19 polymorphic `CDamageModifier*` classes (`CDamageModifierMultiply` 63, `CDamageModifierMultiReference` 58, `CDamageModifierClamp` 39, `CDamageModifierFirstSuccessfulReference` 36, `CDamageModifierMulti` 19, `CDamageModifierInstantKill` 5, 13 others) and `336` nested `Modifiers/Item` entries, gated by `Condition` trees across 65 `CDamageCondition*` classes (`CDamageConditionAnd` 144, `CDamageConditionNot` 126, `CDamageConditionOr` 72, `CDamageConditionFromDamageType` 60, `CDamageConditionFromWeaponType` 49, `CDamageConditionIsPlayerBuddy` 44) |
| Doc 2 | 12,498 | 1 | `DismembermentData` (1) | Dismemberment thresholds and bone/region rules |
| Doc 3 | 4,816 | 1 | `HitReactData` (13), `HitReactGripData` (2) | Hit-reaction and weapon-grip impulse parameters |

### `CTuningFile` (10 documents, 27,718 B) and `CCoverProcessorTuning` (3 documents, 34,380 B)

- **`CTuningFile` (10 single-stream documents, 266 to 11,727 B, median 1,875 B):** each wraps a `<Tunables>` list of
  polymorphic `<Item type="...">` blocks (`14` items total across 14 distinct `type` classes, including
  `CConversationManager__Tunables`, `CDeadeyeSpecialAbility__Tunables`, `CLasso__Tunables`, and
  `CPlayerFocusSpecialAbility__Experimental__Tunables`). Sub-structures include `36` keyed
  `EntityMaskConfig/EntityMaskProfiles/Item` records (`MaskValue` 4D `x/y/z/w`, `MaskValueExtras` 2D `x/y` on 33,
  `BlendOutCurveType` on 4) and a nested `ClipSetTree` (`5` `TransportMotionHelpers__ClipsetTransportConditionNode`
  branches over `8` `GameplayHelpers__ClipsetLeafNode` leaves).
- **`CCoverProcessorTuning` (3 single-stream documents: 11,554, 11,422, and 11,404 B):** flat scalar tables with
  `212`, `211`, and `210` direct `value=` children per document (`fAdditionalDistanceFromTreeToRejectAdditionalCoverpoints`,
  `fAICanAimSweepCount`, `fCornerCoverPointDotThreshold`, `fCornerDistanceCheckResolutionFine`, etc.).

## Combat, AI behaviour and weapon animation documents

The representative roots of the combat/AI (13 documents, 809,074 B) and weapon-animation (6 documents, 162,778 B)
categories use typed polymorphic `<Item type="...">` and keyed map tables:

| Root | Inflated B (streams) | Status | Measured schema |
|---|---:|---|---|
| `CCombatInfoMgr` | 237,621 (4) | well-formed | `CombatInfos/Item` × 66 (`type="CCombatInfo"`), each with `Name`, `CombatMovement`, `BehaviourFlags`, `CombatAbility`, `AttackRanges`, `CoverHeightPreference`, `WeaponShootRateModifier` (`value=`), `FiringPatternSetHash`, `AccuracyProfile`, and `CombatFloats` (`BlindFireChance` and related scalars) |
| `CFiringPatternInfoManager` | 208,240 (4) | tail-only | One of the 7 start-truncated XML payloads whose first block fell in an unread RPF9 gap; identified by its closing tag |
| `CFleeDecisionMakerData` | 17,561 (1) | well-formed | `Profiles/Item` × 8 (`key=`), each defining `{Weight, Weights}` blocks for `Cower`, `RunAway`, `Mount`, and `TakeVehicle` (8 each), plus `Fly/Weights` (14 entries) |
| `Combat__GoalData__Store` | 4,550 (1) | well-formed | `RoleSettings` (`Default` + `DataMap/Item` × 5 keyed `StyleMod` entries) and `PriorityCalculators/DataMap/Item` × 8 (`type="Combat__GoalData__EntityDistanceBasedPriorityCalculator"` with `13` `{MaxDistSq, Priority}` pairs) |
| `WeaponAnimations__FullbodyControllerData` | 21,281 (1) | well-formed | `AnimRequestConfigMap/Item` × 11 (`type="WeaponAnimations__FBAnimRequestConfig"`, `DefaultRequestType` on 11, polymorphic `RequiredCondition` on 9, `7` `Variants/Item`) and `AnimRequestDataMap/Item` × 17 (`type="WeaponAnimations__FBRequestData"`, `LocoParams` on 16, `ControlGraphParams` on 11), plus `Timeouts` (1) |
| `WeaponAnimations__ConfigData` | 1,733 (1) | well-formed | `AIHandPoseRules` and `BuddyHandPoseRules` (`type="WeaponAnimations__HandPoseRules"`), with nested `CAIConditionOr` / `CAIConditionAnd` / `CAIConditionBlackboardBool` trees under `PosePrefOverrides/ForceTwoHanded` |
| `CAimUpperBodyDataBlock` | 1,813 (1) | tail-only | Start-truncated single-stream payload identified from its closing root tag |

## Ped schedules, body variation, formations and energy

Four representative roots account for 1,032,473 B (18 streams) of the 29-document peds/schedules/appearance category:

| Root | Inflated B (streams) | Top-level sections and counts |
|---|---:|---|
| `CPedScheduleInfoManager` | 784,294 (12) | `Schedules/Item` × 4,861 (`{Name, Pack}` pairs), `PedScheduleTuning` (18 children), `PedDynamicScheduleTuning` (19 children), `Volumes/Item` × 9 (`VolumeType`, 3D `Position`/`Orientation`/`Scale`, `IsPenVolume`, and `SpawnPositions` `content="vec3v_array"`), `Routes` (1) |
| `CPedVariationBodyTypeManager` | 77,462 (2) | `BodyShapePresets/Item` × 16 containing `432` `Expressions/Item` records (`27` per preset, `{Id, Values content="float_array"}`), `ScaleVariationProfiles/Item` × 26 (`key=`, `34` `ScaleVariations/Item` with `{Min, Max, SelectionWeight, ValueType}`), `BodyShapeVariationProfiles/Item` × 22 (`key=`, `53` `BodyShapeVariations/Item` with `{BodyShapePreset, SelectionWeight}`), `ScaleProfiles` (2), `HeightPresets` (1) |
| `CPairedPedDataBlock` | 10,011 (1) | `SpacingData/Item` × 4 (seven 2D `x/y` distance pairs per item: `Spacing`, `MiddleSpacing`, `MinSpacing`, `MinSpacing_Crossing`, `MinMiddleSpacing`, `MinMiddleSpacing_Crossing`, `MaxSpacing`), `BespokeSpacingData` (2), `VehicleSpacingData` (1), `AmbientFormationData` (3, 6 2D `Pos` slots), `BespokeFormationData` (3, 11 2D `Pos` slots), `VehicleFormationData` (1) |
| `CEnergyConfigInfos` | 160,706 (3) | `HealthConfig/Item` × 63 (`key=`, `parentFile` on 3, `MeleeProperties/KnockedOutWakeUpCount` on 62, `FireVulnerability` on 61, `EnergyReserveProperties/Reserves` `content="float_array"` on 59), `EnergyReserveConfigs/Item` × 58 (`153` `EnergyReserves/Item` with `{Type, EnergyAmount, Enabled, RechargeRate, RechargeEnabled, RechargeThreshold}`), `EnergyConfig` (54), `StaminaConfig` (8), `EnergyHealthStateMap` (4), `HealthRechargeConfig` (3), `SpecialAbilityConfig` (2) |

In `CEnergyConfigInfos`, **60 of 60** `HealthConfig/Item/EnergyReserveConfig` references resolve case-insensitively to
an `EnergyReserveConfigs/Item` entry in the same document.

## World locations, pathing styles, trains and hazard nodes

Four representative roots in the world/navigation/map category (826,397 B across 15 streams; all 4 well-formed):

- **`GameShape__Data__LocationNameLoopkup` (1 document, 671,393 B, 11 streams):** (root spelling preserves the double
  `o` in `Loopkup`.) Contains four keyed lookup tables (`key=`):
  - `LocationNames/Item` × 2,318 (`Name` on all 2,318; `TextureDictionary` and `Texture` on 46).
  - `InteriorNames/Item` × 1,358, holding `2,280` nested `<Item>` records with `{Name, Pos}`. Their 2,280 3D positions
    span `x = -9,673.0..4,705.3`, `y = -7,308.0..8,589.9`, `z = -78.0..820.9` — 100% inside the world bounds of the
    `PAMH` heightmap and `GridAtlasInfo` grids above.
    span `x = -9,673.0..4,705.3`, `y = -7,308.0..8,589.9`, `z = -78.0..820.9` — their 2D `x`/`y` coordinates fall 100%
    inside the `x`/`y` extent of the `PAMH` heightmap (`-13,312..5,120`, `-9,728..11,264`) and `GridAtlasInfo` grids
    above, while `z` reaches `820.9` (above the `PAMH` `404` m ceiling).
  - `RoadNames/Item` × 409 (`Name` on all 409) and `RiverNames/Item` × 66.
- **`PathingStyleData` (1 document, 5,993 B, 1 stream):** `StyleProfiles/Item` × 12 (`key=`), each defining
  `CurvatureStyle`, `CorneringStyle`, `ApproachStyle`, `ContinuityStyle`, `SlowdownStyle`, `SplineConfig`, and
  `AvoidanceStyle`, plus optional `Flags` (9) and `DoorPathingStyle` (5).
- **`CTrainConfigs` (1 document, 125,494 B, 2 streams):** `trainConfigs/Item` × 46, each carrying `name`,
  `populateTrainDist`, three station stop-duration scalars (`stopAtStationDurationNormal`,
  `stopAtStationDurationPlayer`, `stopAtStationDurationPlayerWithGang`), `announceStations`, `doorsBeep`, and `1..17`
  `carriages/Item` entries (median 8, **314 carriages** total, each with `maxPedsPerCarriage`, `flipModelDir`,
  `doInteriorLights`, `modelName` on 304, and `modelSetName` on 300).
- **`CHazardNodes` (1 document, 23,517 B, 1 stream):** a 20-state hazard state machine (`Nodes/Item` × 20,
  `type="CHazardNode"`, `key=`, `Name`, `ClipSet`, `ClipVictim`, `ClipThreat`, `UseMTH`, `RumbleCurve`,
  `RumbleCurveStart`) linked by `43` `Successes/Item` transitions (`type="CHazardTransition"`, gated by
  `CAIConditionGatorPit` [36], `CAIConditionAmbientEventDirection` [5], `CAIConditionOr` [1], `CAIConditionAnd` [1]).
  All **18 of 18** non-empty transition target `<Node>` references resolve to one of the 20 `CHazardNode` keys.

## No-sharing rule

The archive, every carved `.bin`, `index.json` (its `head`/`pre` fields contain payload bytes), XML
excerpts, entry names, values, paths, screenshots and per-file reports stay on the local machine. Only
aggregate counts, sizes and schema/category names belong in git, issues or pull requests. The unit test uses
synthetic data; CI never reads a game folder. This is the same rule as `AGENTS.md` and `CONTRIBUTING.md`, and
it applies to this note as well.

## Streaming reader (`tools/gamedata_reader.py`, ASSET-139)

`tools/gamedata_reader.py` reads the carved documents with `iterparse` and turns each into plain Python values,
without a schema per tag. Each top-level field is converted when its end tag arrives and then dropped, so memory is
bounded by the largest field. The generic conversion follows the parser's own conventions:

- `value=` is a scalar: bool, decimal or `0x` integer, float (a C `f` suffix allowed), else text.
- `x/y/z/w` is a vector tuple.
- `content=` is the array body. `short_array`, `float_array` and `float16_array` give number lists;
  `vector2_array`, `vec3v_array`, `matrix33` and `matrix34` give tuples.
- `type="Class"` is a polymorphic object `{"$type": ...}`, and `type="NULL"` is `None`.
- `ref=` is `{"$ref": ...}`.
- All-`Item` children are a list, or a dict when every one carries `key=`. A repeated tag is a list. A text-only
  element is its text. Any other attribute is kept as `"@name"`.

`register_root(tag)` and `register_type(name)` plug in a handler where a schema is known. Everything else stays
generic.

```
python -I tools/gamedata_reader.py <work>/common [--root TAG] [--out DIR]   # counts; --out: one JSON per document (local only)
```

On the 1.0.2.1 carve of `common.rpf` the run takes about 5 s, with 0 conversion errors:

| | count |
|---|---:|
| documents read | 259 of the 267 payloads |
| roots | 111 |
| not well-formed (the tail-only and incomplete carves counted above) | 8 |
| scalars | 84,585 |
| vectors | 20,825 |
| arrays | 13,449 |
| lists | 13,465 |
| keyed maps | 526 |
| polymorphic objects | 6,490 (317 `type` classes over 6,903 typed elements) |
| `NULL` pointers | 278 |
| references | 924 |
| elements with extra attributes | 5,262 |

The 7 roots missing from the 118 occur only in documents that are not well-formed. Counts only: the JSON that
`--out` writes is the game's data and stays local. `tests/test_gamedata_reader.py` checks every convention,
both dispatch tables, that streaming equals a whole-tree conversion, and the CLI, on synthetic XML.

## Next step

The most useful next step is the other half of the **schema bridge**: check whether the 118 XML root tags (and the
317 `type` classes) match RTTI class names in the executable, then register typed handlers for them in
`tools/gamedata_reader.py`. That turns the measured XML into the runtime's config layer without hand-writing 5,293
tags. The streaming reader that dispatches on root and `type` exists now (above). The `MHIU`/`PAMH`
heightmaps are decoded by `tools/gamedata_maps.py`; every MHIU header field is identified (0x28 is the
default height), and the row-table layouts and the value-to-Z mapping are measured.
