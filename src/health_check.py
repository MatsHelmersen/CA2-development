"""
health_check.py - Pre-flight environment checks and per-day signal quality
assessment for ephys_pipeline.

Two distinct entry points (see CLI at the bottom):

  --preflight   Environment-level checks that should pass before submitting
                any Slurm job: GPU/CUDA visibility, disk space, package
                imports, dataset-wide aux_channel_ids coverage, probe
                geometry sanity. Fast, no raw data read.

  --report      Per-day signal quality: bad channels, saturation severity,
                shank viability, and (optionally) spectral discharge
                detection. Reads raw data; takes minutes per day.

Scope (per ARCHITECTURE.md §4, §9):
  - Bad channel detection (dead, shorted, IBL-std noisy)
  - Saturation severity scan (two-tier: coarse fraction → precise windows)
  - Shank-skip decision based on remaining viable channels
  - Periodic-discharge spectral diagnostic (detection/reporting only —
    no filtering; see ARCHITECTURE.md §7 for why)
  - Pre-flight: GPU, disk, packages, aux coverage, probe geometry, NNN order

Explicitly OUT of scope:
  - Kilosort4 / sorting            -> ap_sorter.py
  - Unit assessment / classification -> quality_control.py
  - LFP extraction                 -> lfp_extractor.py
  - Saturation muting before sort  -> artifact_cleaning.py (the muting
    wrapper lives there; health_check.py only detects and reports)

Config is read via config_loader.load_config(env). No CONFIG dict is
hardcoded here — every threshold has a corresponding base.yaml key.
"""

import copy
import os
import sys
import json
import traceback
from pathlib import Path
from datetime import datetime
import numpy as np
from scipy.signal import butter, filtfilt, welch

import spikeinterface.full as si

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# Assumes this file lives at <repo_root>/src/health_check.py per ARCHITECTURE.md §3.
from src.config_loader import load_config
from src.io_utils import find_sessions, prepare_day, load_probe, resolve_probe_json_path, select_session


# ============================================================================
# PRE-FLIGHT CHECKS (environment-level, no raw data read)
# ============================================================================

def check_gpu(cfg) -> list:
    """
    Verify that the torch_device specified in ks4_params is reachable.
    Returns list of (level, message) tuples.
    """
    results = []
    device_str = cfg.get("ks4_params", {}).get("torch_device", "cuda:0")
    try:
        import torch
        if device_str.startswith("cuda"):
            if torch.cuda.is_available():
                idx = int(device_str.split(":")[-1]) if ":" in device_str else 0
                name = torch.cuda.get_device_name(idx)
                mem_gb = torch.cuda.get_device_properties(idx).total_memory / 1e9
                results.append(("PASS", f"CUDA device {device_str} visible: {name} ({mem_gb:.1f} GB)"))
            else:
                results.append(("FAIL", f"torch_device={device_str} but torch.cuda.is_available()=False. "
                                        f"Check CUDA installation and CUDA_VISIBLE_DEVICES."))
        else:
            results.append(("WARN", f"torch_device={device_str} (CPU). KS4 on CPU is extremely slow — "
                                    f"intended only for debugging."))
    except ImportError:
        results.append(("FAIL", "torch not importable. Install pytorch before running KS4."))
    except Exception as e:
        results.append(("FAIL", f"GPU check raised: {e}"))
    return results


def check_disk_space(cfg, bytes_per_channel_per_hour=1.5e9) -> list:
    """
    Estimate whether output_base_path and stage_raw_locally have enough
    free space for a typical day's output. The estimate is rough:
    128 channels × 30 kHz × 2 bytes/sample × 3600 s ≈ 27 GB/hour for raw;
    the binary export adds another copy. We flag if free space < 2× that
    estimate.

    bytes_per_channel_per_hour: per-channel raw byte rate (default calibrated
    for int16 at 30 kHz, ~1.5 GB/channel/hour including binary export).
    Not a config key because it's a property of the hardware, not a tunable
    parameter.
    """
    results = []
    n_channels = 128  # ASSY-350-H20 fixed
    typical_day_hours = 6  # conservative upper bound for a full trial+sleep day
    estimated_gb = (n_channels * bytes_per_channel_per_hour * typical_day_hours) / 1e9

    paths_to_check = {}
    output_base = cfg.get("paths", {}).get("output_base_path")
    stage_root = cfg.get("paths", {}).get("stage_raw_locally")

    if output_base:
        paths_to_check["output_base_path"] = output_base
    if stage_root:
        paths_to_check["stage_raw_locally"] = stage_root

    if not paths_to_check:
        results.append(("WARN", "output_base_path unset — output goes alongside raw data. "
                                "Disk space not checked (unknown raw data partition)."))
        return results

    import shutil
    for label, path in paths_to_check.items():
        try:
            os.makedirs(path, exist_ok=True)
            usage = shutil.disk_usage(path)
            free_gb = usage.free / 1e9
            if free_gb >= 2 * estimated_gb:
                results.append(("PASS", f"{label}: {free_gb:.0f} GB free "
                                        f"(estimated need ~{estimated_gb:.0f} GB/day)"))
            elif free_gb >= estimated_gb:
                results.append(("WARN", f"{label}: {free_gb:.0f} GB free — marginal "
                                        f"(estimated ~{estimated_gb:.0f} GB/day). "
                                        f"May fail mid-run on a long day."))
            else:
                results.append(("FAIL", f"{label}: only {free_gb:.0f} GB free, "
                                        f"estimated need ~{estimated_gb:.0f} GB/day."))
        except Exception as e:
            results.append(("FAIL", f"{label}: could not check disk space at {path}: {e}"))

    return results


def check_packages() -> list:
    """
    Verify that the packages required for the full pipeline are importable.
    Versions are checked against known-minimum versions that are required for
    APIs this pipeline uses; they are not pinned to exact versions since
    environment.yml is the canonical source for that.
    """
    results = []
    required = [
        # (import_name, pip_name, min_version_tuple or None)
        ("spikeinterface", "spikeinterface", (0, 100, 0)),
        ("probeinterface", "probeinterface", None),
        ("scipy", "scipy", None),
        ("numpy", "numpy", None),
        ("yaml", "pyyaml", None),
        ("torch", "torch", None),
    ]
    for import_name, pip_name, min_ver in required:
        try:
            mod = __import__(import_name)
            ver_str = getattr(mod, "__version__", "unknown")
            if min_ver is not None:
                try:
                    ver_tuple = tuple(int(x) for x in ver_str.split(".")[:3])
                    if ver_tuple < min_ver:
                        min_str = ".".join(str(x) for x in min_ver)
                        results.append(("WARN", f"{pip_name} {ver_str} < minimum {min_str} — "
                                                f"some APIs used by this pipeline may be absent."))
                        continue
                except ValueError:
                    pass  # non-numeric version string; skip version comparison
            results.append(("PASS", f"{pip_name} {ver_str}"))
        except ImportError:
            results.append(("FAIL", f"{pip_name} not importable. Run: pip install {pip_name}"))
    return results


def check_aux_coverage(cfg) -> list:
    """
    Dataset-wide aux_channel_ids coverage: compare every animal found on disk
    (across ALL animals, not filtered) against every key in aux_channel_ids,
    both directions.

    Does not require reading any raw data — only walks the directory tree
    via find_sessions().
    """
    results = []
    try:
        grouped = find_sessions(cfg)  # no animal_filter -> all animals
        discovered = {animal for (animal, _) in grouped.keys()}
    except Exception as e:
        results.append(("FAIL", f"find_sessions() raised during aux coverage check: {e}"))
        return results

    if not discovered:
        results.append(("WARN", "No animals found on disk — cannot check aux_channel_ids coverage."))
        return results

    configured = set(cfg.get("aux_channel_ids", {}).keys())

    unconfigured = discovered - configured
    if unconfigured:
        results.append(("WARN", f"Animal(s) on disk with no aux_channel_ids entry "
                                f"(fine if genuinely no EMG/ECoG — verify): "
                                f"{sorted(unconfigured)}"))
    else:
        results.append(("PASS", f"All {len(discovered)} discovered animal(s) have an "
                                f"aux_channel_ids entry."))

    stale = configured - discovered
    if stale:
        results.append(("WARN", f"aux_channel_ids has entries for animal(s) not found on disk "
                                f"(typo, or animal not yet run): {sorted(stale)}"))

    return results


def check_probe_geometry(cfg) -> list:
    """
    Resolve the probe JSON and run geometry sanity checks: contact count,
    shank count, no duplicate contact positions. Uses io_utils.load_probe()
    so the override/fallback path logic is not duplicated here.
    """
    results = []
    try:
        probe_path = resolve_probe_json_path(cfg)
        results.append(("PASS", f"Probe JSON resolves to: {probe_path}"))
    except Exception as e:
        results.append(("FAIL", f"Probe JSON path could not be resolved: {e}"))
        return results

    if not probe_path.exists():
        results.append(("FAIL", f"Probe JSON not found at {probe_path}"))
        return results

    try:
        probe = load_probe(cfg)
    except Exception as e:
        results.append(("FAIL", f"load_probe() raised: {e}"))
        return results

    n_contacts = probe.get_contact_count()
    n_shanks = probe.get_shank_count()

    if n_contacts == 128:
        results.append(("PASS", f"Contact count: {n_contacts} (expected 128 for ASSY-350-H20)"))
    else:
        results.append(("FAIL", f"Contact count: {n_contacts} — expected 128 for ASSY-350-H20. "
                                f"Is this the corrected sitemap JSON?"))

    if n_shanks == 4:
        results.append(("PASS", f"Shank count: {n_shanks} (expected 4)"))
    else:
        results.append(("WARN", f"Shank count: {n_shanks} — expected 4 for ASSY-350-H20."))

    # Duplicate contact position check
    positions = probe.contact_positions  # shape (n_contacts, 2)
    rounded = np.round(positions, decimals=1)
    seen = set()
    dupes = []
    for i, pos in enumerate(rounded):
        key = tuple(pos)
        if key in seen:
            dupes.append((i, key))
        seen.add(key)
    if dupes:
        results.append(("FAIL", f"{len(dupes)} duplicate contact position(s) found in probe JSON — "
                                f"this will corrupt spike sorting geometry. "
                                f"First duplicate: contact {dupes[0][0]} at {dupes[0][1]}"))
    else:
        results.append(("PASS", f"No duplicate contact positions."))

    return results


def check_session_ordering(cfg, animal_filter=None) -> list:
    """
    Cross-check NNN-suffix folder order against OpenEphys's own recorded
    start timestamps for every session group. A mismatch means concatenation
    order would be wrong and KS4's drift correction would be fed a
    discontinuous timeline.

    This is the check identified in ARCHITECTURE.md §4a / §7 as the
    'NNN-ordering caveat'. It requires reading the OpenEphys recording
    metadata (not the traces), which is cheap.

    NOTE: si.read_openephys() is called here to retrieve the recording
    object purely for its metadata; no trace data is read. If the SI
    version installed does not expose a reliable creation timestamp via
    the recording object, this check degrades gracefully to WARN rather
    than FAIL or crash.
    """
    results = []
    try:
        grouped = find_sessions(cfg, animal_filter=animal_filter)
    except Exception as e:
        results.append(("FAIL", f"find_sessions() raised during ordering check: {e}"))
        return results

    if not grouped:
        results.append(("WARN", "No session groups found — cannot check ordering."))
        return results

    stream_name = cfg.get("recording", {}).get("stream_name", "")
    block_index = cfg.get("recording", {}).get("block_index", 0)
    any_checked = False

    for (animal_id, date_str), session_paths in grouped.items():
        if len(session_paths) < 2:
            continue  # single session: ordering is trivially correct

        timestamps = []
        for path in session_paths:
            try:
                rec = si.read_openephys(path, stream_name=stream_name, block_index=block_index)
                # SpikeInterface exposes neo-level metadata; the most reliable
                # cross-version way to get start time is the t_start annotation
                # on the first segment. Not all SI versions expose this cleanly —
                # wrapped defensively.
                t_start = None
                for attr in ("_t_start", "t_start"):
                    try:
                        t_start = getattr(rec._recording_segments[0], attr, None)
                        if t_start is not None:
                            break
                    except Exception:
                        pass
                timestamps.append(t_start)
            except Exception as e:
                results.append(("WARN", f"  {animal_id}/{date_str}: could not read metadata "
                                        f"for {os.path.basename(path)}: {e}"))
                timestamps.append(None)
                break

        if any(t is None for t in timestamps):
            results.append(("WARN", f"{animal_id}/{date_str}: start timestamps unavailable in "
                                    f"recording metadata — NNN ordering cannot be verified. "
                                    f"Verify manually that folder NNN order is chronological."))
            continue

        any_checked = True
        expected_order = sorted(range(len(timestamps)), key=lambda i: timestamps[i])
        if expected_order == list(range(len(timestamps))):
            results.append(("PASS", f"{animal_id}/{date_str}: session order matches timestamps "
                                    f"({len(session_paths)} sessions)"))
        else:
            mismatch_detail = ", ".join(
                f"{os.path.basename(session_paths[i])} (rank {expected_order.index(i)})"
                for i in range(len(session_paths))
            )
            results.append(("FAIL", f"{animal_id}/{date_str}: NNN folder order does NOT match "
                                    f"recorded timestamps — concatenation order would be wrong. "
                                    f"Correct order by timestamp: {mismatch_detail}"))

    if not any_checked and not any(level == "FAIL" for level, _ in results):
        results.append(("PASS", "All session groups have only one session — ordering trivially correct."))

    return results


def run_preflight(cfg, animal_filter=None, check_ordering=False) -> list:
    """
    Run all environment-level pre-flight checks and print a PASS/WARN/FAIL
    report. Returns list of (level, message) tuples. Exits with code 1 if
    any FAIL, so this is usable as a gate in a Slurm job script.

    check_ordering=False by default because it requires reading recording
    metadata for every session group, which is slower than the other checks.
    Pass --check-ordering on the CLI to enable it.
    """
    print(f"\n{'='*70}\nPRE-FLIGHT CHECK (env={cfg.get('_env', '?')})\n{'='*70}")

    all_results = []
    sections = [
        ("Packages",            check_packages()),
        ("GPU / CUDA",          check_gpu(cfg)),
        ("Disk space",          check_disk_space(cfg)),
        ("Probe geometry",      check_probe_geometry(cfg)),
        ("Aux channel coverage", check_aux_coverage(cfg)),
    ]
    if check_ordering:
        sections.append(("Session ordering", check_session_ordering(cfg, animal_filter)))

    for section_name, results in sections:
        print(f"\n  [{section_name}]")
        for level, msg in results:
            print(f"    [{level}] {msg}")
        all_results.extend(results)

    n_fail = sum(1 for l, _ in all_results if l == "FAIL")
    n_warn = sum(1 for l, _ in all_results if l == "WARN")
    n_pass = sum(1 for l, _ in all_results if l == "PASS")
    print(f"\n{'='*70}")
    print(f"Pre-flight result: {n_fail} FAIL, {n_warn} WARN, {n_pass} PASS")
    if n_fail > 0:
        print("FAIL — fix the issues above before submitting a batch run.")
    elif n_warn > 0:
        print("WARN — review warnings above before proceeding.")
    else:
        print("PASS — environment looks healthy.")
    print("=" * 70)

    return all_results


# ============================================================================
# BAD CHANNEL DETECTION
# ============================================================================

def detect_bad_channels(raw_shank, shank_chs, fs, cfg, subsample_s=None):
    """
    Detect dead and shorted channels on a single shank.

    raw_shank : (n_channels, n_samples) array
    shank_chs : channel label list matching raw_shank rows (for reporting)
    fs        : sampling rate (Hz)
    cfg       : merged config dict (thresholds read from bad_channel_detection.*)

    Returns (bad_channels: set[int local index], bad_reason: dict, report: list[str])
    """
    bc_cfg = cfg.get("bad_channel_detection", {})

    if subsample_s is None:
        subsample_s = bc_cfg.get("bad_channel_subsample_s", 30.0)

    dead_var_thresh = bc_cfg.get("dead_var_thresh", 10.0)
    short_corr_thresh = bc_cfg.get("short_corr_thresh", 0.95)

    n_ch, n_samp = raw_shank.shape
    step = max(1, n_samp // int(subsample_s * fs))
    data = raw_shank[:, ::step]
    report, bad_channels, bad_reason = [], set(), {}

    # --- dead channel check ---
    for i, v in enumerate(np.var(data, axis=1)):
        if v < dead_var_thresh:
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
                if abs(corr[a_idx, b_idx]) > short_corr_thresh:
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
    report.insert(0, f"  Bad channels: {n_bad}/{n_ch} ({n_bad / max(n_ch, 1):.0%})")
    return bad_channels, bad_reason, report


def _sample_windows(rec, fs, total_s, n_windows=5):
    """
    Pull short windows spread across the full recording rather than loading
    everything. Important for day-long concatenated recordings: a marginal
    connection can appear mid-day, so sampling only the first N seconds
    would miss it.
    Returns (n_channels, n_samples) array, concatenated across windows.
    """
    n_frames = rec.get_num_frames()
    win_frames = int(total_s / n_windows * fs)
    if n_frames <= win_frames * n_windows:
        return rec.get_traces().T
    starts = np.linspace(0, n_frames - win_frames, n_windows, dtype=int)
    chunks = [rec.get_traces(start_frame=s, end_frame=s + win_frames).T for s in starts]
    return np.concatenate(chunks, axis=1)


def find_bad_channels_for_recording(recording_split, fs, cfg):
    """
    Run detect_bad_channels per shank on a split recording dict
    (shank_id -> SpikeInterface recording), then optionally apply the
    IBL std-based outlier check as a supplementary pass.

    The custom dead/shorted logic cannot catch elevated-but-uncorrelated
    noise (e.g. a loose connector). IBL's std outlier method fills that
    gap. See IBL et al. (2022), 'Spike sorting pipeline for the
    International Brain Laboratory'.

    Returns dict: shank_id -> (bad_local_indices: set, reasons: dict, report: list)
    """
    results = {}
    bc_cfg = cfg.get("bad_channel_detection", {})
    # Key must match base.yaml: bad_channel_detection.bad_channel_subsample_s
    subsample_s = bc_cfg.get("bad_channel_subsample_s", 30.0)

    for shank_id, rec in recording_split.items():
        traces = _sample_windows(rec, fs, total_s=subsample_s)
        chan_ids = rec.get_channel_ids()
        bad, reasons, report = detect_bad_channels(traces, chan_ids, fs, cfg,
                                                    subsample_s=subsample_s)

        if bc_cfg.get("run_ibl_std_check", True):
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
                            report.append(
                                f"    ch {cid:>4} (local {i:2d}): NOISY "
                                f"(IBL std-outlier check, label={label})")
                n_ch = len(chan_ids)
                report[0] = f"  Bad channels: {len(bad)}/{n_ch} ({len(bad) / max(n_ch, 1):.0%})"
            except Exception as e:
                report.append(f"    (IBL std-based check failed: {e})")

        results[shank_id] = (bad, reasons, report)
    return results


# ============================================================================
# SATURATION DETECTION (two-tier)
# ============================================================================

def scan_saturation_fraction_per_channel(rec, fs, cfg):
    """
    FAST first pass: per-channel fraction of the whole recording flagged as
    clipped/saturated, computed at chunk granularity (one boolean per chunk
    per channel, not per sample). A channel flagged in more than
    hopeless_fraction_thresh of chunks is 'hopeless' and should be excluded
    outright rather than entering the expensive precise scan.

    Returns dict: local_channel_index -> fraction of chunks flagged (0–1).
    """
    sat_cfg = cfg.get("saturation_detection", {})
    if not sat_cfg.get("enabled", True):
        return {}

    n_frames = rec.get_num_frames()
    n_chans = rec.get_num_channels()
    if n_chans == 0 or n_frames == 0:
        return {}

    chunk_frames = int(sat_cfg.get("chunk_s", 30.0) * fs)
    dtype = rec.get_dtype()
    clip_frac = sat_cfg.get("clip_fraction_of_range", 0.98)

    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        clip_hi = info.max * clip_frac
        clip_lo = info.min * clip_frac
    else:
        probe_chunk = rec.get_traces(start_frame=0, end_frame=min(chunk_frames, n_frames))
        clip_hi = np.percentile(probe_chunk, 99.9) * clip_frac
        clip_lo = np.percentile(probe_chunk, 0.1) * clip_frac

    starts = list(range(0, n_frames, chunk_frames))
    flagged_chunk_counts = np.zeros(n_chans)

    chunk_iter = (tqdm(starts, desc="  Saturation severity scan", unit="chunk")
                  if tqdm else starts)
    for start in chunk_iter:
        end = min(start + chunk_frames, n_frames)
        chunk = rec.get_traces(start_frame=start, end_frame=end)
        # Per-channel fraction of clipped samples within this chunk
        clipped_frac = np.mean((chunk >= clip_hi) | (chunk <= clip_lo), axis=0)
        flagged_chunk_counts += (clipped_frac > 0.01)

    return {ch: float(flagged_chunk_counts[ch] / len(starts)) for ch in range(n_chans)}


def detect_saturation_windows_per_channel(rec, fs, cfg, channels_to_scan=None,
                                           coarse_flagged_chunks=None):
    """
    Precise chunked scan for exact (start_sample, end_sample) saturation
    windows. Only run on channels that survived the coarse pass (i.e., are
    not 'hopeless'). If coarse_flagged_chunks is provided (dict:
    channel -> set of chunk start indices that were flagged in the coarse
    pass), clean chunks are skipped entirely — this is the key bottleneck
    fix: the derivative calculation only runs on chunks already flagged,
    not every chunk in the recording.

    FIX (vs. sort_batch.py): MAD is now computed per-channel
    (axis=0 median), not as a global scalar across all channels and samples.
    The original global MAD collapses the 2D chunk to a single number,
    meaning a dead channel pulls the MAD toward zero and causes the jump
    threshold to collapse, falsely flagging everything as saturation.

    Returns dict: local_channel_index -> list of (start_sample, end_sample).
    """
    sat_cfg = cfg.get("saturation_detection", {})
    if not sat_cfg.get("enabled", True):
        return {}

    n_frames = rec.get_num_frames()
    n_chans = rec.get_num_channels()
    if n_chans == 0 or n_frames == 0:
        return {}

    chunk_frames = int(sat_cfg.get("chunk_s", 30.0) * fs)
    dtype = rec.get_dtype()
    clip_frac = sat_cfg.get("clip_fraction_of_range", 0.98)
    deriv_mad_mult = sat_cfg.get("derivative_mad_multiple", 20.0)

    if channels_to_scan is None:
        channels_to_scan = list(range(n_chans))
    if not channels_to_scan:
        return {}

    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        clip_hi = info.max * clip_frac
        clip_lo = info.min * clip_frac
    else:
        probe_chunk = rec.get_traces(start_frame=0, end_frame=min(chunk_frames, n_frames))
        clip_hi = np.percentile(probe_chunk, 99.9) * clip_frac
        clip_lo = np.percentile(probe_chunk, 0.1) * clip_frac

    per_channel_flags = {ch: [] for ch in channels_to_scan}
    starts = list(range(0, n_frames, chunk_frames))
    dense_chunk_frac_thresh = 0.30

    chunk_iter = (tqdm(starts, desc="  Saturation precise scan", unit="chunk")
                  if tqdm else starts)

    for start in chunk_iter:
        # If coarse pass info is available, skip any chunk that had no
        # coarse flag on ANY of the channels we're scanning. A chunk with
        # no coarse flag cannot contain a saturation window worth recording.
        if coarse_flagged_chunks is not None:
            any_flagged = any(start in coarse_flagged_chunks.get(ch, set())
                              for ch in channels_to_scan)
            if not any_flagged:
                continue

        end = min(start + chunk_frames, n_frames)
        chunk = rec.get_traces(start_frame=start, end_frame=end, channel_ids=None)
        chunk = chunk[:, channels_to_scan]  # (samples, n_scan_chans)

        clipped = (chunk >= clip_hi) | (chunk <= clip_lo)

        # Per-channel MAD (axis=0 = across samples for each channel separately).
        # Broadcasting: mad shape (n_scan_chans,), diffs shape (samples-1, n_scan_chans).
        diffs = np.abs(np.diff(chunk, axis=0))
        mad = np.median(np.abs(chunk - np.median(chunk, axis=0)), axis=0) + 1e-9
        jumped = diffs > (deriv_mad_mult * mad)
        jumped = np.concatenate([np.zeros((1, chunk.shape[1]), dtype=bool), jumped], axis=0)
        flagged = clipped | jumped  # (samples, n_scan_chans)

        for local_out_idx, ch in enumerate(channels_to_scan):
            col = flagged[:, local_out_idx]
            frac = col.mean()
            if frac == 0:
                continue
            if frac > dense_chunk_frac_thresh:
                # Densely flagged — record whole chunk as one window rather
                # than running the expensive segment-finding on it.
                per_channel_flags[ch].append((int(start), int(end - 1)))
                continue
            flagged_idx = np.where(col)[0]
            gaps = np.where(np.diff(flagged_idx) > 1)[0]
            seg_starts = np.concatenate([[0], gaps + 1])
            seg_ends = np.concatenate([gaps, [len(flagged_idx) - 1]])
            for s, e in zip(seg_starts, seg_ends):
                per_channel_flags[ch].append(
                    (int(start + flagged_idx[s]), int(start + flagged_idx[e])))

    return {ch: wins for ch, wins in per_channel_flags.items() if wins}


def _build_coarse_flagged_chunks(rec, fs, cfg, channels_to_scan):
    """
    Re-run the coarse pass on only the channels in channels_to_scan and
    return a dict: channel_local_index -> set of chunk start_frame values
    that were flagged. This is passed to detect_saturation_windows_per_channel
    so it can skip clean chunks entirely.

    This is a targeted re-pass rather than re-using scan_saturation_fraction_per_channel
    because that function returns fractions over all channels; here we need
    the per-chunk boolean for a specific subset of channels.
    """
    sat_cfg = cfg.get("saturation_detection", {})
    n_frames = rec.get_num_frames()
    chunk_frames = int(sat_cfg.get("chunk_s", 30.0) * fs)
    clip_frac = sat_cfg.get("clip_fraction_of_range", 0.98)
    dtype = rec.get_dtype()

    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        clip_hi = info.max * clip_frac
        clip_lo = info.min * clip_frac
    else:
        probe_chunk = rec.get_traces(start_frame=0, end_frame=min(chunk_frames, n_frames))
        clip_hi = np.percentile(probe_chunk, 99.9) * clip_frac
        clip_lo = np.percentile(probe_chunk, 0.1) * clip_frac

    flagged_chunks = {ch: set() for ch in channels_to_scan}
    starts = list(range(0, n_frames, chunk_frames))
    for start in starts:
        end = min(start + chunk_frames, n_frames)
        chunk = rec.get_traces(start_frame=start, end_frame=end)
        chunk_sub = chunk[:, channels_to_scan]
        clipped_frac = np.mean((chunk_sub >= clip_hi) | (chunk_sub <= clip_lo), axis=0)
        for local_out_idx, ch in enumerate(channels_to_scan):
            if clipped_frac[local_out_idx] > 0.01:
                flagged_chunks[ch].add(start)

    return flagged_chunks


# ============================================================================
# PERIODIC DISCHARGE DETECTION (spectral, diagnostic only)
# ============================================================================

def detect_periodic_discharges(rec, fs, cfg) -> dict:
    """
    Detect anomalous narrowband spectral peaks that may indicate a periodic
    hardware or electrophysiological artifact — such as the ~1 kHz recurrent
    discharge observed in a subset of recordings.

    IMPORTANT: This is a DIAGNOSTIC only. No filtering is applied here.
    The detection is agnostic to the specific frequency: it identifies any
    spectral peak whose power significantly exceeds the local spectral
    background (estimated via a rolling median of the power spectrum). This
    catches periodic artifacts at any frequency without requiring a
    pre-specified target.

    The decision NOT to filter here is deliberate:
      - A comb filter is designed for continuous, stationary sinusoidal
        interference (e.g. mains hum). A transient, recurrent discharge is
        not stationary and produces broadband spectral energy; notching it
        introduces time-domain ringing and removes genuine signal.
      - The spatial pattern (phase-locked across shanks vs. single-channel)
        has not been confirmed for this artifact. That information determines
        the correct remediation — common-mode rejection, per-channel muting,
        or no action.
      - If confirmed as a continuous hardware tone, filtering belongs in
        artifact_cleaning.py, not here. See ARCHITECTURE.md §7.

    Method: Welch's PSD per channel, median-normalized to estimate local
    spectral floor, peaks detected via simple threshold on the normalised
    spectrum. Returns per-channel results; the report identifies channels
    with strong peaks and their approximate frequencies.

    Config keys (under discharge_detection in base.yaml, all optional):
      freq_range_hz: [low, high] — search band (default [300, 5000] Hz,
                     below LFP range, above spike-sorting cutoff)
      floor_window_hz: rolling window width (Hz) for local floor estimate
                       (default 200 Hz)
      peak_snr_thresh: minimum peak/floor ratio to flag (default 10.0,
                       i.e. 10 dB above local median floor)
      n_windows: number of windows sampled from the recording (default 3)
      window_s: length of each window in seconds (default 5.0)

    Returns dict: channel_id -> {"peak_freq_hz": float, "peak_snr": float}
                  for channels with a detected peak. Empty dict if none found
                  or if disabled.
    """
    dd_cfg = cfg.get("discharge_detection", {})
    if not dd_cfg.get("enabled", True):
        return {}

    freq_lo, freq_hi = dd_cfg.get("freq_range_hz", [300, 5000])
    floor_window_hz = dd_cfg.get("floor_window_hz", 200.0)
    peak_snr_thresh = dd_cfg.get("peak_snr_thresh", 10.0)
    n_windows = dd_cfg.get("n_windows", 3)
    window_s = dd_cfg.get("window_s", 5.0)

    n_frames = rec.get_num_frames()
    win_frames = int(window_s * fs)
    if win_frames > n_frames:
        win_frames = n_frames

    # Sample n_windows evenly across the recording
    if n_frames <= win_frames * n_windows:
        starts = [0]
    else:
        starts = list(np.linspace(0, n_frames - win_frames, n_windows, dtype=int))

    chan_ids = rec.get_channel_ids()
    n_chans = len(chan_ids)

    # Accumulate PSDs across windows (averaged in linear power space)
    psd_sum = None
    freqs = None
    for start in starts:
        end = min(start + win_frames, n_frames)
        traces = rec.get_traces(start_frame=start, end_frame=end).T  # (chans, samples)
        f, psd = welch(traces, fs=fs, nperseg=min(int(fs), traces.shape[1]),
                       axis=1)  # psd: (chans, freq_bins)
        if psd_sum is None:
            psd_sum = psd.copy()
            freqs = f
        else:
            psd_sum += psd

    if psd_sum is None:
        return {}
    psd_mean = psd_sum / len(starts)

    # Restrict to target frequency range
    mask = (freqs >= freq_lo) & (freqs <= freq_hi)
    freqs_sub = freqs[mask]
    psd_sub = psd_mean[:, mask]  # (chans, freq_bins_in_range)

    if freqs_sub.size == 0:
        return {}

    # Local spectral floor via rolling median (convert floor_window_hz to bins)
    freq_res = freqs_sub[1] - freqs_sub[0] if len(freqs_sub) > 1 else 1.0
    half_win = max(1, int(floor_window_hz / freq_res / 2))

    results = {}
    for ch_idx in range(n_chans):
        spectrum = psd_sub[ch_idx]
        n_bins = len(spectrum)

        # Rolling median floor (reflect-padded for edges)
        floor = np.array([
            np.median(spectrum[max(0, i - half_win): min(n_bins, i + half_win + 1)])
            for i in range(n_bins)
        ])
        floor = np.where(floor > 0, floor, 1e-30)
        snr = spectrum / floor

        peak_idx = np.argmax(snr)
        peak_snr = snr[peak_idx]

        if peak_snr >= peak_snr_thresh:
            results[chan_ids[ch_idx]] = {
                "peak_freq_hz": float(freqs_sub[peak_idx]),
                "peak_snr": float(peak_snr),
            }

    return results


# ============================================================================
# PER-DAY HEALTH REPORT
# ============================================================================

def generate_health_report(cfg, animal_id, date_str, skip_staging=True,
                            run_spectral_check=False, session_name=None) -> bool:
    """
    Load raw data for one animal/day, run bad-channel detection and saturation
    scanning, decide which shanks are viable for sorting, optionally run the
    periodic-discharge spectral diagnostic, and write health_report.json +
    health_report.txt to the day's output directory.

    Parameters
    ----------
    cfg              : merged config dict from load_config()
    animal_id        : animal ID string (must match directory name)
    date_str         : date string YYYYMMDD (must match directory name)
    skip_staging     : if True (default), stage_raw_locally is bypassed even
                       if set in config — avoids a full network copy for a
                       spot-check. Pass False to test staging itself.
    run_spectral_check : if True, run detect_periodic_discharges per shank
                         after the bad-channel/saturation pipeline. Adds
                         compute time (Welch PSD per channel) but is much
                         cheaper than sorting. Disabled by default since the
                         artifact is not present in all recordings.
    session_name     : if given (e.g. "20231105_002"), restricts the report
                       to that ONE session rather than every session found
                       for animal_id/date_str, and forces
                       io_utils.prepare_day(concatenate=False) - matching
                       prepare_day()'s existing individual-session mode, so
                       health_report.json / saturation_windows.json land in
                       the session-specific output dir rather than the
                       day-level one. Use for a day where the probe/drive
                       was moved between sessions (ARCHITECTURE.md Sec.6)
                       and you want a report for just one of them.

    Returns True if the report was written successfully, False if a fatal
    error prevented it (non-zero exit code in CLI).
    """
    session_tag = f" / session {session_name}" if session_name else ""
    print(f"\n{'='*70}\nHealth Report: {animal_id} / {date_str}{session_tag}\n{'='*70}")

    grouped = find_sessions(cfg, animal_filter=animal_id)
    key = (animal_id, date_str)
    if key not in grouped:
        print(f"  [FAIL] no sessions found for {animal_id}/{date_str}.")
        return False

    session_paths = grouped[key]

    concatenate = True
    if session_name is not None:
        try:
            session_paths = select_session(session_paths, session_name)
        except ValueError as e:
            print(f"  [FAIL] {e}")
            return False
        concatenate = False  # 1-element list + concatenate=False -> individual session mode

    # Deep copy so staging bypass doesn't mutate the caller's config.
    cfg_for_check = copy.deepcopy(cfg)
    if skip_staging:
        cfg_for_check.setdefault("paths", {})["stage_raw_locally"] = None

    try:
        probe = load_probe(cfg_for_check)
        result = prepare_day(cfg_for_check, animal_id, date_str, session_paths, probe,
                              concatenate=concatenate)
    except Exception as e:
        print(f"  [FAIL] Data preparation failed: {e}")
        traceback.print_exc()
        return False

    rec = result["recording"]
    fs = result["fs"]
    day_output_dir = result["day_output_dir"]
    recording_split = rec.split_by("group")

    sort_cfg = cfg_for_check.get("sorting", {})
    min_chans = sort_cfg.get("min_channels_to_sort_shank", 2)
    sat_cfg = cfg_for_check.get("saturation_detection", {})
    hopeless_thresh = sat_cfg.get("hopeless_fraction_thresh", 0.5)

    report_data = {
        "animal_id": animal_id,
        "date_str": date_str,
        "generated_at": datetime.now().isoformat(),
        "config_env": cfg_for_check.get("_env", "unknown"),
        "shanks": {},
    }
    # Accumulator for saturation_windows.json (ARCHITECTURE.md Sec.5 "NEW
    # CONTRACT", documented in artifact_cleaning.py's module docstring but
    # never actually implemented here until now - see session note where
    # this was caught). Channel-ID-keyed (not local-index-keyed), unlike
    # per_channel_sat below which is local-to-clean_rec-at-this-instant.
    # Only PASS shanks with at least one flagged window get an entry -
    # SKIPPED shanks were never precisely scanned (see the "STATUS: SKIPPED"
    # branch above, which never reaches the precise-scan block below).
    saturation_windows_data = {
        "sampling_frequency": fs,
        "shanks": {},
    }
    text_lines = [
        f"HEALTH REPORT: {animal_id} / {date_str}",
        f"Generated:     {report_data['generated_at']}",
        f"Env:           {report_data['config_env']}",
        f"Sessions ({len(session_paths)}): "
        + ", ".join(os.path.basename(p) for p in session_paths),
        "-" * 70,
    ]

    # ---- bad channel detection (all shanks) --------------------------------
    print("  Detecting bad channels (all shanks)...")
    bad_results = find_bad_channels_for_recording(recording_split, fs, cfg_for_check)

    # ---- per-shank pipeline ------------------------------------------------
    for shank_id, shank_rec in recording_split.items():
        print(f"\n  Shank {shank_id}:")
        bad_local, reasons, bad_report = bad_results[shank_id]

        # Convert integer-keyed reasons to string keys so json.dump works
        # correctly and programmatic reloaders don't get type surprises.
        reasons_serialisable = {str(k): v for k, v in reasons.items()}

        shank_info = {
            "initial_channels": shank_rec.get_num_channels(),
            "bad_channels_detected": len(bad_local),
            "bad_reasons": reasons_serialisable,
            # bad_channel_ids (added): channel-ID-keyed list, unlike bad_reasons above
            # which is keyed by LOCAL index into shank_rec as it existed in THIS
            # process at report time. A local index is not safe for a downstream
            # consumer running in a separate process (e.g. ap_sorter.py) to reuse
            # against its own recording object - same instability
            # artifact_cleaning.py's NEW CONTRACT note already identified and
            # fixed for saturation_windows.json; bad_reasons had the same latent
            # bug and was simply never consumed cross-process until now.
            # bad_reasons is left in place (still useful for the human-readable
            # report / debugging), bad_channel_ids is the one downstream code
            # should actually key exclusion off of.
            "bad_channel_ids": [],
            "saturated_hopeless_channels": [],
            "viable_channels_remaining": 0,
            "status": "UNKNOWN",
            "skip_reason": None,
            "saturation_windows_flagged": 0,
            "periodic_discharge": {},
        }

        text_lines.append(f"\nSHANK {shank_id}")
        text_lines.extend(bad_report)

        # Build clean recording after bad-channel exclusion
        chan_ids = shank_rec.get_channel_ids()
        shank_info["bad_channel_ids"] = [str(c) for i, c in enumerate(chan_ids) if i in bad_local]
        clean_ids = [c for i, c in enumerate(chan_ids) if i not in bad_local]
        clean_rec = shank_rec.select_channels(clean_ids)

        # -- Coarse saturation scan ------------------------------------------
        if sat_cfg.get("enabled", True):
            print(f"    Coarse saturation scan ({clean_rec.get_num_channels()} channels)...")
            severity = scan_saturation_fraction_per_channel(clean_rec, fs, cfg_for_check)

            hopeless_local_idx = [ch for ch, frac in severity.items()
                                   if frac > hopeless_thresh]
            if hopeless_local_idx:
                clean_chan_ids = clean_rec.get_channel_ids()
                hopeless_ids = [clean_chan_ids[i] for i in hopeless_local_idx]
                shank_info["saturated_hopeless_channels"] = list(hopeless_ids)
                text_lines.append(
                    f"  Excluded (hopeless saturation >{hopeless_thresh:.0%}): {list(hopeless_ids)}")
                clean_ids = [c for c in clean_chan_ids if c not in hopeless_ids]
                clean_rec = clean_rec.select_channels(clean_ids)

        shank_info["viable_channels_remaining"] = clean_rec.get_num_channels()
        text_lines.append(f"  Viable channels remaining: {shank_info['viable_channels_remaining']}")

        # -- Shank viability decision ----------------------------------------
        if shank_info["viable_channels_remaining"] < min_chans:
            shank_info["status"] = "SKIPPED"
            shank_info["skip_reason"] = (
                f"Only {shank_info['viable_channels_remaining']} viable channel(s) "
                f"< min_channels_to_sort_shank ({min_chans})")
            text_lines.append(f"  STATUS: SKIPPED — {shank_info['skip_reason']}")
            print(f"    --> SKIP: {shank_info['skip_reason']}")
        else:
            shank_info["status"] = "PASS"
            text_lines.append("  STATUS: PASS")

            # -- Precise saturation scan (only on viable shanks) -------------
            if sat_cfg.get("enabled", True):
                n_viable = shank_info["viable_channels_remaining"]
                print(f"    Precise saturation scan ({n_viable} channels)...")
                viable_local_idx = list(range(n_viable))

                # Build coarse chunk flags for these channels so the precise
                # scan can skip clean chunks entirely (the key bottleneck fix).
                coarse_flagged = _build_coarse_flagged_chunks(
                    clean_rec, fs, cfg_for_check, viable_local_idx)
                per_channel_sat = detect_saturation_windows_per_channel(
                    clean_rec, fs, cfg_for_check,
                    channels_to_scan=viable_local_idx,
                    coarse_flagged_chunks=coarse_flagged)

                n_windows = sum(len(w) for w in per_channel_sat.values())
                shank_info["saturation_windows_flagged"] = n_windows
                text_lines.append(f"  Precise saturation windows flagged: {n_windows}")

                if per_channel_sat:
                    # per_channel_sat is keyed by LOCAL index into clean_rec as it
                    # exists right now, in this process - not safe to write to disk
                    # as-is (same instability already called out for bad_reasons /
                    # bad_channel_ids above). Convert to the actual channel_id
                    # before it ever leaves this function's scope.
                    clean_chan_ids_now = clean_rec.get_channel_ids()
                    saturation_windows_data["shanks"][str(shank_id)] = {
                        str(clean_chan_ids_now[local_idx]): [[int(s), int(e)] for s, e in windows]
                        for local_idx, windows in per_channel_sat.items()
                    }

            # -- Spectral discharge check (optional) -------------------------
            if run_spectral_check:
                print(f"    Spectral discharge check...")
                discharge_hits = detect_periodic_discharges(clean_rec, fs, cfg_for_check)
                shank_info["periodic_discharge"] = {
                    str(cid): v for cid, v in discharge_hits.items()
                }
                if discharge_hits:
                    text_lines.append(
                        f"  Periodic discharge detected on {len(discharge_hits)} channel(s):")
                    for cid, info in discharge_hits.items():
                        text_lines.append(
                            f"    ch {cid}: peak {info['peak_freq_hz']:.0f} Hz, "
                            f"SNR {info['peak_snr']:.1f}× local floor")
                    text_lines.append(
                        "  NOTE: diagnostic only — no filtering applied. "
                        "Confirm spatial pattern (per-channel vs. phase-locked across shanks) "
                        "before deciding on remediation. See ARCHITECTURE.md §7.")
                else:
                    text_lines.append("  Periodic discharge: none detected above threshold.")

        report_data["shanks"][shank_id] = shank_info

    # ---- write outputs ------------------------------------------------------
    os.makedirs(day_output_dir, exist_ok=True)
    json_path = os.path.join(day_output_dir, "health_report.json")
    txt_path = os.path.join(day_output_dir, "health_report.txt")
    sat_windows_path = os.path.join(day_output_dir, "saturation_windows.json")

    with open(json_path, "w") as f:
        json.dump(report_data, f, indent=2)

    with open(txt_path, "w") as f:
        f.write("\n".join(text_lines) + "\n")

    # Written UNCONDITIONALLY (possibly with an empty "shanks": {}), same as
    # health_report.json - so "file missing" always means "never
    # health-checked", never "nothing was flagged". See ARCHITECTURE.md
    # Sec.5 saturation_windows.json contract and artifact_cleaning.py's
    # load_saturation_windows(), which relies on exactly this guarantee.
    with open(sat_windows_path, "w") as f:
        json.dump(saturation_windows_data, f, indent=2)

    print(f"\n  Reports written to:\n    {json_path}\n    {txt_path}\n    {sat_windows_path}")
    return True


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Health checks for ephys_pipeline. Two modes: "
                    "--preflight (environment) or --report (per-day signal quality).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Pre-flight before submitting a Fox Slurm job:
  python health_check.py --env fox --preflight

  # Pre-flight including session-ordering timestamp check (slower):
  python health_check.py --env fox --preflight --check-ordering

  # Per-day report on a specific animal/date:
  python health_check.py --env local --report --animal 213868 --date 20231105

  # Report for ONE session only (e.g. probe/drive moved between sessions
  # that day - never concatenate across a depth change):
  python health_check.py --env local --report --animal 213868 --date 20231105 --session-name 20231105_002

  # Per-day report with optional spectral discharge check:
  python health_check.py --env local --report --animal 213868 --date 20231105 --spectral-check

  # Per-day report including the staging copy (tests that path too):
  python health_check.py --env biotin --report --animal 213868 --date 20231105 --with-staging
""")

    parser.add_argument("--env", required=True, choices=["local", "fox", "biotin"],
                         help="Environment to load config for.")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true",
                       help="Run environment-level pre-flight checks (no raw data read).")
    mode.add_argument("--report", action="store_true",
                       help="Run per-day signal quality report (reads raw data).")

    parser.add_argument("--animal", default=None,
                         help="Animal ID (required for --report; optional filter for --preflight).")
    parser.add_argument("--date", default=None,
                         help="Date string YYYYMMDD (required for --report).")
    parser.add_argument("--session-name", default=None,
                         help="(--report) Restrict to a single session (e.g. '20231105_002') "
                              "instead of every session found for --animal/--date - forces "
                              "individual-session mode (no concatenation). Use for a day where "
                              "the probe/drive was moved between sessions.")
    parser.add_argument("--with-staging", action="store_true",
                         help="(--report) Test stage_raw_locally copy rather than bypassing it.")
    parser.add_argument("--spectral-check", action="store_true",
                         help="(--report) Run optional periodic-discharge spectral diagnostic.")
    parser.add_argument("--check-ordering", action="store_true",
                         help="(--preflight) Also cross-check NNN folder order against "
                              "OpenEphys timestamps (reads recording metadata, slower).")

    args = parser.parse_args()
    cfg = load_config(args.env)

    if args.preflight:
        results = run_preflight(cfg, animal_filter=args.animal,
                                check_ordering=args.check_ordering)
        raise SystemExit(1 if any(l == "FAIL" for l, _ in results) else 0)

    elif args.report:
        if not args.animal or not args.date:
            parser.error("--report requires --animal and --date.")
        success = generate_health_report(
            cfg, args.animal, args.date,
            skip_staging=not args.with_staging,
            run_spectral_check=args.spectral_check,
            session_name=args.session_name,
        )
        raise SystemExit(0 if success else 1)
