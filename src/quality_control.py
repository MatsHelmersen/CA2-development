"""
quality_control.py - post-sort unit assessment for ephys_pipeline.

Scope (per ARCHITECTURE.md Sec.9, now Sec.4e): classify every unit per
shank into "Noise/Artefact" / "MUA" / "SUA" (Sec.6 conventions: SNR not
absolute amplitude, isolation distance / L-ratio per Schmitzer-Torbert et
al. 2005, no hard gate on firing rate or whole-day presence ratio),
compute per-session presence ratio (Sec.5 session_boundaries.json
contract) and saturation overlap, export to Phy (known dtype gotcha,
SpikeInterface GH #2751 - wrapped defensively), and write run_summary.csv
(Sec.5 contract: commented metadata header + units table). This was
previously assess_shank / assess_only_day / write_run_summary_csv in
sort_batch.py - the per-unit metric logic is ported near-verbatim; the
monolithic script structure is not.

Explicitly OUT of scope (belongs elsewhere):
  - Bad channel / saturation / discharge DETECTION -> health_check.py
  - Saturation muting MECHANISM + health_report.json readback + the
    exclusion-then-muting reconstruction sequence -> artifact_cleaning.py
  - Kilosort4 / sorting itself (si.run_sorter call)  -> ap_sorter.py

============================================================================
DESIGN DECISIONS THIS SESSION (flagged per project convention)
============================================================================

1. NO SEPARATE "--assess-only" MODE (resolves ARCHITECTURE.md Sec.9's open
   question). sort_batch.py needed --assess-only because ONE script could
   either sort-then-assess or assess-only-existing-output. In this
   architecture, quality_control.py NEVER calls si.run_sorter() - that is
   exclusively ap_sorter.py's job. Every invocation of
   process_animal_day() below already only reads existing shank_*_ks4/
   output; it is inherently "assess-only" in sort_batch.py's sense. Want
   to re-classify units after changing assessment_thresholds in
   base.yaml? Just run this module again - no flag required. This is a
   deliberate simplification, not an oversight; flagged here rather than
   silently dropping the flag.

2. NON-NEGOTIABLE CONSTRAINT SATISFIED: every SortingAnalyzer built below
   is built against the output of
   artifact_cleaning.reconstruct_clean_recording_for_shank(cfg, shank_id,
   shank_rec, health_report, saturation_windows) - the same function
   ap_sorter.py calls immediately before si.run_sorter(). This module
   does not re-derive exclusions or re-decide the mute_before_sorting gate
   itself. health_report.json and saturation_windows.json are loaded via
   artifact_cleaning.load_health_report() / load_saturation_windows(),
   exactly as ap_sorter.process_animal_day() does.

3. STALE-REPORT HANDLING: if a shank has KS4 output on disk
   (shank_<id>_ks4/params.py exists) but
   reconstruct_clean_recording_for_shank() returns anything other than
   status == "PASS" for the CURRENT health_report.json /
   saturation_windows.json, that is surfaced as an ERROR in the returned
   summary (not a silent skip, not a silent "proceed anyway"). It means
   the report changed (re-run of health_check.py --report, or a config
   change) after that shank was sorted, and the recording this module
   would otherwise assess against no longer corresponds to what KS4 saw.
   A shank with NO KS4 output at all is a normal SKIP ("not yet sorted"),
   not an error - only "sorted but reconstruction now disagrees" is
   treated as stale-report-worthy.

4. KNOWN LIMITATION, FLAGGED NOT FIXED: prepare_day() (io_utils.py) has
   no persisted local binary cache of the concatenated recording in the
   current architecture - unlike sort_batch.py's original process_day(),
   which called recording.save(folder=binary_path, ...) once and let
   assess_only_day() reload that fast local binary via si.load(). Neither
   io_utils.py nor ap_sorter.py currently write such a cache (ap_sorter.py
   hands clean_rec straight to si.run_sorter() without saving it first).
   This means process_animal_day() below pays the SAME raw-OpenEphys read
   cost ap_sorter.py already paid at sort time, every time assessment is
   re-run. Fixing this would mean adding a shared "recording_binary"
   caching contract that BOTH ap_sorter.py and quality_control.py read
   from - a cross-module interface change out of scope for this session.
   Flagged in ARCHITECTURE.md Sec.7 as an open question rather than
   implemented silently here.

5. shank_sorted_output_present() below duplicates
   ap_sorter.existing_shank_output_present()'s one-line
   "shank_<id>_ks4/params.py exists" check locally, rather than importing
   it from ap_sorter.py. This is a deliberate small duplication, not
   drift: importing from ap_sorter.py would make the sorter module a
   dependency of the assessment module for a single boolean check, which
   is exactly the kind of backwards dependency Sec.4c/4d already avoided
   for the (much more consequential) exclusion+muting logic. If the
   "what counts as sorted" marker ever changes, it must be updated in
   BOTH modules - flagged here and in ARCHITECTURE.md Sec.6.

============================================================================
CHANGES THIS SESSION (against a user-supplied reference script,
assess_sorting_newest.py, using a newer SpikeInterface install than this
module was originally written against - see ARCHITECTURE.md Sec.4e-bugfix2
for the full comparison)
============================================================================

6. API/VERSION FIXES IN assess_shank() (adopted from the reference
   script - real bugs, not style preferences):
     a. metric_names=["mahalanobis"] REPLACES the old
        ["isolation_distance", "l_ratio"] request to analyzer.compute
        ("quality_metrics", ...). CONFIRMED against a real SpikeInterface
        install (user's dummy-data check, this session): requesting
        "mahalanobis" EXPANDS into two columns, "isolation_distance" and
        "l_ratio" - there is no column literally named "mahalanobis".
        Downstream code (classify_unit(), the isolation_distance/l_ratio
        plot panel) is UNCHANGED, since it already reads those two column
        names directly.
     b. "noise_levels" and "spike_amplitudes" extensions are now computed
        as explicit prerequisites - the reference script flags
        spike_amplitudes as "required for amplitude_cutoff". The old
        sequential per-extension calls omitted both; on the currently-
        installed SpikeInterface version this likely produced silently
        degraded (NaN or missing) snr/amplitude_cutoff rather than an
        error. NOT independently re-verified against a real install by
        this session beyond adopting the reference script's usage -
        flagged per Sec.8's "wrap unverified APIs" convention.
     c. si.remove_excess_spikes(sorting, rec) is now called before
        building the SortingAnalyzer - trims spikes whose sample time
        falls past the end of `rec`, a known KS4/SpikeInterface edge
        case that can otherwise raise inside create_sorting_analyzer()/
        waveform extraction. Safe to apply regardless of shank
        reconstruction status: `rec`'s frame count is unchanged by
        channel exclusion (drops channels, not samples) or saturation
        muting (zeroes samples in place, doesn't remove them), so it
        always matches what KS4 actually saw. Wrapped in try/except
        (not silently skipped on failure - a warning is printed so a
        downstream create_sorting_analyzer() failure can be traced back
        to this step if it's the cause).
     d. Extension computation is now batched (analyzer.compute({...},
        n_jobs=...)) for random_spikes/waveforms/templates/noise_levels/
        spike_amplitudes, per the reference script - faster via
        parallelism. principal_components (mahalanobis) is DELIBERATELY
        KEPT in its own separate try/except, NOT folded into the batch:
        isolation-metric APIs are what actually shift across
        SpikeInterface versions (per the pre-existing docstring note this
        module already carried), so keeping it isolated preserves
        graceful degradation - a PCA failure now degrades to
        unavailable isolation_distance/l_ratio (-> conservatively
        not-SUA, Sec.6) rather than aborting the whole shank's
        assessment the way a single all-in-one batch call would.

7. VISUALIZATION ADDED (this session) - plot_shank_qc() and
   plot_day_overview(), new, no prior equivalent in this module.
   DELIBERATELY NOT a port of the reference script's 4-panel figure -
   that figure's own classification gate (SNR + ISI + presence_ratio)
   and its plotted presence_ratio threshold line do not match this
   pipeline's actual classify_unit() logic (Sec.6: no hard gate on
   presence_ratio; isolation_distance/l_ratio, not presence_ratio, is
   the real SUA-only gate) - plotting the reference script's threshold
   lines against THIS pipeline's classifications would visually
   misrepresent why a unit was classified the way it was. Panels here
   were chosen to make classify_unit()'s ACTUAL decision boundaries
   visible instead; see plot_shank_qc()'s docstring for the panel-by-
   panel rationale. Config-gated via cfg["assessment"]["generate_plots"]
   (new optional key, default True if absent) and --skip-plots on the
   CLI, mirroring the existing export_to_phy skip pattern. Degrades to a
   printed warning (not an error) if matplotlib isn't installed.

Config is read via config_loader.load_config(env). Existing required keys
unchanged: cfg["assessment"]["run_unit_assessment"/"export_to_phy"/
"thresholds"], cfg["saturation_detection"]["window_pad_ms"] (see
ARCHITECTURE.md Sec.4). NEW OPTIONAL keys this session (all have in-code
defaults, so an unedited base.yaml keeps working unchanged - see
ARCHITECTURE.md for the recommended base.yaml additions):
  cfg["assessment"]["n_jobs"]              (default 1 if absent)
  cfg["assessment"]["waveforms_ms_before"] (default 1.0 if absent)
  cfg["assessment"]["waveforms_ms_after"]  (default 2.0 if absent)
  cfg["assessment"]["generate_plots"]      (default True if absent)
"""

import argparse
import copy
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import spikeinterface.full as si

# matplotlib is optional: plotting degrades to a printed warning (see
# plot_shank_qc/plot_day_overview) rather than failing the whole
# assessment run if it's unavailable. matplotlib.use("Agg") is set BEFORE
# importing pyplot so this is safe on headless Slurm/Fox compute nodes
# with no display.
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    _MATPLOTLIB_AVAILABLE = True
except ImportError:
    _MATPLOTLIB_AVAILABLE = False

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# Assumes this file lives at <repo_root>/src/quality_control.py per ARCHITECTURE.md Sec.3.
from src.config_loader import load_config
from src.io_utils import find_sessions, prepare_day, load_probe, select_session, get_day_output_dir
from src import artifact_cleaning

# Shared between classify_unit()'s three canonical labels (Sec.5 contract)
# and the QC plotting functions below - one place to keep the label
# strings and colors in sync.
CATEGORY_COLORS = {
    "Noise/Artefact": "#B0B0B0",
    "MUA": "#E8A33D",
    "SUA": "#3E9B4F",
}
CATEGORY_ORDER = ["Noise/Artefact", "MUA", "SUA"]


# ============================================================================
# PER-SHANK SORTED-OUTPUT CHECK (local, deliberately not imported from
# ap_sorter.py - see module docstring, Design Decision 5)
# ============================================================================

def shank_sorted_output_present(day_output_dir: str, shank_id) -> bool:
    """
    True if shank_<id>_ks4/sorter_output/params.py exists.

    BUG FIX (this session): si.run_sorter(folder=X) does NOT write
    params.py directly into X - Kilosort4's actual output (params.py,
    spike_times.npy, etc.) lands in X/sorter_output/, with X itself only
    holding spikeinterface_recording.json / _params.json / _log.json.
    The original check (X/params.py) never matched anything, so this
    function always returned False. si.read_sorter_folder(day_output_dir/
    shank_<id>_ks4) is unaffected by this bug - it already expects the
    OUTER folder and finds sorter_output/ internally itself; only this
    existence check was wrong. Mirrors ap_sorter.
    existing_shank_output_present()'s heuristic (now fixed there too -
    see ARCHITECTURE.md), duplicated locally per Design Decision 5.
    """
    params_path = os.path.join(day_output_dir, f"shank_{shank_id}_ks4", "sorter_output", "params.py")
    return os.path.exists(params_path)


def load_sorting_for_shank(day_output_dir: str, shank_id):
    """
    Load a shank's KS4 sorter output via si.read_sorter_folder().

    register_recording=False (CROSS-ENVIRONMENT FIX, this session):
    si.read_sorter_folder() defaults to also deserialising and attaching
    the RECORDING that was linked to the sorting at sort time, via
    spikeinterface_recording.json - which stores that recording's
    provenance, including the absolute path of the original OpenEphys
    session folder on whatever machine/environment ap_sorter.py actually
    ran on. If a shank was sorted on Fox (Linux paths under
    /fp/projects01/...) and is later assessed from biotin or local
    (Windows/UNC paths), that provenance path does not resolve on the
    new machine and si.read_sorter_folder() raises trying to load it -
    even though the sorting output itself (spike_times.npy etc.) is
    plain files and fully portable.

    This module never needs that attached recording: assess_shank() is
    always handed `clean_rec`, built fresh per-environment via
    artifact_cleaning.reconstruct_clean_recording_for_shank() on top of
    THIS run's own io_utils.prepare_day() call (see module docstring,
    Design Decision 2) - never the recording embedded in the sorter
    folder. register_recording=False makes this module's cross-
    environment portability actual, not incidental: a day sorted on Fox
    can be assessed from biotin (or vice versa) as long as the raw data
    and health_report.json/saturation_windows.json are reachable from
    wherever quality_control.py is being run.

    NOT VERIFIED against your installed SpikeInterface version's exact
    kwarg name/signature - wrapped defensively; falls back to the bare
    call (which will reproduce the original cross-environment failure)
    if register_recording isn't accepted, rather than masking a
    genuinely different error.
    """
    folder = os.path.join(day_output_dir, f"shank_{shank_id}_ks4")
    try:
        return si.read_sorter_folder(folder, register_recording=False)
    except TypeError:
        print(f"    Warning: si.read_sorter_folder() in your installed SpikeInterface "
              f"version does not accept register_recording= - falling back to the bare "
              f"call. If the sorting was produced on a different environment/machine "
              f"than this one, this may fail trying to resolve the original recording's "
              f"path; see load_sorting_for_shank()'s docstring.")
        return si.read_sorter_folder(folder)


# ============================================================================
# PER-UNIT METRICS (ported from sort_batch.py's assess_shank helpers,
# unchanged logic - see ARCHITECTURE.md Sec.6 for the conventions these
# encode: SNR not absolute amplitude, isolation distance / L-ratio per
# Schmitzer-Torbert et al. 2005, no hard gate on firing_rate or whole-day
# presence_ratio)
# ============================================================================

def compute_saturation_overlap(sorting, unit_id, saturation_windows_merged, fs, pad_ms):
    """
    Fraction of a unit's spikes falling within pad_ms of a flagged
    saturation window. `saturation_windows_merged` is the ALL-CHANNEL,
    start-sorted (start, end) list for this shank (from
    artifact_cleaning.merge_windows_across_channels()) - channel identity
    is deliberately dropped here, matching sort_batch.py's original
    semantics: a spike is flagged if it falls near a saturation event on
    ANY channel of this shank, not just the unit's own best channel.
    """
    if not saturation_windows_merged:
        return 0.0
    spike_train = sorting.get_unit_spike_train(unit_id)
    if len(spike_train) == 0:
        return 0.0
    pad_samples = int(pad_ms / 1000.0 * fs)
    starts = np.array([w[0] - pad_samples for w in saturation_windows_merged])
    ends = np.array([w[1] + pad_samples for w in saturation_windows_merged])
    order = np.argsort(starts)
    starts, ends = starts[order], ends[order]
    idx = np.searchsorted(starts, spike_train, side="right") - 1
    idx = np.clip(idx, 0, len(starts) - 1)
    within = (spike_train >= starts[idx]) & (spike_train <= ends[idx])
    return float(np.sum(within)) / len(spike_train)


def compute_per_session_presence_ratio(sorting, unit_id, session_metadata, n_bins_per_session=10):
    """
    Presence ratio computed PER SESSION rather than across the whole
    concatenated day (ARCHITECTURE.md Sec.6: "No hard gate on ...
    whole-day presence_ratio"). A place/social cell that fires only
    during specific trials would show near-zero whole-day presence ratio
    despite being a real, scientifically interesting unit - reporting it
    per-session instead avoids conflating selectivity with instability.
    Diagnostic only, never used for classification (see classify_unit()).

    Returns dict: session_path -> presence_ratio (float, or None if the
    session had zero recorded frames for this shank).
    """
    spike_train = sorting.get_unit_spike_train(unit_id)
    result = {}
    for sess in session_metadata:
        start = sess["frame_offset_in_concatenated"]
        end = start + sess["n_frames"]
        if sess["n_frames"] <= 0:
            result[sess["session_path"]] = None
            continue
        in_session = spike_train[(spike_train >= start) & (spike_train < end)] - start
        bin_edges = np.linspace(0, sess["n_frames"], n_bins_per_session + 1)
        counts, _ = np.histogram(in_session, bins=bin_edges)
        result[sess["session_path"]] = float(np.mean(counts > 0))
    return result


def classify_unit(row, thresholds):
    """
    Rule-based 3-tier classification: Noise/Artefact, MUA, SUA. Does NOT
    use firing_rate or whole-day presence_ratio as exclusion criteria -
    see ARCHITECTURE.md Sec.6.
    """
    if row["n_spikes"] < thresholds["min_spikes_total"]:
        return "Noise/Artefact"
    if row["saturation_overlap_frac"] >= thresholds["saturation_overlap_noise_frac"]:
        return "Noise/Artefact"
    if row["snr"] <= thresholds["snr_noise_max"]:
        return "Noise/Artefact"
    if row["isi_violations_ratio"] >= thresholds["isi_violations_ratio_noise_min"]:
        return "Noise/Artefact"

    is_sua = (
        row["snr"] >= thresholds["snr_sua_min"]
        and row["isi_violations_ratio"] <= thresholds["isi_violations_ratio_sua_max"]
        and (not pd.isna(row["isolation_distance"])
             and row["isolation_distance"] >= thresholds["isolation_distance_sua_min"])
        and (not pd.isna(row["l_ratio"]) and row["l_ratio"] <= thresholds["l_ratio_sua_max"])
    )
    return "SUA" if is_sua else "MUA"


def export_shank_to_phy(shank_id, analyzer, day_output_dir):
    """
    Export this shank's sorting to a Phy-ready folder via
    si.export_to_phy(copy_binary=True) - writes a per-shank recording.dat
    and a params.py with the correct dat_path/n_channels_dat for THIS
    shank, rather than pointing at a multi-shank binary.

    NOTE (documented SpikeInterface gotcha, GH #2751): export_to_phy can
    require waveforms computed with return_scaled=False (or a matching
    dtype); the wrong dtype produces a scaling error. Wrapped
    defensively - a failure here does not affect the unit
    classification already computed, only the Phy export step.
    copy_binary=True writes a full copy of this shank's binary - for a
    multi-hour concatenated day this can be large; that's exactly why
    this step is skippable (cfg["assessment"]["export_to_phy"] = False,
    or --skip-phy-export on the CLI).
    """
    from spikeinterface.exporters import export_to_phy
    phy_folder = os.path.join(day_output_dir, f"shank_{shank_id}_phy")
    try:
        export_to_phy(analyzer, phy_folder, copy_binary=True, remove_if_exists=True,
                       compute_pc_features=True, compute_amplitudes=True, verbose=False)
        print(f"    Exported shank {shank_id} to Phy: {phy_folder}")
        return phy_folder
    except Exception as e:
        print(f"    Warning: Phy export failed for shank {shank_id} ({e}). "
              f"Unit classification is unaffected.")
        return None


def assess_shank(cfg, shank_id, rec, sorting, session_metadata, fs, day_output_dir, day_tag,
                  saturation_windows_merged):
    """
    Full post-sort assessment for one shank: quality metrics via
    SortingAnalyzer, custom saturation overlap, custom per-session
    presence ratio, then classify every unit. Exports to Phy if enabled
    (reuses this same analyzer - no recomputation).

    `rec` MUST be the reconstructed recording from
    artifact_cleaning.reconstruct_clean_recording_for_shank() - see
    module docstring Design Decision 2 - not the raw split-by-shank
    recording.

    Returns (counts: dict, metrics_df: pd.DataFrame tagged with shank_id).
    """
    thresholds = cfg["assessment"]["thresholds"]
    assess_cfg = cfg.get("assessment", {})
    n_jobs = assess_cfg.get("n_jobs", 1)
    waveforms_ms_before = assess_cfg.get("waveforms_ms_before", 1.0)
    waveforms_ms_after = assess_cfg.get("waveforms_ms_after", 2.0)

    # Drop spikes whose sample time falls past the end of `rec` - see
    # module docstring, Change 6c. Safe regardless of reconstruction
    # status: exclusion/muting never change frame count, only channel
    # count or sample VALUES.
    try:
        sorting = si.remove_excess_spikes(sorting, rec)
    except Exception as e:
        print(f"    Warning: si.remove_excess_spikes failed ({e}) - proceeding with "
              f"the sorting as loaded. If create_sorting_analyzer() below fails with "
              f"an out-of-bounds spike time, this is the likely cause.")

    analyzer = si.create_sorting_analyzer(sorting, rec, sparse=True)

    # Batched compute for the well-established, version-stable extensions
    # (Change 6d). noise_levels/spike_amplitudes are new prerequisites
    # this session (Change 6b) - previously omitted, likely silently
    # degrading snr/amplitude_cutoff on the currently-installed version.
    analyzer.compute(
        {
            "random_spikes": {"max_spikes_per_unit": 500},
            "waveforms": {"ms_before": waveforms_ms_before, "ms_after": waveforms_ms_after},
            "templates": {},
            "noise_levels": {},
            "spike_amplitudes": {},  # required for amplitude_cutoff
        },
        n_jobs=n_jobs,
    )

    try:
        # NOTE: PCA/isolation-metric APIs are what actually shift across
        # SpikeInterface versions, so this stays in its own try/except
        # (NOT folded into the batched call above) - see module docstring
        # Change 6d for why. Degrades gracefully to NaN isolation metrics
        # (-> conservatively classified as not-SUA, per ARCHITECTURE.md
        # Sec.6) rather than crashing the whole shank's assessment.
        #
        # metric_names=["mahalanobis"] (Change 6a): CONFIRMED against a
        # real SpikeInterface install (user's dummy-data check) to expand
        # into "isolation_distance" and "l_ratio" columns - there is no
        # column literally named "mahalanobis". This replaces the old,
        # now-unsupported ["isolation_distance", "l_ratio"] metric_names
        # request; everything downstream is unchanged since it already
        # reads those two column names.
        analyzer.compute("principal_components", n_components=5, mode="by_channel_local")
        pca_metric_names = ["mahalanobis"]
    except Exception as e:
        print(f"    Warning: principal_components extension failed ({e}); "
              f"isolation_distance/l_ratio will be unavailable for this shank.")
        pca_metric_names = []

    metrics_ext = analyzer.compute("quality_metrics", metric_names=[
        "firing_rate", "snr", "isi_violation", "presence_ratio", "amplitude_cutoff",
    ] + pca_metric_names)
    metrics_df = metrics_ext.get_data().copy()

    # ISI violation column name varies across SpikeInterface versions.
    isi_col = None
    for candidate in ("isi_violations_ratio", "isi_violations_rate", "isi_violation"):
        if candidate in metrics_df.columns:
            isi_col = candidate
            break
    if isi_col is None:
        metrics_df["isi_violations_ratio"] = np.nan
        isi_col = "isi_violations_ratio"
    metrics_df = metrics_df.rename(columns={isi_col: "isi_violations_ratio"})

    n_spikes = {uid: len(sorting.get_unit_spike_train(uid)) for uid in sorting.unit_ids}
    metrics_df["n_spikes"] = pd.Series(n_spikes)

    # amplitude_cutoff is statistically unreliable below this spike count
    # (ARCHITECTURE.md Sec.6) - reported as NaN, not used.
    if "amplitude_cutoff" in metrics_df.columns:
        insufficient = metrics_df["n_spikes"] < thresholds["min_spikes_for_amplitude_cutoff"]
        metrics_df.loc[insufficient, "amplitude_cutoff"] = np.nan

    metrics_df["saturation_overlap_frac"] = [
        compute_saturation_overlap(sorting, uid, saturation_windows_merged, fs,
                                    cfg["saturation_detection"]["window_pad_ms"])
        for uid in metrics_df.index
    ]

    per_session_presence = {
        uid: compute_per_session_presence_ratio(sorting, uid, session_metadata)
        for uid in metrics_df.index
    }

    for col in ["snr", "isi_violations_ratio", "isolation_distance", "l_ratio"]:
        if col not in metrics_df.columns:
            metrics_df[col] = np.nan

    metrics_df["classification"] = metrics_df.apply(lambda row: classify_unit(row, thresholds), axis=1)

    for sess in session_metadata:
        sess_name = os.path.basename(sess["session_path"])
        col_name = f"presence_ratio_{sess_name}"
        metrics_df[col_name] = [per_session_presence[uid].get(sess["session_path"]) for uid in metrics_df.index]

    metrics_df.index.name = "unit_id"
    metrics_df = metrics_df.reset_index()
    metrics_df.insert(0, "shank_id", shank_id)

    if cfg.get("assessment", {}).get("export_to_phy", True):
        export_shank_to_phy(shank_id, analyzer, day_output_dir)

    if assess_cfg.get("generate_plots", True):
        qc_png_path = os.path.join(day_output_dir, f"shank_{shank_id}_qc_summary.png")
        plot_shank_qc(metrics_df, thresholds, shank_id, day_tag, qc_png_path)

    counts = metrics_df["classification"].value_counts().to_dict()
    for label in ("Noise/Artefact", "MUA", "SUA"):
        counts.setdefault(label, 0)
    return counts, metrics_df


# ============================================================================
# VISUALIZATION (new this session) - manual-inspection QC figures.
# See module docstring, Change 7, for why this is not a port of the
# reference script's figure.
# ============================================================================

def plot_shank_qc(metrics_df, thresholds, shank_id, day_tag, output_png):
    """
    Per-shank QC figure, 3x3 grid, for manual inspection of one shank's
    classification. Panels are chosen to make classify_unit()'s ACTUAL
    decision boundaries visible - not a generic quality-metrics
    dashboard, and not the reference script's panel selection (which
    plots a presence_ratio threshold and an snr+isi+presence decision
    space that don't match this pipeline's real gates - see module
    docstring Change 7):

      A. SNR vs ISI violation ratio - the primary Noise/MUA/SUA decision
         space, with THIS pipeline's actual thresholds drawn in.
      B. Isolation distance vs L-ratio - the SUA-only gate this pipeline
         actually uses (Schmitzer-Torbert et al. 2005) - computed but
         never plotted in the reference script.
      C. Classification counts (bar).
      D. n_spikes (log) - min_spikes_total is a hard Noise/Artefact gate
         in this pipeline; not present in the reference script at all.
      E. saturation_overlap_frac - a hard Noise/Artefact gate unique to
         this pipeline (no equivalent concept in the reference script,
         which has no saturation-muting machinery).
      F. amplitude_cutoff - diagnostic only (NaN below min spike count,
         see assess_shank()); NOT a classification criterion, no
         threshold line drawn.
      G. Firing rate (log) - diagnostic only, no threshold line
         (ARCHITECTURE.md Sec.6: never a classification criterion).
      H. Whole-day presence ratio - diagnostic only, no threshold line -
         DELIBERATELY unlike the reference script, which gates
         classification on presence_ratio >= 0.8. This pipeline does not
         gate on presence_ratio (Sec.6), specifically so a real
         place/social cell that only fires in certain trials isn't
         penalised for low whole-day presence; per-session presence
         ratio (which WOULD show such a cell as present) lives in
         run_summary.csv's presence_ratio_<session> columns, not here.
      I. Text panel: the exact threshold values in effect for this run.

    Degrades to a printed warning (returns None) if matplotlib isn't
    installed or there are zero units - never raises, since a plotting
    failure should not invalidate an otherwise-successful assessment.
    """
    if not _MATPLOTLIB_AVAILABLE:
        print(f"    Skipping QC figure for shank {shank_id}: matplotlib not installed.")
        return None
    if metrics_df.empty:
        return None

    try:
        n_units = len(metrics_df)
        fig, axes = plt.subplots(3, 3, figsize=(16, 14))
        fig.suptitle(f"{day_tag} - Shank {shank_id}  (N = {n_units} units)",
                     fontsize=14, fontweight="bold")
        colors = metrics_df["classification"].map(CATEGORY_COLORS).fillna("#000000")

        # --- A: SNR vs ISI violation ratio (primary decision space) ---
        ax = axes[0, 0]
        isi_vals = metrics_df["isi_violations_ratio"].clip(lower=1e-4)
        snr_series = metrics_df["snr"]
        snr_max = max(
            snr_series.max() * 1.15 if snr_series.notna().any() else 5,
            thresholds["snr_sua_min"] * 1.5, 5)
        ax.scatter(isi_vals, snr_series, c=colors, s=45, edgecolor="k", linewidth=0.4, zorder=3)
        ax.axvline(thresholds["isi_violations_ratio_sua_max"], color=CATEGORY_COLORS["SUA"],
                   ls="--", lw=1, alpha=0.8, label="SUA ISI max")
        ax.axvline(thresholds["isi_violations_ratio_noise_min"], color=CATEGORY_COLORS["Noise/Artefact"],
                   ls="--", lw=1, alpha=0.8, label="Noise ISI min")
        ax.axhline(thresholds["snr_sua_min"], color=CATEGORY_COLORS["SUA"], ls="--", lw=1, alpha=0.8)
        ax.axhline(thresholds["snr_noise_max"], color=CATEGORY_COLORS["Noise/Artefact"], ls="--", lw=1, alpha=0.8)
        ax.set_xscale("log")
        ax.set_xlim(1e-4, max(isi_vals.max() * 1.3, 1.0))
        ax.set_ylim(0, snr_max)
        ax.set_xlabel("ISI violation ratio (log)")
        ax.set_ylabel("SNR")
        ax.set_title("A. SNR vs ISI (Noise/MUA/SUA gate)", fontsize=10)
        ax.legend(fontsize=7, loc="upper right")

        # --- B: isolation_distance vs l_ratio (SUA-only gate) ---
        ax = axes[0, 1]
        has_iso = metrics_df["isolation_distance"].notna() & metrics_df["l_ratio"].notna()
        if has_iso.any():
            sub = metrics_df[has_iso]
            ax.scatter(sub["l_ratio"], sub["isolation_distance"], c=colors[has_iso],
                       s=45, edgecolor="k", linewidth=0.4, zorder=3)
            ax.axvline(thresholds["l_ratio_sua_max"], color=CATEGORY_COLORS["SUA"], ls="--", lw=1, alpha=0.8)
            ax.axhline(thresholds["isolation_distance_sua_min"], color=CATEGORY_COLORS["SUA"], ls="--", lw=1, alpha=0.8)
            ax.set_xlabel("L-ratio")
            ax.set_ylabel("Isolation distance")
        else:
            ax.text(0.5, 0.5, "isolation_distance/l_ratio\nunavailable this run\n(PCA extension failed)",
                    ha="center", va="center", transform=ax.transAxes, fontsize=9, color="gray")
        ax.set_title("B. Isolation distance vs L-ratio (SUA gate)", fontsize=10)

        # --- C: classification counts ---
        ax = axes[0, 2]
        counts = metrics_df["classification"].value_counts().reindex(CATEGORY_ORDER, fill_value=0)
        bars = ax.bar(counts.index, counts.values, color=[CATEGORY_COLORS[c] for c in counts.index],
                       edgecolor="k", linewidth=0.5)
        for b, v in zip(bars, counts.values):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.02 * max(counts.values.max(), 1), str(v),
                    ha="center", va="bottom", fontsize=10)
        ax.set_ylabel("Unit count")
        ax.set_title("C. Classification counts", fontsize=10)

        # --- D: n_spikes (min_spikes_total gate) ---
        ax = axes[1, 0]
        n_spk_all = metrics_df["n_spikes"].clip(lower=1)
        bins = np.logspace(np.log10(max(n_spk_all.min(), 1)), np.log10(n_spk_all.max() * 1.05 + 1), 20)
        for cat in CATEGORY_ORDER:
            vals = metrics_df.loc[metrics_df["classification"] == cat, "n_spikes"].clip(lower=1)
            if len(vals):
                ax.hist(vals, bins=bins, color=CATEGORY_COLORS[cat], alpha=0.8, label=cat,
                        edgecolor="white", linewidth=0.5)
        ax.axvline(thresholds["min_spikes_total"], color="k", ls="--", lw=1, alpha=0.7, label="min_spikes_total")
        ax.set_xscale("log")
        ax.set_xlabel("n_spikes (log)")
        ax.set_ylabel("Unit count")
        ax.set_title("D. Spike count (Noise/Artefact gate)", fontsize=10)
        ax.legend(fontsize=7)

        # --- E: saturation overlap fraction (Noise/Artefact gate) ---
        ax = axes[1, 1]
        bins = np.linspace(0, 1, 21)
        for cat in CATEGORY_ORDER:
            vals = metrics_df.loc[metrics_df["classification"] == cat, "saturation_overlap_frac"]
            if len(vals):
                ax.hist(vals, bins=bins, color=CATEGORY_COLORS[cat], alpha=0.8, label=cat,
                        edgecolor="white", linewidth=0.5)
        ax.axvline(thresholds["saturation_overlap_noise_frac"], color="k", ls="--", lw=1, alpha=0.7,
                   label="noise threshold")
        ax.set_xlabel("Saturation overlap fraction")
        ax.set_ylabel("Unit count")
        ax.set_title("E. Saturation overlap (Noise/Artefact gate)", fontsize=10)
        ax.legend(fontsize=7)

        # --- F: amplitude_cutoff (diagnostic only) ---
        ax = axes[1, 2]
        if "amplitude_cutoff" in metrics_df.columns and metrics_df["amplitude_cutoff"].notna().any():
            for cat in CATEGORY_ORDER:
                vals = metrics_df.loc[metrics_df["classification"] == cat, "amplitude_cutoff"].dropna()
                if len(vals):
                    ax.hist(vals, bins=15, color=CATEGORY_COLORS[cat], alpha=0.8, label=cat,
                            edgecolor="white", linewidth=0.5)
            ax.set_xlabel("Amplitude cutoff")
            ax.set_ylabel("Unit count")
            ax.legend(fontsize=7)
        else:
            ax.text(0.5, 0.5, "No units with sufficient\nspikes for amplitude_cutoff",
                    ha="center", va="center", transform=ax.transAxes, fontsize=9, color="gray")
        ax.set_title("F. Amplitude cutoff (diagnostic, NOT a gate)", fontsize=10)

        # --- G: firing rate (diagnostic only) ---
        ax = axes[2, 0]
        fr_all = metrics_df["firing_rate"].clip(lower=1e-3)
        if fr_all.notna().any():
            bins = np.logspace(np.log10(max(fr_all.min(skipna=True), 1e-3)),
                               np.log10(fr_all.max(skipna=True) * 1.05 + 1e-3), 20)
            for cat in CATEGORY_ORDER:
                vals = metrics_df.loc[metrics_df["classification"] == cat, "firing_rate"].clip(lower=1e-3)
                if len(vals):
                    ax.hist(vals, bins=bins, color=CATEGORY_COLORS[cat], alpha=0.8, label=cat,
                            edgecolor="white", linewidth=0.5)
            ax.set_xscale("log")
            ax.legend(fontsize=7)
        ax.set_xlabel("Firing rate (Hz, log)")
        ax.set_ylabel("Unit count")
        ax.set_title("G. Firing rate (diagnostic, NOT a gate)", fontsize=10)

        # --- H: whole-day presence ratio (diagnostic only) ---
        ax = axes[2, 1]
        if "presence_ratio" in metrics_df.columns:
            bins = np.linspace(0, 1, 21)
            for cat in CATEGORY_ORDER:
                vals = metrics_df.loc[metrics_df["classification"] == cat, "presence_ratio"].dropna()
                if len(vals):
                    ax.hist(vals, bins=bins, color=CATEGORY_COLORS[cat], alpha=0.8, label=cat,
                            edgecolor="white", linewidth=0.5)
            ax.legend(fontsize=7)
        ax.set_xlabel("Whole-day presence ratio")
        ax.set_ylabel("Unit count")
        ax.set_title("H. Presence ratio (diagnostic, NOT a gate -\nsee per-session columns in run_summary.csv)", fontsize=9)

        # --- I: thresholds text summary ---
        ax = axes[2, 2]
        ax.axis("off")
        lines = ["Thresholds in effect:"]
        for k, v in thresholds.items():
            lines.append(f"  {k} = {v}")
        ax.text(0.02, 0.98, "\n".join(lines), transform=ax.transAxes, fontsize=8,
                va="top", ha="left", family="monospace")
        ax.set_title("I. Thresholds", fontsize=10)

        handles = [mpatches.Patch(color=CATEGORY_COLORS[c], label=c) for c in CATEGORY_ORDER]
        fig.legend(handles=handles, loc="lower center", ncol=3, bbox_to_anchor=(0.5, -0.01), fontsize=10)
        fig.tight_layout(rect=[0, 0.02, 1, 0.96])
        fig.savefig(output_png, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"    Wrote QC figure: {output_png}")
        return output_png
    except Exception as e:
        print(f"    Warning: QC figure generation failed for shank {shank_id} ({e}). "
              f"Unit classification and run_summary.csv are unaffected.")
        return None


def plot_day_overview(units_df, day_tag, output_png):
    """
    Day-level overview PNG: stacked horizontal bar of classification
    counts per shank, so cross-shank trends for one day/session are
    visible without opening every per-shank figure. Adapted from the
    reference script's animal-level overview, but scoped to one day -
    this module's processing unit is one animal/day(/session)
    (ARCHITECTURE.md Sec.6: concatenate same-day sessions), not a whole
    animal across many days.

    Degrades to a printed warning (returns None) if matplotlib isn't
    installed or units_df is empty/None - never raises.
    """
    if not _MATPLOTLIB_AVAILABLE:
        print("  Skipping day overview figure: matplotlib not installed.")
        return None
    if units_df is None or units_df.empty:
        return None

    try:
        pivot = units_df.pivot_table(index="shank_id", columns="classification", values="unit_id",
                                      aggfunc="count", fill_value=0).reindex(columns=CATEGORY_ORDER, fill_value=0)
        fig, ax = plt.subplots(figsize=(8, max(3, 0.6 * len(pivot))))
        left = np.zeros(len(pivot))
        for cat in CATEGORY_ORDER:
            ax.barh([f"shank {s}" for s in pivot.index], pivot[cat], left=left,
                    color=CATEGORY_COLORS[cat], label=cat, edgecolor="k", linewidth=0.4)
            left += pivot[cat].values
        ax.set_xlabel("Unit count")
        ax.set_title(f"{day_tag} - unit classification by shank", fontsize=12, fontweight="bold")
        ax.legend(loc="lower right")
        fig.tight_layout()
        fig.savefig(output_png, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"  Wrote day overview figure: {output_png}")
        return output_png
    except Exception as e:
        print(f"  Warning: day overview figure generation failed ({e}). "
              f"run_summary.csv is unaffected.")
        return None


# ============================================================================
# run_summary.csv (Sec.5 contract: commented metadata header + units table)
# ============================================================================

def write_run_summary_csv(day_output_dir, day_tag, animal_id, date_str, session_paths, probe,
                           cfg, concatenating, shank_reconstruction, shank_unit_counts,
                           units_df=None):
    """
    Write run_summary.csv: commented ('#') metadata header, followed by
    the combined per-unit table across all assessed shanks.
    pandas.read_csv(path, comment='#') skips the header cleanly for
    programmatic use (ARCHITECTURE.md Sec.5 contract); the file is still
    human-readable directly in a text editor.

    shank_reconstruction: dict shank_id -> the result dict returned by
    artifact_cleaning.reconstruct_clean_recording_for_shank() for that
    shank (only shanks that were actually assessed this run - i.e.
    status == "PASS" and had KS4 output).
    """
    header = []
    header.append(f"# UNIT ASSESSMENT RUN SUMMARY - {day_tag}")
    header.append(f"# Generated: {datetime.now().isoformat(timespec='seconds')}")
    header.append(f"# Animal ID: {animal_id}")
    header.append(f"# Date: {date_str}")
    header.append(f"# Config env: {cfg.get('_env', 'unknown')}")
    header.append(f"# Mode: {'concatenated (same-day sessions merged)' if concatenating else 'individual session (not concatenated)'}")
    header.append(f"# Sessions ({len(session_paths)}): " + " | ".join(session_paths))
    if probe is not None:
        header.append(f"# Probe: {probe.get_contact_count()} contacts, {probe.get_shank_count()} shanks")
    header.append(f"# Stream name: {cfg.get('recording', {}).get('stream_name', '?')}")

    header.append("# --- Shank reconstruction (health_report.json exclusions + saturation muting) ---")
    for shank_id, recon in shank_reconstruction.items():
        header.append(f"#   shank {shank_id}: {recon['message']}")

    header.append("# --- Kilosort4 parameters (from config, as sorted by ap_sorter.py) ---")
    for k, v in cfg.get("ks4_params", {}).items():
        header.append(f"#   {k}: {v}")

    header.append("# --- Assessment thresholds ---")
    header.append("# NOTE: SNR-based, not absolute amplitude (see ARCHITECTURE.md Sec.6). "
                   "Firing rate and whole-day presence ratio are reported per unit below but "
                   "NOT used as exclusion criteria.")
    for k, v in cfg.get("assessment", {}).get("thresholds", {}).items():
        header.append(f"#   {k}: {v}")

    header.append("# --- Sorting output (units assessed per shank) ---")
    total_units = 0
    for shank_id, n_units in shank_unit_counts.items():
        header.append(f"#   shank {shank_id}: {n_units} units -> shank_{shank_id}_ks4/")
        total_units += n_units
    header.append(f"#   total units across assessed shanks: {total_units}")

    if units_df is not None and not units_df.empty:
        header.append("# --- Unit classification totals ---")
        totals = units_df["classification"].value_counts().to_dict()
        header.append(f"#   SUA={totals.get('SUA', 0)}  MUA={totals.get('MUA', 0)}  "
                       f"Noise/Artefact={totals.get('Noise/Artefact', 0)}")

    csv_path = os.path.join(day_output_dir, "run_summary.csv")
    with open(csv_path, "w", newline="") as f:
        f.write("\n".join(header) + "\n")
        if units_df is not None and not units_df.empty:
            units_df.to_csv(f, index=False)
        else:
            f.write("# (no units were assessed for this day)\n")

    print(f"  Wrote run_summary.csv")
    return csv_path


# ============================================================================
# PER-DAY / PER-SESSION ORCHESTRATION
# ============================================================================

def process_animal_day(cfg, animal_id, date_str, session_name=None, shank_filter=None,
                        skip_staging=True, dry_run=False, skip_phy_export=False,
                        skip_plots=False):
    """
    Load one animal/day (or single session), read back health_report.json
    and saturation_windows.json, split by shank, and assess every shank
    that has KS4 output on disk AND reconstructs to status == "PASS"
    against the CURRENT reports (or just shank_filter, if given).

    Returns a summary dict: {"assessed": [...], "skipped": [...],
    "errors": [...]} of human-readable strings. Does not raise for
    ordinary per-shank problems (caught and reported); a KeyboardInterrupt
    is not expected here (no interactive prompts) but is not suppressed
    if raised from underneath.
    """
    session_tag = f" / session {session_name}" if session_name else ""
    day_tag = f"{animal_id}/{date_str}{session_tag}"
    print(f"\n{'='*70}\nquality_control: {day_tag}\n{'='*70}")

    summary = {"assessed": [], "skipped": [], "errors": []}

    grouped = find_sessions(cfg, animal_filter=animal_id)
    key = (animal_id, date_str)
    if key not in grouped:
        msg = f"{animal_id}/{date_str}: no sessions found."
        print(f"  [FAIL] {msg}")
        summary["errors"].append(msg)
        return summary

    session_paths = grouped[key]
    concatenate = True
    if session_name is not None:
        try:
            session_paths = select_session(session_paths, session_name)
        except ValueError as e:
            print(f"  [FAIL] {e}")
            summary["errors"].append(f"{animal_id}/{date_str}: {e}")
            return summary
        concatenate = False

    cfg_for_run = copy.deepcopy(cfg)
    if skip_staging:
        cfg_for_run.setdefault("paths", {})["stage_raw_locally"] = None
    if skip_phy_export:
        cfg_for_run.setdefault("assessment", {})["export_to_phy"] = False
    if skip_plots:
        cfg_for_run.setdefault("assessment", {})["generate_plots"] = False

    # See module docstring Design Decision 4: this re-reads raw OpenEphys
    # data via prepare_day() - there is currently no persisted local
    # binary cache this module can load from instead.
    try:
        probe = load_probe(cfg_for_run)
        result = prepare_day(cfg_for_run, animal_id, date_str, session_paths, probe,
                              concatenate=concatenate)
    except Exception as e:
        msg = f"Data preparation failed: {e}"
        print(f"  [FAIL] {msg}")
        traceback.print_exc()
        summary["errors"].append(f"{day_tag}: {msg}")
        return summary

    rec = result["recording"]
    session_metadata = result["session_metadata"]
    fs = result["fs"]
    day_output_dir = result["day_output_dir"]

    try:
        health_report = artifact_cleaning.load_health_report(day_output_dir)
    except FileNotFoundError as e:
        print(f"  [FAIL] {e}")
        summary["errors"].append(f"{day_tag}: {e}")
        return summary

    try:
        saturation_windows = artifact_cleaning.load_saturation_windows(day_output_dir)
    except FileNotFoundError as e:
        print(f"  [FAIL] {e}")
        summary["errors"].append(f"{day_tag}: {e}")
        return summary

    recording_split = rec.split_by("group")

    shank_reconstruction = {}
    shank_unit_counts = {}
    all_shank_dfs = []

    for shank_id, shank_rec in recording_split.items():
        if shank_filter is not None and str(shank_id) != str(shank_filter):
            continue

        tag = f"{day_tag} shank {shank_id}"
        has_output = shank_sorted_output_present(day_output_dir, shank_id)

        try:
            recon = artifact_cleaning.reconstruct_clean_recording_for_shank(
                cfg_for_run, shank_id, shank_rec, health_report, saturation_windows)
        except Exception as e:
            msg = f"{tag}: reconstruct_clean_recording_for_shank raised: {e}"
            print(f"  [FAIL] {msg}")
            traceback.print_exc()
            summary["errors"].append(msg)
            continue

        if not has_output:
            msg = f"{tag}: no shank_{shank_id}_ks4/params.py on disk - not yet sorted."
            print(f"  [SKIP] {msg}")
            summary["skipped"].append(msg)
            continue

        if recon["status"] != "PASS":
            # Design Decision 3: sorted output EXISTS but the current
            # reports disagree with what must have produced it - loud
            # error, never a silent skip or silent proceed.
            msg = (f"{tag}: KS4 output EXISTS but reconstruct_clean_recording_for_shank "
                   f"now returns status={recon['status']} ({recon['message']}) - STALE REPORT. "
                   f"health_report.json / saturation_windows.json likely changed after this "
                   f"shank was sorted, or config changed. Re-run health_check.py --report and "
                   f"ap_sorter.py before trusting assessment for this shank.")
            print(f"  [FAIL] {msg}")
            summary["errors"].append(msg)
            continue

        shank_reconstruction[shank_id] = recon
        clean_rec = recon["recording"]
        print(f"  Shank {shank_id}: {recon['message']}")

        try:
            sorting = load_sorting_for_shank(day_output_dir, shank_id)
        except Exception as e:
            msg = f"{tag}: failed to load sorter folder: {e}"
            print(f"  [FAIL] {msg}")
            traceback.print_exc()
            summary["errors"].append(msg)
            continue

        n_units = len(sorting.unit_ids)
        shank_unit_counts[shank_id] = n_units

        if n_units == 0:
            msg = f"{tag}: 0 units in KS4 output - nothing to assess."
            print(f"  [SKIP] {msg}")
            summary["skipped"].append(msg)
            continue

        if dry_run:
            msg = (f"{tag}: [DRY RUN] would assess {n_units} unit(s) against "
                   f"{clean_rec.get_num_channels()} channel(s) (no metrics computed).")
            print(f"  {msg}")
            summary["assessed"].append(msg)
            continue

        sat_windows_shank_dict = saturation_windows.get("shanks", {}).get(str(shank_id), {})
        merged_sat_windows = artifact_cleaning.merge_windows_across_channels(sat_windows_shank_dict)

        try:
            print(f"  Assessing units on shank {shank_id}...")
            counts, metrics_df = assess_shank(
                cfg_for_run, shank_id, clean_rec, sorting, session_metadata, fs,
                day_output_dir, tag, merged_sat_windows)
        except Exception as e:
            msg = f"{tag}: assess_shank raised: {e}"
            print(f"  [FAIL] {msg}")
            traceback.print_exc()
            summary["errors"].append(msg)
            continue

        all_shank_dfs.append(metrics_df)
        summary["assessed"].append(
            f"{tag}: SUA={counts['SUA']} MUA={counts['MUA']} Noise/Artefact={counts['Noise/Artefact']}")

    if dry_run:
        return summary

    if shank_reconstruction:
        units_df = pd.concat(all_shank_dfs, ignore_index=True) if all_shank_dfs else None
        write_run_summary_csv(day_output_dir, day_tag, animal_id, date_str, session_paths, probe,
                               cfg_for_run, concatenate, shank_reconstruction, shank_unit_counts,
                               units_df)
        if cfg_for_run.get("assessment", {}).get("generate_plots", True):
            overview_png_path = os.path.join(day_output_dir, "qc_overview.png")
            plot_day_overview(units_df, day_tag, overview_png_path)

    return summary


# ============================================================================
# SELF-CHECK (module-local verification, per ARCHITECTURE.md Sec.8)
# ============================================================================

def self_check(cfg: dict, day_output_dir: str = None) -> list:
    """
    Cheap, read-only checks for this module's own responsibilities: config
    keys it reads, and (if day_output_dir given) that health_report.json /
    saturation_windows.json are present and at least one shank_*_ks4
    folder exists to assess. Does not load raw recordings or build a
    SortingAnalyzer.
    """
    results = []

    def check(level, msg):
        results.append((level, msg))
        print(f"  [{level}] {msg}")

    print(f"\n{'='*70}\nquality_control.py self-check (env={cfg.get('_env', '?')})\n{'='*70}")

    assess_cfg = cfg.get("assessment", {})
    if assess_cfg:
        check("PASS", f"assessment config present (run_unit_assessment="
                       f"{assess_cfg.get('run_unit_assessment')}, "
                       f"export_to_phy={assess_cfg.get('export_to_phy')})")
    else:
        check("FAIL", "cfg['assessment'] missing or empty")

    thresholds = assess_cfg.get("thresholds", {})
    required_keys = ["min_spikes_total", "min_spikes_for_amplitude_cutoff", "snr_noise_max",
                      "snr_sua_min", "isi_violations_ratio_sua_max", "isi_violations_ratio_noise_min",
                      "isolation_distance_sua_min", "l_ratio_sua_max", "saturation_overlap_noise_frac"]
    missing_thresh = [k for k in required_keys if k not in thresholds]
    if missing_thresh:
        check("FAIL", f"cfg['assessment']['thresholds'] missing key(s): {missing_thresh}")
    else:
        check("PASS", f"all {len(required_keys)} assessment threshold keys present")

    if "window_pad_ms" not in cfg.get("saturation_detection", {}):
        check("WARN", "cfg['saturation_detection']['window_pad_ms'] not set - "
                       "compute_saturation_overlap will fail without it.")
    else:
        check("PASS", "saturation_detection.window_pad_ms present")

    if _MATPLOTLIB_AVAILABLE:
        check("PASS", "matplotlib importable - QC figures (plot_shank_qc/plot_day_overview) "
                       "will be generated unless generate_plots=False or --skip-plots.")
    else:
        check("WARN", "matplotlib not importable - QC figures will be skipped (numerical "
                       "assessment and run_summary.csv are unaffected). "
                       "Run: pip install matplotlib")

    if day_output_dir is not None:
        hr_path = os.path.join(day_output_dir, "health_report.json")
        sw_path = os.path.join(day_output_dir, "saturation_windows.json")
        if os.path.exists(hr_path):
            check("PASS", f"health_report.json present at {hr_path}")
        else:
            check("FAIL", f"health_report.json not found at {hr_path} - run "
                          f"health_check.py --report first.")
        if os.path.exists(sw_path):
            check("PASS", f"saturation_windows.json present at {sw_path}")
        else:
            check("FAIL", f"saturation_windows.json not found at {sw_path}.")

        shank_dirs = [d for d in (os.listdir(day_output_dir) if os.path.isdir(day_output_dir) else [])
                      if d.startswith("shank_") and d.endswith("_ks4")]
        n_with_params = sum(
            1 for d in shank_dirs
            if os.path.exists(os.path.join(day_output_dir, d, "params.py")))
        if n_with_params:
            check("PASS", f"{n_with_params} shank_*_ks4/params.py found - "
                          f"there is KS4 output to assess.")
        else:
            check("WARN", f"no completed shank_*_ks4/params.py found under {day_output_dir} - "
                          f"run ap_sorter.py --run first, or this run will have nothing to assess.")
    else:
        check("PASS", "no day_output_dir given - skipped health_report.json / "
                       "saturation_windows.json / sorted-output checks (pass --day-output-dir, "
                       "or --animal/--date, to validate a specific day)")

    n_fail = sum(1 for level, _ in results if level == "FAIL")
    n_warn = sum(1 for level, _ in results if level == "WARN")
    print(f"\n{n_fail} FAIL, {n_warn} WARN, "
          f"{sum(1 for l, _ in results if l == 'PASS')} PASS\n")
    return results


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Post-sort unit assessment for ephys_pipeline. Reads exclusions/muting "
                    "back via artifact_cleaning.reconstruct_clean_recording_for_shank() so "
                    "metrics are computed against exactly what Kilosort4 saw. Never sorts - "
                    "ap_sorter.py must be run first. Re-run this module (with no special flag) "
                    "after changing assessment_thresholds in base.yaml to re-classify units.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Cheap config-only check:
  python quality_control.py --env local --check

  # Check that a specific day has health_report.json / saturation_windows.json
  # and at least one completed shank_*_ks4 folder before attempting assessment:
  python quality_control.py --env local --check --animal 213868 --date 20231105

  # Assess every PASS/sorted shank for one animal/day:
  python quality_control.py --env local --run --animal 213868 --date 20231105

  # Dry run: see which shanks would be assessed, with how many units/channels,
  # without building a SortingAnalyzer or writing run_summary.csv:
  python quality_control.py --env local --run --animal 213868 --date 20231105 --dry-run

  # Re-classify after editing assessment_thresholds in base.yaml - just run again:
  python quality_control.py --env local --run --animal 213868 --date 20231105

  # Assess only shank 2, for a single-session day (probe/drive moved mid-day):
  python quality_control.py --env local --run --animal 213868 --date 20231105 --session-name 20231105_002 --shank 2

  # Skip the (slow, disk-heavy) Phy export step:
  python quality_control.py --env local --run --animal 213868 --date 20231105 --skip-phy-export
""",
    )
    parser.add_argument("--env", required=True, choices=["local", "fox", "biotin"])

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                       help="Run self_check(): cheap, read-only validation of config, and "
                            "(if a day is specified) health_report.json/saturation_windows.json/"
                            "sorted-output presence.")
    mode.add_argument("--run", action="store_true",
                       help="Actually assess. Requires --animal and --date.")

    day_group = parser.add_mutually_exclusive_group()
    day_group.add_argument("--day-output-dir", default=None,
                            help="(--check only) Day output directory to validate directly. "
                                 "Mutually exclusive with --animal/--date.")
    day_group.add_argument("--animal", default=None,
                            help="Animal ID. Required for --run; optional for --check.")

    parser.add_argument("--date", default=None, help="Date string YYYYMMDD.")
    parser.add_argument("--session-name", default=None,
                         help="Restrict to a single session (e.g. '20231105_002') - forces "
                              "individual-session mode, matching health_check.py --report "
                              "--session-name / ap_sorter.py --session-name for the same "
                              "animal/date.")
    parser.add_argument("--shank", default=None,
                         help="Assess only this shank ID (e.g. '2') instead of every sorted "
                              "shank for the day.")
    parser.add_argument("--with-staging", action="store_true",
                         help="(--run) Don't bypass stage_raw_locally - actually test the "
                              "staging copy too (slower).")
    parser.add_argument("--dry-run", action="store_true",
                         help="(--run) Reconstruct exclusions/muting and report how many "
                              "units/channels would be assessed per shank, without building a "
                              "SortingAnalyzer or writing run_summary.csv.")
    parser.add_argument("--skip-phy-export", action="store_true",
                         help="(--run) Skip exporting each assessed shank to a Phy-ready "
                              "folder (saves disk space / time on long recordings).")
    parser.add_argument("--skip-plots", action="store_true",
                         help="(--run) Skip generating per-shank QC figures and the "
                              "day-level overview figure (matplotlib PNGs). Numerical "
                              "assessment and run_summary.csv are unaffected either way.")

    args = parser.parse_args()

    if args.date and not args.animal:
        parser.error("--date requires --animal.")
    if args.session_name and not args.animal:
        parser.error("--session-name requires --animal/--date.")
    if args.shank and not args.run:
        parser.error("--shank only applies to --run.")
    if args.run and (not args.animal or not args.date):
        parser.error("--run requires --animal and --date.")
    if args.day_output_dir and args.run:
        parser.error("--day-output-dir is only supported with --check; --run needs "
                      "--animal/--date so prepare_day() can resolve paths itself.")

    cfg = load_config(args.env)

    if args.check:
        resolved_day_output_dir = args.day_output_dir
        if args.animal and args.date:
            resolved_day_output_dir = get_day_output_dir(
                cfg, args.animal, args.date, session_name=args.session_name)
        results = self_check(cfg, day_output_dir=resolved_day_output_dir)
        raise SystemExit(1 if any(level == "FAIL" for level, _ in results) else 0)

    elif args.run:
        summary = process_animal_day(
            cfg, args.animal, args.date, session_name=args.session_name,
            shank_filter=args.shank, skip_staging=not args.with_staging,
            dry_run=args.dry_run, skip_phy_export=args.skip_phy_export,
            skip_plots=args.skip_plots,
        )
        print("\n" + "=" * 70)
        print("QUALITY_CONTROL SUMMARY" + (" (DRY RUN)" if args.dry_run else ""))
        print("=" * 70)
        print(f"Assessed: {len(summary['assessed'])}")
        for line in summary["assessed"]:
            print(f"  OK   {line}")
        print(f"Skipped:  {len(summary['skipped'])}")
        for line in summary["skipped"]:
            print(f"  SKIP {line}")
        print(f"Errors:   {len(summary['errors'])}")
        for line in summary["errors"]:
            print(f"  FAIL {line}")
        raise SystemExit(1 if summary["errors"] else 0)
