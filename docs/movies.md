# Movies (`movies.rpf`)

**Status: indexed. All 515 Bink 2 streams have been enumerated and their fixed headers read; the first two frames
of every stream, and the start of every audio track, decode with the package's own Bink 2 library. The RPF9 table
of contents is encrypted, but the streams can be named from the game's mount lists
([section 8](#8-names-from-the-mount-lists)). Nothing here is a playback path yet.**

Every number below was measured on the Xbox Series 1.0.2.1 package on 2026-11-21. The archive is read-only. No
game video, audio, frames, carved streams or reports that quote them are in this repository; the tools write only
offsets and header facts. Commands are at the end.

## 1. What the package ships

| Fact | Measured |
|---|---|
| `movies.rpf` size | 8,832,481,776 bytes |
| Bink 2 streams (`KB2[a-z]`) | **515** |
| Bytes occupied by those streams | **8,831,336,892** (99.99% of the archive) |
| Data start in the RPF header (word at +4) | 27,328 (0x6AC0) |
| First stream offset | 28,672 (0x7000) |
| Zero padding between streams | 1,094,744 bytes (includes 1,344 bytes before the first stream) |
| Other bytes after the last stream | 22,812; not a Bink stream and not zero (purpose unknown) |
| Other carved file types | none; the whole data area is Bink 2 |
| Revisions | 505 `KB2n`, 10 `KB2j` |
| Frame rate | 30/1 in all 515 headers |
| Frame counts | 15 to 16,015; **425,440** frames total |
| Total duration at 30 fps | 14,181.3 s (3 h 56 min) |
| Audio tracks (header field) | 319 streams with 0, 165 with 1, 30 with 2, 1 with 4; 229 tracks total |
| Decoded samples | 9 streams, 14 frames, both revisions, 512x512 to 3840x2160 |
| Decode test over the archive | 515 of 515 streams: frames 0 and 1; 229 of 229 audio tracks: PCM for the first 60 frames ([below](#decode-coverage-over-the-whole-archive)) |
| ffprobe decode result | 0 of 515 (`decodes=no` with a forced `-f bink` probe) |

`8,831,336,892 + 1,094,744 + 22,812 = 8,832,454,448`, the span from the RPF header's data start to EOF. Readable
coverage is 100.0%; the 27,328-byte RPF header (before the data start) and the 22,812-byte tail are the only
unindexed ranges.

`KB2n` is the newer of the two revisions. One DLL string mentions Bink 2.2, but the revision-to-version mapping is
not independently established.

### Resolution distribution (header width x height)

| Resolution | Streams | | Resolution | Streams |
|---|---:|---|---|---:|
| 1280x720 | 246 | | 2048x468 | 1 |
| 1920x1080 | 130 | | 2048x1024 | 1 |
| 720x1280 | 67 | | 512x384 | 1 |
| 512x512 | 23 | | 1407x1600 | 1 |
| 1024x1024 | 19 | | 1635x720 | 1 |
| 2560x1440 | 14 | | 1920x850 | 1 |
| 512x256 | 2 | | 1024x768 | 1 |
| 2048x558 | 2 | | 3840x2160 | 1 |
| 2048x512 | 2 | | 1080x1080 | 1 |
| 1024x512 | 1 | | | |

There are 19 distinct sizes. Square and small streams (512x512, 1024x1024, 512x256) may be animated textures or
UI footage rather than cutscenes; that is a guess, not verified. The 10 `KB2j` streams are nine 512x512, 15-frame
(0.5 s) files of three slightly different sizes, plus one 1920x1080, 60-frame (2 s) file. Their similarity suggests
splash/logo variants, but nothing confirms it.

## 2. Bink 2 container facts that were verified

All 515 headers use the same fixed layout for the first 44 bytes (0x00-0x2B). The names in the table follow the
FFmpeg Bink demuxer's reading order; values in the last column are what this archive contains. The tool reads only
these fields plus the audio descriptors and never trusts a size past the stored stream length.

| Offset | Size | Meaning | Observed |
|---:|---:|---|---|
| 0x00 | 4 | `KB2` + revision letter | `KB2n` (505), `KB2j` (10) |
| 0x04 | u32 | stored stream size minus 8 | `stored = field + 8`; matches the carve exactly for all 515 |
| 0x08 | u32 | frame count | 15 to 16,015 |
| 0x0C | u32 | largest frame size | at most `stored - 8` |
| 0x10 | u32 | frame count again | equals 0x08 in all 515 |
| 0x14 | u32 | width | 512 to 3840 |
| 0x18 | u32 | height | 256 to 2160 |
| 0x1C | u32 | fps numerator | 30 in all 515 |
| 0x20 | u32 | fps denominator | 1 in all 515 |
| 0x24 | u32 | opaque; FFmpeg reads it as codec extradata | 0x00040013 common; 0x00100012 on the 512x512 `KB2j` files, 0x00000012 on the 1920x1080 `KB2j` |
| 0x28 | u32 | audio track count | 0, 1, 2, 4 |
| 0x2C | u32 | opaque; present on revisions i/j/k/n | not interpreted (`0x87E9xxxx` variants on `KB2n`, `0x02070D00` on `KB2j`) |

Only the first 0x2C bytes are fixed. When the audio count is non-zero, the next region is:

- `count` u32 values: maximum decoded size per audio track (for example 0x23A00 = 146,432).
- `count` pairs of u16: sample rate and flags. Every readable track in this archive is 48,000 Hz.
- `count` u32 values: track IDs (0 and 1 in the two-track files).
- then the frame index: `frame_count` u32 offsets. FFmpeg treats the low bit of each offset as the keyframe flag.

The audio flag bits verified against FFmpeg's demuxer are `0x4000` (prefer 16-bit), `0x2000` (stereo) and `0x1000`
(DCT). 227 tracks have `0x7000` (16-bit, stereo, DCT); 2 tracks have `0x3000` (stereo, DCT, no 16-bit preference).
All 229 are therefore Bink Audio DCT, stereo, 48 kHz by the header's own description. That does not prove the
decoder output format or that every track is used by the game.

The frame index is not read by `bink_index.py`: its length depends on the frame count, and the index is not needed
to inventory the stream. `rpf9_carve.py` advances by the stored size instead, which is why it can carve without
parsing the index.

## 3. How the streams are found

`tools/rpf9_carve.py` treats the data area as zlib streams or Bink 2 streams laid end to end. It:

1. Starts at the u32 at archive offset +4 (27,328 here; the first stream begins 1,344 bytes later).
2. Searches for the zlib header bytes or `KB2[a-z]`.
3. For a Bink candidate, reads the size at +4 (`stored = size + 8`), the frame count at +8 and width/height at
   +0x14, and rejects anything outside 64 bytes..EOF or 16..8192 pixels. Those checks keep random `KB2` bytes in a
   payload from becoming a stream.
4. On a valid stream, records the offset and advances by the stored size. Gaps that are all zero are counted as
   padding; any other gap is counted as unread.
5. `--index-only` performs steps 1-4 without writing the streams.

`tools/bink_index.py` is the stdlib-only indexer added with this document. It scans an RPF, a single `.bk2`, or a
directory, applies the same signature and size checks plus header sanity (duplicate frame count, fps, audio count),
reads the 44-byte header and audio descriptors, and writes CSV or JSON. It never extracts unless `--probe` is used,
and then only to a temporary file that is deleted.

On this archive both tools find the same 515 streams, the same 8,831,336,892 bytes, 1,094,744 bytes of zero
padding and 22,812 unread bytes. Running `bink_index.py` on the 515 carved `.bk2` files finds 515 streams with zero
unread bytes, which confirms the stored sizes cover each file exactly.

## 4. Decoding

### FFmpeg is not a Bink 2 decoder here

Three FFmpeg builds on the test machine were checked: `N-127054-g9d3f0f2c58` (2026-10-01), `n8.0`, and `7.0`.
The two that include Bink support expose `binkvideo` (Bink 1) and the two Bink Audio decoders; the 7.0 build
includes neither. None exposes a Bink 2 video decoder. FFmpeg's Bink demuxer explicitly sets the codec to none for
a `KB2` tag ("Bink 2 video is not implemented"). Measured results:

- `ffmpeg -i <stream>.bk2` fails with "Invalid data found when processing input" for `KB2n`; for `KB2j` the
  demuxer may parse the header but there is no decoder, so no frame is produced.
- `ffprobe -f bink` parses the fixed header even for `KB2n` (forced demuxing skips the probe's revision list) and
  reports a duration for 206 of 515 streams, but `codec_name` is empty and `decodes` is `no` for all 515.
- Without `-f bink`, generic probing misidentified one 168,505,620-byte stream as MPEG-4 (the cause was not
  investigated). `bink_index.py --probe` therefore forces `-f bink`; the `decodes` column means "ffprobe found a
  video codec", not "the stream was visually checked".

So the task's working assumption that the installed FFmpeg can decode Bink 2 does not hold on this machine. The
decode evidence below comes from the package's own library, not FFmpeg.

### The package's Bink 2 library

`Bink2Scarlett.dll` is 255,488 bytes, x86-64, GUI subsystem, with 74 named exports at ordinals 0-73 (ordinal
equals index). It has no separate `BinkOpenAudio`; audio is track-based (`BinkOpenTrack`, `BinkGetTrackType`,
`BinkGetTrackData`, `BinkSetSoundTrack`, `BinkSetSoundSystem`). The full export list is in Appendix A.

Facts from the PE and strings:

- Imports include `xmem.dll!XMemAlloc` and `xmem.dll!XMemFree`, `XAudio2_9.dll` ordinal 1, VCRUNTIME140 and
  Windows API sets. There is no `xmem.dll` on Windows, so `LoadLibrary` on the DLL as shipped fails with a missing
  dependency.
- Build string: `_xbld_edition_sdktype=GXDK,220604` (GDK build dated 2022-06-04). PE timestamp 2023-02-08
  06:36:22 UTC. PDB path `C:\devel\projects\bink\build\Bink2Scarlett.pdb`. No semantic version string such as
  "2.7" was found.
- Strings include "BinkGPU is only supported for Bink 2.2 and later files.", "Not a Bink file.", "Error reading
  Bink header." and the async-decompression and subtitle messages.
- Exports include `BinkOpen`, `BinkOpenWithOptions`, `BinkClose`, `BinkDoFrame`, `BinkDoFrameAsync`,
  `BinkDoFrameAsyncWait`, `BinkNextFrame`, `BinkCopyToBuffer`, `BinkCopyToBufferRect`, `BinkWait`, `BinkGoto`,
  `BinkGetSummary`, `BinkGetRects`, `BinkGetTrackType`, `BinkGetTrackData`, `BinkOpenTrack`, `BinkCloseTrack`,
  `BinkSetSoundSystem`, `BinkSetSoundSystem2`, `BinkOpenXAudio2`, the subtitle calls and the async/thread calls.

### Decode evidence

For evidence only, a copy of the DLL was made in temp and its `xmem.dll` import was redirected to
`msvcrt.dll!malloc`/`free` (x64 ignores the extra Xbox attribute argument). The copy is not committed and is not a
build dependency. With that copy:

- `BinkOpen(path, 0)` returned a non-null handle for every tested stream.
- `BinkDoFrame` followed by `BinkCopyToBuffer(..., BINKSURFACE32)` wrote a non-empty BGRA buffer for every test.
  Nine streams and fourteen frames were decoded, covering both revisions: `KB2j` at 512x512 (3 frames) and
  1920x1080 (1 frame), and `KB2n` at 1280x720 (3 frames), 2560x1440, 2048x558, 1920x1080, 1407x1600, 720x1280
  and 3840x2160 (2 frames: 0 and 200). Four of the resulting images (512x512, 1280x720, 2048x558 and the 4K
  frame 200) were viewed and are coherent pictures; the others were not viewed, so their content is not claimed.
  Everything stayed in temp; none is in the repository or PR.
- `BinkGoto(handle, 200, 0)` followed by `BinkDoFrame` produced the 4K frame above, so absolute frame seeking
  works at least in this path. The `BinkGoto` flag meaning was not otherwise tested.
- `BinkDoFrame` and `BinkCopyToBuffer` returned 0 even when they produced a correct frame. The return semantics of
  this GDK build are therefore not established; a caller must not treat 0 as failure without checking the pixels.
- `BinkGetSummary` with a guessed `BINKSUMMARY` layout crashed with an access violation. The `HBINK`,
  `BINKSUMMARY`, `BINKTRACK` and related struct layouts are not established.

### Decode coverage over the whole archive

The two package-gated runtime tests were run on every stream of `movies.rpf`, one at a time: each stream is copied
from its `bink_index.py` offset and size to one temp file, the test runs on it, and the file is overwritten by the
next. Both tests map the package's `Bink2Scarlett.dll` with `rt::Loader` and bind its two `xmem.dll` imports to the
runtime's shim, so no patched copy of the DLL is involved.

| Test | Scope | Result |
|---|---|---|
| `bink_decode_test` | all 515 streams | 515 exit 0: `BinkOpen` returns a handle, and frames 0 and 1 each replace the sentinel buffer with more than one byte value |
| `bink_audio_test`, `LEONIDA_AUDIO_FRAMES=60` | all 229 tracks of the 196 streams that have audio (`LEONIDA_AUDIO_TRACK` 0..n-1) | 229 `RESULT PASS`, 0 `FAIL` lines: track id, type and maximum size match the header, `BinkOpenTrack` returns a track, and `BinkGetTrackData` returns a non-empty, whole number of 16-bit samples on every track |

Measured at `main` `16282101` and again at `f847748c`, with the same counts, on Windows 11 (MSVC 14.44,
RelWithDebInfo). This is a decode check of the start of
each stream. It does not decode every frame, compare pixels with a reference, or look at the frames (none was
viewed), so it says the library accepts all 515 streams and all 229 tracks, not that every frame is correct.

The nine sampled streams (all at 30 fps):

| Revision | Resolution | Frames | Duration | Audio tracks |
|---|---|---:|---:|---:|
| `KB2j` | 512x512 | 15 | 0.5 s | 0 |
| `KB2j` | 1920x1080 | 60 | 2.0 s | 0 |
| `KB2n` | 1280x720 | 40 | 1.333 s | 0 |
| `KB2n` | 2560x1440 | 115 | 3.833 s | 0 |
| `KB2n` | 2048x558 | 360 | 12.0 s | 1 |
| `KB2n` | 1920x1080 | 150 | 5.0 s | 0 |
| `KB2n` | 1407x1600 | 2395 | 79.833 s | 1 |
| `KB2n` | 720x1280 | 150 | 5.0 s | 1 |
| `KB2n` | 3840x2160 | 450 | 15.0 s | 1 |

To turn a decoded BGRA buffer into a viewable image without a Bink 2 decoder:

```sh
ffmpeg -f rawvideo -pix_fmt bgra -s 1280x720 -i frame.raw -frames:v 1 frame.png
```

The raw buffer and PNG stay in temp. FFmpeg is only the raw-to-PNG step here; it never sees the `.bk2`.

## 5. What is still unknown

- **Roles.** The RPF9 table of contents is encrypted, but `tools/bink_names.py` recovers each stream's path from
  the mount lists ([section 8](#8-names-from-the-mount-lists)), and the folder names group the streams by use. What
  the game does with each folder at run time is not established.
- **Audio association.** The header gives a track count, sample rate, flags and track IDs, but not which audio
  belongs to which video frame beyond the interleaving described by the Bink format. `BinkOpenTrack` and
  `BinkGetTrackData` return 16-bit PCM for all 229 tracks (`runtime/test/bink_audio_test.md`,
  [coverage above](#decode-coverage-over-the-whole-archive)), but no track was matched to a language or a mix.
- **Playback integration.** The executable is not in the package. Where the game calls `BinkOpen`, which flags it
  passes, whether it uses `BinkDoFrameAsync`, `BinkGoto`, subtitles or the GPU path, and how it schedules movies
  against streaming are all unknown.
- **Bink2Scarlett ABI.** The struct layouts, the real return values of `BinkDoFrame`/`BinkCopyToBuffer`, the
  `BinkGetSummary` layout, the async API, the GPU API and the XAudio2 setup are not established. The DLL loads on
  Windows only after the missing `xmem.dll` imports are supplied; a proper port needs an allocator shim with the
  Xbox semantics, not necessarily `malloc`.
- **Why 22,812 bytes at the end are unread.** They are not a `KB2` stream and not zero. They may be an RPF footer
  or leftover table data; the encrypted TOC prevents a name-level answer.
- **Whether every frame of all 515 streams decodes correctly.** All 515 open and decode their first two frames
  through the package's DLL ([coverage above](#decode-coverage-over-the-whole-archive)); nine were decoded as a
  sample with frames viewed. No stream was decoded from its first frame to its last, and nothing compares the
  output with a reference.

## 6. No-sharing rule

The repository and every PR contain **no** `.bk2`, no decoded frame, no audio, no extracted movie, and no log or
report that quotes frame pixels. The index files contain offsets, sizes, revisions, dimensions, frame counts, fps
and audio header flags only. Keep carved streams and decoded frames in your own temp/work folder. The project's
[CONTRIBUTING.md](../CONTRIBUTING.md) rule applies unchanged.

## 7. Reproduce

```sh
# 1. Index-only carve of the archive (no stream written)
python -I tools/rpf9_carve.py <package>/movies.rpf <temp>/movies-index --index-only

# 2. Stdlib index: CSV from the archive, or JSON from a carved directory
python -I tools/bink_index.py <package>/movies.rpf --out <temp>/movies.csv
python -I tools/bink_index.py <temp>/movies --out <temp>/movies.json --format json

# 3. Optional ffprobe columns (forced Bink demuxer; expects "no" for Bink 2 today)
python -I tools/bink_index.py <temp>/movies --out <temp>/movies-probe.csv --probe
```

The measured run produced 515 rows, 8,831,336,892 stream bytes, 1,094,744 zero bytes and 22,812 unread bytes from
the archive, and the same 515 rows with zero unread from the carved directory. `--probe` produced 515 `no` values.

## Appendix A: `Bink2Scarlett.dll` exports

All 74 are named; the ordinal equals the index. This is the complete export table, read without loading the DLL.

```
 0 BinkAllocateFrameBuffers          37 BinkRegisterGPUDataBuffers
 1 BinkClose                         38 BinkRequestStopAsyncThread
 2 BinkCloseTrack                    39 BinkRequestStopAsyncThreadsMulti
 3 BinkControlBackgroundIO           40 BinkService
 4 BinkCopyToBuffer                  41 BinkSetError
 5 BinkCopyToBufferRect              42 BinkSetFileOffset
 6 BinkCurrentSubtitle               43 BinkSetFrameRate
 7 BinkDoFrame                       44 BinkSetIO
 8 BinkDoFrameAsync                  45 BinkSetIOSize
 9 BinkDoFrameAsyncMulti             46 BinkSetMemory
10 BinkDoFrameAsyncWait              47 BinkSetOSFileCallbacks
11 BinkDoFramePlane                  48 BinkSetPan
12 BinkFindXAudio2WinDevice          49 BinkSetSimulate
13 BinkFreeGlobals                   50 BinkSetSoundOnOff
14 BinkGetError                      51 BinkSetSoundSystem
15 BinkGetFrameBuffersInfo           52 BinkSetSoundSystem2
16 BinkGetGPUDataBuffersInfo         53 BinkSetSoundTrack
17 BinkGetKeyFrame                   54 BinkSetSpeakerVolumes
18 BinkGetPlatformInfo               55 BinkSetVideoOnOff
19 BinkGetRealtime                   56 BinkSetVolume
20 BinkGetRects                      57 BinkSetWillLoop
21 BinkGetSubtitleByIndex            58 BinkShouldSkip
22 BinkGetSummary                    59 BinkStartAsyncThread
23 BinkGetTrackData                  60 BinkUtilCPUs
24 BinkGetTrackID                    61 BinkUtilFree
25 BinkGetTrackMaxSize               62 BinkUtilMalloc
26 BinkGetTrackType                  63 BinkUtilMutexCreate
27 BinkGoto                          64 BinkUtilMutexDestroy
28 BinkLoadSubtitles                 65 BinkUtilMutexLock
29 BinkLogoAddress                   66 BinkUtilMutexLockTimeOut
30 BinkNextFrame                     67 BinkUtilMutexUnlock
31 BinkOpen                          68 BinkUtilSoundGlobalLock
32 BinkOpenTrack                     69 BinkUtilSoundGlobalUnlock
33 BinkOpenWithOptions               70 BinkWait
34 BinkOpenXAudio2                   71 BinkWaitStopAsyncThread
35 BinkPause                         72 BinkWaitStopAsyncThreadsMulti
36 BinkRegisterFrameBuffers          73 RADTimerRead
```

## 8. Names from the mount lists

The table of contents is encrypted, but every pack registers its videos in a `CDataFileMgr__ContentChangeSetDef`
mount list as `<filename>$(common)/movies/<folder>/<name>.bk2</filename>`. For `movies.rpf` these are the
`core_binks` and `core_stinger_binks` lists in `common.rpf`; each other pack carries its own `<pack>_binks` list.
The archive stores a pack's streams in the order of its list sorted by lowercase path: folder first, then file name
without the extension, compared as bytes (so `x_final_y` sorts before `x_finale`). A list can name more files than
the archive holds, because some entries are stored in other packs, so `tools/bink_names.py` aligns the two rather
than zipping them. The longest list (`core_binks`, 74,268 bytes) spans two zlib streams with an entry cut at the
boundary, so `--mount` rebuilds whole XML payloads with `gamedata_index.py` when it is given an archive or an
`rpf9_carve.py` directory.

The alignment is a monotone dynamic programme over (list entry, stream in offset order) that may skip list entries,
never streams. Scores come from header facts only: `_720`/`_1080`/`_1440`/`_4k` in a name against the height,
`snapmatic/` against a portrait frame, `tv/sprt/` against 1280x720, and neighbours of one family (the name without
trailing digits or side/size words) sharing width, height and frame count. An optional pins CSV (`offset,path`,
alternatives separated by `|`) adds paths confirmed by watching the stream. Each row gets a confidence:

| Confidence | Meaning |
|---|---|
| `verified` | a pin names exactly this path |
| `anchored` | a family neighbour agrees on the header, a name hint matched, or a pin allows this path |
| `bracketed` | between the nearest anchored rows (or the ends) the names and streams have equal counts, so no skipped entry can hide there |
| `positional` | only the order supports it |

Measured on the 1.0.2.1 package (2026-11-22), without pins:

| Archive | List entries | Streams | Anchored | Bracketed | Positional | Entries in other packs |
|---|---:|---:|---:|---:|---:|---:|
| `movies.rpf` | 537 | 515 | 361 | 129 | 25 | 22 |
| `story000.rpf` | 271 | 264 | 78 | 37 | 149 | 7 |
| `story000_restofworld.rpf` | 7 | 7 | 5 | 2 | 0 | 0 |
| `core_restofworld.rpf` | 11 | 11 | 0 | 11 | 0 | 0 |

All 797 streams in the four archives get a path. A local pins file of 53 streams (42 exact, 11 narrowed to two or
three names), made by watching them, moved 32 rows in `movies.rpf` and 32 in `story000.rpf`. In `story000.rpf` all
32 had been `positional`. In `movies.rpf` 21 were `positional`, 1 `bracketed` and 10 `anchored`: runs of same-shape
siblings (`..._01`, `_02`, `_03`) anchor each other even when the whole run is shifted by one entry, so `anchored`
does not rule out an off-by-one inside such a run. With the pins, `movies.rpf` has 2 positional rows left and
`story000.rpf` 7.

The collation was checked by eye on `story000.rpf`, where one list's own order disagrees with the archive. Comparing
names with the extension instead gives the same paths in all four archives of this package.

The output names game files. Like the index, it is a local work product: do not commit it or attach it to an issue.

```sh
# Lists read straight from common.rpf (a list split over several zlib streams is rebuilt whole)
python -I tools/bink_names.py <package>/movies.rpf --mount <package>/common.rpf --out <temp>/movies-names.csv

# Reuse an index or an rpf9_carve.py directory, add pins, or name another pack's streams with its own list
python -I tools/bink_names.py <temp>/movies.csv --mount <temp>/common --pins <temp>/pins.csv --out <temp>/names.json
python -I tools/bink_names.py <package>/pack/story000/story000.rpf --mount <package>/pack/story000/story000.rpf --out <temp>/story000-names.csv
```
