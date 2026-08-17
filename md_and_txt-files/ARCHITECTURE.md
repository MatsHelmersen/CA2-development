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
│       ├── recording_binary/        # NOT currently written anywhere - see §7 open question
│       ├── shank_<N>_ks4/           # KS4 sorter output
│       ├── shank_<N>_phy/           # Phy export (copy_binary=True)
│       ├── lfp_continuous/
│       ├── session_boundaries.json  # written by io_utils.py — see §5
│       ├── health_report.json       # written by health_check.py — see §5
│       ├── health_report.txt        # human-readable version of above
│       ├── saturation_windows.json  # written by health_check.py — see §5
│       ├── ap_sorter_log.json       # written by ap_sorter.py — see §4d (debug bookkeeping, not a contract)
│       ├── run_summary.csv          # written by quality_control.py — see §5
│       ├── shank_<N>_qc_summary.png # written by quality_control.py — manual inspection only, see §11
│       └── qc_overview.png          # written by quality_control.py — manual inspection only, see §11
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
    │   ├── quality_control.py       # DONE — see §4e
    │   └── lfp_extractor.py         # NOT STARTED
    ├── run_pipeline.py              # subcommands: health-check, sort, lfp, qc, phy-export — NOT YET WIRED, see §4 OPEN ISSUE
    └── environment.yml
```

Output location is decoupled from raw data location by design
(config-driven, not hardcoded).

**Status as of this session:** `io_utils.py`, `health_check.py`,
`artifact_cleaning.py`, `ap_sorter.py`, and `quality_control.py` are all
functionally complete for their stated scope and cross-checked against
each other's contracts. `quality_control.py` has since been updated
twice more after its initial version: once for two real bugs surfaced by
running against actual data (`sorter_output/params.py` path, §4d-bugfix;
cross-environment `register_recording=False`, §4e-bugfix), and again
this session (§4e-bugfix2) for a set of SpikeInterface API updates
(`mahalanobis` metric naming, `noise_levels`/`spike_amplitudes`
prerequisite extensions, `remove_excess_spikes`) plus a new QC
visualization capability (§11). A cross-module grep this session
confirmed no other current module touches the SpikeInterface analyzer
APIs these fixes concern — `quality_control.py` is the only consumer.
The pipeline has never been run end-to-end against real data or a real
SpikeInterface install in this conversation beyond the specific errors
reported back and fixed above; see each module's "Verification status"
note and §10 for concrete steps to run yourself before trusting a full
batch. The next module is `lfp_extractor.py` (not yet started, not
scoped in this document), after which `run_pipeline.py`'s config
mismatch (see §4 OPEN ISSUE) needs resolving before any module can be
wired in as a Slurm-friendly subcommand.

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
- **No new REQUIRED config keys were introduced for `quality_control.py`.**
  It reads `cfg["assessment"]["run_unit_assessment" / "export_to_phy" /
  "thresholds"]` and `cfg["saturation_detection"]["window_pad_ms"]`, both
  already present in `base.yaml`.
- **NEW OPTIONAL keys (this session, §4e-bugfix2)**, all read via
  `cfg.get(...)` with an in-code default — an unedited `base.yaml` keeps
  working unchanged, but adding them explicitly is recommended so the
  actual values in effect are visible in one place rather than buried in
  code defaults:
  ```yaml
  assessment:
    n_jobs: 4                  # parallel workers for analyzer.compute() batch extensions (default 1 if absent)
    waveforms_ms_before: 1.0   # waveform extraction window, ms before spike (default 1.0 if absent)
    waveforms_ms_after: 2.0    # waveform extraction window, ms after spike (default 2.0 if absent)
    generate_plots: true       # per-shank + day-level QC PNGs (default true if absent)
  ```
  Add this under the existing `assessment:` block in `base.yaml` (not
  environment-specific — same rationale as the rest of `assessment:`,
  §4).

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
  Shared by `health_check.py --report --session-name`,
  `artifact_cleaning.py --check --session-name`, `ap_sorter.py
  --run --session-name`, and (new this session) `quality_control.py --run
  --session-name`.
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
  **Consumed by `quality_control.py` too (new this session)** — see the
  §7 open question about the cost of this: there is no persisted binary
  cache, so every caller of `prepare_day()` (now `ap_sorter.py` AND
  `quality_control.py`) re-reads raw OpenEphys data.

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
timestamps. `health_check.py --preflight --check-ordering` detects this
but nothing *fixes* it automatically.

## 4b. `health_check.py` — interface (DONE)

Scope: environment-level pre-flight checks and per-day signal quality
assessment. Depends only on `io_utils.py` for data loading. Explicitly
excludes Kilosort4, unit assessment, and any filtering — detection and
reporting only.

Two entry points:

**`--preflight`** (no raw data read, fast):
- `check_packages()`, `check_gpu(cfg)`, `check_disk_space(cfg)`,
  `check_probe_geometry(cfg)`, `check_aux_coverage(cfg)`,
  `check_session_ordering(cfg, animal_filter=None)` (`--check-ordering`).

**`--report`** (reads raw data):
- `find_bad_channels_for_recording(recording_split, fs, cfg)`.
- `scan_saturation_fraction_per_channel(rec, fs, cfg)`.
- `detect_saturation_windows_per_channel(rec, fs, cfg, channels_to_scan,
  coarse_flagged_chunks)`.
- `detect_periodic_discharges(rec, fs, cfg)` (`--spectral-check`, detection
  only, no filtering — see §7).
- `generate_health_report(cfg, animal_id, date_str, skip_staging=True,
  run_spectral_check=False, session_name=None)` — writes
  `health_report.json` + `saturation_windows.json` (+ `health_report.txt`).

CLI:
```
python health_check.py --env {local,fox,biotin} --preflight [--check-ordering] [--animal ANIMAL]
python health_check.py --env {local,fox,biotin} --report --animal ANIMAL --date YYYYMMDD [--session-name NAME] [--spectral-check] [--with-staging]
```

Exits with code 1 on any FAIL, so usable as a Slurm pre-flight gate.

**Known caveat (unchanged, still open):** `check_session_ordering`
accesses a private SpikeInterface attribute for start timestamps.
Degrades gracefully to WARN if absent.

## 4c. `artifact_cleaning.py` — interface (DONE)

Scope: consumes saturation windows detected upstream by `health_check.py`
and applies per-(channel, sample-range) muting as a lazy SpikeInterface
preprocessing wrapper, before Kilosort4 runs. Also owns reading back
`health_report.json` exclusions and composing them with saturation muting
into the one reconstruction sequence that reproduces exactly what
Kilosort4 saw for a shank. Explicitly excludes detection of any kind (bad
channels, saturation, discharge — all stay in `health_check.py`), the
`si.run_sorter()` call itself (`ap_sorter.py`), and unit assessment
(`quality_control.py`).

Public functions:
- `load_saturation_windows(day_output_dir)` → `dict`. Reads
  `saturation_windows.json` (§5). Raises `FileNotFoundError` if missing.
- `mute_saturation_for_shank(recording, shank_id, saturation_windows)` →
  recording. No-op passthrough if the shank has no entry. Does not itself
  consult `mute_before_sorting` — that gate lives in
  `reconstruct_clean_recording_for_shank`.
- `load_health_report(day_output_dir)` → `dict`. Raises
  `FileNotFoundError` if missing.
- `get_shank_exclusion_ids(health_report, shank_id)` → `(status, exclude_ids, detail)`.
  `status` ∈ `{"PASS", "SKIPPED", "MISSING"}`.
- `apply_health_report_exclusions(shank_rec, exclude_ids)` →
  `(clean_recording, missing_ids)`.
- `reconstruct_clean_recording_for_shank(cfg, shank_id, shank_rec,
  health_report, saturation_windows)` → `dict` with keys `status`
  (`"PASS"` | `"SKIPPED"` | `"MISSING"` | `"SKIPPED_MIN_CHANNELS"`),
  `recording`, `exclude_ids`, `missing_ids`, `muted`, `message`,
  `shank_health`. **The single source of truth** for "what did Kilosort4
  actually see (or would it see) for this shank" — composes
  `get_shank_exclusion_ids` → `apply_health_report_exclusions` →
  min-channel re-check → `mute_saturation_for_shank`, gated on
  `cfg["saturation_detection"]["mute_before_sorting"]`.
  **Now called by both `ap_sorter.py` (before `si.run_sorter()`) and
  `quality_control.py` (before building each `SortingAnalyzer`)** — this
  was the whole point of moving this logic here; confirmed working as
  the shared contract this session.
- `SaturationMutedRecording` / `_SaturationMutedSegment` — the lazy
  muting wrapper.
- `merge_windows_across_channels(per_channel_windows)` → `list`. Flattens
  to an all-channel, start-sorted window list. **Now consumed by
  `quality_control.py`'s `compute_saturation_overlap()` as documented**
  — `quality_control.py` calls it directly on the channel-ID-keyed dict
  read from `saturation_windows.json` for a shank (values only matter,
  so no local-index remapping is needed for this particular use —
  channel identity is deliberately dropped for the overlap calculation,
  matching the original `sort_batch.py` semantics).
- `mute_periodic_discharge(recording, discharge_hits, cfg)` — always
  raises `PeriodicDischargeNotCharacterisedError`. See §7.

**Verification status (unchanged):** import path for
`BasePreprocessor`/`BasePreprocessorSegment` confirmed
(`spikeinterface.preprocessing.basepreprocessor`). Runtime correctness of
`get_traces()` under all three `channel_indices` forms, and behaviour
under `si.run_sorter()`, still unverified against real data — see §10.

## 4d-bugfix. KS4 output folder structure — CORRECTED this session

**Bug (present in both `ap_sorter.py` and `quality_control.py` prior to
this fix):** `si.run_sorter("kilosort4", rec, folder=X, ...)` does NOT
write `params.py` directly into `X`. Kilosort4's actual output
(`params.py`, `spike_times.npy`, etc.) is written to `X/sorter_output/`;
`X` itself only holds `spikeinterface_recording.json`,
`spikeinterface_params.json`, `spikeinterface_log.json`. Both
`ap_sorter.existing_shank_output_present()` and
`quality_control.shank_sorted_output_present()` checked
`X/params.py` (i.e. `shank_<id>_ks4/params.py`) — a path that can never
exist — so both always returned `False`.

**Impact:**
- **`ap_sorter.py` (more serious):** `sort_one_shank()` uses this check
  to decide whether to skip an already-sorted shank. Always returning
  `False` meant it never detected existing output and re-sorted (then
  overwrote, via `remove_existing_folder=True`) every shank on every
  invocation, regardless of `--existing-output-action`. This defeats the
  whole point of the per-shank resumability design in §4d.
- **`quality_control.py`:** the same bug only produced false `SKIP`s
  ("not yet sorted") for shanks that were, in fact, sorted — annoying but
  not destructive.

**Fix (both modules, corrected signature/behaviour, unchanged
elsewhere):**
```python
params_path = os.path.join(day_output_dir, f"shank_{shank_id}_ks4", "sorter_output", "params.py")
return os.path.exists(params_path)
```
`si.read_sorter_folder(day_output_dir/shank_<id>_ks4)` (used by both
`ap_sorter.py`'s sorting call and `quality_control.load_sorting_for_shank()`)
was NOT affected — it already expects the OUTER folder and resolves
`sorter_output/` internally itself. Only the existence-check helper was
wrong.

**New documented contract — KS4 output folder structure** (should have
been documented explicitly from the start; adding now):
```
shank_<id>_ks4/                          # the `folder=` arg to si.run_sorter()
├── spikeinterface_recording.json        # SI's own bookkeeping
├── spikeinterface_params.json
├── spikeinterface_log.json
└── sorter_output/                       # Kilosort4's actual output
    ├── params.py                        # <- "is this shank sorted?" marker
    ├── spike_times.npy
    └── ...
```
Any future code that needs to check "has this shank finished sorting"
must check `shank_<id>_ks4/sorter_output/params.py`, not
`shank_<id>_ks4/params.py`. This is exactly the kind of thing §6's "not a
shared function, but keep both copies in sync" note was meant to guard
against — and did not, since the bug was duplicated identically into
both copies at the point they were written. Consider whether this
warrants promoting the check to an actual shared function (see §7 note
on this).

## 4d. `ap_sorter.py` — interface (DONE)

Scope: Kilosort4 execution, one shank at a time. Chains
`io_utils.prepare_day()` → `recording.split_by("group")` →
`artifact_cleaning.reconstruct_clean_recording_for_shank()` → per-shank
existing-output check → `si.run_sorter("kilosort4", ...)`. Explicitly
excludes detection, the muting mechanism, and unit assessment / phy
export.

Public functions:
- `existing_shank_output_present(day_output_dir, shank_id)` → `bool`.
  **Note:** `quality_control.py` deliberately does NOT import this — it
  defines its own one-line equivalent, `shank_sorted_output_present()`,
  locally (see §4e, §6). If the "what counts as sorted" marker
  (`shank_<id>_ks4/params.py` existing) ever changes, update BOTH.
- `sort_one_shank(...)` → `dict` with `status` ∈
  `{"SORTED", "SKIPPED", "ERROR", "DRY_RUN"}`.
- `process_animal_day(cfg, animal_id, date_str, session_name=None,
  shank_filter=None, skip_staging=True, existing_output_action="skip",
  dry_run=False)` → summary `dict`. Writes `ap_sorter_log.json` (own
  bookkeeping, not a cross-module contract).
- `write_ap_sorter_log(day_output_dir, animal_id, date_str, shank_results)` → `str`.

**Does NOT save a local recording binary before calling `si.run_sorter()`**
— `clean_rec` (from `reconstruct_clean_recording_for_shank`) is passed
directly. This is a behavioural difference from `sort_batch.py`'s
original `process_day()`, which called `recording.save(folder=binary_path,
...)` first. Flagged as a §7 open question this session (raised while
building `quality_control.py`, which would have benefited from exactly
such a cache) — not fixed here, since it is `ap_sorter.py`'s call whether
to add it, and doing so would create a new cross-module contract
(`quality_control.py` would need to know to prefer the cached binary over
re-reading raw data) that needs its own design pass.

Module-local verification: `self_check(cfg, day_output_dir=None)`. CLI:
`python ap_sorter.py --env {local,fox,biotin} [--check ...] | [--run ...]`.

## 4e. `quality_control.py` — interface (DONE, new this session)

Scope: post-sort unit assessment. Classifies every unit per shank into
`"Noise/Artefact"` / `"MUA"` / `"SUA"` (§6 conventions), computes
per-session presence ratio and saturation overlap (diagnostic only, not
used for classification), exports to Phy, and writes `run_summary.csv`
(§5 contract). Ported from `sort_batch.py`'s `assess_shank` /
`assess_only_day` / `write_run_summary_csv` — logic ported, monolithic
script structure not. Explicitly excludes detection (`health_check.py`),
the muting/exclusion mechanism (`artifact_cleaning.py`), and sorting
itself (`ap_sorter.py`) — **this module never calls `si.run_sorter()`.**

Design decisions (see module docstring for full reasoning — summarised
here as the four things a future session must not silently relitigate):

1. **No separate `--assess-only` CLI mode** (resolves the open question
   from the previous session's prompt). Since this module never sorts,
   every invocation already only reads existing `shank_*_ks4/` output —
   it *is* "assess-only" in `sort_batch.py`'s sense, unconditionally.
   Re-classifying units after editing `assessment_thresholds` in
   `base.yaml` is just running `quality_control.py --run` again.
2. **Every `SortingAnalyzer` is built against
   `artifact_cleaning.reconstruct_clean_recording_for_shank()`'s output**,
   never against the raw split-by-shank recording. `health_report.json`
   and `saturation_windows.json` are loaded via `artifact_cleaning`'s
   loader functions, exactly as `ap_sorter.process_animal_day()` does.
3. **Stale-report handling is loud, not silent.** If
   `shank_<id>_ks4/params.py` exists (the shank WAS sorted) but
   `reconstruct_clean_recording_for_shank()` now returns anything other
   than `"PASS"`, that shank's result is reported in `summary["errors"]`
   with an explicit "STALE REPORT" message — not skipped, not assessed
   against an unreconstructed recording. A shank with no KS4 output at
   all is an ordinary `SKIPPED` ("not yet sorted"), not an error.
4. **`shank_sorted_output_present()` locally duplicates**
   `ap_sorter.existing_shank_output_present()`'s check rather than
   importing it, to avoid making the sorter module a dependency of the
   assessment module for a single boolean (see §4d note, §6).

Public functions:
- `shank_sorted_output_present(day_output_dir, shank_id)` → `bool`.
- `load_sorting_for_shank(day_output_dir, shank_id)` → SpikeInterface
  `Sorting` (via `si.read_sorter_folder`).
- `compute_saturation_overlap(sorting, unit_id, saturation_windows_merged,
  fs, pad_ms)` → `float`. `saturation_windows_merged` is the flattened,
  all-channel list from `artifact_cleaning.merge_windows_across_channels()`.
- `compute_per_session_presence_ratio(sorting, unit_id, session_metadata,
  n_bins_per_session=10)` → `dict[session_path, float|None]`. Diagnostic
  only — never a classification criterion (§6).
- `classify_unit(row, thresholds)` → `str`, one of the three canonical
  labels (§5, §6).
- `export_shank_to_phy(shank_id, analyzer, day_output_dir)` → `str|None`.
  Wrapped defensively for the SpikeInterface GH #2751 dtype gotcha.
- `assess_shank(cfg, shank_id, rec, sorting, session_metadata, fs,
  day_output_dir, day_tag, saturation_windows_merged)` → `(counts: dict,
  metrics_df: pd.DataFrame)`. `rec` MUST already be the reconstructed
  recording — this function does not reconstruct anything itself.
  **Updated this session (§4e-bugfix2):** calls `si.remove_excess_spikes()`
  before analyzer construction; computes `random_spikes`/`waveforms`/
  `templates`/`noise_levels`/`spike_amplitudes` as one batched
  `analyzer.compute({...}, n_jobs=...)` call; requests the
  `"mahalanobis"` metric (which SpikeInterface expands into
  `isolation_distance`/`l_ratio` columns) instead of the now-unsupported
  direct request for those two names; calls `plot_shank_qc()` at the end
  if `cfg["assessment"]["generate_plots"]` is true (default).
- `plot_shank_qc(metrics_df, thresholds, shank_id, day_tag, output_png)`
  → `str|None` (path written, or `None` if skipped/failed). **New this
  session.** Per-shank 3×3 QC figure built around this pipeline's actual
  classification gates, not a generic metrics dashboard — see
  §4e-bugfix2 and the function's own docstring.
- `plot_day_overview(units_df, day_tag, output_png)` → `str|None`. **New
  this session.** Day-level stacked-bar-by-shank classification summary.
- `write_run_summary_csv(day_output_dir, day_tag, animal_id, date_str,
  session_paths, probe, cfg, concatenating, shank_reconstruction,
  shank_unit_counts, units_df=None)` → `str` (path written). Implements
  the §5 `run_summary.csv` contract: commented metadata header
  (reconstruction status/message per shank, KS4 params, assessment
  thresholds, per-shank unit counts, classification totals) + units
  table.
- `process_animal_day(cfg, animal_id, date_str, session_name=None,
  shank_filter=None, skip_staging=True, dry_run=False,
  skip_phy_export=False, skip_plots=False)` → summary `dict`
  (`assessed`/`skipped`/`errors` lists of human-readable strings). Loads
  `health_report.json` + `saturation_windows.json` once per day/session,
  splits by shank, reconstructs + assesses each sorted shank (or just
  `shank_filter`), writes one `run_summary.csv` per day/session at the
  end (only if at least one shank was successfully reconstructed), and
  (new this session) calls `plot_day_overview()` on the combined
  units table unless `skip_plots`/`generate_plots=False`.

Module-local verification:
- `self_check(cfg, day_output_dir=None)` → `list[(level, message)]`.
  Config-key presence (`assessment.thresholds`'s 9 required keys,
  `saturation_detection.window_pad_ms`); with `day_output_dir`, also
  checks `health_report.json` / `saturation_windows.json` presence and
  counts completed `shank_*_ks4/params.py` folders (WARN if none — "there
  is nothing to assess yet", not a hard failure, since this module may
  legitimately be run before any shank has finished sorting).
- CLI: `python quality_control.py --env {local,fox,biotin} [--check
  [--day-output-dir PATH | --animal ANIMAL --date YYYYMMDD
  [--session-name NAME]]] | [--run --animal ANIMAL --date YYYYMMDD
  [--session-name NAME] [--shank ID] [--with-staging] [--dry-run]
  [--skip-phy-export]]`.

**Verification status:** not yet exercised against real data or a real
SpikeInterface/Kilosort4 install in this conversation — same caveat as
§4a–§4d. See §10 item 5 for concrete steps.

## 4e-bugfix. Cross-environment sorter-folder loading — CORRECTED this session

**Bug:** `si.read_sorter_folder(folder)` defaults to also deserialising
and attaching the *recording* that was linked to the sorting at sort
time, via `spikeinterface_recording.json`. That file stores the
recording's provenance, including the absolute path of the original
OpenEphys session folder **on whatever machine/environment `ap_sorter.py`
actually ran on**. Since sorting and assessment do not have to happen on
the same machine (§2: Fox for batch GPU jobs, biotin/local for
interactive work), a shank sorted on Fox (Linux paths under
`/fp/projects01/ec31/...`) produces a `spikeinterface_recording.json`
that is unresolvable when `quality_control.py` is later run from biotin
or local (Windows/UNC paths) — `si.read_sorter_folder()` raises trying to
open an OpenEphys folder at a path that doesn't exist on the new machine,
even though the sorting output itself (`spike_times.npy`, `params.py`,
etc.) is plain files and fully portable.

**Fix:** `quality_control.load_sorting_for_shank()` now calls
`si.read_sorter_folder(folder, register_recording=False)`. This module
never needed that attached recording in the first place —
`assess_shank()` is always handed `clean_rec`, built fresh **for the
current environment** via
`artifact_cleaning.reconstruct_clean_recording_for_shank()` on top of
this run's own `io_utils.prepare_day()` call (§4e Design Decision 2),
never the recording embedded in the sorter folder. Wrapped defensively
(falls back to the bare call on `TypeError`, in case the installed
SpikeInterface version uses a different kwarg) — not verified against a
real install in this conversation.

**Consequence worth knowing:** this is what makes "sort on Fox, assess
from biotin" (or any environment pairing) actually work, not just
work-by-coincidence-when-paths-happen-to-match. Any *future* code that
calls `si.read_sorter_folder()` directly (rather than going through
`quality_control.load_sorting_for_shank()`) — e.g. a future
`lfp_extractor.py`, or ad-hoc analysis scripts — should do the same,
or it will reproduce this exact failure the first time someone sorts on
one machine and analyses from another. Worth a one-line callout in §6's
conventions list.

## 4e-bugfix2. `quality_control.py` API updates + visualization — this session

User supplied a standalone reference script (`assess_sorting_newest.py`,
not part of this repo/architecture) written against a newer
SpikeInterface install, prompting a side-by-side comparison. Findings
split into three categories — only the first two resulted in code
changes:

**(A) Real bugs in `quality_control.py`, fixed — adopted from the
reference script:**
1. `metric_names=["isolation_distance", "l_ratio"]` → `["mahalanobis"]`.
   **Confirmed against a real SpikeInterface install** (user's
   dummy-data check, this session): requesting `"mahalanobis"` inside
   `quality_metrics` **expands into two columns**, `"isolation_distance"`
   and `"l_ratio"` — there is no column literally named `"mahalanobis"`.
   All downstream code (`classify_unit()`, the isolation/L-ratio plot
   panel, `run_summary.csv`'s column names) is **unchanged**, since it
   already reads those two column names directly. This is a pure
   metric-name-request fix, not a schema change.
2. `"noise_levels"` and `"spike_amplitudes"` extensions are now computed
   as explicit prerequisites before `quality_metrics` — the reference
   script's own comment flags `spike_amplitudes` as required for
   `amplitude_cutoff`. The old code omitted both; on the currently-
   installed SpikeInterface version this likely silently degraded
   (NaN'd or errored on) `snr`/`amplitude_cutoff` rather than failing
   loudly. **Not independently re-verified against a real install beyond
   adopting the reference script's usage** — flagged per §8's "wrap
   unverified APIs" convention; worth spot-checking one shank's
   `run_summary.csv` for non-null `amplitude_cutoff`/`snr` values.
3. `si.remove_excess_spikes(sorting, rec)` is now called immediately
   before `si.create_sorting_analyzer()` — trims spikes whose sample
   time falls past the end of the recording, a known KS4/SpikeInterface
   edge case that can otherwise raise inside analyzer/waveform
   construction. Safe regardless of shank reconstruction status: `rec`'s
   frame count is unchanged by channel exclusion (drops channels, not
   samples) or saturation muting (zeroes samples in place, doesn't
   remove them) — so it always matches what KS4 actually saw. Wrapped in
   try/except with a printed warning (not silently swallowed), so a
   downstream analyzer-construction failure can be traced back to this
   step if it's the cause.
4. Extension computation is now **batched**
   (`analyzer.compute({...}, n_jobs=cfg["assessment"].get("n_jobs", 1))`)
   for `random_spikes`/`waveforms`/`templates`/`noise_levels`/
   `spike_amplitudes` — faster via parallelism, and matches the
   reference script's pattern. **`principal_components` (mahalanobis) is
   deliberately kept in its own separate try/except, not folded into the
   batch** — isolation-metric APIs are what actually shift across
   SpikeInterface versions (this was already flagged in the pre-existing
   docstring before this session), so isolating it preserves graceful
   degradation: a PCA failure now degrades to unavailable
   `isolation_distance`/`l_ratio` (→ conservatively not-SUA, §6) rather
   than aborting the whole shank's assessment, which a single all-in-one
   batch call would do.

**(B) Differences deliberately NOT adopted** — the reference script
reverts several decisions this pipeline already made intentionally, and
the user confirmed staying consistent with the existing architecture
over adopting these:
- Hard-gates classification on `presence_ratio >= 0.8`. This pipeline's
  §6 rule (*"No hard gate on firing_rate or whole-day presence_ratio"*)
  exists specifically so a real place/social cell that only fires in
  certain trials isn't excluded for low whole-day presence — kept as-is.
- Computes `mahalanobis`-derived metrics but never actually uses them in
  its own classification logic (only `snr`/`isi`/`presence_ratio` gate
  units) — this pipeline's `classify_unit()` still uses
  `isolation_distance`/`l_ratio` as the SUA-only gate (§6, Schmitzer-
  Torbert et al. 2005), unchanged.
- Different category label strings (`"Noise/Artifact"` / `"Good MUA"` /
  `"Single Unit (SUA)"` vs. this pipeline's `"Noise/Artefact"` / `"MUA"`
  / `"SUA"`, §5's three-label contract) — not adopted, would break
  `run_summary.csv`'s documented schema.
- No saturation handling, no `reconstruct_clean_recording_for_shank()`
  call, no `health_report.json`/`saturation_windows.json` — builds the
  analyzer straight from a raw per-session recording. Not adopted: this
  is the core correctness guarantee of this whole pipeline (§4c/§6 —
  "any module that needs the exact recording Kilosort4 saw for a shank
  MUST call `reconstruct_clean_recording_for_shank()`").
- Per-session (not per-day-concatenated) processing, with its own
  probe-loading and a dependency on a local `recording_binary` cache
  that the current modular pipeline doesn't write (§7). Not adopted —
  contradicts §6's "concatenate same-day sessions" rule and would need
  its own `io_utils`-equivalent path resolution.
- `run_summary.csv` written as a bare per-shank CSV with no metadata
  header. Not adopted — breaks the §5 commented-header + combined-table
  contract.

The reference script appears to be an earlier, standalone prototype
exploring the newer SpikeInterface quality-metrics API, predating the
modular `io_utils.py`/`artifact_cleaning.py`/`ap_sorter.py` architecture
— useful as a signal for what's changed in SpikeInterface itself (item A
above), not as a design reference for this pipeline's conventions.

**(C) New capability added — QC visualization, not a port of (B)'s
figure:** `plot_shank_qc()` (per-shank, 3×3 grid PNG) and
`plot_day_overview()` (day-level, stacked-bar-by-shank PNG) are new this
session. Deliberately **not** a port of the reference script's 4-panel
figure, because that figure's own classification gate and its plotted
`presence_ratio` threshold line don't correspond to what this pipeline's
`classify_unit()` actually does (per (B) above) — plotting the reference
script's threshold lines against this pipeline's classifications would
visually misrepresent why a unit was classified the way it was. Instead,
panels were chosen to make **this pipeline's actual decision boundaries**
visible: SNR-vs-ISI and isolation-distance-vs-L-ratio (the two real
classification gates), n_spikes and saturation-overlap-fraction (the two
hard Noise/Artefact gates), and firing-rate/presence-ratio/
amplitude-cutoff shown as diagnostics with explicitly no threshold lines
(since none of the three are classification criteria in this pipeline).
See `plot_shank_qc()`'s docstring in `quality_control.py` for the
panel-by-panel rationale.

- Config-gated: `cfg["assessment"]["generate_plots"]` (new optional key,
  default `True`) and `--skip-plots` on the CLI, mirroring the existing
  `export_to_phy`/`--skip-phy-export` pattern.
- Degrades gracefully (printed warning, `None` return, never raises) if
  `matplotlib` isn't installed, or if figure generation itself throws —
  a plotting failure never invalidates an otherwise-successful numerical
  assessment or `run_summary.csv` write.
- `matplotlib.use("Agg")` is set before importing `pyplot`, so this is
  safe on headless Fox/Slurm compute nodes with no display.
- Output files (new): `shank_<id>_qc_summary.png` and `qc_overview.png`,
  both written to the day/session output directory alongside
  `run_summary.csv`. **Not yet added to the §3 directory tree or §5
  contracts list below as formal cross-module contracts** — nothing else
  currently reads these files, they're for manual inspection only. Add
  them there if that changes.

**Cross-module check performed this session:** grepped `io_utils.py`,
`health_check.py`, `artifact_cleaning.py`, and `ap_sorter.py` for
`create_sorting_analyzer`, `quality_metrics`, `isolation_distance`,
`principal_components`, `read_sorter_folder`, and `remove_excess_spikes`
— **none of them use any of these APIs.** Only `quality_control.py`
builds a `SortingAnalyzer` or reads sorter output anywhere in this
pipeline, so the (A) fixes above and the §4e-bugfix
`register_recording=False` fix apply nowhere else. Confirmed separately
that `ap_sorter.py`'s `sorter_output/params.py` path fix (§4d-bugfix) is
already applied in the current project copy. The legacy, superseded
`sort_batch.py` (not part of the `src/` module set, §3) has the same old
API patterns but was deliberately left untouched — flag if it's still in
active use somewhere and it should be kept in sync too.

## 5. Cross-module data contracts

- **`session_boundaries.json`**: `{"sampling_frequency": fs, "sessions":
  [{"session_path", "frame_offset_in_concatenated", "n_frames",
  "duration_s", "ttl": {...} or null}]}`. Consumed by
  `quality_control.py`'s per-session presence ratio (via
  `prepare_day()`'s returned `session_metadata`, not by re-reading the
  JSON file directly — see §7 for why that's a minor inefficiency worth
  revisiting alongside the binary-cache question).

- **`health_report.json`**: `{"animal_id", "date_str", "generated_at",
  "config_env", "shanks": {shank_id: {"initial_channels",
  "bad_channels_detected", "bad_reasons", "bad_channel_ids",
  "saturated_hopeless_channels", "viable_channels_remaining", "status",
  "skip_reason", "saturation_windows_flagged", "periodic_discharge"}}}`.

- **`saturation_windows.json`**: `{"sampling_frequency": fs, "shanks":
  {shank_id: {channel_id: [[start_sample, end_sample], ...]}}}`.
  Channel-ID-keyed. **`quality_control.py` reads a shank's dict from this
  file directly and passes it straight to
  `artifact_cleaning.merge_windows_across_channels()`** for its
  saturation-overlap diagnostic — this is a case where local-index
  remapping is unnecessary (only the window boundaries matter, not which
  channel each came from), so `quality_control.py` does not go through
  `_channel_windows_for_shank()`'s local-index mapping the way the muting
  path does.

- **`run_summary.csv`** (written by `quality_control.write_run_summary_csv`):
  commented (`#`) metadata header (reconstruction status/message per
  shank, KS4 params as configured, assessment thresholds, per-shank unit
  counts, classification totals) + units table (`shank_id, unit_id,
  ...metrics..., classification, presence_ratio_<session>...`).
  `pandas.read_csv(path, comment='#')` for programmatic use.

- **Bad-channel report format**: `shank_id -> (bad_local_indices: set,
  reasons: dict[int, str], report: list[str])`.

- **Unit classification**: exactly three labels — `"Noise/Artefact"`,
  `"MUA"`, `"SUA"`.

- **Session-specific output path (single-session mode)**: `io_utils.py`,
  `health_check.py`, `artifact_cleaning.py`, `ap_sorter.py`, and now
  `quality_control.py` all resolve single-session output the same way
  (`get_day_output_dir(..., session_name=...)` + `select_session()`).

- **`io_utils.prepare_day()` return dict**: unchanged (§4a). Now consumed
  by both `ap_sorter.py` and `quality_control.py` — see §7 for the
  resulting duplicated-raw-read cost.

- **`artifact_cleaning.reconstruct_clean_recording_for_shank(...)`**:
  unchanged contract (§4c). **Confirmed this session as actually shared**
  — `quality_control.py` calls it with the same `cfg` /
  `health_report.json` / `saturation_windows.json` a sort run used,
  exactly as required. This is the concrete instance of the correctness
  guarantee §4c/§9 (previous session) described in the abstract.

## 6. Conventions (don't relitigate without reason)

- **No hard gate on firing_rate or whole-day presence_ratio.**
- **SNR, not absolute amplitude (µV).**
- **Isolation distance / L-ratio** (Schmitzer-Torbert et al., 2005) as the
  primary SUA/MUA separability metric. NaN isolation metrics default to
  not-SUA (conservative).
- **amplitude_cutoff gated on minimum spike count.**
- **Saturation is muted per (channel, window), not the whole
  channel/session.** A channel hopeless above `hopeless_fraction_thresh`
  is excluded outright.
- **No external bandpass/CMR before KS4.**
- **Concatenate same-day sessions**, never across days or DV/drive moves.
- **Shank skipped if fewer than `min_channels_to_sort_shank` channels
  remain after exclusion.**
- **Aux-channel removal before probe attachment.**
- **Config via `config_loader`, never hardcoded.**
- **Chunked/lazy processing** for anything touching a full day's raw data.
- **Graceful degradation** for any SpikeInterface API whose signature
  wasn't verified against the installed version.
- **Single-session mode is one consistent pattern, not per-module.**
- **A shared "is this shank sorted?" check is intentionally NOT a shared
  function** — `ap_sorter.existing_shank_output_present()` and
  `quality_control.shank_sorted_output_present()` are separate one-line
  duplicates of the same `params.py`-existence heuristic, to avoid making
  the assessment module depend on the sorter module for a trivial check.
  If the completion marker changes, update both (§4d, §4e).
- **Any code loading a sorter folder with `si.read_sorter_folder()` MUST
  pass `register_recording=False`** (§4e-bugfix) — sorting and assessment
  are not guaranteed to run on the same machine/environment (§2), and the
  default behaviour tries to resolve the original recording's path from
  wherever it was sorted, which fails across environments. Load the
  recording independently via `io_utils.prepare_day()` +
  `artifact_cleaning.reconstruct_clean_recording_for_shank()` instead,
  exactly as `quality_control.py` does.
- **Any module that needs the exact recording Kilosort4 saw for a shank
  MUST call `artifact_cleaning.reconstruct_clean_recording_for_shank()`**
  — never re-derive exclusions or re-decide the muting gate independently.
  This is now exercised by two callers (`ap_sorter.py`, `quality_control.py`)
  and must stay that way for any future caller (e.g. a future `lfp_extractor.py`
  that wants clean LFP, if it turns out to need the same exclusions).

## 7. Open questions

- **No persisted recording-binary cache (NEW this session).**
  `sort_batch.py`'s original `process_day()` called
  `recording.save(folder=binary_path, ...)` once per day and
  `assess_only_day()` reloaded that fast local binary via `si.load()`.
  The current modular architecture has no equivalent: `ap_sorter.py`
  hands `clean_rec` straight to `si.run_sorter()`, and
  `quality_control.py` (this session) has no faster path than calling
  `io_utils.prepare_day()` again, which re-reads raw OpenEphys data from
  scratch. For a network-mounted (`biotin`) or staged (`stage_raw_locally`)
  day, this means the same expensive read happens at least twice
  (sort time, assessment time), and a third time for every re-assessment
  after a threshold change. **Not fixed this session** — the right fix
  (a shared `recording_binary` cache both `ap_sorter.py` and
  `quality_control.py` read from, written once) is a cross-module
  interface change and needs its own design pass: where does it get
  written (which module owns it), what invalidates it (a stale cache is
  worse than no cache), and does it apply per-day or per-shank. Flag this
  explicitly to whoever picks up `lfp_extractor.py` next, since LFP
  extraction will have the exact same "read raw data a third/fourth time"
  problem.
- **~1 kHz periodic discharge** — unchanged from previous session, see
  `artifact_cleaning.mute_periodic_discharge`'s docstring for the
  characterisation workflow. Still not implemented; still gated behind
  `PeriodicDischargeNotCharacterisedError`.
- **`nearest_chans`** — unchanged, still not empirically validated.
- **`SaturationMutedRecording`** — import path confirmed; `get_traces()`
  runtime correctness and `si.run_sorter()` behaviour still unexercised
  against real data (§4c, §10).
- **Phy export** (`copy_binary=True`) — known dtype/`return_scaled`
  gotcha (SpikeInterface GH #2751), wrapped defensively in both
  `sort_batch.py`'s original and now `quality_control.export_shank_to_phy`.
  Not root-caused.
- **`run_pipeline.py` config** — still inconsistent with the base/env
  schema (§4 OPEN ISSUE).
- **`check_session_ordering`** — still uses a private SI attribute.
- **`mute_before_sorting` flag wiring** — resolved (previous session),
  unchanged.
- **Should `shank_sorted_output_present`/`existing_shank_output_present`
  become one real shared function?** §6 deliberately kept these as two
  independent one-line duplicates to avoid a backwards module dependency.
  That reasoning still holds for *dependency direction*, but this
  session's bug (§4d-bugfix: the exact same wrong path was written
  independently into both copies and neither was caught until real data
  exposed it) is a concrete cost of duplication that the original
  argument didn't weigh. A cheap alternative that doesn't create an
  `ap_sorter.py` ↔ `quality_control.py` dependency either way: move this
  one function into `io_utils.py` (which both already depend on, and
  which already owns path resolution) as e.g.
  `io_utils.shank_ks4_output_present(day_output_dir, shank_id)`, and have
  both `ap_sorter.py` and `quality_control.py` import it from there.
  Not done in this session (out of scope for the bug fix itself) — flagged
  for whoever touches either module next.
- **`lfp_extractor.py`** — not started. Whoever picks this up next should
  decide up front whether it needs
  `reconstruct_clean_recording_for_shank()` too (probably yes, for
  consistent channel exclusion between spike and LFP views) and should
  read the "no persisted binary cache" open question above before writing
  a third independent `prepare_day()` caller.

## 8. Coding conventions

- Config in `base.yaml`/environment YAML only.
- Wrap any SpikeInterface API whose signature wasn't verified; degrade
  gracefully rather than crashing.
- Chunked/lazy processing for full-day data.
- Expose a cheap `self_check()`-style CLI flag per module.
- Reuse `io_utils.select_session()` / `get_day_output_dir(...,
  session_name=...)` for single-session handling.
- Reuse `artifact_cleaning.reconstruct_clean_recording_for_shank()` for
  anything needing the exact post-exclusion, post-muting recording — do
  not reimplement the exclusion+muting sequence in a new module.

## 9. Suggested prompt for the next session (`lfp_extractor.py`)

> Continuing work on `ephys_pipeline`. Read `ARCHITECTURE.md` first
> (especially §7's open questions on the recording-binary cache and
> §4c/§4e's shared-reconstruction pattern), then `io_utils.py`,
> `artifact_cleaning.py`, `ap_sorter.py`, and `quality_control.py` from
> Project knowledge before writing any code. All five existing modules
> are DONE; treat their public functions as fixed unless you find an
> actual bug.
>
> **Module:** `src/lfp_extractor.py`
>
> **Goal:** extract LFP (likely via `spikeinterface.preprocessing`
> bandpass + decimate) per shank, writing to `lfp_continuous/` under the
> day output dir (§3).
>
> **Open design question to resolve, not silently:** should LFP
> extraction apply the same `health_report.json` channel exclusions as
> spike sorting (via `reconstruct_clean_recording_for_shank()`), or
> should it use its own (looser?) exclusion policy — a channel that's
> "bad" for spike detection (e.g. borderline noisy) is not necessarily
> unusable for LFP. Decide explicitly and document the reasoning here.
>
> **Also worth deciding, given §7's open question:** if you find yourself
> wanting a fast local copy of the concatenated recording (LFP extraction
> reads the same raw data `ap_sorter.py` and `quality_control.py` already
> read), this is the third module that would benefit from a persisted
> binary cache. Either propose the shared caching contract now (flag it
> as an interface change touching `io_utils.py`/`ap_sorter.py`/
> `quality_control.py`) or explicitly accept the third redundant raw-data
> read and note why deferring is fine for now.

## 10. Verification steps to run before trusting the current modules on a full batch

None of the following has been exercised in this conversation
(`spikeinterface` is not installed in the sandbox these modules were
written in) — run these yourself before a full batch:

1. **`io_utils.py`**: `python io_utils.py --env local --check`, then
   `python io_utils.py --env local --check-day ANIMAL_ID DATE` against one
   real animal/day. Also try `--session-name` against a probe/drive-move day.
2. **`health_check.py`**: `python health_check.py --env local --preflight
   --check-ordering` once. Then `python health_check.py --env local
   --report --animal ANIMAL_ID --date DATE` and sanity-check a couple of
   flagged bad channels / saturation windows by eye.
3. **`artifact_cleaning.py`**: `--check --animal ANIMAL_ID --date DATE`
   after step 2; construct a synthetic `si.NumpyRecording` and confirm
   `SaturationMutedRecording.get_traces()` zeros exactly the right cells
   for `channel_indices` = `None`/`slice`/array; on one real shank, run
   `mute_saturation_for_shank()` and plot a flagged window before/after;
   call `reconstruct_clean_recording_for_shank()` directly with
   `mute_before_sorting` `True` then `False` and confirm `result["muted"]`
   and channel count differ as expected, and that
   `"SKIPPED_MIN_CHANNELS"` is reachable via a deliberately-lowered test
   `health_report.json` copy.
4. **`ap_sorter.py`**: `--check --animal ANIMAL_ID --date DATE` after step
   2; `--run --animal ANIMAL_ID --date DATE --dry-run` and sanity-check
   the per-shank channel count against `health_report.json`'s
   `viable_channels_remaining`; then run without `--dry-run` on one real
   shank and confirm `shank_<id>_ks4/params.py` is written and muted
   windows are actually zero in what KS4 saw.
5. **`quality_control.py` (NEW — concrete steps for this session's module):**
   - `python quality_control.py --env local --check` (cheap, config-only).
   - `python quality_control.py --env local --check --animal ANIMAL_ID
     --date DATE` after step 4 has produced at least one
     `shank_*_ks4/params.py` — confirm it reports `health_report.json` /
     `saturation_windows.json` present and the completed-shank count
     matches what you actually sorted.
   - `python quality_control.py --env local --run --animal ANIMAL_ID
     --date DATE --dry-run` — confirm the reported unit/channel counts
     per shank match `ap_sorter_log.json`'s `n_units` from step 4, and
     that a shank you deliberately have NOT sorted yet shows up in
     `summary["skipped"]` as "not yet sorted", not silently omitted.
   - Run without `--dry-run` on that same day; open the resulting
     `run_summary.csv` and confirm (a) `pandas.read_csv(path,
     comment='#')` loads the units table cleanly, (b) the header's
     per-shank reconstruction message matches what `ap_sorter.py`'s
     `ap_sorter_log.json` recorded for the same shank at sort time
     (same `exclude_ids`, same `muted` state) — a mismatch here would
     mean the two modules' calls to
     `reconstruct_clean_recording_for_shank()` diverged, which should be
     structurally impossible given they're handed the same
     `health_report.json`/`saturation_windows.json`/`cfg`, but is worth
     confirming once for real.
   - **Stale-report test**: on one already-sorted shank, re-run
     `health_check.py --report` for the same animal/day (regenerating
     `health_report.json`/`saturation_windows.json` with, ideally, a
     slightly different `bad_channel_detection` threshold in a scratch
     copy of `base.yaml` so the exclusion set actually changes), then run
     `quality_control.py --run` again on that day WITHOUT re-running
     `ap_sorter.py`. Confirm the affected shank appears in
     `summary["errors"]` with a "STALE REPORT" message rather than being
     silently assessed or silently skipped — this is the concrete test of
     Design Decision 3 in `quality_control.py`'s module docstring.
   - Spot-check one exported Phy folder (`shank_<id>_phy/`) opens
     correctly in `phy` itself, or at minimum that `params.py` inside it
     points at a `recording.dat` with the right channel/sample counts for
     that shank (SpikeInterface GH #2751 — confirm it did NOT silently
     produce a corrupted export).
   - **New this session (§4e-bugfix2) — confirm the API updates actually
     work against your installed SpikeInterface:**
     - Open one `run_summary.csv` and confirm `isolation_distance` and
       `l_ratio` columns are populated (not all-NaN) for at least some
       units — this confirms the `metric_names=["mahalanobis"]` request
       is being correctly expanded on your install, not silently
       degrading to the PCA-failure fallback path.
     - Confirm `snr` and `amplitude_cutoff` columns are populated for
       units with enough spikes — this is the check for the
       `noise_levels`/`spike_amplitudes` prerequisite-extension fix
       (item A2), which was adopted from the reference script but not
       independently re-verified.
     - Deliberately test `si.remove_excess_spikes()` on a shank if you
       have one you suspect has spikes near the recording boundary
       (e.g. KS4 occasionally reports a spike time exactly at or past
       the last valid frame) — confirm assessment no longer raises where
       it previously would have.
     - Open one `shank_<id>_qc_summary.png` and one `qc_overview.png`
       and visually sanity-check: does panel A's SNR/ISI scatter
       actually put units where their `classification` column says they
       are? Does panel B show isolation_distance/l_ratio points, or the
       "unavailable" placeholder (worth knowing which, given item A1's
       verification above)? Run once with `--skip-plots` to confirm the
       rest of the pipeline (numerical results, `run_summary.csv`) is
       byte-identical either way.
6. **`lfp_extractor.py`** (once it exists): analogous checks, plus the
   channel-exclusion-policy decision from §9 above.

## 11. Interpreting `quality_control.py`'s QC figures

Two PNGs are written per day/session run (unless
`assessment.generate_plots: false` in config, or `--skip-plots` on the
CLI). **These are for manual visual review only — nothing downstream
reads them, and they play no role in classification.** Classification is
always decided by `classify_unit()` against the numeric thresholds in
`base.yaml`; the figures exist so you can sanity-check that those
thresholds are actually behaving sensibly on real data, and catch
anomalies a CSV read row-by-row is easy to miss.

### `shank_<id>_qc_summary.png` — one per assessed shank

A 3×3 grid, saved next to `run_summary.csv` in the day/session output
directory. Panels A and B are the two panels showing *why* a unit landed
where it did; C is a bookkeeping check; D and E are the pipeline's other
two hard gates; F, G, and H are explicitly diagnostic-only (no threshold
lines are drawn on them, because none of the three factor into
classification, §6); I is a reference copy of the thresholds in effect.

| Panel | Shows | How to read it |
|---|---|---|
| **A. SNR vs ISI violation ratio** | Primary Noise/MUA/SUA gate, with the actual `snr_noise_max` / `snr_sua_min` / `isi_violations_ratio_noise_min` / `isi_violations_ratio_sua_max` thresholds drawn as dashed lines. | A healthy shank shows green (SUA) points clustered top-left, grey (Noise/Artefact) bottom-right, orange (MUA) in between. A cluster of grey points sitting *just* past a threshold line is worth a second look — may indicate the threshold needs recalibrating for this recording rather than genuinely bad units. |
| **B. Isolation distance vs L-ratio** | The SUA-only gate (Schmitzer-Torbert et al. 2005), with `isolation_distance_sua_min` / `l_ratio_sua_max` drawn in. | Green points should cluster top-left. If this panel shows "unavailable (PCA extension failed)" instead of points, no unit could be called SUA on isolation grounds this run — check the console log for a `principal_components` warning for that shank. |
| **C. Classification counts** | Bar chart, raw unit counts per label. | Quick sanity check against `run_summary.csv`'s header totals — should always match exactly. |
| **D. Spike count (log)** | `min_spikes_total` hard gate, dashed line. | A pile-up of grey units just below the line, repeated across many shanks/days, may point to a systematic low-yield sorting problem rather than genuinely sparse units. |
| **E. Saturation overlap fraction** | `saturation_overlap_noise_frac` hard gate, dashed line — unique to this pipeline. | Many units pushed up near 1.0 on a shank that otherwise passed the channel-level hopeless-saturation check (§4b) usually means substantial *transient* saturation is still muddying spike detection there — cross-check `health_report.json`'s saturation window count for that shank. |
| **F. Amplitude cutoff** | Diagnostic only — no threshold line. | Only computed for units with ≥ `min_spikes_for_amplitude_cutoff` spikes (§6); an empty/placeholder panel on a low-yield shank is expected, not a bug. |
| **G. Firing rate (log)** | Diagnostic only — no threshold line (§6: never a classification criterion). | Use to spot units that look odd for reasons *outside* the formal gates — e.g. a "SUA" with an oddly bimodal firing rate is worth a manual look in Phy even though it passed every numeric gate. |
| **H. Whole-day presence ratio** | Diagnostic only — no threshold line, **deliberately** (§6: no hard gate on presence ratio, so a real place/social cell that only fires in specific trials isn't penalised). | A cluster of SUA/MUA units with low whole-day presence isn't itself a red flag — check that unit's `presence_ratio_<session>` columns in `run_summary.csv` before assuming instability; it may simply be selective. |
| **I. Thresholds** | Text listing the exact `assessment.thresholds` values in effect. | Use to confirm you're looking at the run you think you are, especially right after a threshold change + re-run. |

### `qc_overview.png` — one per day/session run

A stacked horizontal bar, one row per shank, showing SUA/MUA/Noise-
Artefact counts side by side across the whole day. Written to the same
output directory as `run_summary.csv`. Useful for spotting a shank that
behaves very differently from its neighbours on the same probe/day —
e.g. one shank coming back almost entirely Noise/Artefact while the
other three look normal is usually a cue to check that shank's entry in
`health_report.json` (bad-channel count, saturation severity, or an
outright `SKIPPED` status) before trusting its `run_summary.csv` rows.

### What these figures are *not*

- **Not a substitute for opening Phy** on a borderline shank — they
  summarize the numeric metrics, not the actual waveforms or spike
  trains, and cannot show you drift, double-counted units, or template
  quality directly.
- **Not consumed by any other module** — purely for the person running
  the pipeline; safe to delete, regenerate, or skip entirely
  (`--skip-plots`) without affecting `run_summary.csv` or anything
  downstream.
- **Not the authoritative record of which thresholds produced a given
  `run_summary.csv`** — that's the commented header block in
  `run_summary.csv` itself (§5); panel I is a visual convenience copy of
  the same values, not a separate source of truth.
