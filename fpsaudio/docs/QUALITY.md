# Quality: what is guaranteed, what is measured, what is not

## The acceptance gates

| Gate | Value | Where enforced |
| --- | --- | --- |
| Ratio maths exact-rational throughout | no float ever | `core/ratio.py`, asserted in `tests/test_ratio.py` |
| Output sample count | `round(in / speed)`, half-up, exactly | `verify.py::_check_sample_count` |
| Duration drift | < 1 ms | `verify.py::_check_duration` |
| PCM MD5 on the lossless path | identical | `verify.py::_check_pcm_md5` |
| Null test | < −140 dBFS (see caveat) | `verify.py::_check_null` |
| Loudness delta | < 0.1 LU (see caveat) | `verify.py::_check_loudness` |
| Channel layout | unchanged | `verify.py::_check_layout` |

Every job writes `<output>.verification.json` with every check, its measured
value, its tolerance, and whether it ran at all.

**A skipped check is never counted as a pass.** If a check could not run, the
report says so and says why.

## The three retime methods

### `redeclare` — bit-exact, when the arithmetic allows

Rewrites the declared sample rate and copies the PCM payload byte-for-byte.
Not one sample changes; the PCM MD5 of the output is asserted equal to the
input's and the job hard-fails otherwise.

Available when `source_rate × speed` is a whole number of hertz:

| At 48 kHz | Result | Exact? |
| --- | --- | --- |
| 24 → 25 | 50000 Hz | yes |
| 25 → 24 | 46080 Hz | yes |
| 23.976 → 24 | 48048 Hz | yes |
| 23.976 → 25 | 50050 Hz | yes |
| 24 → 23.976 | 47952.05 Hz | **no** |

Check before committing:

```
fpsaudio explain --preset 24_to_23.976 --rate 48000
```

The caveat is that the output carries an unusual sample rate. That is correct
and lossless, but not every downstream tool likes 50050 Hz. If you need a
standard rate, use `resample`.

Because this path must not alter a sample, dither is forced off and the
quantiser rounds back onto the source's own integer grid.

### `resample` — the default

libsoxr VHQ: linear-phase, driven by the exact integer rate pair. Pitch moves
with speed, as a real speed change does.

Measured on the synthetic suite (997 Hz sine, float32 round trip, all of
mono/stereo/5.1/7.1):

```
interior residual   −163 dBFS RMS
peak residual       −150 dBFS
```

That is 23 dB below the −140 dBFS gate.

### `stretch` — opt-in only

Rubber Band, pitch preserved. **This is not what happens when film runs at a
different speed.** It is the operation both legacy converters performed by
default, which is why they sounded wrong. Use it only when you deliberately want
the original pitch retained, and expect phase-vocoder artefacts on transients.

A stretcher cannot hit an exact rational length, so the frame drift is measured
and reported rather than asserted away.

## Caveats, stated plainly

### The null test is bounded by the output's own dither floor

A 16-bit output cannot null below about −98 dBFS, however good the resampler is:
TPDF dither plus quantisation error has RMS `step/√6`, which is
−98.1 dBFS at 16 bits and −146.2 dBFS at 24 bits.

So the gate is the *less strict* of the configured value and the physical floor
plus 6 dB, and the report always names which was applied:

```
PASS null_test: residual -96.4 dBFS RMS in the interior (1024 edge frames
excluded; whole-file -93.5 dBFS), gate -92.1 dBFS; gate relaxed from -140.0 to
-92.1 dBFS because a 16-bit output's dither floor is -98.1 dBFS
```

Holding a 16-bit file to −140 dBFS would fail every time and tell you nothing.

### The null test excludes the signal edges

A resampler's filter ramps in and out at the boundaries, and the forward pass
truncates its tail at the exact expected frame count, so the first and last few
dozen frames of a round trip can never null. Measured: 260 frames out of 192000
exceed −140 dBFS, all within ~130 frames of an edge.

The report gives both figures — interior and whole-file — so nothing is hidden:

```
residual -142.1 dBFS in the interior ... whole-file -75.8 dBFS
```

The interior figure is the one that says whether the chain is transparent.

### The loudness gate is bounded by the meter's resolution

pyloudnorm rejects layouts above five channels, so every 5.1 and 7.1 track is
measured with ffmpeg's `ebur128` instead. `ebur128` prints one decimal place, so
two of its readings can differ by exactly 0.1 LU with no real change.

A 0.1 LU tolerance against a 0.1 LU measurement is a coin toss, so the tolerance
is widened by the meter's resolution and the meter is named:

```
PASS loudness: -0.70 -> -0.80 LUFS, delta -0.100 LU
(tolerance 0.1 LU + 0.1 LU ffmpeg ebur128 resolution = 0.2 LU)
```

A small genuine loudness change is also expected: K-weighting is
frequency-dependent, and a speed change moves the spectrum.

### AAC quality

AAC is produced by **fdkaac** (Fraunhofer FDK). It is the best non-Apple AAC
encoder available, and no Apple encoder DLLs are used in this build, by design.
At low bitrates Apple's encoder still measures better. If that matters for a
given master, use FLAC or Opus.

**fdkaac's 7.1 support is not assumed.** The adapter reads the installed
binary's `--help`; if 7.1 is not advertised, a 7.1 AAC target is **refused**,
with FLAC/WavPack/Opus offered as alternatives and an explicit
`--accept downmix-to-5.1` if you really want to lose two channels. It will never
downmix silently.

## Decode hygiene

Every Dolby decode uses `-drc_scale 0`, so no dynamic range compression is baked
in, and `-target_level 0`, so no dialnorm-driven gain is applied. This is the
single most common quality bug in DD+ conversion and it appears nowhere in
either legacy program.

Every extract uses `-map 0:<index> -vn -sn -dn`, so no video track is ever
touched.

## Bit depth and dither

32-bit float end to end. The signal is quantised **exactly once**, at the final
write, with TPDF dither by default. A 16-bit source stays 16-bit; a 24-bit or
float source becomes 24-bit unless told otherwise.

The legacy build quantised to undithered 24-bit before the stretch and again on
encode — twice per job.

## Sync metadata

Delays, MKV `CodecDelay`, chapter marks and Atmos object timestamps are all
rescaled by the *same* exact `Fraction` as the audio. A +42 ms delay retimed
23.976 → 25 becomes `42 × 960/1001 = 40.28 ms`. Leaving it at 42 ms would put
the track 1.7 ms out before a sample has played.

## What is not guaranteed

- **Dolby encoding.** No DEE, so DD / DD+ / DD+ Atmos output is refused.
  TrueHD output gets a hand-off bundle instead, because Dolby Media Encoder is
  GUI-only and cannot be automated.
- **DD+ Atmos sources.** No JOC decoder exists outside Dolby's own tools.
  ffmpeg would decode the 5.1 core and discard the objects, so these refuse.
- **DTS in any form.** Out of scope by decision. Detected and refused, never
  silently transcoded.
- **truehdd's output modes.** `truehdd` is a young project and no real TrueHD
  Atmos file was available for testing. The adapter verifies every flag against
  the installed binary's help before using it and refuses if the expected
  surface is absent, but the Atmos path has not been proven end to end against
  real object audio.
