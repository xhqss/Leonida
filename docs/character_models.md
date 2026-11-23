# Character models and ped parts

Measured from the pinned `e293917` export of `packs.rpf` (`tools/rpf9dump models`): **26,585
manifest rows**, **11,335 unique names**, **23,720 `.#dr` drawable rows** and **2,865 `.#ft`
fragment rows**. One row is one LOD or variant, so these are row counts, not character counts.

No game data is reproduced here.

## A character is a drawable plus a fragment

- A `.#dr` drawable carries the geometry, materials and texture references.
- A `.#ft` fragment carries the shared 597-name humanoid skeleton and the character's bounds. The
  measured skeleton is in [character_skeleton.md](character_skeleton.md).

The current exporter writes every fragment as a **25-vertex bounds cube** with `Embedded: null`
(2,865 of 2,865). Fragment child drawables are not walked, so a part that exists only as a
fragment child is not in the OBJ/GLB output even though its parent fragment is.
The current exporter writes every fragment as a **flat 25-vertex / 32-triangle bounds grid** (a 5x5
grid at z=0, `Min`/`Max` z = 0) with `Embedded: null` (2,865 of 2,865). Fragment child drawables
are not walked, so a part that exists only as a fragment child is not in the OBJ/GLB output even
though its parent fragment is.

## Lucia records

22 unique names contain `lucia` (12 contain `cs_lucia`). They are parts, not one assembled model.
The decoded export used by the previews contains 13 parts:

| Part | Source drawable | Vertices | Triangles | Diffuse key (`0xC8E8C282` only) |
|---|---|---:|---:|---|
| head (hi) | `player_story000_00_head_fr1_lat_000_m.#dr` | 8,983 | 13,320 | `0x8EDB5664DA38C47E` (head-skin preset) |
| head L0 | same | 4,638 | 4,940 | same preset |
| head L2 | same | 599 | 514 | same preset |
| eyes | `…_head_fr1_lat_000_m_eyes.#dr` | 434 | 816 | `0x429453B7A2EB9980` (sRGB + alpha) |
| eyecap | `…_head_fr1_lat_000_m_eyecap.#dr` | 214 | 304 | `0x5CE55BAA340248BD` (sRGB + alpha) |
| hands | `hand_fr1_000_nude_cull.#dr` | 10,440 | 13,534 | none in the package — `0x4D89F2311DBBB7A8` is in no TTPF pool (see [Hands and feet](#hands-and-feet)) |
| feet | `feet_fr1_000_full.#dr` | 2,309 | 4,082 | none in the package — `0xA54D9F8B51CA6034` is in no TTPF pool (see [Hands and feet](#hands-and-feet)) |
| body (legacy) | `cs_lucia_legacypedcs_fr1_000_fullbody_ws00_full.#dr` | 874 | 908 | `0x996FDBA2DB30CC12` |
| legacy head | `cs_lucia_legacyheadcs_fr1_001_regular_ws00_full.#dr` | 592 | 1,028 | `0x707771F769AA3542` |
| jacket (biker) | `cs_lucia_jacketbiker_fr1_000_regular_c_hl_long_full.#dr` | 9,818 | 10,837 | none — `speculartex` only (`0xF5073AA039BB3CED`) |
| crop tee | `player_story000_00_tshirtcrop_fr1_001_slim_cc_un_long_full.#dr` | 4,932 | 6,756 | none — `speculartex` only (`0x46E8FA9436E9D43C`) |
| bikini top | `brabikini_fr1_012_regular_ws00_full.#dr` | 6,547 | 5,697 | `0xDF3615ADFB1F4787` (sRGB + alpha) |
| undies | `undiesgstring_fr1_002_slim_hw_full.#dr` | 961 | 913 | `0xCFD339832A9C724A` (sRGB + alpha) |

### Higher-detail variants

The rows above are the preview build, not the densest rows in the archive. LOD0 counts come from
the manifests; "distinct" is unique vertex positions decoded from a GLB, which removes duplicate
cull seams:

| Component | Denser variant | LOD0 V / T | Distinct |
|---|---|---:|---:|
| jacket | `player_story000_00_jacketbiker_fr1_000_regular_o_hl_long_full` | 11,093 / 12,023 | 9,597 |
| crop tee | `player_story000_00_tshirtcrop_fr1_001_slim_cc_un_long_cull_00` | 12,254 / 15,437 | 4,558 |
| jeans | `player_story000_00_pantsjeans_fr1_002_regular_hw_mid_full` | 4,917 / 5,550 | 2,826 |
| boots | `cs_roxy_bootsankle_fr1_000_mid_hh_full` | 7,764 / 7,960 | - |

The library maximum for jeans is `pantsjeans_fr1_019_slim_mw_long_full` at 23,880 V / 25,230 T;
that is a single 0.41 x 0.26 x 0.95 m mesh, but the raw count includes seams, so validate it
visually. `_cull_00` variants can have more raw vertices and less surface than their `_full`
sibling; compare distinct positions, not the manifest total.

Two caveats that matter for any render:

- **`body` is the shared player/cutscene body.** The only `lucia`-named full-body record in
  `packs.rpf` is `cs_lucia_legacypedcs_fr1_000_fullbody_ws00_full`. Its LOD0 is **874 vertices /
  908 triangles** (468 distinct positions); the 1,748 / 1,816 figure is LOD0 + LOD1 counted
  twice, and the 640-vertex row is LOD2. Its `FragName` is byte-identical to `cs_monalisa` and
  `cs_mandy`, so it is the shared player/cutscene body, not a unique high-detail mesh. Do not
  present it as a high-detail hero body.
- **The head drawable has no material texture references**, but its face comes from a **head-skin
  preset** keyed by the head name's `joaat32` (see Textures).

`player_story000_00` (Lucia's slot prefix) has 104 unique names: **63 drawables** plus **41
`char_bipedbounds` fragments**. The largest drawables:

| Drawable | Vertices | Triangles |
|---|---:|---:|
| `…_hoodiezip_fr1_001_regular_o_hd_long_cull_00.#dr` | 20,311 | 14,256 |
| `…_sweatshirtcrewneck_fr1_000_loose_cc_un_long_cull_00.#dr` | 19,217 | 13,600 |
| `…_hoodiezip_fr1_001_regular_h_hd_long_cull_00.#dr` | 19,141 | 14,106 |
| `…_shirtdress_fr1_000_slim_c_oc_un_long_cull_00.#dr` | 14,341 | 16,146 |
| `…_tshirtcrop_fr1_001_slim_cc_un_long_cull_00.#dr` | 12,254 | 15,437 |
| `…_jacketbiker_fr1_000_regular_o_hl_long_full.#dr` | 11,093 | 12,023 |

## Slot tokens

These are counts of unique names containing the token. They are a naming heuristic, not the game's
component table.

| Token | Unique names | Note |
|---|---:|---|
| `player_story` | 196 | Lucia (`000_00`) and Jason (`000_01`) |
| `head` | 3,741 | heads and head attachments across all characters |
| `hair` | 278 | includes accessories and non-human fur |
| `belt` | 44 | accessories |
| `pants` | 459 | `pantsjeans`, `pantscargo`, `pantsleggings`, `pantstrack`, … |
| `jeans` | 157 | `pantsjeans` / `shortsjeans` |
| `jacket` | 327 | `jacketbiker`, `jacketdenim`, `jacketblazer`, `jackettrack`, … |
| `uppr` | 17 | 14 are `slod_uppr_*`; 3 are `char_bipedbounds_female_armor…_uppr_l/r.#ft` |
| `lowr` | 21 | 17 are `slod_lowr_*`; 4 are `char_bipedbounds_female_armor…_lowr_l/r.#ft` |
| `jbib` | 0 | the GTA V slot code is not a name token here |
| `hands` | 8 | the singular `hand_` appears in 35 names |
| `feet` | 26 | bare feet plus creature variants |
| `bikini` | 45 | `brabikini_*` and related |
| `undies` | 102 | `undiesgstring`, `undiesbriefs`, `undiesboxers`, … |

## Textures

The character export requested 215 texture keys and matched **184**. The only sRGB albedo slot is
`0xC8E8C282` (`diffusetex`); `0xC6F46E79` is `speculartex` (specular/gloss pack), `0x1E77BF4C` is
`bumptex` (the tangent-space normal), `0x739E3D0D` is `speculartex2` (packed ORM), and
`0x8ADB8480` is `materialid` (an ID/palette map). The jacket's and the crop tee's material rows
reference one (`0x6CD70ED1054EF726`, `0xF0722B975F329141`, both `BC4_UNORM` textures in the
`ttdb_peds` pools); the bikini top's and the undies' rows have no such slot. Whether a shader
samples it is not measured. The garment
shaders (`d5ed50f4`, `ea8d09d0`, `39ddb9ca`) do not declare a `0xC8E8C282` slot at all: none of
their 7,948 material rows in the export has one. The jacket and the crop tee are on them, so they
have no albedo. A part without one keeps a flat colour; it must not
be painted with `speculartex` or an ORM map. The garments' `_COLOR0` vertex data is baked shading
(alpha 255 on every vertex), not a tint, and the shader
declares no tint or palette slot. No authored base colour exists to recover for those two.

The bikini top and the undies are not on the garment shaders, and they do have an albedo. All
three records of each drawable bind `0xC8E8C282` to the key in the table: the bikini top on
shaders `84af5c61` and `81153bcc` (`76c45dd8` and `428b44d6` in its smallest record), the undies
on `a9dfb1e7` (`428b44d6` in its smallest record). Every material row on those five shaders in
the export binds the slot (1,372 of 1,372). Both keys are 512x256 `BC3_UNORM_SRGB` textures with
7 mips in the `ttdb_peds` pools: an sRGB format with an alpha channel, as the eyes' and eyecap's
`BC7_UNORM_SRGB` keys are. Their `speculartex` keys are
other textures (`0x93CB433D30220BA6` and `0x5D237CCCD91F8E4D`, 256x128 `BC7_UNORM`), where the
jacket's and the crop tee's table keys are `BC7_UNORM` `speculartex` sheets themselves.

The hands and feet bind an albedo key that is not in the package (see [Hands and feet](#hands-and-feet)).
The head drawable has no material texture reference, but its face is selected by
a **head-skin preset** keyed by the head name's `joaat32`:

| Head | Face albedo | Face normal |
|---|---|---|
| `player_story000_00_head_fr1_lat_000_m` | `0x8EDB5664DA38C47E` | `0x953715153D75447C` |
| `cs_monalisa_head_fr1_afr_000_m_full` | `0x53A2896F4E5A6BA6` | `0xE82C7F6E02C8B701` |

The preset library covers 897 heads; `cs_lucia_head_fr1_lat_000_y_full` is not in it.

### Hands and feet

Measured against the catalogs of all 57 TTPF pools (357,115 textures). The hands and the feet bind
the four maps on the same two shaders (`210FBC6A`, `8D69EDAB`), and **only the normal maps are in
the package**:

| Part | `diffusetex` | `speculartex` | `bumptex` (normal) | `speculartex2` (ORM) |
|---|---|---|---|---|
| hands (`hand_fr1_000_*`) | `0x4D89F2311DBBB7A8` — none | `0xA805B9A446CCE950` — none | `0x9D337DABCA636906` — `ttdb_peds_2_3`, 512×1024 BC5 | `0x09D2C98C5B40D16C` — none |
| feet (`feet_fr1_000_*`) | `0xA54D9F8B51CA6034` — none | `0x752EA8A7A086558F` — none | `0x5091E86DE6196BC3` — `ttdb_peds_3_3`, 512×1024 BC7 | `0xD5C8D2A180326DE7` — none |

Each material has ten slots; the other six bind `0x5D7A3A87CC19967D` on both shaders, which is in
no pool either: it reads as an engine-supplied default, not a missing asset.

**They are not textured on their own.** The hands and feet share their maps with the body skin
pieces in `pack/core_restofworld/core_restofworld.rpf`:

- `bodynipplesgen_fr1_000_regular_ws00_full`, the female upper body, binds **the same four keys**
  as the hands, on the same two shaders. The hands are drawn with the upper-body skin sheet.
- `bodygenitalsgen_f{r,b,o}1_008_regular_ws00_full`, the female lower body (and the `fr1`
  `_alt_00`–`_02` variants), binds the feet's
  `speculartex`, `bumptex` and `speculartex2`, and its own `diffusetex` `0x5B6829CFEC0F41A1`, also in
  no pool.
- The male lower body, `bodygenitalsgen_mr1_000_*`, has all four maps in the pools
  (`diffusetex` `0xBCE4D91DA23689C7`).

So the female body skin has no albedo, specular or ORM map in the package, for the hands and feet
and for the body pieces that share their sheets. The skin colour has to come from data outside these
materials, as the face comes from the head-skin preset. That data was not found: none of the keys
above appears in another resource of `data.rpf`, `common.rpf`, `script.rpf`, `packs.rpf`,
`anim.rpf`, `core_restofworld.rpf` or `core_restofworld001.rpf` (`story000.rpf` and
`story000_restofworld.rpf` recover no entries with this reader).

The pattern holds across the set. Of the 56 `hand_`/`feet_` drawables in `packs.rpf`, 52 bind only a
`diffusetex` key that is in no pool; the four exceptions with all four maps shipped are
`hand_fsk1_000_nude_cull`, `feet_fsk1_000_nude_cull` and the first-person hands
`hand_mb1_000_nude_cull_fp` and `hand_mo1_000_nude_cull_fp`. The male hands and feet
(`hand_mr1_000_nude_cull`, `feet_mr1_000_nude_cull`) also ship only their normal maps, and male and
female feet bind the same `diffusetex` key. Until the skin data is decoded, render these parts with
their normal map and a flat skin tone; do not borrow another part's albedo.

## Materials

A binding audit of 120 exported scenes (232 material rows) found 35 rows with a real `C8E8C282`
albedo and 174 rows with `C6F46E79` but no `C8E8C282`, all on garment shaders. The four rows that
produced teal previews were the specular sheet used as the base colour, not a normal map. Those
parts have **no albedo**, so a preview must render them as clay rather than invent a colour.

- **The belt is not a separate part.** It is 172 connected components / 2,229 vertices inside the
  jeans mesh, with no node, material or texture key; it cannot be separated without editing the
  geometry.
- **UVs above 1 are wraps, not atlas pages.** The blazer reaches U=2.0014 (1,628 vertices); after
  subtracting 1, 90.9% of those vertices are within 1e-4 of a page-0 vertex. The jeans reach
  U=-0.9936, a -1 mirror.
- **The body atlas is a shared collage.** `996FDBA2DB30CC12` is 256x256 and contains several ped
  variants (faces, a tropical shirt, blue jeans, dark cloth). Only 43.7% of the sheet is covered
  by the body's islands, so bilinear sampling at an island edge can read a neighbouring variant.
  The blue in an earlier preview came from the authored `baseColorFactor` `(0.200, 0.270, 0.400)`,
  not from this atlas.

## Bodies

A geometry-first census — every `packs.rpf` drawable and fragment, all 50,802 `levels.rpf`
fragments, 178,073 of 184,778 `levels.rpf` drawables (96.4%; the 6,705 above 3 MB were not
decoded), all 4,195 `story000.rpf` drawables, and the `anim.rpf` rig dictionaries — found no
Lucia or Mandy body. `story000.rpf` is mission map cells, not ped actors: its full read finds
4,195 drawables, 177,811 meshes, **0 skeletons, 0 fragments and 0 body-like names**, 28.5 M
vertices of unrigged 64 m cells. `anim.rpf`'s `LOD0_Body_Shapes_Female` is a rig program with no
mesh.

A raw RSC8 scan of **862,555** resources across all 13 indexed RPF9 archives (every `rageobj`
entry, unnamed pages and the >3 MiB band included; 25.6 GB compressed / 56.3 GB inflated) found
453 ped-skeleton resources: 342 animal bounds, 2,212 `char_bipedbounds_*` fragments, 4 mannequins
and 73 anim clips. **0** combined a ped skeleton with the skin/flesh shader, and 0 of the 908
resources on that shader contained a single `SKEL_*` bone name. The unnamed-resource route
therefore does not add a body.
2,631 ped-skeleton resources: 342 animal bounds, 2,212 `char_bipedbounds_*` fragments,
4 mannequins and 73 anim clips. **0** combined a ped skeleton with the skin/flesh shader, and
0 of the 908 resources that use the skin/flesh shader contained a single `SKEL_*` bone name.
The unnamed-resource route therefore does not add a body.

The only **nude** continuous human-skeleton meshes are a **generic female mannequin** in
`levels.rpf`: `gmt_mannequin_proxy_f_01a` (15,488 vertices / 27,725 triangles, 95 bones,
1.72 m, A-pose) and `gmt_mannequin_f_01b` (the same body posed mid-stride). It has no Lucia or
Mandy name or texture link, and its rig is a clothing form, not the 596-bone ped skeleton. 78 of
95 bone names are shared, and all 71 weighted bones match; its pivots are a rigid 180° yaw plus a
31.7 mm drop, not a mirror, so no L/R weight swap is needed. A 39.59° upper-arm correction lands
the hand on the ped pivot. It has no skin albedo — only a flat tint swatch atlas — so it is an
untextured base proxy for a reconstruction, not Lucia's body.

A separate bare female mesh exists only as a **scene monster** in `packs.rpf`:
`sc_fast_demon_bodyuppergen_fr1_000_m_full` (10,048 v / 13,124 t) and
`sc_fast_demon_bodylowergen_fr1_000_m_full` (3,717 v / 4,082 t), shader `0x210FBC6A`, with no
garment parts. It is a monster asset, not a player or ped body. The `bodyupper`/`bodylower`
families confirm the negative: all 18 rows are human male (`cs_farmm`, `cs_mister_mangrove`,
`u_m_m_farm_monster_man`) or monsters (`sc_*`), none female. The `hc_stripper_delights`,
`hc_stripper_joh` and `hc_stripper_nine1nine` characters ship heads and eyes only, with no body.

The player and cutscene body is a separate shared **clothed** fragment,
`cs_lucia_legacypedcs_fr1_000_fullbody_ws00_full` (874 v), used by `cs_lucia`, `cs_monalisa`
and `cs_mandy` (their `FragName` is identical). It is the body the game uses; it is low-poly and
clothed, so it is not a high-detail nude body. A flat-skin render of the 874-v body with its
normal map disconnected still shows the modelled top, shorts and ankle boots, so the clothing
silhouette is geometry, not a painted-on atlas.

## What is not decoded

- **Fragment children.** A raw child-array walk (`tools/fragment_children.py`) covers every fragment
  of `packs.rpf` and `levels.rpf`. The fragment header's u16 at `+0x30` is the number of drawables;
  the records are 0x110 bytes apart, starting at the main drawable that `+0x18` points at, so
  records 1 and up are the child drawables. All 2,865 `packs.rpf` fragments have a single drawable
  and the flat 25-vertex / 32-triangle bounds grid, with **no child arrays**. The four
  `gmt_mannequin*` fragments have no child arrays either; `ambient_femalebody` is a 596-bone
  skeleton plus the same grid, with no skin mesh. The path is alive on world fragments
  (`bus2_frag_hi`: 14 model structs, 11 children, a 650 KB `model_parts.glb`), so the negative is
  a property of the character files; those figures come from a different walk and were not
  re-derived by this one. In `levels.rpf` the walk finds **50,802 fragments** (the exporter's
  manifest counts 50,798): 50,068 have a single drawable and **734 have a child array**, 733 of
  one child and 1 of two, **735 child drawables** in all. Every declared child record passes the
  drawable-body test, and a scan of all other 16-byte offsets of those fragments' system pages
  finds 0 further drawables. 10,097 of the 50,802 have a null `+0x10`, `+0x20` or `+0x28`, and 6
  have a main drawable that fails the embedded-drawable header test. For comparison `data.rpf` has
  1,758 fragments, all single-drawable. The other header counts are unrelated to children: the
  `+0x28` array of 0x50-byte part records (the u16 at `+0x34`) has 153,028 records over
  `levels.rpf` (13,871 fragments with more than one) and 27,778 over `packs.rpf` (2,605). The gap
  is closed: every fragment in both archives was walked, and the only ones with child drawables
  are 734 of the `levels.rpf` ones.
- **A ped-variation table.** `common.rpf` contains `CPedVariationBodyTypeManager`
  (`0x226000`) and `CPedComponentSetManager` (`0x228000`), but neither maps a component variation
  index to a drawable plus texture. The first is height and body-shape presets
  (`SCALE_PLAYER_STORY000_00` is 1.62 m); the second lists gameplay components. Slot selection
  from names is a heuristic, not the game's rule.
- **Lucia's hair.** The `packs.rpf` manifest has three `player_story000_00` hair rows
  (`hairbob`, `hairpixie`, `hairponytail`), each **3 vertices / 1 triangle**; `hairbuzzcut`
  appears only in male `_mr1` rows. All 54 non-SLOD rows of those four styles are 3 v / 1 t.
  Across `packs.rpf`, 219 of the 267 measurable hair rows are <=3 v / 1 t. The 48 larger rows
  are 29 accessories (`hairaccsband`, `hairaccsclip`, `hairaccessoriescuff`; up to 7,251 v), 10
  SLOD proxies (71-147 v), 3 fused `cs_wyman_head_mr1_cau_000_o_hair_alt0` rows (up to 13,529 v),
  3 `hairbun` and 1 `hairlob` unit-box stubs (24 v / 12 t), and 2 blowfly
  `cri_fly_blow_m_00_hair_00` rows (1,436 / 2,040 v); no stand-alone full-resolution female hair
  mesh exists. `levels.rpf` and `story000.rpf` have zero character hair rows. A static Lucia
  export is bald by design; a render needs a clearly labelled placeholder, not a missing decode.
- **`levels.rpf`.** The full archive was name-enumerated: 1,116,073 streams, 98.32% inflated,
  235,814 named streams, 318,266 unique names. It contains no Lucia, Mandy, `player_story` or
  `ambient_female` drawable; the Lucia and Mandy hits are props (photos, frames, luggage, a
  curtain). The only human-skeleton meshes in `levels.rpf` are the generic mannequin pair in
  **Bodies**.
- **Bind pose and skinning.** The delivered GLBs are static: they contain **0 skins, 0 `JOINTS_0`
  and 0 `WEIGHTS_0`**, so there are no inverse-bind matrices to apply. The reference TRS is the
  only exported pose, and the meshes are not skinned to the 596-bone ped skeleton. A rigged export
  is still open; this is a data omission, not identity inverse-bind matrices.
- **Bind pose and full skinning.** The `rpf9dump` GLBs use identity inverse-bind matrices and do
  not apply the 8-influence bone-palette skinning. The inverse bind matrices are at skeleton header
  `+0x30` (the drawable's `+0x18` points at the header) and agree with the default bone transforms;
  `tools/anim_glb.py` exports a part with them as a glTF skin, with clips (see
  [character_skeleton.md](character_skeleton.md#animated-gltf-export)).

## Reproduce

```powershell
rpf9dump models <packs.rpf> <outdir>
python -I tools/fragment_children.py <package>\levels.rpf --jobs 12 --json <work>\levels_fragments.json
```

The second command prints the fragment and child-drawable totals quoted under "Fragment children"; it
reads the archive in place (about 22 minutes for `levels.rpf` with 12 workers over a WSL mount of
the dump) and writes nothing else.

```python
import json

items = json.load(open(r"<outdir>/packs/models/manifest.json", encoding="utf-8"))["Items"]
names = sorted({i["Name"] for i in items})
print(len(items), len(names))
print(sum(1 for i in items if i.get("IsFragment")), "fragments")
print([n for n in names if "lucia" in n.lower()])
```

```python
import json

items = json.load(open(r"<outdir>/packs/models/manifest.json", encoding="utf-8"))["Items"]
names = sorted({i["Name"] for i in items})
print(len(items), len(names))
print(sum(1 for i in items if i.get("IsFragment")), "fragments")
print([n for n in names if "lucia" in n.lower()])
```

The material rows are in the same manifest, one per shader of a record, each with the texture
references by slot:

```python
import json

items = json.load(open(r"<outdir>/packs/models/manifest.json", encoding="utf-8"))["Items"]
for item in items:
    if item["Name"] == "brabikini_fr1_012_regular_ws00_full.#dr":
        for material in item["Materials"]:
            print(material["Shader"], {t["Param"]: t["Id64"] for t in material["Textures"]})
```

A key's size and format come from the pool that holds it: the `AssetKey`, `Width`, `Height`,
`DxgiFormat` and `Mips` fields of the manifest `rpf9dump ttpf <pool.ttpf> <outdir>` writes.

The [Hands and feet](#hands-and-feet) tables also need `core_restofworld.rpf` and the 57 pool
catalogs (`AssetKey` in each pool's `manifest.json`):

```powershell
rpf9dump models <pack\core_restofworld\core_restofworld.rpf> <outdir>
rpf9dump ttpf <each .ttpf under data\ttdb and pack\*\data\ttdb> <outdir>\ttpf\<pool>
```

```python
import glob, json

maps = {"C8E8C282": "diffusetex", "C6F46E79": "speculartex", "1E77BF4C": "bumptex", "739E3D0D": "speculartex2"}
pooled = {t["AssetKey"] for m in glob.glob(r"<outdir>/ttpf/*/manifest.json")
          for t in json.load(open(m, encoding="utf-8"))["Textures"]}
items = [i for m in (r"<outdir>/packs/models/manifest.json", r"<outdir>/core_restofworld/models/manifest.json")
         for i in json.load(open(m, encoding="utf-8"))["Items"]]
for name in ("hand_fr1_000_nude_cull.#dr", "feet_fr1_000_full.#dr", "bodynipplesgen_fr1_000_regular_ws00_full.#dr",
             "bodygenitalsgen_fr1_008_regular_ws00_full.#dr", "bodygenitalsgen_mr1_000_regular_ws00_full.#dr"):
    keys = {maps[t["Param"]]: t["Id64"] for i in items if i["Name"] == name
            for mat in i["Materials"] for t in mat["Textures"] if t["Param"] in maps}
    print(name, {k: (v, v in pooled) for k, v in keys.items()})
```

The fragment and skinning fields are in the same manifest; the skeleton measurements are in
[character_skeleton.md](character_skeleton.md), and the world model export is in
[models.md](models.md).
