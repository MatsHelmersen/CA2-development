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
│       ├── session_boundaries.json  # see §5
│       └── run_summary.csv          # see §5
└── ephys_pipeline/
    ├── config/
    │   ├── base.yaml                # parameters/thresholds, environment-independent
    │   ├── local.yaml               # local machine paths
    │   ├── biotin.yaml              # shared network drive paths
    │   └── fox.yaml                 # cluster paths
    ├── src/
    │   ├── io_utils.py
    │   ├── config_loader.py
    │   ├── health_check.py
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

## 5. Cross-module data contracts

These are the file formats/interfaces modules pass to each other. Treat
changes to these as breaking changes requiring a note in this doc.

- **`session_boundaries.json`** (written by `io_utils.py` after loading/
  concatenating a day): `{"sampling_frequency": fs, "sessions": [{
  "session_path", "frame_offset_in_concatenated", "n_frames", "duration_s",
  "ttl": {"channel_id", "first_onset_s", "last_offset_s", "n_events"} or
  null}, ...]}`. Consumed by `quality_control.py` for per-session presence
  ratio and by anything doing video/TTL alignment. Known caveat: OpenEphys
  event timestamps are not guaranteed zero-referenced to recording start
  (SpikeInterface GH #3300) — downstream consumers must not assume this
  without verification.
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

## 8. Coding conventions

- Config lives in `base.yaml`/environment YAML, not hardcoded in module
  source — every threshold introduced should be a config key with an
  inline comment explaining what it does and why the default was chosen.
- Every function operating on a `SpikeInterface` API whose exact signature
  wasn't verified against the installed version should say so in a comment
  and degrade gracefully (not crash the whole run) on failure.
- Chunked/lazy processing for anything touching a full day's raw data —
  never materialize a whole concatenated recording in memory.
