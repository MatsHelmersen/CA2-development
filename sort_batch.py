#!/usr/bin/env python3
"""
sort_batch.py

Batch Kilosort4 spike sorting for Cambridge NeuroTech ASSY-350 H20 (128 ch)
recordings, walking a directory tree of the form:

    <base_path>/<AnimalID>/Raw_data/<YYYYMMDD>/<YYYYMMDD_NNN>/  (OpenEphys files)

Design decisions (see conversation record for full reasoning):
  - All sessions recorded on the same day, at the same drive (DV) position,
    are concatenated and sorted together as a single Kilosort4 run per shank.
    This lets KS4's internal drift correction operate over the full day and
    keeps unit IDs consistent across trials/sleep sessions for before/during/
    after comparisons. Do NOT concatenate across days or across DV moves.
  - EMG/ECoG channels are removed from the recording BEFORE probe assignment
    (they have no valid position on the H20 geometry - this is not a "bad
    channel" flag, it's a channel-count/identity issue).
  - Noisy/shorted/dead channels are detected automatically (adapted from the
    user's detect_bad_channels function) and excluded before CMR/whitening.
  - No external bandpass or CMR is applied before Kilosort4; KS4 does its own
    internal filtering and CAR (do_CAR=True, explicit).
  - Existing sorted output triggers an interactive prompt (skip/overwrite),
    per user's stated preference.
"""

import os
import sys
import glob
import json
import shutil
import argparse
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt

import spikeinterface.full as si
import probeinterface as pi

try:
    from tqdm import tqdm
except ImportError:
    print("tqdm not found - progress bars will fall back to plain print statements.")
    tqdm = None


# ============================================================================
# CONFIGURATION - edit these before running, or override via CLI flags
# ============================================================================

CONFIG = {
    # --- paths ---
    "base_path": Path("\\\\biotin4.hpc.uio.no\\boccaraarea2\\Data\\Animals\\data"),  # root folder containing animal subfolders
    # "base_path": Path("C:\\Users\\matshhe\\Desktop\\CA2\\spikesorting\\test_data"),  # root folder containing animal subfolders
    "probe_json": Path("C:/Users/matshhe/Desktop/CA2/spikesorting/ASSY-350-H20_sitemap.json"),
    # Optional local scratch path for the intermediate recording export.
    # On network drives, writing this binary to a local SSD/temp dir is often much faster.
    "binary_cache_dir": None,
    # Optional local scratch path to stage raw OpenEphys session folders
    # BEFORE reading them, when base_path is a network mount. A single
    # sequential copy is usually much faster than the scattered reads
    # si.read_openephys performs directly over SMB/CIFS for every
    # downstream step (bad-channel sampling, binary export, KS4 itself).
    # None disables staging (read directly from base_path, as before).
    "stage_raw_locally": None,  # e.g. Path("C:/Users/matshhe/scratch")

    # Optional separate root for ALL sorting/assessment/phy output (binary,
    # KS4 folders, CSVs, phy exports). Mirrors animal/Raw_data/date[/session]
    # under this root instead of writing alongside the raw data. None ->
    # write in-place under base_path (previous default behaviour).
    "output_base_path": None,  # e.g. Path("D:/spikesorting_output")

    # --- concatenation behaviour ---
    # True (default): concatenate all sessions for a given animal/day and
    # sort together - use when the probe was NOT moved between sessions.
    # False: sort each session independently - use for days where the
    # probe/drive was lowered between recordings (breaks drift-correction
    # continuity, so must not be concatenated). Chosen once per batch run
    # via the wizard, or override with --no-concat.
    "concatenate_sessions": True,

    # --- bad channel handling ---
    # If True, automatically omit detected bad channels from sorting for
    # the whole batch (decided once, not prompted per shank).
    "auto_omit_bad_channels": True,
    # Supplementary IBL std-based outlier check (catches noisy-but-
    # uncorrelated channels that the custom dead/shorted logic misses).
    "run_ibl_std_check": True,

    # --- recording stream (OpenEphys) ---
    "stream_name": "Record Node 101#Acquisition_Board-100.acquisition_board",
    # "experiment_name": "experiment1",
    "block_index": 0,  # which block to load if multiple blocks exist in the same folder
    
    # --- channel exclusion: fixed, hardware-defined per animal ---
    # channel IDs (0-indexed, matching the recording's raw channel order,
    # NOT the probe contact order) that are wired to EMG/ECoG rather than
    # the silicon probe. Populate per-animal; an empty list means "none".
    "aux_channel_ids": {
        "213868": [63, 64, 65, 66],  # example: channels 63-66 are EMG/ECoG for this animal
        "211355": [63, 64, 65, 66],  # example: channels 63-66 are EMG/ECoG for this animal
    },

    # --- bad channel detection thresholds ---
    "dead_var_thresh": 10.0,        # uV^2 on subsampled trace; tune to your gain
    "short_corr_thresh": 0.95,     # |correlation| above which channels are clustered
    "bad_channel_subsample_s": 30.0,

    # --- behaviour on existing output ---
    "existing_output_action": "prompt",  # "prompt" | "skip" | "overwrite"

    # --- Kilosort4 parameters (defaults; wizard can override interactively) ---
    "ks4_params": {
        "do_CAR": True,
        "Th_universal": 9,
        "Th_learned": 8,
        "nblocks": 5,
        "dmin": None,       # None -> KS4 default (min vertical contact spacing)
        "dminx": None,      # None -> KS4 default (min horizontal contact spacing)
        "min_template_size": 10,
        "nearest_chans": 10,
        "torch_device": "cuda:0",
    },

    # --- post-sort unit assessment (Noise/Artefact, MUA, SUA classification) ---
    # NOTE: none of these thresholds are copied from IBL/Neuropixels defaults.
    # IBL's absolute amplitude threshold (50 uV) is calibrated to Neuropixels'
    # specific amplifier gain and has no principled reason to transfer to a
    # different headstage - SNR is used here instead (unit-agnostic, relative
    # to your own noise floor). Firing rate and presence ratio are computed
    # and reported but NOT used as exclusion criteria by default - see the
    # accompanying discussion for why (developmental/immature units, and
    # place/social cells that are, by design, only active in specific
    # locations/contexts should not be penalized for low whole-day presence).
    # Treat every number below as a starting point to validate against your
    # own metric distributions (e.g. plot histograms across all units and
    # check for a visually defensible bimodal split) before trusting blindly.
    "run_unit_assessment": True,
    "assessment_thresholds": {
        "min_spikes_total": 50,                # below this: Noise/Artefact (can't assess reliably)
        "min_spikes_for_amplitude_cutoff": 300, # below this: amplitude_cutoff reported as NaN, not used
        "snr_noise_max": 1.5,                   # SNR at or below this: Noise/Artefact candidate
        "snr_sua_min": 4.0,                     # SNR at or above this (+ other criteria): SUA candidate
        "isi_violations_ratio_sua_max": 0.02,   # <=2% estimated contamination for SUA
        "isi_violations_ratio_noise_min": 0.5,  # >=50% contamination: Noise/Artefact candidate
        "isolation_distance_sua_min": 15.0,     # Schmitzer-Torbert et al. (2005)-informed starting point
        "l_ratio_sua_max": 0.2,
        "saturation_overlap_noise_frac": 0.25,  # fraction of a unit's spikes near saturation -> Noise/Artefact
    },
    # Export each shank to a Phy-ready folder (si.export_to_phy, copy_binary=True)
    # after assessment. Writes a per-shank recording.dat, so disable for very
    # long concatenated days if disk space is a concern.
    "export_to_phy": True,
    "saturation_detection": {
        # Custom - not an IBL/SpikeInterface built-in. Addresses the
        # static-electricity-type saturation you've observed. Full-day
        # chunked scan (not sampled) since saturation events are transient
        # and a representative sample (as used for bad-channel detection)
        # could miss them entirely.
        "enabled": True,
        "mute_before_sorting": True,  # zero flagged (channel, window) pairs before KS4, per-channel/per-window only
        "chunk_s": 30.0,
        "clip_fraction_of_range": 0.98,   # fraction of dtype dynamic range considered "clipped"
        "derivative_mad_multiple": 20.0,  # sample-to-sample jump size, in MADs, flagged as saturation-like
        "window_pad_ms": 5.0,             # padding around a flagged sample when checking spike overlap
        # A channel flagged in more than this fraction of chunks is treated
        # as effectively broken and EXCLUDED outright (folded into bad-channel
        # handling) rather than run through the expensive precise window scan
        # and surgical muting - muting a channel that's bad ~always leaves
        # near-total zeros anyway, so precise treatment buys nothing and
        # costs a lot for a badly damaged shank.
        "hopeless_fraction_thresh": 0.5,
    },
    # Minimum number of channels remaining on a shank (after bad-channel +
    # hopeless-saturation exclusion) for it to be worth precise scanning and
    # sorting at all. Below this, the shank is skipped entirely and logged -
    # this is the fix for a fully-excluded shank still consuming scan/sort time.
    "min_channels_to_sort_shank": 2,
}

DEAD_VAR_THRESH = CONFIG["dead_var_thresh"]
SHORT_CORR_THRESH = CONFIG["short_corr_thresh"]


# ============================================================================
# BAD CHANNEL DETECTION (adapted from user-supplied function)
# ============================================================================

def detect_bad_channels(raw_shank, shank_chs, fs, subsample_s=None):
    """
    Detect dead and shorted channels on a single shank.

    raw_shank : (n_channels, n_samples) array, one shank's worth of traces
    shank_chs : list of channel labels (for reporting) matching raw_shank rows
    fs        : sampling rate (Hz)
    subsample_s : seconds of data to use for the check (default: config value)

    Returns (bad_channels: set[int local index], bad_reason: dict, report: list[str])
    """
    if subsample_s is None:
        subsample_s = CONFIG["bad_channel_subsample_s"]

    n_ch, n_samp = raw_shank.shape
    step = max(1, n_samp // int(subsample_s * fs))
    data = raw_shank[:, ::step]
    report, bad_channels, bad_reason = [], set(), {}

    # --- dead channel check ---
    for i, v in enumerate(np.var(data, axis=1)):
        if v < DEAD_VAR_THRESH:
            bad_channels.add(i)
            bad_reason[i] = "dead"
            report.append(f"    ch {shank_chs[i]:>4} (local {i:2d}): DEAD (var={v:.2f} uV^2)")

    # --- shorted channel check ---
    live = [i for i in range(n_ch) if i not in bad_channels]
    if len(live) >= 2:
        fs_effective = fs / step
        cutoff = min(150.0, fs_effective / 2.0 * 0.5)
        if data.shape[1] > 15 and cutoff > 0.5:
            b, a = butter(2, cutoff / (fs_effective / 2.0), btype="high")
            corr_data = filtfilt(b, a, data, axis=1)
        else:
            corr_data = data

        corr = np.corrcoef(corr_data[live])
        visited = set()
        for a_idx, a in enumerate(live):
            if a in visited:
                continue
            cluster = [a]
            for b_idx, b in enumerate(live):
                if b == a or b in visited:
                    continue
                if abs(corr[a_idx, b_idx]) > SHORT_CORR_THRESH:
                    cluster.append(b)
            if len(cluster) > 1:
                mads = [np.median(np.abs(data[i] - np.median(data[i]))) for i in cluster]
                keep = cluster[int(np.argmax(mads))]
                for ch in cluster:
                    visited.add(ch)
                    if ch != keep:
                        bad_channels.add(ch)
                        bad_reason[ch] = "shorted"
                        k_idx = live.index(keep)
                        c_idx = live.index(ch)
                        report.append(
                            f"    ch {shank_chs[ch]:>4} (local {ch:2d}): SHORTED "
                            f"(r={corr[c_idx, k_idx]:.3f} with ch {shank_chs[keep]})")

    n_bad = len(bad_channels)
    report.insert(0, f"  Bad channels: {n_bad}/{n_ch} ({n_bad / max(n_ch,1):.0%})")
    return bad_channels, bad_reason, report


def _sample_windows(rec, fs, total_s, n_windows=5):
    """
    Pull short windows spread across the full recording rather than loading
    everything into memory. Important for day-long concatenated recordings:
    a marginal/shorting connection can appear mid-day, so sampling only the
    first N seconds would miss it.
    Returns (channels, samples) array, concatenated across windows.
    """
    n_frames = rec.get_num_frames()
    win_frames = int(total_s / n_windows * fs)
    if n_frames <= win_frames * n_windows:
        # short recording - just take the whole thing
        return rec.get_traces().T
    starts = np.linspace(0, n_frames - win_frames, n_windows, dtype=int)
    chunks = [rec.get_traces(start_frame=s, end_frame=s + win_frames).T for s in starts]
    return np.concatenate(chunks, axis=1)


def find_bad_channels_for_recording(recording_split, fs):
    """
    Run detect_bad_channels per shank on a split (dict of shank_id -> recording).
    Samples several windows spread across the full (possibly day-long,
    concatenated) recording rather than loading it wholesale.

    Also runs SpikeInterface's built-in IBL-derived detect_bad_channels
    (method='std') as a supplementary check. The custom dead/shorted logic
    above cannot catch a channel with elevated, UNCORRELATED noise (e.g. a
    loose connector) - it's neither near-zero variance (dead) nor highly
    correlated with a neighbor (shorted). The std-based outlier method fills
    that specific gap. See International Brain Laboratory et al. (2022),
    "Spike sorting pipeline for the International Brain Laboratory".

    Returns dict: shank_id -> (bad_local_indices set, reasons dict, report lines)
    """
    results = {}
    for shank_id, rec in recording_split.items():
        traces = _sample_windows(rec, fs, total_s=CONFIG["bad_channel_subsample_s"])
        chan_ids = rec.get_channel_ids()
        bad, reasons, report = detect_bad_channels(traces, chan_ids, fs, subsample_s=None)

        if CONFIG.get("run_ibl_std_check", True):
            try:
                import spikeinterface.preprocessing as spre
                ibl_bad_ids, ibl_labels = spre.detect_bad_channels(rec, method="std")
                ibl_bad_set = set(ibl_bad_ids)
                id_to_local = {c: i for i, c in enumerate(chan_ids)}
                for cid, label in zip(chan_ids, ibl_labels):
                    if cid in ibl_bad_set:
                        i = id_to_local[cid]
                        if i not in bad:
                            bad.add(i)
                            reasons[i] = f"noisy (IBL std outlier, {label})"
                            report.append(f"    ch {cid:>4} (local {i:2d}): NOISY (IBL std-outlier check)")
                # header line (report[0]) was written before this check ran - rebuild it
                n_ch = len(chan_ids)
                report[0] = f"  Bad channels: {len(bad)}/{n_ch} ({len(bad) / max(n_ch,1):.0%})"
            except Exception as e:
                report.append(f"    (IBL std-based check failed: {e})")

        results[shank_id] = (bad, reasons, report)
    return results


# ============================================================================
# PROBE / SESSION HANDLING
# ============================================================================

def load_probe():
    probe = pi.read_probeinterface(CONFIG["probe_json"]).probes[0]
    if probe.device_channel_indices is None:
        probe.set_device_channel_indices(np.arange(probe.get_contact_count()))
    return probe


def get_day_output_dir(animal_id, date_str, session_name=None):
    """
    Compute the output directory for a given animal/day (or single session,
    in individual-session mode). If CONFIG['output_base_path'] is set, all
    sorting/assessment output is written there, mirroring the
    animal/Raw_data/date[/session] structure but rooted somewhere other
    than the raw data folder. If unset, output stays in-place under
    base_path, alongside the raw data (previous default behaviour).
    """
    root = CONFIG.get("output_base_path") or CONFIG["base_path"]
    if session_name is not None:
        return os.path.join(root, animal_id, "Raw_data", date_str, session_name)
    return os.path.join(root, animal_id, "Raw_data", date_str)


def find_sessions(base_path, animal_filter=None):
    """
    Walk <base_path>/<AnimalID>/Raw_data/<YYYYMMDD>/<YYYYMMDD_NNN> and group
    session folders by (AnimalID, YYYYMMDD) so same-day sessions can be
    concatenated. Returns dict: (animal_id, date_str) -> sorted list of paths.
    """
    pattern = os.path.join(base_path, "*", "Raw_data", "*", "*_[0-9][0-9][0-9]")
    all_sessions = sorted(glob.glob(pattern))

    grouped = {}
    for path in all_sessions:
        parts = path.split(os.sep)
        # .../<AnimalID>/Raw_data/<YYYYMMDD>/<YYYYMMDD_NNN>
        animal_id = parts[-4]
        date_str = parts[-2]
        if animal_filter and animal_id != animal_filter:
            continue
        grouped.setdefault((animal_id, date_str), []).append(path)

    for key in grouped:
        grouped[key] = sorted(grouped[key])  # NNN order -> chronological, verify below

    return grouped


def existing_output_present(day_output_dir):
    if not os.path.isdir(day_output_dir):
        return False
    # heuristic: any shank_*_ks4 folder with a params.py inside means KS4 ran
    for shank_dir in glob.glob(os.path.join(day_output_dir, "shank_*_ks4")):
        if os.path.exists(os.path.join(shank_dir, "params.py")):
            return True
    return False


# ============================================================================
# INTERACTIVE KILOSORT4 PARAMETER WIZARD
# ============================================================================

KS4_PARAM_INFO = [
    # (key, prompt, explanation)
    ("do_CAR", "Apply common average referencing internally?",
     "Subtracts the median across channels per timepoint before whitening. "
     "Leave True unless you have a specific reason to disable it (e.g. very "
     "few channels per shank, or reference contamination)."),
    ("Th_universal", "Universal spike detection threshold",
     "Threshold (in standardised units) used by the initial, untrained "
     "template-matching pass to find candidate spikes before templates "
     "are learned. Lower = more sensitive but more false positives."),
    ("Th_learned", "Learned-template detection threshold",
     "Threshold applied once KS4 has learned unit-specific templates from "
     "the data. Usually set slightly lower than Th_universal since learned "
     "templates are more specific."),
    ("nblocks", "Number of blocks for drift correction",
     "Number of non-rigid blocks along the probe's depth used for motion "
     "estimation. 1 = rigid (whole probe moves together). Higher values "
     "allow different depths to drift independently - relevant if you see "
     "differential drift across your ~165 um shank length."),
    ("dmin", "Minimum vertical contact spacing (um), or blank for default",
     "Used internally for template/drift grid spacing. Leave blank to let "
     "KS4 infer it from your probe geometry (recommended for the H20's "
     "staggered layout)."),
    ("dminx", "Minimum horizontal contact spacing (um), or blank for default",
     "As above, but horizontal. Leave blank unless you have a specific "
     "reason to override."),
    ("min_template_size", "Minimum template size (um)",
     "Smallest spatial footprint (in um) considered when building templates. "
     "Affects how tightly localized a 'unit' can be."),
    ("nearest_chans", "Number of nearest channels used per template",
     "How many spatially nearest channels contribute to each unit's "
     "template. With ~8-10 contacts per shank row, the default of 10 "
     "usually spans most/all of a shank."),
    ("torch_device", "Torch device string",
     "e.g. 'cuda:0' for GPU 0, or 'cpu' to force CPU (much slower)."),
]


def run_batch_wizard(defaults):
    """
    Ask the small set of once-per-batch questions that used to be prompted
    repeatedly (per shank / implicitly assumed): whether to concatenate
    same-day sessions, and whether to auto-omit detected bad channels.
    Then hands off to the existing KS4 parameter wizard.
    """
    print("\n" + "=" * 70)
    print("BATCH-WIDE SETTINGS")
    print("=" * 70)

    print("\nConcatenate all sessions within the same animal/day before sorting?")
    print("  Choose NO for any day where the probe/drive depth was changed")
    print("  between sessions - concatenating across a depth change breaks")
    print("  Kilosort4's drift-correction assumption of continuous motion.")
    raw = input("  [Y/n] > ").strip().lower()
    CONFIG["concatenate_sessions"] = (raw != "n")

    print("\nAutomatically omit detected bad channels (dead/shorted/noisy) "
          "from sorting for the whole batch?")
    print("  If no, bad channels will still be detected and reported, but "
          "left in the data for Kilosort4 to handle.")
    raw = input("  [Y/n] > ").strip().lower()
    CONFIG["auto_omit_bad_channels"] = (raw != "n")

    print("\nRun post-sort unit assessment (classify units into "
          "Noise/Artefact, MUA, SUA) after each shank finishes sorting?")
    print("  Adds a saturation scan and quality-metric computation per shank - "
          "increases runtime but produces a unit_assessment.csv per shank.")
    raw = input("  [Y/n] > ").strip().lower()
    CONFIG["run_unit_assessment"] = (raw != "n")

    return run_ks4_wizard(defaults)


def run_ks4_wizard(defaults):
    print("\n" + "=" * 70)
    print("KILOSORT4 PARAMETER WIZARD")
    print("=" * 70)
    print("Press Enter to accept the default shown in [brackets] for each "
          "parameter, or type a new value.\n")

    params = dict(defaults)
    for key, prompt, explanation in KS4_PARAM_INFO:
        current = defaults.get(key)
        print(f"\n{prompt}")
        print(f"  {explanation}")
        raw = input(f"  [{current}] > ").strip()
        if raw == "":
            continue
        if raw.lower() in ("none", "null", ""):
            params[key] = None
            continue
        # try to cast to the same type as the default, fall back to str
        if isinstance(current, bool):
            params[key] = raw.lower() in ("true", "1", "yes", "y")
        elif isinstance(current, int):
            try:
                params[key] = int(raw)
            except ValueError:
                params[key] = raw
        elif isinstance(current, float):
            try:
                params[key] = float(raw)
            except ValueError:
                params[key] = raw
        else:
            params[key] = raw

    print("\nFinal Kilosort4 parameters:")
    for k, v in params.items():
        print(f"  {k}: {v}")
    confirm = input("\nProceed with these parameters for the whole batch? [Y/n] > ").strip().lower()
    if confirm == "n":
        print("Aborting - re-run the script to try again.")
        sys.exit(0)
    return params


# ============================================================================
# CORE PROCESSING - one (animal, day) at a time
# ============================================================================

def write_run_summary_csv(day_output_dir, day_tag, animal_id, date_str, session_paths,
                           probe, ks4_params, aux_ids, bad_channel_report, shank_unit_counts,
                           concatenating, auto_omit, units_df=None):
    """
    Write ONE summary file per day: run_summary.csv. Sorting-run metadata
    (parameters, bad channels, output counts) is written as commented ('#')
    header lines, followed by the combined per-unit assessment table across
    all shanks (if assessment was run) - so everything lives in one file,
    per your request, rather than a separate .txt report and per-shank
    CSVs. pandas.read_csv(path, comment='#') skips the header block cleanly
    for downstream analysis; opening the file directly in a text editor
    still shows the full human-readable metadata above the table.
    """
    header = []
    header.append(f"# SPIKE SORTING RUN SUMMARY - {day_tag}")
    header.append(f"# Generated: {datetime.now().isoformat(timespec='seconds')}")
    header.append(f"# Animal ID: {animal_id}")
    header.append(f"# Date: {date_str}")
    header.append(f"# Mode: {'concatenated (same-day sessions merged)' if concatenating else 'individual session (not concatenated)'}")
    header.append(f"# Sessions ({len(session_paths)}): " + " | ".join(session_paths))
    header.append(f"# Probe: {probe.annotations.get('model_name', 'ASSY-350-H20')} "
                   f"({probe.get_contact_count()} contacts, {probe.get_shank_count()} shanks)")
    header.append(f"# Probe JSON source: {CONFIG['probe_json']}")
    header.append(f"# EMG/ECoG channels excluded (animal-specific): {aux_ids if aux_ids else 'none configured'}")
    header.append(f"# Stream name: {CONFIG['stream_name']}")

    header.append(f"# --- Bad channel detection --- auto_omit={auto_omit}, "
                   f"dead_var_thresh={CONFIG['dead_var_thresh']}, "
                   f"short_corr_thresh={CONFIG['short_corr_thresh']}, "
                   f"ibl_std_check={CONFIG.get('run_ibl_std_check', True)}")
    for shank_id, report in bad_channel_report.items():
        for line in report:
            header.append(f"#   shank {shank_id}: {line.strip()}")

    header.append(f"# --- Saturation muting --- enabled={CONFIG['saturation_detection'].get('enabled', True)}, "
                   f"mute_before_sorting={CONFIG['saturation_detection'].get('mute_before_sorting', True)}")

    header.append("# --- Kilosort4 parameters ---")
    for k, v in ks4_params.items():
        header.append(f"#   {k}: {v}")

    header.append("# --- Sorting output (units before assessment) ---")
    total_units = 0
    for shank_id, n_units in shank_unit_counts.items():
        header.append(f"#   shank {shank_id}: {n_units} units -> shank_{shank_id}_ks4/")
        total_units += n_units
    header.append(f"#   total units across shanks: {total_units}")
    header.append(f"# Session boundary / TTL metadata: session_boundaries.json")

    if units_df is not None and not units_df.empty:
        header.append("# --- Unit assessment (Noise/Artefact, MUA, SUA) ---")
        header.append("# NOTE: thresholds are project-specific starting points, not IBL/Neuropixels")
        header.append("# defaults. Firing rate and whole-day presence ratio are reported per unit")
        header.append("# below but NOT used as exclusion criteria - see accompanying discussion.")
        totals = units_df["classification"].value_counts().to_dict()
        header.append(f"#   Day total: SUA={totals.get('SUA',0)}  MUA={totals.get('MUA',0)}  "
                       f"Noise/Artefact={totals.get('Noise/Artefact',0)}")

    csv_path = os.path.join(day_output_dir, "run_summary.csv")
    with open(csv_path, "w", newline="") as f:
        f.write("\n".join(header) + "\n")
        if units_df is not None and not units_df.empty:
            units_df.to_csv(f, index=False)
        else:
            f.write("# (no unit assessment was run for this day)\n")

    print(f"  Wrote run_summary.csv")
    return csv_path


# ============================================================================
# POST-SORT UNIT ASSESSMENT: Noise/Artefact, MUA, SUA classification
# ============================================================================
# Merges in what was previously a separate assess_sorting.py script. Runs
# automatically after each shank's KS4 output is produced (see process_day),
# using the SortingAnalyzer / quality_metrics machinery, plus two custom
# additions not available off-the-shelf: a full-day saturation scan, and
# per-session (not whole-day) presence ratio. See the accompanying discussion
# for why whole-day presence ratio and IBL's absolute amplitude threshold are
# deliberately NOT used as classification criteria here.

def scan_saturation_fraction_per_channel(rec, fs):
    """
    FAST first pass: per-channel fraction of the whole recording flagged as
    clipped/saturated, computed at chunk granularity (one boolean per
    chunk per channel, not per sample) - this is what makes it cheap.
    This is the fix for the "shank dominated by saturation takes forever"
    problem: a channel that's bad ~all the time gets identified in one
    coarse pass, without ever running the expensive precise per-sample
    window-finding on it.
    Returns dict: local_channel_index -> fraction of chunks flagged (0-1).
    """
    cfg = CONFIG["saturation_detection"]
    if not cfg.get("enabled", True):
        return {}

    n_frames = rec.get_num_frames()
    n_chans = rec.get_num_channels()
    if n_chans == 0 or n_frames == 0:
        return {}
    chunk_frames = int(cfg["chunk_s"] * fs)
    dtype = rec.get_dtype()

    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        clip_hi = info.max * cfg["clip_fraction_of_range"]
        clip_lo = info.min * cfg["clip_fraction_of_range"]
    else:
        probe_chunk = rec.get_traces(start_frame=0, end_frame=min(chunk_frames, n_frames))
        clip_hi = np.percentile(probe_chunk, 99.9) * cfg["clip_fraction_of_range"]
        clip_lo = np.percentile(probe_chunk, 0.1) * cfg["clip_fraction_of_range"]

    starts = list(range(0, n_frames, chunk_frames))
    flagged_chunk_counts = np.zeros(n_chans)
    chunk_iter = tqdm(starts, desc="  Saturation severity scan", unit="chunk") if tqdm else starts
    for start in chunk_iter:
        end = min(start + chunk_frames, n_frames)
        chunk = rec.get_traces(start_frame=start, end_frame=end)
        clipped_frac = np.mean((chunk >= clip_hi) | (chunk <= clip_lo), axis=0)  # per-channel
        # a chunk counts as "flagged" for a channel if a meaningful fraction of
        # its samples are clipped - one wild sample shouldn't condemn a chunk
        flagged_chunk_counts += (clipped_frac > 0.01)

    return {ch: flagged_chunk_counts[ch] / len(starts) for ch in range(n_chans)}


def detect_saturation_windows_per_channel(rec, fs, channels_to_scan=None):
    """
    Precise chunked scan for exact (start_sample, end_sample) windows,
    following IBL's two-part approach: threshold on absolute amplitude AND
    on the sample-to-sample derivative. This is the expensive, exact pass -
    only run it on `channels_to_scan` (local indices), which should already
    have been screened by scan_saturation_fraction_per_channel to exclude
    channels that are hopeless (those get excluded outright instead - see
    process_day). If channels_to_scan is None, scans everything (used by
    --assess-only where the severity pre-pass already happened at sort time).

    Within each chunk, if a channel is flagged for >30% of that chunk's
    samples, the whole chunk is recorded as one window rather than finding
    exact sub-sample boundaries - this bounds the cost for channels that are
    "noisy all the time" within an otherwise-acceptable overall fraction,
    rather than generating thousands of fragmented micro-windows.

    Returns dict: local_channel_index -> list of (start_sample, end_sample).
    """
    cfg = CONFIG["saturation_detection"]
    if not cfg.get("enabled", True):
        return {}

    n_frames = rec.get_num_frames()
    n_chans = rec.get_num_channels()
    if n_chans == 0 or n_frames == 0:
        return {}
    chunk_frames = int(cfg["chunk_s"] * fs)
    dtype = rec.get_dtype()

    if channels_to_scan is None:
        channels_to_scan = list(range(n_chans))
    if not channels_to_scan:
        return {}

    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        clip_hi = info.max * cfg["clip_fraction_of_range"]
        clip_lo = info.min * cfg["clip_fraction_of_range"]
    else:
        probe_chunk = rec.get_traces(start_frame=0, end_frame=min(chunk_frames, n_frames))
        clip_hi = np.percentile(probe_chunk, 99.9) * cfg["clip_fraction_of_range"]
        clip_lo = np.percentile(probe_chunk, 0.1) * cfg["clip_fraction_of_range"]

    per_channel_flags = {ch: [] for ch in channels_to_scan}
    starts = list(range(0, n_frames, chunk_frames))
    chunk_iter = tqdm(starts, desc="  Saturation precise scan", unit="chunk") if tqdm else starts
    dense_chunk_frac_thresh = 0.30
    for start in chunk_iter:
        end = min(start + chunk_frames, n_frames)
        chunk = rec.get_traces(start_frame=start, end_frame=end, channel_ids=None)
        chunk = chunk[:, channels_to_scan]

        clipped = (chunk >= clip_hi) | (chunk <= clip_lo)
        diffs = np.abs(np.diff(chunk, axis=0))
        mad = np.median(np.abs(chunk - np.median(chunk))) + 1e-9
        jump_thresh = cfg["derivative_mad_multiple"] * mad
        jumped = diffs > jump_thresh
        jumped = np.concatenate([np.zeros((1, chunk.shape[1]), dtype=bool), jumped], axis=0)
        flagged = clipped | jumped  # (samples, len(channels_to_scan))

        for local_out_idx, ch in enumerate(channels_to_scan):
            col = flagged[:, local_out_idx]
            frac = col.mean()
            if frac == 0:
                continue
            if frac > dense_chunk_frac_thresh:
                # densely flagged chunk - record as one window, skip exact
                # sub-sample boundary finding (that's the expensive part)
                per_channel_flags[ch].append((start, end - 1))
                continue
            flagged_idx = np.where(col)[0]
            gaps = np.where(np.diff(flagged_idx) > 1)[0]
            seg_starts = np.concatenate([[0], gaps + 1])
            seg_ends = np.concatenate([gaps, [len(flagged_idx) - 1]])
            for s, e in zip(seg_starts, seg_ends):
                per_channel_flags[ch].append((start + flagged_idx[s], start + flagged_idx[e]))

    return {ch: wins for ch, wins in per_channel_flags.items() if wins}


def merge_windows_across_channels(per_channel_windows):
    """Flatten per-channel windows into a single merged (start,end) list, for
    reporting / spike-overlap purposes where the specific channel doesn't matter."""
    all_windows = [w for wins in per_channel_windows.values() for w in wins]
    return sorted(all_windows)


class _SaturationMutedSegment(si.BasePreprocessorSegment):
    def __init__(self, parent_segment, per_channel_windows):
        si.BasePreprocessorSegment.__init__(self, parent_segment)
        self.per_channel_windows = per_channel_windows

    def get_traces(self, start_frame, end_frame, channel_indices):
        traces = self.parent_recording_segment.get_traces(start_frame, end_frame, channel_indices).copy()
        if channel_indices is None:
            channel_indices = np.arange(traces.shape[1])
        elif isinstance(channel_indices, slice):
            channel_indices = np.arange(*channel_indices.indices(traces.shape[1] + (channel_indices.start or 0)))
        for out_col, ch in enumerate(np.atleast_1d(channel_indices)):
            for (w_start, w_end) in self.per_channel_windows.get(int(ch), []):
                ov_start = max(w_start, start_frame)
                ov_end = min(w_end + 1, end_frame)
                if ov_start < ov_end:
                    traces[ov_start - start_frame: ov_end - start_frame, out_col] = 0
        return traces


class SaturationMutedRecording(si.BasePreprocessor):
    """
    Lazy preprocessing wrapper: zeros out exactly the flagged (channel,
    sample-range) pairs from detect_saturation_windows_per_channel, leaving
    every other sample - including other channels during the same window,
    and this channel during other windows/sessions - completely untouched.
    Nothing is materialized until get_traces() is actually called, matching
    SpikeInterface's normal lazy-preprocessing pattern.

    NOTE: this subclasses BasePreprocessor/BasePreprocessorSegment directly
    rather than using a documented public preprocessing function, because I
    could not confirm from documentation that SpikeInterface's built-in
    silence_periods() supports genuinely per-channel period specification
    (as opposed to the same periods applied to every channel) - and getting
    that distinction wrong would silently zero out good data on channels
    that never saturated. This has not been tested against your installed
    SpikeInterface version; verify the output traces (e.g. plot a known
    saturation window before/after) before trusting it on a full batch.
    """
    def __init__(self, recording, per_channel_windows):
        si.BasePreprocessor.__init__(self, recording)
        for parent_segment in recording._recording_segments:
            self.add_recording_segment(_SaturationMutedSegment(parent_segment, per_channel_windows))
        self._kwargs = dict(recording=recording, per_channel_windows=per_channel_windows)


def compute_saturation_overlap(sorting, unit_id, saturation_windows, fs, pad_ms):
    """Fraction of a unit's spikes falling within pad_ms of a flagged saturation window.
    `saturation_windows` here is the MERGED (all-channel) list."""
    if not saturation_windows:
        return 0.0
    spike_train = sorting.get_unit_spike_train(unit_id)
    if len(spike_train) == 0:
        return 0.0
    pad_samples = int(pad_ms / 1000.0 * fs)
    starts = np.array([w[0] - pad_samples for w in saturation_windows])
    ends = np.array([w[1] + pad_samples for w in saturation_windows])
    order = np.argsort(starts)
    starts, ends = starts[order], ends[order]
    idx = np.searchsorted(starts, spike_train, side="right") - 1
    idx = np.clip(idx, 0, len(starts) - 1)
    within = (spike_train >= starts[idx]) & (spike_train <= ends[idx])
    return float(np.sum(within)) / len(spike_train)


def compute_per_session_presence_ratio(sorting, unit_id, session_metadata, n_bins_per_session=10):
    """
    Presence ratio computed PER SESSION rather than across the whole
    concatenated day. A place/social cell that fires only during specific
    trials will show near-zero whole-day presence ratio despite being a
    perfectly real, and scientifically interesting, unit - this is reported
    per-session instead so that selectivity isn't conflated with instability.
    Returns dict: session_path -> presence_ratio (float, or None if the
    session had zero duration recorded for this shank).
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
    Rule-based 3-tier classification: Noise/Artefact, MUA, SUA.
    Deliberately does NOT use firing_rate or whole-day presence_ratio as
    exclusion criteria - see module docstring / accompanying discussion.
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
        and (not pd.isna(row["isolation_distance"]) and row["isolation_distance"] >= thresholds["isolation_distance_sua_min"])
        and (not pd.isna(row["l_ratio"]) and row["l_ratio"] <= thresholds["l_ratio_sua_max"])
    )
    return "SUA" if is_sua else "MUA"


def export_shank_to_phy(shank_id, analyzer, day_output_dir):
    """
    Export this shank's sorting to a Phy-ready folder using SpikeInterface's
    built-in exporter, which is the correct fix for the error you saw:
    it writes both a per-shank recording.dat (via copy_binary=True - this
    IS the "local segmented copy per shank" you proposed, just via the
    built-in tool rather than a hand-rolled one) and a params.py with the
    correct dat_path/n_channels_dat for that shank specifically, rather
    than pointing at the multi-shank binary.

    NOTE: there is a documented SpikeInterface gotcha (GitHub issue #2751)
    where export_to_phy requires waveforms to have been computed with
    return_scaled=False (or matching dtype), and the wrong dtype produces a
    scaling error. Wrapped defensively - a failure here does not affect
    the KS4 output or the unit assessment, only the phy export step.
    Also, copy_binary=True writes a full copy of this shank's binary -
    for a multi-hour concatenated day this can be large; that's exactly
    why this step is skippable (--skip-phy-export / config flag).
    """
    from spikeinterface.exporters import export_to_phy
    phy_folder = os.path.join(day_output_dir, f"shank_{shank_id}_phy")
    try:
        export_to_phy(analyzer, phy_folder, copy_binary=True, remove_if_exists=True,
                       compute_pc_features=True, compute_amplitudes=True, verbose=False)
        print(f"  Exported shank {shank_id} to Phy: {phy_folder}")
        return phy_folder
    except Exception as e:
        print(f"  Warning: Phy export failed for shank {shank_id} ({e}). "
              f"KS4 output and unit assessment are unaffected.")
        return None


def assess_shank(shank_id, rec, sorting, session_metadata, fs, day_output_dir, day_tag, saturation_windows):
    """
    Run the full post-sort assessment for one shank: quality metrics via
    SortingAnalyzer, custom saturation overlap, custom per-session presence
    ratio, then classify every unit into Noise/Artefact, MUA, or SUA.
    Also exports to Phy if enabled (reuses this same analyzer - no
    recomputation). Returns (counts dict, metrics DataFrame tagged with
    shank_id) - the caller merges DataFrames across shanks into one
    per-day CSV rather than this function writing its own file, per your
    request to have sorting metadata and unit assessment in the same
    summary output.
    """
    thresholds = CONFIG["assessment_thresholds"]

    analyzer = si.create_sorting_analyzer(sorting, rec, sparse=True)
    analyzer.compute("random_spikes", max_spikes_per_unit=500)
    analyzer.compute("waveforms")
    analyzer.compute("templates")
    try:
        # NOTE: I have not verified this extension name/signature against
        # your specific installed SpikeInterface version - it's consistent
        # with the general analyzer.compute(extension_name, **params)
        # pattern used elsewhere, but PCA-extension APIs have shifted across
        # SI versions. Wrapped so a mismatch degrades gracefully (isolation
        # metrics -> NaN -> conservatively classified as MUA, not SUA)
        # rather than crashing the whole shank's assessment.
        analyzer.compute("principal_components", n_components=5, mode="by_channel_local")
        pca_metric_names = ["isolation_distance", "l_ratio"]
    except Exception as e:
        print(f"    Warning: principal_components extension failed ({e}); "
              f"isolation_distance/l_ratio will be unavailable for this shank.")
        pca_metric_names = []

    metrics_ext = analyzer.compute("quality_metrics", metric_names=[
        "firing_rate", "snr", "isi_violation", "presence_ratio",
        "amplitude_cutoff",
    ] + pca_metric_names)
    metrics_df = metrics_ext.get_data().copy()

    # ISI violation column name varies across SpikeInterface versions -
    # handle both, matching the defensive pattern already used in the
    # original assess_sorting.py.
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

    # gate amplitude_cutoff on minimum spike count - below this the metric
    # is statistically unreliable, not just noisy (see discussion)
    if "amplitude_cutoff" in metrics_df.columns:
        insufficient = metrics_df["n_spikes"] < thresholds["min_spikes_for_amplitude_cutoff"]
        metrics_df.loc[insufficient, "amplitude_cutoff"] = np.nan

    metrics_df["saturation_overlap_frac"] = [
        compute_saturation_overlap(sorting, uid, saturation_windows, fs,
                                    CONFIG["saturation_detection"]["window_pad_ms"])
        for uid in metrics_df.index
    ]

    # per-session presence ratio (diagnostic only, not used for classification)
    per_session_presence = {}
    for uid in metrics_df.index:
        per_session_presence[uid] = compute_per_session_presence_ratio(sorting, uid, session_metadata)

    for col in ["snr", "isi_violations_ratio", "isolation_distance", "l_ratio"]:
        if col not in metrics_df.columns:
            metrics_df[col] = np.nan

    metrics_df["classification"] = metrics_df.apply(lambda row: classify_unit(row, thresholds), axis=1)

    # attach per-session presence ratio as extra columns (session basename -> ratio)
    for sess in session_metadata:
        sess_name = os.path.basename(sess["session_path"])
        col_name = f"presence_ratio_{sess_name}"
        metrics_df[col_name] = [per_session_presence[uid].get(sess["session_path"]) for uid in metrics_df.index]

    metrics_df.index.name = "unit_id"
    metrics_df = metrics_df.reset_index()
    metrics_df.insert(0, "shank_id", shank_id)

    if CONFIG.get("export_to_phy", True):
        export_shank_to_phy(shank_id, analyzer, day_output_dir)

    counts = metrics_df["classification"].value_counts().to_dict()
    for label in ("Noise/Artefact", "MUA", "SUA"):
        counts.setdefault(label, 0)
    return counts, metrics_df


def assess_only_day(animal_id, date_str, session_paths, summary):
    """
    Re-run unit assessment on already-sorted shank_*_ks4 output, without
    re-running Kilosort4. Loads the saved binary + sorter folders directly
    (mirrors the loading pattern from the original standalone
    assess_sorting.py: si.load(binary_folder), rec.split_by('group'),
    si.read_sorter_folder(sorting_folder)).
    """
    day_tag = f"{animal_id}/{date_str}"
    day_output_dir = get_day_output_dir(animal_id, date_str)
    binary_path = os.path.join(day_output_dir, "recording_binary")
    boundaries_path = os.path.join(day_output_dir, "session_boundaries.json")

    if not os.path.isdir(binary_path):
        print(f"  No recording_binary found for {day_tag} - skipping (was it ever sorted?).")
        summary["errors"].append(f"{day_tag}: no recording_binary for --assess-only")
        return

    try:
        rec = si.load(binary_path)
        rec_split = rec.split_by("group")
        fs = rec.get_sampling_frequency()

        if os.path.exists(boundaries_path):
            with open(boundaries_path) as f:
                session_metadata = json.load(f)["sessions"]
        else:
            print(f"  Warning: no session_boundaries.json for {day_tag} - "
                  f"per-session presence ratio will be unavailable.")
            session_metadata = []

        shank_assessment_counts = {}
        all_shank_dfs = []
        for shank_id, rec_shank in rec_split.items():
            sorting_folder = os.path.join(day_output_dir, f"shank_{shank_id}_ks4")
            if not os.path.isdir(sorting_folder):
                continue
            sorting = si.read_sorter_folder(sorting_folder)
            if len(sorting.unit_ids) == 0:
                continue
            print(f"  Re-assessing shank {shank_id}...")
            # NOTE: this only re-assesses already-sorted units - it does NOT
            # re-apply saturation muting (that only happens before KS4
            # runs). Windows are recomputed here purely for the
            # saturation_overlap_frac diagnostic column. Severity-gated the
            # same way as the main pipeline: hopeless channels skip the
            # expensive precise scan.
            severity = scan_saturation_fraction_per_channel(rec_shank, fs)
            hopeless_thresh = CONFIG["saturation_detection"]["hopeless_fraction_thresh"]
            scan_channels = [ch for ch, frac in severity.items() if frac <= hopeless_thresh]
            per_channel_sat = detect_saturation_windows_per_channel(rec_shank, fs, channels_to_scan=scan_channels)
            merged_sat_windows = merge_windows_across_channels(per_channel_sat)
            counts, metrics_df = assess_shank(shank_id, rec_shank, sorting, session_metadata, fs,
                                               day_output_dir, day_tag, merged_sat_windows)
            shank_assessment_counts[shank_id] = counts
            all_shank_dfs.append(metrics_df)
            summary["assessed"].append(
                f"{day_tag} shank {shank_id}: SUA={counts['SUA']} MUA={counts['MUA']} "
                f"Noise/Artefact={counts['Noise/Artefact']}")

        # preserve the existing run_summary.csv's metadata header (written
        # by the original sorting run) and only refresh the units table -
        # re-assessment shouldn't lose the record of what KS4 parameters
        # actually produced these units.
        units_df = pd.concat(all_shank_dfs, ignore_index=True) if all_shank_dfs else None
        summary_path = os.path.join(day_output_dir, "run_summary.csv")
        if units_df is not None and os.path.exists(summary_path):
            with open(summary_path) as f:
                existing_lines = f.readlines()
            header_lines = [l for l in existing_lines if l.startswith("#")]
            header_lines.append(f"# --- Re-assessed: {datetime.now().isoformat(timespec='seconds')} ---\n")
            with open(summary_path, "w", newline="") as f:
                f.writelines(header_lines)
                units_df.to_csv(f, index=False)
            print(f"  Updated run_summary.csv (metadata header preserved).")
        elif units_df is not None:
            with open(summary_path, "w", newline="") as f:
                f.write(f"# Re-assessment only - no original run_summary.csv found to preserve metadata from.\n")
                f.write(f"# Generated: {datetime.now().isoformat(timespec='seconds')}\n")
                units_df.to_csv(f, index=False)

        print(f"  Re-assessment complete for {day_tag} "
              f"({len(shank_assessment_counts)} shank(s)).")

    except Exception as e:
        print(f"\n  ERROR re-assessing {day_tag}: {e}")
        traceback.print_exc()
        summary["errors"].append(f"{day_tag}: {e}")


def process_day(animal_id, date_str, session_paths, probe, ks4_params, summary):
    """
    Load all sessions for a given animal/day, concatenate in chronological
    order, remove EMG/ECoG channels, attach probe, split by shank, detect
    and remove bad channels, then run Kilosort4 per shank.
    """
    day_tag = f"{animal_id}/{date_str}"
    concatenating = CONFIG.get("concatenate_sessions", True)
    if not concatenating and len(session_paths) == 1:
        # individual-session mode: output must be keyed per session, not
        # just per day, or a second session on the same day would silently
        # overwrite the first session's output in the same folder.
        session_name = os.path.basename(session_paths[0])
        day_output_dir = get_day_output_dir(animal_id, date_str, session_name)
        day_tag = f"{animal_id}/{session_name}"
    else:
        day_output_dir = get_day_output_dir(animal_id, date_str)

    print(f"\n{'-'*70}\n{day_tag}  ({len(session_paths)} session(s))\n{'-'*70}")

    # --- existing output check ---
    if existing_output_present(day_output_dir):
        action = CONFIG["existing_output_action"]
        if action == "prompt":
            resp = input(f"  Sorted output already exists for {day_tag}. "
                          f"[s]kip / [o]verwrite / [c]ancel batch > ").strip().lower()
            if resp == "s" or resp == "":
                print("  Skipped.")
                summary["skipped"].append(day_tag)
                return
            elif resp == "c":
                print("Batch cancelled by user.")
                raise KeyboardInterrupt
            # else fall through to overwrite
        elif action == "skip":
            print("  Skipped (existing output, config set to skip).")
            summary["skipped"].append(day_tag)
            return
        # "overwrite" falls through

    try:
        # --- optionally stage raw session folders to local scratch first ---
        # For network-mounted data, si.read_openephys() performs scattered
        # reads directly over the network for every downstream operation
        # (bad-channel sampling, binary export, KS4 itself). A single
        # upfront sequential copy is usually far more efficient on SMB/CIFS
        # shares than many small chunked reads over the mount.
        work_paths = session_paths
        staged_root = None
        if CONFIG.get("stage_raw_locally"):
            staged_root = os.path.join(CONFIG["stage_raw_locally"], animal_id, date_str)
            os.makedirs(staged_root, exist_ok=True)
            work_paths = []
            print(f"  Staging {len(session_paths)} session(s) to local scratch...")
            stage_iter = tqdm(session_paths, desc="  Copying", unit="session") if tqdm else session_paths
            for p in stage_iter:
                dest = os.path.join(staged_root, os.path.basename(p))
                if not os.path.isdir(dest):
                    shutil.copytree(p, dest)
                work_paths.append(dest)

        # --- load recordings in folder order ---
        # NOTE: order here relies entirely on the zero-padded NNN suffix
        # sorting correctly (session_paths was sorted() in find_sessions).
        # This does NOT cross-check OpenEphys's own recorded start timestamps.
        # If a session was ever renumbered manually, or NNN does not reflect
        # true chronological order, concatenation order will be wrong and
        # KS4's drift correction will be fed a discontinuous timeline. Verify
        # your folder naming convention actually guarantees this - I have not
        # implemented a timestamp cross-check here.
        recordings = []
        session_frame_counts = []
        for p in work_paths:
            rec = si.read_openephys(p, stream_name=CONFIG["stream_name"],
                                     block_index=CONFIG["block_index"])
            recordings.append(rec)
            session_frame_counts.append(rec.get_num_frames())

        concatenate = CONFIG.get("concatenate_sessions", True)
        if concatenate and len(recordings) > 1:
            recording = si.concatenate_recordings(recordings)
        elif len(recordings) > 1:
            # individual-session mode: process each session as its own
            # independent recording (e.g. probe was lowered between them).
            # We still go through this function per session via the caller.
            raise RuntimeError(
                "concatenate_sessions=False with multiple sessions must be "
                "handled by looping process_day per-session in main(), not here.")
        else:
            recording = recordings[0]

        fs = recording.get_sampling_frequency()

        # --- record per-session frame offsets + TTL times for later video/spike alignment ---
        # NOTE on the TTL side: OpenEphys event timestamps are not guaranteed
        # to be zero-referenced to the continuous recording's sample 0 - the
        # acquisition clock can start before the recording clock. This is a
        # known open issue in SpikeInterface (GH #3300). The values saved
        # here are read directly from each session's own event stream, but
        # you should sanity-check the first TTL onset against your camera
        # trigger script's expected delay before trusting downstream
        # alignment blindly.
        session_metadata = []
        cumulative_offset = 0
        for p, orig_p, n_frames in zip(work_paths, session_paths, session_frame_counts):
            ttl_info = None
            try:
                events = si.read_openephys_event(p, block_index=CONFIG["block_index"])
                event_channels = list(events.channel_ids)
                # Match against the acquisition board stream (per user's
                # directory structure: .../events/Acquisition_Board-100.
                # acquisition_board/TTL), rather than blindly taking the
                # first event channel - other event streams may exist
                # (other Record Nodes, other devices) and index 0 is not
                # guaranteed to be the right one.
                match_key = CONFIG["stream_name"].split("#")[-1]  # e.g. "Acquisition_Board-100.acquisition_board"
                matched = [c for c in event_channels if match_key in str(c)]
                if not matched and event_channels:
                    print(f"    Warning: no event channel matched '{match_key}' for "
                          f"{os.path.basename(orig_p)}; found {event_channels}. "
                          f"Falling back to the first one - verify this is really the TTL line.")
                    matched = [event_channels[0]]

                if matched:
                    ev = events.get_events(channel_id=matched[0], segment_index=0)
                    if len(ev) > 0:
                        ttl_info = {
                            "channel_id": str(matched[0]),
                            "first_onset_s": float(ev[0]["time"]),
                            "last_offset_s": float(ev[-1]["time"] + ev[-1]["duration"]),
                            "n_events": int(len(ev)),
                        }
            except Exception as ev_err:
                print(f"    Warning: could not read TTL events for {os.path.basename(orig_p)}: {ev_err}")

            session_metadata.append({
                "session_path": orig_p,
                "frame_offset_in_concatenated": cumulative_offset,
                "n_frames": n_frames,
                "duration_s": n_frames / fs,
                "ttl": ttl_info,
            })
            cumulative_offset += n_frames

        os.makedirs(day_output_dir, exist_ok=True)
        with open(os.path.join(day_output_dir, "session_boundaries.json"), "w") as f:
            json.dump({"sampling_frequency": fs, "sessions": session_metadata}, f, indent=2)
        print(f"  Wrote session_boundaries.json ({len(session_metadata)} session(s)).")

        # --- remove EMG/ECoG auxiliary channels BEFORE probe assignment ---
        # This must happen before set_probe(): the probe has exactly 128
        # contacts, and set_probe requires (or silently mis-maps, depending
        # on version) the channel count to match. Attaching the probe to a
        # recording that still contains aux channels is a bug, not a
        # reordering choice - fixed here.
        aux_ids = CONFIG["aux_channel_ids"].get(animal_id, [])
        if aux_ids:
            all_ids = recording.get_channel_ids()
            keep_ids = [c for c in all_ids if c not in aux_ids]
            recording = recording.select_channels(keep_ids)
            print(f"  Removed {len(aux_ids)} EMG/ECoG channel(s) for {animal_id}.")

        n_chans = recording.get_num_channels()
        if n_chans != probe.get_contact_count():
            raise ValueError(
                f"Channel count mismatch after EMG/ECoG removal: recording has "
                f"{n_chans} channels, probe expects {probe.get_contact_count()}. "
                f"Check CONFIG['aux_channel_ids'] for {animal_id}.")

        # --- save to binary, then attach probe (recommended SI order) ---
        # Prefer a local scratch path for this temporary export when available.
        cache_root = CONFIG.get("binary_cache_dir") or os.environ.get("SPIKESORTING_BINARY_CACHE")
        if cache_root:
            binary_path = os.path.join(cache_root, animal_id, date_str, "recording_binary")
        else:
            binary_path = os.path.join(day_output_dir, "recording_binary")

        recording_saved = recording.save(folder=binary_path, overwrite=True,
                                          n_jobs=1, chunk_duration="10s")
        recording_saved = recording_saved.set_probe(probe, group_mode="by_shank")
        recording_split = recording_saved.split_by("group")

        # --- bad channel detection per shank ---
        bad_results = find_bad_channels_for_recording(recording_split, fs)
        clean_split = {}
        bad_channel_report = {}  # for run_report.txt
        auto_omit = CONFIG.get("auto_omit_bad_channels", True)
        for shank_id, rec in recording_split.items():
            bad_local, reasons, report = bad_results[shank_id]
            bad_channel_report[shank_id] = report
            print(f"  Shank {shank_id}:")
            for line in report:
                print(f"  {line}")

            if bad_local and auto_omit:
                chan_ids = rec.get_channel_ids()
                keep_ids = [c for i, c in enumerate(chan_ids) if i not in bad_local]
                clean_split[shank_id] = rec.select_channels(keep_ids)
                summary["channels_omitted"].append(
                    f"{day_tag} shank {shank_id}: {len(bad_local)} channel(s) "
                    f"({', '.join(sorted(set(reasons.values())))})")
            else:
                clean_split[shank_id] = rec

        # --- run Kilosort4 per shank, then assess units ---
        shank_iter = clean_split.items()
        if tqdm is not None:
            shank_iter = tqdm(list(shank_iter), desc=f"  KS4 [{day_tag}]", unit="shank")

        shank_unit_counts = {}
        shank_assessment_counts = {}
        all_shank_dfs = []
        run_assessment = CONFIG.get("run_unit_assessment", True)
        mute_saturation = CONFIG["saturation_detection"].get("mute_before_sorting", True)
        sat_cfg = CONFIG["saturation_detection"]
        min_chans = CONFIG.get("min_channels_to_sort_shank", 2)

        for shank_id, rec in shank_iter:
            if rec.get_num_channels() == 0:
                print(f"  Shank {shank_id}: 0 channels remaining after bad-channel exclusion - skipping "
                      f"(no saturation scan, no sorting).")
                summary["skipped"].append(f"{day_tag} shank {shank_id}: 0 channels remaining")
                continue

            merged_sat_windows = []
            if sat_cfg.get("enabled", True):
                # cheap first pass: identify channels that are saturated
                # ~always, and exclude them outright rather than paying for
                # the expensive precise scan + surgical muting on a channel
                # that's essentially unusable anyway.
                print(f"  Scanning shank {shank_id} for saturation severity...")
                severity = scan_saturation_fraction_per_channel(rec, fs)
                hopeless_local_idx = [ch for ch, frac in severity.items()
                                       if frac > sat_cfg["hopeless_fraction_thresh"]]
                if hopeless_local_idx:
                    chan_ids = rec.get_channel_ids()
                    hopeless_ids = [chan_ids[i] for i in hopeless_local_idx]
                    keep_ids = [c for c in chan_ids if c not in hopeless_ids]
                    print(f"  Shank {shank_id}: {len(hopeless_ids)} channel(s) saturated in "
                          f">{sat_cfg['hopeless_fraction_thresh']:.0%} of the recording - "
                          f"excluding outright rather than muting: {list(hopeless_ids)}")
                    summary["channels_omitted"].append(
                        f"{day_tag} shank {shank_id}: {len(hopeless_ids)} channel(s) (hopeless saturation)")
                    rec = rec.select_channels(keep_ids)

                if rec.get_num_channels() < min_chans:
                    print(f"  Shank {shank_id}: only {rec.get_num_channels()} channel(s) remain "
                          f"(< min_channels_to_sort_shank={min_chans}) - skipping sorting entirely.")
                    summary["skipped"].append(
                        f"{day_tag} shank {shank_id}: only {rec.get_num_channels()} channel(s) after exclusion")
                    continue

                # precise pass, now only over the channels actually worth it
                print(f"  Scanning shank {shank_id} for exact saturation windows "
                      f"({rec.get_num_channels()} channel(s))...")
                per_channel_sat = detect_saturation_windows_per_channel(rec, fs)
                merged_sat_windows = merge_windows_across_channels(per_channel_sat)
                if per_channel_sat:
                    n_windows = sum(len(w) for w in per_channel_sat.values())
                    print(f"  Flagged {n_windows} saturation window(s) across {len(per_channel_sat)} channel(s).")
                    if mute_saturation:
                        rec = SaturationMutedRecording(rec, per_channel_sat)
                        print(f"  Muted (zeroed) flagged windows before sorting "
                              f"(per-channel, per-window only - unaffected channels/periods untouched).")
            elif rec.get_num_channels() < min_chans:
                summary["skipped"].append(
                    f"{day_tag} shank {shank_id}: only {rec.get_num_channels()} channel(s), saturation check disabled")
                continue

            out_folder = os.path.join(day_output_dir, f"shank_{shank_id}_ks4")
            sorting = si.run_sorter(
                "kilosort4", rec, folder=out_folder,
                remove_existing_folder=True,
                **ks4_params,
            )
            n_units = len(sorting.get_unit_ids())
            shank_unit_counts[shank_id] = n_units
            summary["sorted"].append(f"{day_tag} shank {shank_id}: {n_units} units")

            if run_assessment and n_units > 0:
                print(f"  Assessing units on shank {shank_id}...")
                counts, metrics_df = assess_shank(shank_id, rec, sorting, session_metadata, fs,
                                                   day_output_dir, day_tag, merged_sat_windows)
                shank_assessment_counts[shank_id] = counts
                all_shank_dfs.append(metrics_df)
                summary["assessed"].append(
                    f"{day_tag} shank {shank_id}: SUA={counts['SUA']} MUA={counts['MUA']} "
                    f"Noise/Artefact={counts['Noise/Artefact']}")

        units_df = pd.concat(all_shank_dfs, ignore_index=True) if all_shank_dfs else None
        write_run_summary_csv(day_output_dir, day_tag, animal_id, date_str, session_paths,
                               probe, ks4_params, aux_ids, bad_channel_report, shank_unit_counts,
                               concatenating, auto_omit, units_df)

    except Exception as e:
        print(f"\n  ERROR processing {day_tag}: {e}")
        traceback.print_exc()
        resp = input("  [s]kip and continue / [a]bort batch > ").strip().lower()
        summary["errors"].append(f"{day_tag}: {e}")
        if resp == "a":
            raise
        return


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Batch Kilosort4 sorting for ASSY-350-H20 recordings.")
    parser.add_argument("--animal", type=str, default=None,
                         help="Restrict to a single AnimalID (default: all found).")
    parser.add_argument("--dry-run", action="store_true",
                         help="List sessions/grouping that would be processed, without sorting.")
    parser.add_argument("--skip-wizard", action="store_true",
                         help="Skip the interactive wizard (batch settings + KS4 params) and use CONFIG defaults.")
    parser.add_argument("--binary-cache-dir", type=str, default=None,
                         help="Optional local scratch directory for the temporary recording binary export.")
    parser.add_argument("--output-dir", type=str, default=None,
                         help="Write all sorting/assessment/phy output here instead of alongside the raw data "
                              "(mirrors animal/Raw_data/date[/session] under this root).")
    parser.add_argument("--stage-raw-locally", type=str, default=None,
                         help="Optional local scratch directory to copy raw OpenEphys sessions to before reading "
                              "(recommended when base_path is a network/UNC mount).")
    parser.add_argument("--no-concat", action="store_true",
                         help="Sort each session individually instead of concatenating same-day sessions "
                              "(use when the probe/drive depth changed between sessions).")
    parser.add_argument("--skip-assessment", action="store_true",
                         help="Skip post-sort unit assessment (Noise/Artefact/MUA/SUA classification).")
    parser.add_argument("--assess-only", action="store_true",
                         help="Skip sorting entirely and re-run unit assessment on already-sorted "
                              "shank_*_ks4 output for the matched animal/day group(s). Use this to "
                              "re-classify units (e.g. after changing assessment_thresholds) without "
                              "re-running Kilosort4.")
    parser.add_argument("--skip-phy-export", action="store_true",
                         help="Skip exporting each shank to a Phy-ready folder (skips writing a "
                              "per-shank recording.dat copy - saves disk space on long recordings).")
    args = parser.parse_args()

    os.environ["CUDA_VISIBLE_DEVICES"] = "0"  # must precede any CUDA-aware import in a fresh process

    if args.binary_cache_dir is not None:
        CONFIG["binary_cache_dir"] = args.binary_cache_dir
    if args.output_dir is not None:
        CONFIG["output_base_path"] = args.output_dir
    if args.stage_raw_locally is not None:
        CONFIG["stage_raw_locally"] = args.stage_raw_locally
    if args.no_concat:
        CONFIG["concatenate_sessions"] = False
    if args.skip_assessment:
        CONFIG["run_unit_assessment"] = False
    if args.skip_phy_export:
        CONFIG["export_to_phy"] = False

    grouped = find_sessions(CONFIG["base_path"], animal_filter=args.animal)
    if not grouped:
        print("No sessions found matching the expected directory structure. Check base_path.")
        return

    print(f"Found {len(grouped)} animal/day group(s):")
    for (animal_id, date_str), paths in grouped.items():
        print(f"  {animal_id} / {date_str}: {len(paths)} session(s)")
        for p in paths:
            print(f"    {os.path.basename(p)}")

    if args.dry_run:
        print("\nDry run - no sorting performed.")
        return

    summary = {"sorted": [], "skipped": [], "errors": [], "channels_omitted": [], "assessed": []}

    if args.assess_only:
        print("\n--assess-only: skipping sorting, re-running unit assessment on existing output.\n")
        for (animal_id, date_str), paths in grouped.items():
            assess_only_day(animal_id, date_str, paths, summary)
        print("\n" + "=" * 70)
        print("RE-ASSESSMENT SUMMARY")
        print("=" * 70)
        for line in summary["assessed"]:
            print(f"  UNIT {line}")
        for line in summary["errors"]:
            print(f"  FAIL {line}")
        return

    probe = load_probe()

    if args.skip_wizard:
        ks4_params = dict(CONFIG["ks4_params"])
    else:
        ks4_params = run_batch_wizard(CONFIG["ks4_params"])
    ks4_params = {k: v for k, v in ks4_params.items() if v is not None}

    day_items = list(grouped.items())
    try:
        for (animal_id, date_str), paths in day_items:
            if CONFIG.get("concatenate_sessions", True):
                process_day(animal_id, date_str, paths, probe, ks4_params, summary)
            else:
                # individual-session mode: process each session in the
                # group separately so no cross-session concatenation
                # happens (e.g. probe depth changed between recordings).
                for single_path in paths:
                    process_day(animal_id, date_str, [single_path], probe, ks4_params, summary)
    except KeyboardInterrupt:
        print("\nBatch cancelled.")

    # --- final summary ---
    print("\n" + "=" * 70)
    print("BATCH SUMMARY")
    print("=" * 70)
    print(f"Sorted:  {len(summary['sorted'])}")
    for line in summary["sorted"]:
        print(f"  OK   {line}")
    print(f"Skipped (existing output): {len(summary['skipped'])}")
    for line in summary["skipped"]:
        print(f"  SKIP {line}")
    print(f"Channel exclusions applied: {len(summary['channels_omitted'])}")
    for line in summary["channels_omitted"]:
        print(f"  OMIT {line}")
    print(f"Unit assessment: {len(summary['assessed'])}")
    for line in summary["assessed"]:
        print(f"  UNIT {line}")
    print(f"Errors: {len(summary['errors'])}")
    for line in summary["errors"]:
        print(f"  FAIL {line}")


if __name__ == "__main__":
    main()
