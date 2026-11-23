# Audio config music records

`audio.rpf` starts with a zlib-compressed **audio config**, then the raw AWC banks begin at archive
offset `0x129B000` (19,509,248). On the package measured here the config is 837 zlib streams and
54,143,493 inflated bytes. It is not a script resource and it is not part of the encrypted table of
contents. It holds the game's music/radio record table, the station record blocks and the mixer
configuration.

Carve the config without touching the AWC banks. `--until-adat` ends the scan at the first raw AWC bank
(an `ADAT` whose header parses as a bank, not the four bytes alone, which also occur in `METADATA`), so the
boundary isn't specific to one build; the summary in `<out>/index.json` records it as `first_adat_bank`
(`0x129B000` on the package measured here, the same window as `--limit 0x129B000`):

```powershell
python -I tools/rpf9_carve.py audio.rpf <out> --until-adat
python -I tools/audio_config.py <out> --join --json
```

`--join` scans the carved streams in name order as one stream. That matters because the config is
one logical blob split into zlib blocks, and a record can straddle a block boundary. On the
measured carve the per-file scan finds 1,588 records and the joined scan finds 1,589. The parts are
read in blocks and never concatenated (see Limits).

Under `--join` each record has two positions. JSON `offset` is the byte position in the joined
stream; `file_offset` is the position inside `file`, the part that holds the record's `0c` marker
byte. Text output prints `file:file_offset`, so a reference such as `middle.bin:0` points at the
marker in that file even when the duration sits at the end of the previous part (so `file_offset`
can be 0-3 under `--join`). Without `--join`, `offset` and `file_offset` are both the position
inside the file.

## Record layout

The music records share one 46-byte layout. A record's marker is at `+4`; the bytes before it carry
the duration:

| Offset | Size | Field |
|---:|---:|---|
| -4 | 4 | duration, milliseconds (u32) |
| 0 | 2 | `0c 01` marker |
| +2 | 2 | config id (u16) |
| +4 | 4 | `02 00 0c 00` |
| +8 | 4 | hash A (unresolved) |
| +12 | 4 | hash B: `joaat(normalized name)` |
| +16 | 4 | `00 00 02 23` |
| +20 | 22 | fixed flags/parameters (constant on this package) |

`hash B` is Jenkins one-at-a-time over the lowercased name, with runs of spaces and punctuation
collapsed to `_`. The AWC stream id is `hash B & 0x1FFFFFFF`. The duration is the real song/ad
length. `awc_manifest.py` now reports the per-channel duration: for a 1-channel streamed descriptor it
divides the interleaved sample count by the Opus packet channel count (2 when the TOC stereo bit is
set), so it matches the config instead of reporting twice.

The records are not necessarily contiguous: they are 46 bytes apart inside a run, but omitted
records leave gaps. The tool finds records by the marker and the duration range, not by a fixed
stride or a hard-coded stream offset.

## What the table gives

On the measured package:

- **1,589 records**, 1,589 distinct ids, range `0xB422`–`0xBFC6`, 272,442.8 s total.
- **1,584 of 1,589** durations equal `AWC manifest duration / 2` within 0.01 s. The five exceptions
  are duplicate-title or placeholder hash collisions.
- **All 1,589** records map to an AWC stream id; the ids are the music streams exported by
  `tools/awc_opus.py`.
- The records are grouped by contiguous id ranges that follow station order. The same config also
  holds station records and station-local content-id lists; they are not part of this record table
  and this tool does not embed the package's ranges.

It does **not** give per-track start offsets. Id order is not mix order, and the schedule-like
structures in the config carry constant template fields, not cue times. A continuous radio mix must
be matched in the audio (`docs/music.md` if present, or the local radio-timing tools).

## Tool

```text
python -I tools/audio_config.py <carve dir or blob> [--json] [--names FILE] [--join]
                                 [--min-ms N] [--max-ms N]
```

- `scan(blob)` returns `offset`, `id`, `duration_ms`, `duration_s`, `hash_a`, `hash_b`,
  `stream_id`. `scan_stream(chunks)` yields the same records for byte chunks read in order,
  without joining them; its `offset` is the joined position.
- `collect(src, names=..., join=...)` scans a blob or a directory and adds `file` and
  `file_offset`; `names` maps hash B to a display string when a NUL-separated local name pool is
  supplied.
- `joaat` and `normalized_name` are the shared hash helpers. `joaat` is the lowercased **latin-1** byte hash
  that `tools/hashdict.py` and `tools/awc_names.py` use, so a name hashes alike in every tool; a non-ASCII
  character is one byte (`é` = `0xE9`), not its UTF-8 pair. `tests/test_joaat_agreement.py` holds every copy in
  `tools/` to that rule.
- Text output prints one `file:file_offset` line per record and a summary; `--json` writes
  `{records, summary}`.

`--names` is optional. The package's debug name stream and the localization credits are local game
files; nothing from them is committed.

## Limits

- The table is record-level. It does not say which record is a song, a DJ line, an ad or a jingle;
  duration and the contiguous station ranges are the only built-in signals.
- Hash A is unresolved, and hash B resolves only through a supplied name pool or an AWC manifest.
- The first ~19.5 MiB bound is specific to this package. A different build can put the AWC start at
  another offset; `rpf9_carve.py --until-adat` finds it (the first AWC bank whose header parses).
- No start offsets, no play order and no playlists are stored here. The config's station content
  lists reference records, but the mix order is assembled at runtime.
- A marker is reported only when at least 20 bytes follow it in the scanned input, as before.
- `--join` memory: at most two read blocks of input (`JOIN_BLOCK_BYTES`, 1 MiB each) plus a
  24-byte carry, one name and start offset per part, and the record list. The non-joined scan
  reads one whole part at a time, and a single blob is read whole.
