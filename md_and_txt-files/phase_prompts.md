# Phase starter prompts

Each of these goes into a NEW conversation in the Project. Each already
includes the master_prompt.md framing - don't paste master_prompt.md
separately, just use the phase block below as-is.

---

## Phase 1 — io_utils.py

I'm continuing work on my modular electrophysiology pipeline (`ephys_pipeline`).
Before doing anything else, read `ARCHITECTURE.md` in this Project's knowledge
— it defines the module boundaries, the config schema, and the interface
contracts that every module must stay consistent with. Also check
`config/base.yaml`, `config/local.yaml`, and `sort_batch.py` in Project
knowledge before writing new code.

**Module for this conversation:** `src/io_utils.py`

**Goal for this session:** Extract the loading/probe/path logic from
`sort_batch.py` into `src/io_utils.py`: `load_probe()`, `find_sessions()`,
`get_day_output_dir()`, the OpenEphys loading + same-day concatenation logic
from `process_day()`, and `session_boundaries.json` writing (including the
TTL event reading). This module should have NO dependency on Kilosort4 or
the assessment code — loading, probe binding, path resolution, and session
metadata only. Read config via `config_loader.py` (already in `src/`) rather
than a hardcoded CONFIG dict.

After the module is written, help me write `tests/test_io_utils.py` that
points at one real animal/day on my local machine and confirms: sessions
are discovered correctly, the probe loads with the right contact count, a
recording loads and concatenates, and `session_boundaries.json` is written
with sane frame offsets. I want to actually run this against real data
before we call this module done.

Ground rules: if this requires changing a function signature, config key,
or file format another module depends on (per ARCHITECTURE.md), flag it
before proceeding. If ARCHITECTURE.md is ambiguous or silent on something
this module needs, ask rather than guessing. At the end of the session,
summarize anything that should be added to or changed in ARCHITECTURE.md.

---

## Phase 2 — health_check.py

I'm continuing work on my modular electrophysiology pipeline (`ephys_pipeline`).
Before doing anything else, read `ARCHITECTURE.md` in this Project's
knowledge. Also check `src/io_utils.py` (should already exist from the
previous module conversation — if it's not in Project knowledge yet, ask me
to upload it before proceeding) and `sort_batch.py` for the logic being
migrated.

**Module for this conversation:** `src/health_check.py`

**Goal for this session:** Extract `detect_bad_channels`,
`find_bad_channels_for_recording`, `scan_saturation_fraction_per_channel`,
`detect_saturation_windows_per_channel`, and the shank-skip decision logic
(minimum channel count check) from `sort_batch.py`. This module should
depend only on `io_utils.py` for loading — nothing from Kilosort4 or
assessment. Add a NEW capability that doesn't exist in `sort_batch.py`
today: a standalone report mode that produces one health-check
summary per day (bad channels, saturation severity per channel, which
shanks would be skipped and why) without touching Kilosort4 at all.

Test this specifically against a day where I know a shank is mostly
dead/saturated (the case that caused a severe runtime bug in the
monolithic version). Confirm the new module finishes fast and correctly
flags/skips it — this is the direct regression test for that bug.

Ground rules: if this requires changing a function signature, config key,
or file format another module depends on (per ARCHITECTURE.md), flag it
before proceeding. If ARCHITECTURE.md is ambiguous or silent on something
this module needs, ask rather than guessing. At the end of the session,
summarize anything that should be added to or changed in ARCHITECTURE.md.

---

## Phase 3 — artifact_cleaning.py

I'm continuing work on my modular electrophysiology pipeline (`ephys_pipeline`).
Before doing anything else, read `ARCHITECTURE.md` in this Project's
knowledge. Also check `src/health_check.py` (should already exist — ask me
to upload it if it's not in Project knowledge yet) and `sort_batch.py`.

**Module for this conversation:** `src/artifact_cleaning.py`

**Goal for this session:** Extract `SaturationMutedRecording`,
`_SaturationMutedSegment`, and `merge_windows_across_channels` from
`sort_batch.py` into this module. It should consume the per-channel
saturation window format produced by `health_check.py` (see
ARCHITECTURE.md section 5 for the exact format) and apply muting.

This is the highest-risk untested piece of the whole pipeline so far — it
was never verified against a running SpikeInterface installation. Before
anything else, write an actual unit test in `tests/test_artifact_cleaning.py`
using a small synthetic recording (a few seconds, a few channels, with
known artificial saturation inserted at specific sample ranges on specific
channels) and verify the muted output is EXACTLY right — the flagged
samples zeroed, everything else (other channels, other time ranges on the
same channel) byte-for-byte unchanged. Don't consider this module done
until that test passes.

Once we know the noise diagnosis for the ~1kHz artifact (see
ARCHITECTURE.md section 7 open questions), this module will also need a
template-subtraction or comb-filter component — don't build that yet if
it's still undiagnosed, just leave a clear placeholder/TODO.

Ground rules: if this requires changing a function signature, config key,
or file format another module depends on (per ARCHITECTURE.md), flag it
before proceeding. If ARCHITECTURE.md is ambiguous or silent on something
this module needs, ask rather than guessing. At the end of the session,
summarize anything that should be added to or changed in ARCHITECTURE.md.

---

## Phase 4 — ap_sorter.py

I'm continuing work on my modular electrophysiology pipeline (`ephys_pipeline`).
Before doing anything else, read `ARCHITECTURE.md` in this Project's
knowledge. Also check `src/io_utils.py`, `src/health_check.py`,
`src/artifact_cleaning.py`, and `sort_batch.py` (ask me to upload any that
aren't in Project knowledge yet).

**Module for this conversation:** `src/ap_sorter.py`

**Goal for this session:** Extract the Kilosort4 orchestration logic from
`sort_batch.py`'s `process_day()` — the KS4 parameter wizard, bad-channel
exclusion application, and the `run_sorter` call itself — into this module,
now importing bad-channel/saturation logic from `health_check.py` and
`artifact_cleaning.py` instead of containing it inline. This module should
NOT contain the post-sort assessment logic (that's `quality_control.py`,
next phase) — it should stop once sorting is done and return the sorting
object(s) plus the per-shank metadata `quality_control.py` will need.

Test this by running it end-to-end on one real day locally and diffing the
output (unit counts, at minimum — ideally spike trains) against what the
current monolithic `sort_batch.py` produces on the exact same data. This
is the regression check that splitting the code didn't silently change
sorting behavior.

Ground rules: if this requires changing a function signature, config key,
or file format another module depends on (per ARCHITECTURE.md), flag it
before proceeding. If ARCHITECTURE.md is ambiguous or silent on something
this module needs, ask rather than guessing. At the end of the session,
summarize anything that should be added to or changed in ARCHITECTURE.md.

---

## Phase 5 — quality_control.py

I'm continuing work on my modular electrophysiology pipeline (`ephys_pipeline`).
Before doing anything else, read `ARCHITECTURE.md` in this Project's
knowledge. Also check `src/ap_sorter.py` and `sort_batch.py` (ask me to
upload `ap_sorter.py` if it's not in Project knowledge yet).

**Module for this conversation:** `src/quality_control.py`

**Goal for this session:** Extract `assess_shank`, `classify_unit`,
`compute_saturation_overlap`, `compute_per_session_presence_ratio`,
`export_shank_to_phy`, and `write_run_summary_csv` from `sort_batch.py`
into this module. It should take a sorting object (from `ap_sorter.py`) and
produce the classified units table, `run_summary.csv`, and the Phy export.

Test by running it against Phase 4's actual output on the same real day,
then physically opening one shank's Phy export and confirming it loads
without the original `recording.dat` error.

Ground rules: if this requires changing a function signature, config key,
or file format another module depends on (per ARCHITECTURE.md), flag it
before proceeding. If ARCHITECTURE.md is ambiguous or silent on something
this module needs, ask rather than guessing. At the end of the session,
summarize anything that should be added to or changed in ARCHITECTURE.md.

---

## Phase 6 — run_pipeline.py

I'm continuing work on my modular electrophysiology pipeline (`ephys_pipeline`).
Before doing anything else, read `ARCHITECTURE.md` in this Project's
knowledge. All of `src/io_utils.py`, `health_check.py`,
`artifact_cleaning.py`, `ap_sorter.py`, and `quality_control.py` should
exist by now — ask me to upload any missing ones.

**Module for this conversation:** `run_pipeline.py`

**Goal for this session:** Build the CLI entry point with subcommands
(`health-check`, `sort`, `qc`, `phy-export`, and a `--env local`/`--env fox`
flag using `config_loader.py`) that wire the modules above together. Each
subcommand should be independently runnable — I want to be able to run
just a health check, or just re-run QC, without re-sorting. Also think
through and propose the `--animal`/`--date` single-unit-of-work interface
needed for Slurm array batch mode on Fox (see `slurm/sort_one_day.sbatch`
in Project knowledge for the target invocation shape).

Test each subcommand in isolation against one real local day, then a full
local end-to-end run through all of them in sequence.

Ground rules: if this requires changing a function signature, config key,
or file format another module depends on (per ARCHITECTURE.md), flag it
before proceeding. At the end of the session, summarize anything that
should be added to or changed in ARCHITECTURE.md.

---

## Phase 7 — lfp_extractor.py (can be done any time after Phase 1, doesn't block the others)

I'm continuing work on my modular electrophysiology pipeline (`ephys_pipeline`).
Before doing anything else, read `ARCHITECTURE.md` in this Project's
knowledge. Also check `src/io_utils.py` and my existing
`depth_resolved_lfp.py` (please ask me to upload this if it's not already
in Project knowledge — it has the bandpass filtering and shorted-channel QC
logic this module should reuse rather than reimplement).

**Module for this conversation:** `src/lfp_extractor.py`

**Goal for this session:** Migrate the LFP-specific processing from
`depth_resolved_lfp.py` into this module, using `io_utils.py`'s loading
functions instead of `depth_resolved_lfp.py`'s own (likely duplicated)
loading code. Keep the depth-resolved visualization capability but
restructure around the shared config/path conventions in ARCHITECTURE.md.

Ground rules: if this requires changing a function signature, config key,
or file format another module depends on (per ARCHITECTURE.md), flag it
before proceeding. If ARCHITECTURE.md is ambiguous or silent on something
this module needs, ask rather than guessing. At the end of the session,
summarize anything that should be added to or changed in ARCHITECTURE.md.
