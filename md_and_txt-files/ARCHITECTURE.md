# ephys_pipeline — Architecture & Interface Contracts

Living document. Update this whenever a module conversation changes a
shared interface, config key, or file format. Every module-specific
conversation should read this before writing code.

## 1. Research context

- Hippocampal CA2 social/place cell development in freely-moving rats,
  P20–P40. Behavioural trials (5 min) interleaved with sleep sessions
  (30–45 min), aiming to capture replay/SWR and pre/during/post-sleep unit
  comparisons.
- Hardware: Cambridge NeuroTech ASSY-350 H20, 4-shank, 128-channel silicon
  probe. OpenEphys acquisition (binary format), Acquisition Board stream.
  Some channels per animal are wired to EMG/ECoG, not the probe (animal-
  specific, hardware-fixed).
- Probe geometry JSON has been corrected (previous version had a ~9x
  vertical-scale error). True minimum contact pitch ~24–30 µm.

## 2. Environments

- **Biotin**: Windows machine, data on a network/UNC share
  (`\\biotin4.hpc.uio.no\...`), local processing via Windows paths.
- **Local** Windows machine, data on local storage.
- **Cluster**: Fox (UiO Educloud), Slurm-scheduled, GPU jobs go through the
  `accel` partition (`--gpus=N`, up to 4/node, `--account=ecXX`). Interactive
  work (editing, debugging, Jupyter) should go through Educloud On Demand
  (`ondemand.educloud.no`) rather than manual SSH+WinSCP where possible.
  True batch processing (many animal/day units) should be a Slurm job
  array — one array task per animal/day — not a single sequential process.
- Paths differ fundamentally between environments; parameters/thresholds do
  not. Config is split accordingly (see §4).

NOTE: `biotin.yaml`'s header comment currently still reads "local.yaml...
Selected via `--env local`" (copy-paste artifact from `local.yaml`). Content
is correct (UNC paths, matches this section); only the comment is wrong.
Cosmetic, low priority, not yet fixed.

## 3. Directory structure

```
ephys_workspace/
├── raw_data/                # READ-ONLY. <date>_<animal>_<day>/Record Node 101/...
├── processed_data/          # READ/WRITE, fast storage
│   └── <animal>/Raw_data/<date>/[<session>/]
│       ├── recording_binary/
│       ├── shank_<N>_ks4/           # KS4 sorter output
│       ├── shank_<N>_phy/           # Phy export (copy_binary=True)
│       ├── lfp_continuous/
│       ├── session_boundaries.json  # see §5 — written by io_utils.py
│       └── run_summary.csv          # see §5
└── ephys_pipeline/
    ├── config/
    │   ├── base.yaml                # parameters/thresholds, environment-independent
    │   ├── local.yaml               # local machine paths
    │   ├── biotin.yaml               # shared network drive paths
    │   └── fox.yaml                 # cluster paths
    ├── src/
    │   ├── io_utils.py               # DONE — see §4a for interface
    │   ├── config_loader.py
    │   ├── health_check.py           # NOT YET WRITTEN — see §9 for next-session prompt
    │   ├── artifact_cleaning.py
    │   ├── ap_sorter.py
    │   ├── lfp_extractor.py
    │   └── quality_control.py
    ├── run_pipeline.py               # subcommands: health-check, sort, lfp, qc, phy-export
    └── environment.yml
```

Output location is decoupled from raw data location by design (config-driven,
not hardcoded) — this was a specific requirement, not an accident.

## 4. Config schema (high-level — expand as config.yaml is written)

- `base.yaml`: probe JSON path (relative to repo or a named constant, not
  hardcoded per-environment), stream name, EMG/ECoG channel lists per
  animal, bad-channel thresholds, saturation-detection thresholds, KS4
  parameters, assessment thresholds, concatenation/output toggles.
- `local.yaml` / `fox.yaml` / `biotin.yaml`: `base_path`, `output_base_path`,
  `stage_raw_locally`/scratch dir, `binary_cache_dir`. Selected via a
  `--env` flag to `run_pipeline.py`.
- Merge order: `base.yaml` loaded first, environment file overrides/extends.
- `config_loader.load_config(env)` returns the merged dict, with `${VAR}`
  env-vars expanded and `_env` set to the requested env name.

**RESOLVED (previously open):** `config_loader.CONFIG_DIR` was
`Path(__file__).resolve().parent / "config"`, which mismatched the
`src/config_loader.py` + `ephys_pipeline/config/` layout above. Fixed by
the user to `.parent.parent / "config"`. No longer an issue.

**OPEN ISSUE:** `run_pipeline.py` does not currently use
`config_loader.load_config(env)` at all — it does
`yaml.safe_load(open("config/config.yaml"))` and reads keys
(`cfg['paths']['raw_data_dir']`, `cfg['artifact_filters']['comb_filter_channels']`)
that don't exist in the actual `base.yaml`/env-yaml schema above. This
predates the base/env split. It needs reconciling before `run_pipeline.py`
can actually call `io_utils.py` or any other module — flagged, not fixed,
since it's outside any single module's scope.

## 4a. `io_utils.py` — interface (DONE this session)

Scope: OpenEphys loading, probe binding, output-path resolution, session
metadata. Explicitly excludes Kilosort4, bad-channel/saturation detection,
and unit assessment (those stay in `ap_sorter.py` / `artifact_cleaning.py` /
`quality_control.py`).

Public functions:
- `load_probe(cfg)` → probeinterface `Probe`. Resolves
  `paths.probe_json_override` if set, else `probe.json_relative_path`
  relative to repo root.
- `resolve_probe_json_path(cfg)` → `Path` (used internally by `load_probe`,
  exposed for debugging).
- `bind_probe(recording, probe, animal_id, cfg)` → `(recording, aux_ids)`.
  Removes EMG/ECoG aux channels (config-driven, per-animal) *before*
  `set_probe(..., group_mode="by_shank")`, per the channel-count contract
  `set_probe` enforces.
- `get_day_output_dir(cfg, animal_id, date_str, session_name=None)` → `str`.
- `find_sessions(cfg, animal_filter=None)` → `dict[(animal_id, date_str), list[str]]`.
- `stage_sessions_locally(cfg, animal_id, date_str, session_paths)` → `list[str]`.
  No-op passthrough if `stage_raw_locally` unset.
- `load_day_recording(cfg, session_paths, concatenate=True)` →
  `(recording, session_frame_counts, fs)`. Raises `RuntimeError` if
  `concatenate=False` and more than one session path is given — the caller
  must loop per-session itself in that mode.
- `build_session_metadata(cfg, session_paths, session_frame_counts, fs)` →
  `list[dict]` matching the `session_boundaries.json` "sessions" contract
  (§5), including TTL info via internal `_read_ttl_info`.
- `write_session_boundaries_json(day_output_dir, fs, session_metadata)` → `str` (path written).
- `prepare_day(cfg, animal_id, date_str, session_paths, probe, concatenate=True)` →
  `dict` with keys `recording`, `session_metadata`, `fs`, `aux_ids`,
  `day_output_dir`, `session_boundaries_path`. Convenience wrapper chaining
  all of the above; equivalent to the pre-bad-channel-detection portion of
  `sort_batch.py`'s old `process_day()`. Returned `recording` is
  probe-attached and aux-removed but **not yet split by shank** — callers
  do `recording.split_by("group")` themselves.

Verification/control tooling (module-local, see §9 for the fuller
cross-module health check that's still to be written):
- `self_check(cfg, animal_filter=None)` → `list[(level, message)]`. Cheap,
  read-only: checks `base_path`/`output_base_path`/`stage_raw_locally`
  reachability, probe load + contact count (128 expected), session
  discovery, and `aux_channel_ids` keys vs. discovered animal IDs (catches
  typo'd animal IDs). Does not touch raw data.
- `check_day(cfg, animal_id, date_str, skip_staging=True)` → `bool`. Heavier,
  opt-in: actually runs `prepare_day()` on one animal/day and reports
  channel counts, per-session duration, TTL presence, and confirms
  `session_boundaries.json` round-trips through `json.load()`. Run this
  after touching `io_utils.py`, or the first time a new animal/day shows
  up, before trusting it in a full batch.
- CLI: `python io_utils.py --env {local,fox,biotin} [--check | --check-day ANIMAL DATE [--with-staging] | --animal ANIMAL]`.

**Known deliberately-preserved caveat (not a regression from refactoring):**
`find_sessions`'s ordering relies entirely on the zero-padded `NNN` session
suffix sorting correctly; it does not cross-check OpenEphys's own recorded
start timestamps. If a session folder was ever renumbered manually,
concatenation order — and therefore KS4's drift-correction timeline — would
be silently wrong. Inherited unchanged from `sort_batch.py`; not yet
implemented as a check anywhere. Candidate for `health_check.py` (§9) or a
future `--verify-timestamps` addition to `check_day`.

**Corrected (previously mis-flagged as a bug):** `aux_channel_ids`'s removal
logic (`bind_probe`) was flagged as a type mismatch on the assumption that
base.yaml's "0-indexed, raw recording channel order" comment was accurate.
Verified against real recordings: the values are matched correctly and
directly against `recording.get_channel_ids()` when listed 1-indexed (true
channel + 1), matching how OpenEphys reports channel labels. **OPEN
ACTION:** `base.yaml`'s comment ("0-indexed, raw recording channel order")
is inaccurate and should be corrected to describe 1-indexed OpenEphys
channel labels — cosmetic/doc-only, not yet fixed.

**Fixed this session:** `_read_ttl_info`'s event-channel matching only
tried `match_key` (derived from the continuous stream name, e.g.
`"Acquisition_Board-100.acquisition_board"`). OpenEphys names event-stream
channels independently (observed: `"Acquisition Board TTL Input"`,
`"Messages"`), so `match_key` never matched and the function silently fell
back to the first event channel every time — correct only because "TTL"
happened to sort before "Messages" in the observed data, not by design.
Now tries a `"ttl"` substring match (case-insensitive) before falling back
to first-channel. Verify against a session where TTL isn't the first
channel if one exists, to confirm the fallback path is no longer being
exercised silently.

## 5. Cross-module data contracts

These are the file formats/interfaces modules pass to each other. Treat
changes to these as breaking changes requiring a note in this doc.

- **`session_boundaries.json`** (written by `io_utils.write_session_boundaries_json`,
  called from `prepare_day`): `{"sampling_frequency": fs, "sessions": [{
  "session_path", "frame_offset_in_concatenated", "n_frames", "duration_s",
  "ttl": {"channel_id", "first_onset_s", "last_offset_s", "n_events"} or
  null}, ...]}`. Consumed by `quality_control.py` for per-session presence
  ratio and by anything doing video/TTL alignment. Known caveat: OpenEphys
  event timestamps are not guaranteed zero-referenced to the continuous
  recording's sample 0 (SpikeInterface GH #3300) — downstream consumers
  must not assume this without verification.
- **`run_summary.csv`** (written by `quality_control.py`): commented (`#`)
  metadata header (sorting params, bad channels, saturation handling) +
  a units table (`shank_id, unit_id, ...metrics..., classification`).
  `pandas.read_csv(path, comment='#')` for programmatic use.
- **Bad-channel report format**: dict `shank_id -> (bad_local_indices: set,
  reasons: dict[int, str], report: list[str])`. Reasons currently in use:
  `"dead"`, `"shorted"`, `"noisy (IBL std outlier, ...)"`,
  `"hopeless saturation"`.
- **Per-channel saturation windows**: dict `local_channel_index -> list of
  (start_sample, end_sample)`, sample indices local to whatever recording
  object was scanned. Two-tier: `scan_saturation_fraction_per_channel`
  (cheap, coarse) gates which channels get the expensive
  `detect_saturation_windows_per_channel` (precise) pass.
- **Unit classification**: exactly three labels — `"Noise/Artefact"`,
  `"MUA"`, `"SUA"`. Do not add a fourth without updating every module that
  filters/counts on these strings.
- **`io_utils.prepare_day()` return dict**: `recording` (probe-attached,
  aux-removed, NOT split by shank), `session_metadata` (see
  `session_boundaries.json` above), `fs`, `aux_ids`, `day_output_dir`,
  `session_boundaries_path`. This is the handoff point from `io_utils.py`
  into `artifact_cleaning.py` / `ap_sorter.py` — those modules should
  consume this dict rather than re-deriving any of it.

## 6. Conventions established so far (don't relitigate without reason)

- **No hard gate on firing_rate or whole-day presence_ratio** for unit
  classification — immature/low-firing principal cells and highly
  selective place/social cells must not be systematically excluded.
  Presence ratio is computed **per session**, not whole-day, and is
  diagnostic only.
- **SNR, not absolute amplitude (µV)**, as the amplitude-based quality
  criterion — IBL's 50 µV threshold is Neuropixels-gain-specific and does
  not transfer.
- **Isolation distance / L-ratio** (Schmitzer-Torbert et al., 2005) as the
  primary SUA/MUA separability metric — chosen over IBL's 3-metric gate
  because it's calibrated for extracellular/tetrode-style recordings, not
  Neuropixels. NaN isolation metrics (e.g. too few units/spikes to compute)
  default to **not-SUA** (conservative), never an automatic pass.
- **amplitude_cutoff gated on minimum spike count** — below threshold,
  reported as NaN, not trusted.
- **Saturation is muted, not the whole channel or whole session** — exact
  (channel, sample-range) zeroing, so a channel bad in one trial doesn't
  lose data from the rest of the day. A channel bad "hopelessly" (above
  `hopeless_fraction_thresh` of the recording) is excluded outright instead
  — muting a channel that's bad nearly always buys nothing and costs a lot.
- **No external bandpass/CMR before KS4** — KS4's internal filtering/CAR is
  relied on; double-filtering was deliberately rejected.
- **Concatenate same-day sessions for sorting** (drift correction + stable
  unit IDs across trials/sleep), **never across days or DV/drive moves**.
- **A shank must have ≥ `min_channels_to_sort_shank` channels** after
  exclusion to be worth precise scanning/sorting — else skip immediately,
  don't burn scan/sort time on an already-known-dead shank.
- **Aux-channel removal happens before probe attachment**, not as a
  bad-channel exclusion — it's a channel-identity/count fix
  (`io_utils.bind_probe`), conceptually distinct from bad-channel detection.

## 7. Open questions / not yet resolved

- Source of the ~1 kHz periodic discharge artifact on a subset of
  channels — phase-locked-across-channels vs. per-channel not yet
  determined. Don't build `artifact_cleaning.py`'s comb-filter piece until
  this is diagnosed; a frequency-domain filter is likely wrong for a
  transient (vs. continuous tone) artifact.
- `nearest_chans` (currently 10, ~49 µm radius under corrected geometry) —
  reasonable per literature, not yet empirically validated against this
  probe/tissue's actual amplitude-vs-distance falloff.
- `silence_periods()` built-in vs. the custom `SaturationMutedRecording`
  class — custom implementation used because per-channel period support in
  the built-in couldn't be confirmed from docs. Revisit if this becomes a
  maintenance burden.
- Phy export (`export_to_phy`, `copy_binary=True`) has a known
  dtype/`return_scaled` gotcha (SpikeInterface GH #2751) — currently
  wrapped defensively, not root-caused.
- `run_pipeline.py`'s config loading is inconsistent with the base/env
  schema (see §4 OPEN ISSUE) — needs reconciling before any module can be
  wired into it as a subcommand.
- `find_sessions`'s NNN-suffix ordering is not cross-checked against
  OpenEphys's recorded start timestamps (see §4a caveat) — a manually
  renumbered session folder would silently break concatenation order.
- `biotin.yaml` header comment is stale (see §2 note) — cosmetic.

## 8. Coding conventions

- Config lives in `base.yaml`/environment YAML, not hardcoded in module
  source — every threshold introduced should be a config key with an
  inline comment explaining what it does and why the default was chosen.
- Every function operating on a `SpikeInterface` API whose exact signature
  wasn't verified against the installed version should say so in a comment
  and degrade gracefully (not crash the whole run) on failure.
- Chunked/lazy processing for anything touching a full day's raw data —
  never materialize a whole concatenated recording in memory.
- Modules should expose a cheap, read-only `self_check()`-style function
  (see `io_utils.py`, §4a) for validating their own config/inputs without
  running the expensive part of the pipeline. Follow this pattern in new
  modules rather than inventing a different convention per module.

## 9. Suggested prompt for the next session (`health_check.py`)

`io_utils.py`'s `self_check()`/`check_day()` only validate *that module's*
responsibilities (config, probe, paths, one day's OpenEphys read). They
don't check GPU/CUDA availability for KS4, disk space at the output
location, whether required packages/versions are importable, or the
cross-animal/cross-day picture (e.g. every animal in `aux_channel_ids` vs.
every animal actually on disk, across the *whole* dataset rather than one
`--animal` filter at a time). That's `health_check.py`'s job. Suggested
prompt:

> Continuing work on `ephys_pipeline`. Read `ARCHITECTURE.md` first —
> `io_utils.py` is now done (§4a has its interface; note its `self_check()`
> and `check_day()` are module-local and narrow in scope, not a substitute
> for this module). Also read `io_utils.py` itself in Project knowledge so
> `health_check.py` reuses its functions (`load_probe`, `find_sessions`,
> `load_config`, etc.) rather than re-implementing them.
>
> Module for this session: `health_check.py`.
> Goal: a standalone, environment-aware health check runnable before a
> batch run (`sort_batch.py`/`run_pipeline.py`) or a Slurm job array on
> Fox. Should check: (1) GPU/CUDA visible to torch (for KS4,
> `torch_device` from `base.yaml`), (2) disk space at
> `output_base_path`/`stage_raw_locally` against a rough per-day size
> estimate, (3) required packages importable at the versions
> `environment.yml` expects, (4) dataset-wide `aux_channel_ids` coverage —
> every animal `find_sessions()` discovers across ALL animals (not one at a
> time) vs. every key in `aux_channel_ids`, both directions, (5) probe JSON
> resolves and geometry sanity-checks (contact count, shank count, no
> duplicate positions) via `io_utils.load_probe()`, (6) the
> `find_sessions()` NNN-ordering caveat (§4a/§7) — cross-check folder NNN
> order against each session's actual OpenEphys-recorded start timestamp
> and flag any mismatch. Output should be a single PASS/WARN/FAIL report,
> same style as `io_utils.self_check()`, with a non-zero exit code on any
> FAIL so it's usable as a pre-flight gate in a Slurm job script. Flag
> explicitly if this requires changing `find_sessions()`'s return shape or
> any other `io_utils.py` interface rather than just calling it.
