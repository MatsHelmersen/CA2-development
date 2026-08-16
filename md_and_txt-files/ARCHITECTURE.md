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
- **Local**: Windows machine, data on local storage.
- **Fox**: UiO Educloud cluster, Slurm-scheduled. GPU jobs go through the
  `accel` partition (`--gpus=N`, up to 4/node, `--account=ecXX`). Interactive
  work should go through Educloud On Demand (`ondemand.educloud.no`).
  True batch processing should be a Slurm job array — one task per
  animal/day — not a single sequential process.
- Paths differ fundamentally between environments; parameters/thresholds do
  not. Config is split accordingly (see §4).

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
│       ├── session_boundaries.json  # written by io_utils.py — see §5
│       ├── health_report.json       # written by health_check.py — see §5
│       ├── health_report.txt        # human-readable version of above
│       ├── saturation_windows.json  # written by health_check.py — see §5
│       └── run_summary.csv          # written by quality_control.py — see §5
└── ephys_pipeline/
    ├── config/
    │   ├── base.yaml                # parameters/thresholds, environment-independent
    │   ├── local.yaml               # local machine paths
    │   ├── biotin.yaml               # shared network drive paths
    │   └── fox.yaml                  # cluster paths
    ├── src/
    │   ├── config_loader.py
    │   ├── io_utils.py              # DONE — see §4a
    │   ├── health_check.py          # DONE — see §4b
    │   ├── artifact_cleaning.py     # DONE — see §4c
    │   ├── ap_sorter.py             # DONE — see §4d
    │   ├── lfp_extractor.py
    │   └── quality_control.py       # NEXT — see §9
    ├── run_pipeline.py              # subcommands: health-check, sort, lfp, qc, phy-export
    └── environment.yml
```

Output location is decoupled from raw data location by design
(config-driven, not hardcoded).

**Status as of this session:** `io_utils.py`, `health_check.py`,
`artifact_cleaning.py`, and `ap_sorter.py` are all functionally complete
for their stated scope and cross-checked against each other's contracts
(the `saturation_windows.json` handoff, the `select_session`/`session_name`
single-session path, the `SaturationMutedRecording` import-path fix, and
— new this session — the health_report.json exclusion-readback +
saturation-muting reconstruction sequence, which now has exactly one
implementation shared by `ap_sorter.py` and (going forward)
`quality_control.py` — see §4c/§4d/§5). The pipeline has never been run
end-to-end against real data or a real SpikeInterface install in this
conversation; see each module's "Verification status" note and §10 for
concrete steps to run yourself before trusting a full batch. The next
module is `quality_control.py` (§9), which must reuse
`artifact_cleaning.reconstruct_clean_recording_for_shank()` rather than
reimplementing exclusion + muting independently — see §9 for why this
matters concretely, not just as a style preference.

## 4. Config schema

- `base.yaml`: probe JSON path, stream name, EMG/ECoG channel lists per
  animal, bad-channel thresholds, saturation-detection thresholds, KS4
  parameters, assessment thresholds, concatenation/output toggles,
  discharge detection thresholds.
- `local.yaml` / `fox.yaml` / `biotin.yaml`: `base_path`, `output_base_path`,
  `stage_raw_locally`, `binary_cache_dir`. Selected via `--env`.
- Merge order: `base.yaml` loaded first, environment file overrides/extends.
- `config_loader.load_config(env)` returns the merged dict, with `${VAR}`
  env-vars expanded and `_env` set to the active env name.

**OPEN ISSUE (unchanged):** `run_pipeline.py` does not use
`config_loader.load_config()` — it reads a non-existent
`config/config.yaml` with a different key schema. Needs reconciling
before any module can be wired into it as a subcommand.

## 4a. `io_utils.py` — interface (DONE)

Scope: OpenEphys loading, probe binding, output-path resolution, session
metadata. Explicitly excludes Kilosort4, bad-channel/saturation detection,
and unit assessment.

Public functions:
- `load_probe(cfg)` → probeinterface `Probe`.
- `resolve_probe_json_path(cfg)` → `Path`.
- `bind_probe(recording, probe, animal_id, cfg)` → `(recording, aux_ids)`.
  Removes EMG/ECoG aux channels *before* `set_probe(..., group_mode="by_shank")`.
- `get_day_output_dir(cfg, animal_id, date_str, session_name=None)` → `str`.
- `find_sessions(cfg, animal_filter=None)` → `dict[(animal_id, date_str), list[str]]`.
- `select_session(session_paths, session_name)` → `list[str]` (1-element).
  Filters one `(animal_id, date_str)` group's session list down to the
  single path whose basename matches `session_name` exactly. Raises
  `ValueError` (listing what *was* found) on no match — a typo'd session
  name fails loudly rather than silently sorting the whole day or nothing.
  The returned 1-element list is exactly what `prepare_day(...,
  concatenate=False)` already recognises as individual-session mode, so no
  changes to `prepare_day` were needed to support single-session runs.
  Added this session so `health_check.py --report --session-name` and
  `artifact_cleaning.py --check --session-name` share one implementation
  instead of each re-deriving the same filter.
- `stage_sessions_locally(cfg, animal_id, date_str, session_paths)` → `list[str]`.
  No-op passthrough if `stage_raw_locally` unset.
- `load_day_recording(cfg, session_paths, concatenate=True)` →
  `(recording, session_frame_counts, fs)`. Raises `RuntimeError` if
  `concatenate=False` with more than one path — caller must loop per-session.
- `build_session_metadata(cfg, session_paths, session_frame_counts, fs)` →
  `list[dict]` matching the `session_boundaries.json` contract (§5).
- `write_session_boundaries_json(day_output_dir, fs, session_metadata)` → `str`.
- `prepare_day(cfg, animal_id, date_str, session_paths, probe, concatenate=True)` →
  `dict` with keys `recording`, `session_metadata`, `fs`, `aux_ids`,
  `day_output_dir`, `session_boundaries_path`. Returned `recording` is
  probe-attached and aux-removed but **not yet split by shank** — callers
  do `recording.split_by("group")` themselves. `individual_session_mode`
  (a 1-element `session_paths` with `concatenate=False`) resolves
  `day_output_dir` to the session-specific subfolder via
  `get_day_output_dir(..., session_name=...)`.

Module-local verification:
- `self_check(cfg, animal_filter=None)` → `list[(level, message)]`. Cheap,
  read-only: path reachability, probe load, session discovery, aux coverage.
- `check_day(cfg, animal_id, date_str, skip_staging=True, session_name=None)`
  → `bool`. Runs `prepare_day()` on one day (or one session, if
  `session_name` given) and verifies channel counts, TTL presence, and
  `session_boundaries.json` round-trip.
- CLI: `python io_utils.py --env {local,fox,biotin} [--check | --check-day ANIMAL DATE [--session-name NAME] [--with-staging] | --animal ANIMAL]`.

**Known caveat (unchanged, still open):** `find_sessions` ordering relies
on the zero-padded NNN suffix; it does not cross-check OpenEphys start
timestamps. If a session folder was ever renumbered manually,
concatenation order would be silently wrong. `health_check.py --preflight
--check-ordering` detects this (see §4b) but nothing *fixes* it
automatically — a FAIL there means go re-check/rename the affected day
by hand before sorting it.

**RESOLVED this session:** the previous "OPEN ISSUE" about
`aux_channel_ids` comment/indexing mismatch is closed. It was confirmed
against real recordings that values are 1-indexed OpenEphys channel
labels, matched directly (correctly) against
`recording.get_channel_ids()`. `base.yaml`'s comment now reads
`"channel IDs (1-indexed OpenEphys channel labels, matching
recording.get_channel_ids())"` — no code or config change was needed
beyond that comment fix, which has already been made. No action
remaining here.

## 4b. `health_check.py` — interface (DONE)

Scope: environment-level pre-flight checks and per-day signal quality
assessment. Depends only on `io_utils.py` for data loading. Explicitly
excludes Kilosort4, unit assessment, and any filtering — detection and
reporting only.

Two entry points:

**`--preflight`** (no raw data read, fast):
- `check_packages()` — importability of all runtime dependencies.
- `check_gpu(cfg)` — torch_device from ks4_params is reachable via CUDA.
- `check_disk_space(cfg)` — free space at output_base_path and
  stage_raw_locally vs. a rough per-day estimate.
- `check_probe_geometry(cfg)` — contact count (128), shank count (4), no
  duplicate contact positions.
- `check_aux_coverage(cfg)` — dataset-wide aux_channel_ids vs. all animals
  on disk, both directions.
- `check_session_ordering(cfg, animal_filter=None)` — cross-checks NNN
  folder order against OpenEphys start timestamps; enabled via
  `--check-ordering` (reads recording metadata, slower than the other
  preflight checks).

**`--report`** (reads raw data):
- `find_bad_channels_for_recording(recording_split, fs, cfg)` — dead,
  shorted, and IBL-std-noisy channels per shank.
- `scan_saturation_fraction_per_channel(rec, fs, cfg)` — coarse first pass;
  identifies hopeless channels (excluded outright).
- `detect_saturation_windows_per_channel(rec, fs, cfg, channels_to_scan,
  coarse_flagged_chunks)` — precise pass; skips clean chunks via
  coarse_flagged_chunks (the key bottleneck fix vs. sort_batch.py).
- `detect_periodic_discharges(rec, fs, cfg)` — optional spectral diagnostic
  for narrowband artifact peaks; enabled via `--spectral-check`. Detection
  only, no filtering. See §7 for rationale.
- `generate_health_report(cfg, animal_id, date_str, skip_staging=True,
  run_spectral_check=False, session_name=None)` — orchestrates the above
  and writes `health_report.json` + `saturation_windows.json` (+
  `health_report.txt`) to the resolved day (or single-session) output
  dir. `session_name` forces `io_utils.prepare_day(concatenate=False)`
  via `select_session`, matching `io_utils.check_day`'s and
  `artifact_cleaning.py`'s single-session handling — all three modules
  now resolve the same session-specific output path the same way.

CLI:
```
python health_check.py --env {local,fox,biotin} --preflight [--check-ordering] [--animal ANIMAL]
python health_check.py --env {local,fox,biotin} --report --animal ANIMAL --date YYYYMMDD [--session-name NAME] [--spectral-check] [--with-staging]
```

Exits with code 1 on any FAIL, so usable as a Slurm pre-flight gate.

**Known caveat (unchanged, still open):** `check_session_ordering`
accesses `rec._recording_segments[0].t_start`/`_t_start`, a private
SpikeInterface attribute. Degrades gracefully to WARN if absent. The
correct public API for start timestamps should be confirmed against the
installed SI version before relying on this as anything more than a
best-effort check.

## 4c. `artifact_cleaning.py` — interface (DONE, scope expanded this session)

Scope: consumes saturation windows detected upstream by `health_check.py`
and applies per-(channel, sample-range) muting as a lazy SpikeInterface
preprocessing wrapper, before Kilosort4 runs. Also hosts the (currently
unimplemented) periodic-discharge remediation stub. **Expanded this
session:** also owns reading back `health_report.json` exclusions and
composing them with saturation muting into the one reconstruction
sequence that reproduces exactly what Kilosort4 saw for a shank — this
was previously split across `ap_sorter.py` (exclusion readback) and here
(muting only); moved so `quality_control.py` can call the same sequence
instead of reimplementing it (see `reconstruct_clean_recording_for_shank`
below, and §5/§9). Explicitly excludes detection of any kind (bad
channels, saturation, discharge — all stay in `health_check.py`), the
`si.run_sorter()` call itself (`ap_sorter.py`), and unit assessment.

Public functions:
- `load_saturation_windows(day_output_dir)` → `dict`. Reads
  `saturation_windows.json` (§5). Raises `FileNotFoundError` with an
  actionable message if the file is missing — deliberately does not
  fall back to "no windows", since "never health-checked" and "checked,
  nothing flagged" must not be conflated.
- `mute_saturation_for_shank(recording, shank_id, saturation_windows)` →
  recording. Maps channel-id-keyed windows to local indices *for the
  specific recording passed in*, at call time (not at detection time —
  see §5 rationale). No-op passthrough if the shank has no entry (either
  it was SKIPPED at health-check, or nothing was flagged). **Does not
  itself consult `cfg["saturation_detection"]["mute_before_sorting"]`**
  — that check now lives one level up, in
  `reconstruct_clean_recording_for_shank` (below), not in the caller
  module. This was previously `ap_sorter.py`'s responsibility (§9's old
  "Known gap to close") — resolved this session by moving the gate
  itself here, alongside the function it gates.
- `load_health_report(day_output_dir)` → `dict`. **Moved from
  `ap_sorter.py` this session.** Reads `health_report.json` (§5). Raises
  `FileNotFoundError` with an actionable message if missing — same
  "never health-checked ≠ silently clean" rationale as
  `load_saturation_windows`.
- `get_shank_exclusion_ids(health_report, shank_id)` → `(status: str,
  exclude_ids: set[str], detail: dict|None)`. **Moved from
  `ap_sorter.py` this session**, unchanged behaviour. `status` is
  `"PASS"`, `"SKIPPED"`, or `"MISSING"`.
- `apply_health_report_exclusions(shank_rec, exclude_ids)` →
  `(clean_recording, missing_ids: set[str])`. **Moved from `ap_sorter.py`
  this session**, unchanged behaviour. Channels in `exclude_ids` absent
  from `shank_rec` are skipped with a printed warning, not an error.
- `reconstruct_clean_recording_for_shank(cfg, shank_id, shank_rec,
  health_report, saturation_windows)` → `dict` with keys `status`
  (`"PASS"` | `"SKIPPED"` | `"MISSING"` | `"SKIPPED_MIN_CHANNELS"`),
  `recording` (or `None` unless PASS), `exclude_ids`, `missing_ids`,
  `muted` (bool), `message`, `shank_health`. **New this session — the
  single source of truth** for "what did Kilosort4 actually see (or
  would it see) for this shank": composes
  `get_shank_exclusion_ids` → `apply_health_report_exclusions` → a
  min-channel re-check against the *current* recording object (stale
  reports produce `"SKIPPED_MIN_CHANNELS"`, distinct from
  health-check-time `"SKIPPED"`) → `mute_saturation_for_shank`, gated on
  `cfg["saturation_detection"]["mute_before_sorting"]`. `ap_sorter.py`
  calls this immediately before `si.run_sorter()`.
  **`quality_control.py` must call this too**, with the same
  `cfg`/`health_report`/`saturation_windows` a given sort run used,
  before building a `SortingAnalyzer` — see §5 and §9 for why divergence
  here is a correctness bug (mismatched waveforms/metrics), not a style
  issue.
- `SaturationMutedRecording` / `_SaturationMutedSegment` — the lazy
  muting wrapper itself (subclasses `si.BasePreprocessor` /
  `BasePreprocessorSegment` from
  `spikeinterface.preprocessing.basepreprocessor` — see "Verification
  status" below for the import-path history).
- `merge_windows_across_channels(per_channel_windows)` → `list`. Flattens
  to an all-channel, start-sorted window list — consumed downstream by
  `quality_control.py`'s `compute_saturation_overlap()`.
- `mute_periodic_discharge(recording, discharge_hits, cfg)` → **always
  raises** `PeriodicDischargeNotCharacterisedError` (a `NotImplementedError`
  subclass). Docstring contains the suggested characterisation workflow
  (spatial clustering across shanks, cross-channel correlation at zero
  lag, whether the artifact survives KS4's internal CAR) — see §7. Do not
  implement filtering here until that workflow has actually been run on
  affected recordings.

Module-local verification:
- `self_check(cfg, day_output_dir=None)` → `list[(level, message)]`.
  Without `day_output_dir`: config-key presence only. With it: also
  parses `saturation_windows.json` and reports shank/channel/window
  counts.
- CLI: `python artifact_cleaning.py --env {local,fox,biotin} --check
  [--day-output-dir PATH | --animal ANIMAL --date YYYYMMDD
  [--session-name NAME]]`. `--animal`/`--date` resolve to a day output
  dir via `io_utils.get_day_output_dir()` — the same resolution
  `io_utils.py`/`health_check.py` use — so this validates the exact
  `saturation_windows.json` that `--report` would have written for that
  animal/day(/session). `--day-output-dir` and `--animal`/`--date` are
  mutually exclusive. `--check` is currently the only supported action;
  running without it errors explicitly rather than printing help text.

**Verification status (updated this session — read carefully, two
separate claims, do not conflate):**
1. **Import path — CONFIRMED.** Two earlier guesses
   (`spikeinterface.full`, then `spikeinterface.core`) were both wrong;
   the correct location for `BasePreprocessor`/`BasePreprocessorSegment`
   in the installed SpikeInterface version is
   `spikeinterface.preprocessing.basepreprocessor`. Class construction
   (import + `__init__` + `add_recording_segment`) has been verified by
   the user against a real install.
2. **Runtime correctness — STILL UNVERIFIED.** Whether `get_traces()`
   zeros exactly the right `(channel, sample)` cells for each of the
   three `channel_indices` forms (`None`, `slice`, explicit array);
   whether iterating `recording._recording_segments` (a private
   attribute) is still correct for this SI version; and whether the
   wrapped recording behaves correctly once handed to `si.run_sorter()`
   — none of this has been exercised against real data or a real sorter
   call. See §10 for concrete tests to run before trusting this on a
   full batch.

**Bug fixed vs. `sort_batch.py`:** the original `_SaturationMutedSegment`
derived absolute channel indices from a `slice` via
`slice.indices(traces.shape[1] + slice.start)`, which is not a correct
general slice→range conversion (worked for the specific cases exercised
in `sort_batch.py`, not guaranteed for others). Rewritten to resolve
against the segment's actual total channel count instead.

## 4d. `ap_sorter.py` — interface (DONE)

Scope: Kilosort4 execution, one shank at a time. Chains
`io_utils.prepare_day()` (or `prepare_day` + `select_session` for
single-session days) → `recording.split_by("group")` →
`artifact_cleaning.reconstruct_clean_recording_for_shank()` (health-report
exclusions + gated saturation muting — see §4c) → per-shank existing-output
check → `si.run_sorter("kilosort4", ...)`. Explicitly excludes bad-channel
/ saturation / discharge *detection* (`health_check.py`), the muting
*mechanism* and exclusion-readback (both now `artifact_cleaning.py`, §4c),
and unit assessment / phy export (`quality_control.py`).

Design decisions (see module docstring for full reasoning):
1. **Exclusion source = `health_report.json` readback, not re-detection.**
   Re-running bad-channel/saturation detection here would double the cost
   of `health_check.py --report` and could silently diverge from the
   report a human already reviewed. `load_health_report()` (now in
   `artifact_cleaning.py`, §4c) raises `FileNotFoundError` if missing —
   a day that was never health-checked is never silently sorted as clean.
2. **`bad_channel_ids` field (channel-ID-keyed) added to
   `health_report.json`**, additive alongside the pre-existing
   `bad_reasons` (local-index-keyed, kept for the human-readable report).
   `ap_sorter.py`/`artifact_cleaning.py` read `bad_channel_ids`. Running
   against an older `health_report.json` without this field means
   bad-channel exclusion silently does nothing — re-run `health_check.py
   --report` to regenerate it.
3. **`mute_before_sorting` gating** — **resolved this session** (was
   listed as an open gap; see §7). The flag is checked inside
   `artifact_cleaning.reconstruct_clean_recording_for_shank()`, not in
   `ap_sorter.py` itself (moved there along with the rest of the
   exclusion/muting sequence — see §4c and §5).
4. **Existing-output check is per-shank** (`shank_<id>_ks4/params.py`
   present), not per-day like `sort_batch.py`'s original — a partially
   sorted day (e.g. after a crash) is resumable shank-by-shank.

Public functions:
- `existing_shank_output_present(day_output_dir, shank_id)` → `bool`.
- `sort_one_shank(cfg, shank_id, shank_rec, health_report,
  saturation_windows, day_output_dir, ks4_params,
  existing_output_action="skip", dry_run=False)` → `dict` with `status`
  ∈ `{"SORTED", "SKIPPED", "ERROR", "DRY_RUN"}`. Calls
  `artifact_cleaning.reconstruct_clean_recording_for_shank()` first;
  does not itself apply exclusions or muting any more (§4c). Does not
  raise on ordinary skip/error conditions — reported in the returned
  dict so a caller can continue past one bad shank.
- `process_animal_day(cfg, animal_id, date_str, session_name=None,
  shank_filter=None, skip_staging=True, existing_output_action="skip",
  dry_run=False)` → summary `dict` (`sorted`/`skipped`/`errors` lists of
  human-readable strings). Loads `health_report.json` +
  `saturation_windows.json` once per day/session (both via
  `artifact_cleaning.py`), splits by shank, calls `sort_one_shank()` per
  shank (or just `shank_filter`, if given), writes `ap_sorter_log.json`
  (this module's own resume/debug bookkeeping — **not** a documented
  cross-module contract; see module docstring if you want to promote it
  to one).
- `write_ap_sorter_log(day_output_dir, animal_id, date_str,
  shank_results)` → `str` (path written).

Module-local verification:
- `self_check(cfg, day_output_dir=None)` → `list[(level, message)]`.
  Config-key presence (`ks4_params`, `sorting.min_channels_to_sort_shank`,
  `saturation_detection.mute_before_sorting`); with `day_output_dir`,
  also validates `health_report.json`'s `bad_channel_ids` field is
  present (warns if it predates that fix) and that
  `saturation_windows.json` sits alongside it.
- CLI: `python ap_sorter.py --env {local,fox,biotin} [--check
  [--day-output-dir PATH | --animal ANIMAL --date YYYYMMDD
  [--session-name NAME]]] | [--run --animal ANIMAL --date YYYYMMDD
  [--session-name NAME] [--shank ID] [--with-staging]
  [--existing-output-action skip|overwrite|prompt] [--dry-run]]`.

**Verification status:** not yet exercised against real data or a real
SpikeInterface/Kilosort4 install in this conversation — same caveat as
§4a–§4c. See §10 for concrete steps.

## 5. Cross-module data contracts

Treat changes to these as breaking changes requiring a note here.

- **`session_boundaries.json`** (written by `io_utils.write_session_boundaries_json`):
  `{"sampling_frequency": fs, "sessions": [{"session_path",
  "frame_offset_in_concatenated", "n_frames", "duration_s",
  "ttl": {"channel_id", "first_onset_s", "last_offset_s", "n_events"} or
  null}]}`. Consumed by `quality_control.py` (per-session presence ratio)
  and anything doing video/TTL alignment. Caveat: OpenEphys event timestamps
  are not guaranteed zero-referenced to the continuous recording's sample 0
  (SpikeInterface GH #3300).

- **`health_report.json`** (written by `health_check.generate_health_report`):
  `{"animal_id", "date_str", "generated_at", "config_env",
  "shanks": {shank_id: {"initial_channels", "bad_channels_detected",
  "bad_reasons": {str_key: reason}, "saturated_hopeless_channels",
  "viable_channels_remaining", "status": "PASS"|"SKIPPED"|"UNKNOWN",
  "skip_reason", "saturation_windows_flagged",
  "periodic_discharge": {channel_id: {"peak_freq_hz", "peak_snr"}}}}}`.
  Note: `bad_reasons` keys are strings (JSON requirement) representing
  local channel indices. Note: `saturation_windows_flagged` is a per-shank
  **count only** — the actual `(start, end)` windows are NOT in this file;
  see `saturation_windows.json` below.

- **`saturation_windows.json`** (written by `health_check.generate_health_report`,
  alongside `health_report.json`; consumed by `artifact_cleaning.load_saturation_windows`):
  `{"sampling_frequency": fs, "shanks": {shank_id: {channel_id:
  [[start_sample, end_sample], ...]}}}`. Only shanks with `status ==
  "PASS"` and at least one flagged window get an entry — `SKIPPED` shanks
  were never precisely scanned. Written unconditionally (possibly with an
  empty `"shanks": {}`) on every `--report` run, so "file missing" always
  means "never health-checked," never "nothing was flagged."
  **Deliberately keyed by actual `channel_id` (string), not local index**
  — unlike the in-memory `per_channel_windows` contract below, which is
  local-index-keyed and internal to `health_check.py`. Local indices are
  only meaningful relative to the specific `clean_rec` `health_check.py`
  built for itself at detection time (after bad-channel + hopeless-
  saturation exclusion); they cannot be assumed to still line up against
  whatever recording object `artifact_cleaning.py` is handed later, in a
  separate process. `artifact_cleaning.py` re-maps channel_id → local
  index itself, against whatever recording it actually receives, at
  call time. This contract is now implemented end-to-end: `health_check.py`
  writes it, `artifact_cleaning.load_saturation_windows` /
  `mute_saturation_for_shank` read and consume it. Not yet exercised by
  a caller in a real pipeline run — that's `ap_sorter.py`'s job (§9).

- **`run_summary.csv`** (written by `quality_control.py`): commented (`#`)
  metadata header + units table (`shank_id, unit_id, ...metrics...,
  classification`). `pandas.read_csv(path, comment='#')` for programmatic use.

- **Bad-channel report format**: `shank_id -> (bad_local_indices: set,
  reasons: dict[int, str], report: list[str])`. Reasons in use: `"dead"`,
  `"shorted"`, `"noisy (IBL std outlier, ...)"`, `"hopeless saturation"`.

- **Per-channel saturation windows (in-memory, internal to `health_check.py`)**:
  `local_channel_index -> list of (start_sample, end_sample)`, sample
  indices local to `clean_rec` at detection time. This is the shape
  `detect_saturation_windows_per_channel()` returns and works with
  internally. It is **not** the same shape as the on-disk
  `saturation_windows.json` contract above, which is channel-id-keyed —
  `health_check.py` converts between the two right before writing the
  file (local index → `clean_rec.get_channel_ids()[local_index]`). Don't
  assume these are interchangeable across a process boundary.

- **Unit classification**: exactly three labels — `"Noise/Artefact"`,
  `"MUA"`, `"SUA"`. Do not add a fourth without updating every module that
  filters/counts on these strings.

- **Session-specific output path (single-session mode)**: any module
  operating on one session rather than a whole day (probe/drive moved
  mid-day, ARCHITECTURE.md §6) resolves its output directory via
  `io_utils.get_day_output_dir(cfg, animal_id, date_str,
  session_name=<basename>)` and selects that one session's path via
  `io_utils.select_session(session_paths, session_name)`. `io_utils.py`
  (`check_day`), `health_check.py` (`generate_health_report`),
  `artifact_cleaning.py`'s CLI, and `ap_sorter.py`
  (`process_animal_day`) all now do this the same way. Any future module
  needing single-session output paths should follow the same pattern
  rather than re-deriving it independently.

- **`io_utils.prepare_day()` return dict**: `recording` (probe-attached,
  aux-removed, not split by shank), `session_metadata`, `fs`, `aux_ids`,
  `day_output_dir`, `session_boundaries_path`. Handoff point from
  `io_utils.py` onward. **Downstream flow is now implemented**
  (`ap_sorter.process_animal_day`, §4d): split by shank →
  `artifact_cleaning.reconstruct_clean_recording_for_shank()` (below) →
  Kilosort4. `day_output_dir` from this dict is what
  `artifact_cleaning.load_saturation_windows()` /
  `load_health_report()` expect.

- **`artifact_cleaning.reconstruct_clean_recording_for_shank(cfg,
  shank_id, shank_rec, health_report, saturation_windows)`** — **added
  this session; the canonical shared contract `quality_control.py` must
  use.** Returns `{"status", "recording", "exclude_ids", "missing_ids",
  "muted", "message", "shank_health"}`; `status` is one of `"PASS"`,
  `"SKIPPED"`, `"MISSING"`, `"SKIPPED_MIN_CHANNELS"`. This is the one
  place health-report exclusions and `mute_before_sorting`-gated
  saturation muting are composed, in order. **Why this is a contract and
  not an implementation detail:** `quality_control.py` will build a
  `SortingAnalyzer` from a shank's KS4 output; if it reconstructs the
  input recording differently than `ap_sorter.py` did at sort time (e.g.
  skips the mute gate, or re-derives exclusions itself instead of
  reading `health_report.json` back), the waveforms/metrics it computes
  will not correspond to what KS4 actually clustered spikes on — a
  silent correctness bug, not a style inconsistency. `quality_control.py`
  must call this function with the same `cfg` /
  `health_report.json` / `saturation_windows.json` the sort run used, not
  reimplement the sequence. Previously this logic lived split across
  `ap_sorter.py` (exclusion readback: `load_health_report`,
  `get_shank_exclusion_ids`, `apply_health_report_exclusions`) and
  `artifact_cleaning.py` (muting only); all four functions now live in
  `artifact_cleaning.py` (§4c) — moved this session, flagged as an
  interface change rather than made silently.

## 6. Conventions (don't relitigate without reason)

- **No hard gate on firing_rate or whole-day presence_ratio** — immature
  and highly selective cells must not be excluded. Presence ratio is computed
  per session, diagnostic only.
- **SNR, not absolute amplitude (µV)** — IBL's 50 µV threshold is
  Neuropixels-gain-specific and does not transfer.
- **Isolation distance / L-ratio** (Schmitzer-Torbert et al., 2005) as the
  primary SUA/MUA separability metric. NaN isolation metrics default to
  not-SUA (conservative).
- **amplitude_cutoff gated on minimum spike count** — below threshold,
  reported as NaN.
- **Saturation is muted per (channel, window), not the whole channel or
  session**. A channel hopeless above `hopeless_fraction_thresh` is excluded
  outright — muting a nearly-always-bad channel buys nothing.
- **No external bandpass/CMR before KS4** — KS4's internal filtering/CAR
  is used; double-filtering was deliberately rejected.
- **Concatenate same-day sessions**, never across days or DV/drive moves.
- **Shank skipped if fewer than `min_channels_to_sort_shank` channels**
  remain after exclusion — no scan/sort time wasted on a dead shank.
- **Aux-channel removal before probe attachment** (`io_utils.bind_probe`) —
  channel-identity fix, not bad-channel exclusion.
- **Config via `config_loader`, never hardcoded** — every threshold is a
  `base.yaml` key with an explanatory comment.
- **Chunked/lazy processing** for anything touching a full day's raw data.
- **Graceful degradation** for any SpikeInterface API whose signature wasn't
  verified against the installed version — wrap defensively, degrade to WARN,
  don't crash the run.
- **Single-session mode is one consistent pattern, not per-module** — see
  the "Session-specific output path" contract in §5. Any new module that
  needs to operate on one session should reuse
  `io_utils.select_session()` + `get_day_output_dir(..., session_name=...)`
  rather than reinventing the filter.

## 7. Open questions

- **~1 kHz periodic discharge**: a narrowband spectral diagnostic exists
  (`health_check.detect_periodic_discharges`, `--spectral-check`) and a
  hard-stop stub exists (`artifact_cleaning.mute_periodic_discharge`,
  always raises `PeriodicDischargeNotCharacterisedError`). The open
  question is unchanged: whether hits are phase-locked across channels on
  the same shank (common-mode hardware artifact → candidate for CMR-style
  or common-mode muting) or per-channel (biological or per-electrode →
  more ambiguous, likely needs bad-channel/window-style exclusion instead
  of a filter). The suggested characterisation workflow (spatial
  clustering across shanks, zero-lag cross-correlation of flagged
  channels, whether the artifact survives KS4's internal CAR) now lives
  in `mute_periodic_discharge`'s docstring — see that function rather
  than restating it here. A comb/notch filter is very likely the wrong
  tool class for a transient artifact regardless of spatial pattern
  (frequency-domain filtering of non-stationary signals causes
  time-domain ringing — see Widmann, Schröger & Maess, 2015, *J Neurosci
  Methods*, §3, cited in the stub's docstring); any filtering implementation
  belongs in `artifact_cleaning.py`, replacing the stub, and only after
  the workflow above has actually been run on affected recordings.
- **`nearest_chans`** (currently 10, ~49 µm radius under corrected geometry)
  — not yet empirically validated against this probe/tissue's actual
  amplitude-vs-distance falloff.
- **`SaturationMutedRecording`** — import path is now confirmed against a
  real SpikeInterface install (§4c). Still unresolved: (1) whether
  per-channel period support in SI's `silence_periods()` is available in
  the installed version, which would let this custom subclass be retired
  in favour of a documented public API; (2) `get_traces()` correctness
  and behaviour under `si.run_sorter()` are still unexercised against real
  data — see §4c "Verification status" and §10.
- **Phy export** (`export_to_phy`, `copy_binary=True`) has a known
  dtype/`return_scaled` gotcha (SpikeInterface GH #2751) — wrapped
  defensively, not root-caused.
- **`run_pipeline.py` config** is inconsistent with the base/env schema
  (see §4 OPEN ISSUE) — needs reconciling before any module can be wired
  in as a subcommand.
- **`check_session_ordering`** accesses a private SI attribute for
  start timestamps — see §4b caveat.
- **`mute_before_sorting` flag wiring — RESOLVED this session.**
  `artifact_cleaning.reconstruct_clean_recording_for_shank()` (§4c, §5)
  now checks `cfg["saturation_detection"]["mute_before_sorting"]` and
  gates the call to `mute_saturation_for_shank()` on it.
  `ap_sorter.sort_one_shank()` calls the reconstruction function and no
  longer checks the flag itself. `quality_control.py` will pick up the
  same gating automatically by calling the same shared function — no
  separate wiring needed there.

## 8. Coding conventions

- Config in `base.yaml`/environment YAML only — every threshold needs a key
  and an inline comment explaining the default.
- Wrap any SpikeInterface API whose signature wasn't verified; degrade
  gracefully rather than crashing.
- Chunked/lazy processing for full-day data — never materialise a whole
  concatenated recording in memory.
- Expose a cheap `self_check()`-style CLI flag per module for validating
  config and inputs without running the expensive pipeline path.
- Reuse `io_utils.select_session()` / `get_day_output_dir(...,
  session_name=...)` for single-session handling rather than
  reimplementing it per module (see §5, §6).

## 9. Suggested prompt for the next session (`quality_control.py`)

> Continuing work on `ephys_pipeline`. Read `ARCHITECTURE.md` first, then
> `io_utils.py`, `health_check.py`, `artifact_cleaning.py`, and
> `ap_sorter.py` from Project knowledge before writing any code. All four
> are DONE and their interfaces are stable as documented in §4a–§4d;
> treat their public functions as fixed unless you find an actual bug.
>
> **Module:** `src/quality_control.py`
>
> **Goal:** post-sort unit assessment — classify every unit per shank
> into `"Noise/Artefact"` / `"MUA"` / `"SUA"` (§6 conventions: SNR not
> absolute amplitude, isolation distance / L-ratio per Schmitzer-Torbert
> et al. 2005, no hard gate on firing rate or whole-day presence ratio),
> compute per-session presence ratio (§5 `session_boundaries.json`
> contract) and saturation overlap (`merge_windows_across_channels`),
> export to Phy (`export_to_phy`, `copy_binary=True` — known dtype
> gotcha, SpikeInterface GH #2751, wrap defensively), and write
> `run_summary.csv` (§5 contract: commented metadata header + units
> table, `pandas.read_csv(path, comment='#')` for programmatic reads).
> This was previously `assess_shank`/`assess_only_day`/
> `write_run_summary_csv` in `sort_batch.py` — port the logic, not the
> monolithic structure.
>
> **Non-negotiable constraint, not a suggestion:** to build a
> `SortingAnalyzer` against a shank's KS4 output, you need the *exact*
> recording KS4 was given as input — same channels excluded, same
> saturation windows muted (or not). That reconstruction is
> **`artifact_cleaning.reconstruct_clean_recording_for_shank(cfg,
> shank_id, shank_rec, health_report, saturation_windows)`** (§4c, §5) —
> call it, do not reimplement exclusion-then-muting yourself. Load
> `health_report.json` via `artifact_cleaning.load_health_report()` and
> `saturation_windows.json` via `artifact_cleaning.load_saturation_windows()`,
> exactly as `ap_sorter.process_animal_day()` does, then split by shank
> and call the reconstruction function per shank before building each
> `SortingAnalyzer`. If reconstruction returns anything other than
> `status == "PASS"` for a shank that nonetheless has KS4 output on disk,
> that's a stale-report situation worth surfacing loudly (it means the
> report changed after that shank was sorted) — don't silently skip or
> silently proceed with an unreconstructed recording.
>
> **Open design question to resolve in this session, not silently:**
> `--assess-only` re-assessment (re-running unit assessment on already-
> sorted output without re-sorting, e.g. after changing
> `assessment_thresholds`) existed in `sort_batch.py`. Decide whether
> `quality_control.py` keeps this mode, and if so, confirm it still goes
> through `reconstruct_clean_recording_for_shank()` for the recording it
> builds the analyzer against — a re-assessment that skips reconstruction
> and loads the raw split-by-shank recording directly would silently
> assess against un-excluded, un-muted channels.
>
> Ground rules: flag any required interface or config key change before
> making it. At the end of the session, summarise what should be updated
> in `ARCHITECTURE.md`, and give concrete verification steps — note that
> `spikeinterface` may not be available to execute in the coding sandbox,
> so verification steps should be things I can actually run in my own
> environment (Fox or local), not claimed as already tested.

## 10. Verification steps to run before trusting the current modules on a full batch

None of the following has been exercised in this conversation
(`spikeinterface` is not installed in the sandbox these modules were
written in) — run these yourself before a full batch, not just before
`quality_control.py`:

1. **`io_utils.py`**: `python io_utils.py --env local --check` (cheap),
   then `python io_utils.py --env local --check-day ANIMAL_ID DATE`
   against one real animal/day you know the answer for (session count,
   durations, TTL presence). Also try `--session-name` against a day with
   a probe/drive move, if you have one, to confirm individual-session
   mode resolves the output path correctly.
2. **`health_check.py`**: `python health_check.py --env local --preflight
   --check-ordering` once, to confirm the private-attribute TTL/timestamp
   access degrades sensibly (WARN, not a crash) on your installed SI
   version. Then `python health_check.py --env local --report --animal
   ANIMAL_ID --date DATE` on one real day and manually sanity-check a
   couple of flagged bad channels / saturation windows against the raw
   trace by eye.
3. **`artifact_cleaning.py`**:
   - `python artifact_cleaning.py --env local --check --animal ANIMAL_ID
     --date DATE` after step 2, to confirm `saturation_windows.json`
     round-trips.
   - Construct a small synthetic `si.NumpyRecording`, apply
     `SaturationMutedRecording` with a known window, and confirm
     `get_traces()` zeros exactly the right `(channel, sample)` cells for
     `channel_indices=None`, a `slice`, and an explicit array — this is
     the specific unverified claim flagged in §4c.
   - On one real shank from step 2, run
     `mute_saturation_for_shank()` and plot a flagged window before/after
     muting to confirm visually.
   - **New this session:** on the same shank, call
     `reconstruct_clean_recording_for_shank()` directly with
     `mute_before_sorting` set `True` and then `False` in a copied
     config, and confirm `result["muted"]` and the resulting
     `get_num_channels()` differ as expected between the two calls, and
     that `status == "SKIPPED_MIN_CHANNELS"` is actually reachable (e.g.
     by temporarily lowering a shank's channel count in a test
     `health_report.json` copy below `min_channels_to_sort_shank`).
4. **`ap_sorter.py`**:
   - `python ap_sorter.py --env local --check --animal ANIMAL_ID --date
     DATE` after step 2, to confirm `health_report.json`'s
     `bad_channel_ids` field and `saturation_windows.json` are both
     present and well-formed for that day.
   - `python ap_sorter.py --env local --run --animal ANIMAL_ID --date
     DATE --dry-run` to see, per shank, how many channels would actually
     be sorted (post-exclusion, post-min-channel-check) without calling
     Kilosort4 — sanity-check this count against `health_report.json`'s
     `viable_channels_remaining` for that shank.
   - Then run without `--dry-run` on one real shank, confirm
     `shank_<id>_ks4/params.py` is written, and spot-check that muted
     windows are actually zero in what KS4 saw (e.g. via
     `si.run_sorter`'s intermediate binary, if it writes one) rather than
     just trusting the wrapper was applied — this was previously listed
     as an "End-to-end" step for `ap_sorter.py` before it existed; it's
     now concrete and should be run.
5. **`quality_control.py`** (once it exists): pick one already-sorted
   shank, build a `SortingAnalyzer` via
   `reconstruct_clean_recording_for_shank()` as the module will, and
   independently confirm its channel count matches `ap_sorter_log.json`
   / the actual `recording.dat` channel count inside that shank's
   `shank_<id>_ks4/` folder — a mismatch here would mean the shared
   reconstruction function and the actual KS4 run have diverged (e.g.
   `health_report.json` or `saturation_windows.json` were regenerated
   between sorting and assessment), which is exactly the class of bug
   this session's refactor was meant to make structurally harder to hit,
   not impossible to hit.
