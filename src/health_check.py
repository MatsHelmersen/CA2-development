"""
health_check.py - Automated quality control and shank health assessment.

Scope (per ARCHITECTURE.md): Bad channel detection (dead, shorted, noisy), 
saturation scanning, and shank-skip decision logic based on remaining 
viable channels. 

This module does NOT run Kilosort4 or perform unit assessment. It depends 
exclusively on io_utils.py for data loading and probe binding.
"""

import os
import sys
import json
import traceback
from pathlib import Path
from datetime import datetime
import numpy as np
from scipy.signal import butter, filtfilt

import spikeinterface.full as si

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# Repo root, assumes this file lives at <repo_root>/src/health_check.py (per ARCHITECTURE.md Sec.3). 
# If that's not where io_utils.py ends up, this needs revisiting.
from src.config_loader import load_config
from src.io_utils import find_sessions, prepare_day, load_probe


# ============================================================================
# BAD CHANNEL DETECTION
# ============================================================================

def detect_bad_channels(raw_shank, shank_chs, fs, cfg, subsample_s=None):
    """
    Detect dead and shorted channels on a single shank.
    """
    bc_cfg = cfg.get("bad_channel_detection", {})       # Dictionary mirroring the config.yaml section for bad channel detection    

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
    Pull short windows spread across the full recording rather than loading everything.
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
    Run detect_bad_channels per shank on a split recording, plus IBL standard check.
    """
    results = {}
    bc_cfg = cfg.get("bad_channel_detection", {})
    subsample_s = bc_cfg.get("subsample_s", 30.0)

    for shank_id, rec in recording_split.items():
        traces = _sample_windows(rec, fs, total_s=subsample_s)
        chan_ids = rec.get_channel_ids()
        bad, reasons, report = detect_bad_channels(traces, chan_ids, fs, cfg, subsample_s=subsample_s)

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
                            report.append(f"    ch {cid:>4} (local {i:2d}): NOISY (IBL std-outlier check)")
                
                n_ch = len(chan_ids)
                report[0] = f"  Bad channels: {len(bad)}/{n_ch} ({len(bad) / max(n_ch, 1):.0%})"
            except Exception as e:
                report.append(f"    (IBL std-based check failed: {e})")

        results[shank_id] = (bad, reasons, report)
    return results


# ============================================================================
# SATURATION DETECTION
# ============================================================================

def scan_saturation_fraction_per_channel(rec, fs, cfg):
    """
    FAST first pass: per-channel fraction of the whole recording flagged as clipped.
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
    
    chunk_iter = tqdm(starts, desc="  Saturation severity scan", unit="chunk") if tqdm else starts
    for start in chunk_iter:
        end = min(start + chunk_frames, n_frames)
        chunk = rec.get_traces(start_frame=start, end_frame=end)
        clipped_frac = np.mean((chunk >= clip_hi) | (chunk <= clip_lo), axis=0) 
        flagged_chunk_counts += (clipped_frac > 0.01)

    return {ch: float(flagged_chunk_counts[ch] / len(starts)) for ch in range(n_chans)}


def detect_saturation_windows_per_channel(rec, fs, cfg, channels_to_scan=None):
    """
    Precise chunked scan for exact (start_sample, end_sample) saturation windows.
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
    
    chunk_iter = tqdm(starts, desc="  Saturation precise scan", unit="chunk") if tqdm else starts
    dense_chunk_frac_thresh = 0.30
    
    for start in chunk_iter:
        end = min(start + chunk_frames, n_frames)
        chunk = rec.get_traces(start_frame=start, end_frame=end, channel_ids=None)
        chunk = chunk[:, channels_to_scan]

        clipped = (chunk >= clip_hi) | (chunk <= clip_lo)
        diffs = np.abs(np.diff(chunk, axis=0))
        mad = np.median(np.abs(chunk - np.median(chunk))) + 1e-9
        jump_thresh = sat_cfg.get("derivative_mad_multiple", 20.0) * mad
        jumped = diffs > jump_thresh
        jumped = np.concatenate([np.zeros((1, chunk.shape[1]), dtype=bool), jumped], axis=0)
        flagged = clipped | jumped  

        for local_out_idx, ch in enumerate(channels_to_scan):
            col = flagged[:, local_out_idx]
            frac = col.mean()
            if frac == 0:
                continue
            if frac > dense_chunk_frac_thresh:
                per_channel_flags[ch].append((int(start), int(end - 1)))
                continue
            flagged_idx = np.where(col)[0]
            gaps = np.where(np.diff(flagged_idx) > 1)[0]
            seg_starts = np.concatenate([[0], gaps + 1])
            seg_ends = np.concatenate([gaps, [len(flagged_idx) - 1]])
            for s, e in zip(seg_starts, seg_ends):
                per_channel_flags[ch].append((int(start + flagged_idx[s]), int(start + flagged_idx[e])))

    return {ch: wins for ch, wins in per_channel_flags.items() if wins}


# ============================================================================
# STANDALONE REPORT GENERATOR
# ============================================================================

def generate_health_report(cfg, animal_id, date_str, skip_staging=True):
    """
    Loads raw data, executes the health check pipeline, and produces a summary 
    report determining which shanks are viable for sorting.
    """
    print(f"\n{'='*70}\nRunning Health Check: {animal_id} / {date_str}\n{'='*70}")
    
    grouped = find_sessions(cfg, animal_filter=animal_id)
    key = (animal_id, date_str)
    if key not in grouped:
        print(f"  [FAIL] no sessions found for {animal_id}/{date_str}.")
        return False
        
    session_paths = grouped[key]
    
    cfg_for_check = cfg.copy()
    if skip_staging and "paths" in cfg_for_check and "stage_raw_locally" in cfg_for_check["paths"]:
        cfg_for_check["paths"] = cfg_for_check["paths"].copy()
        cfg_for_check["paths"]["stage_raw_locally"] = None
        
    try:
        probe = load_probe(cfg_for_check)
        result = prepare_day(cfg_for_check, animal_id, date_str, session_paths, probe)
    except Exception as e:
        print(f"  [FAIL] Failed during data preparation: {e}")
        traceback.print_exc()
        return False

    rec = result["recording"]
    fs = result["fs"]
    day_output_dir = result["day_output_dir"]
    recording_split = rec.split_by("group")
    
    report_data = {
        "animal_id": animal_id,
        "date_str": date_str,
        "generated_at": datetime.now().isoformat(),
        "shanks": {}
    }
    
    text_lines = [
        f"HEALTH REPORT: {animal_id} / {date_str}",
        f"Generated: {report_data['generated_at']}",
        "-" * 50
    ]
    sort_cfg = cfg_for_check.get("sorting", {})
    min_chans = sort_cfg.get("min_channels_to_sort_shank", 2)
    sat_cfg = cfg_for_check.get("saturation_detection", {})
    hopeless_thresh = sat_cfg.get("hopeless_fraction_thresh", 0.5)

    print("  Detecting bad channels...")
    bad_results = find_bad_channels_for_recording(recording_split, fs, cfg_for_check)

    for shank_id, shank_rec in recording_split.items():
        print(f"\n  Analyzing Shank {shank_id}...")
        bad_local, reasons, bad_report = bad_results[shank_id]
        
        shank_info = {
            "initial_channels": shank_rec.get_num_channels(),
            "bad_channels_detected": len(bad_local),
            "bad_reasons": reasons,
            "saturated_hopeless_channels": [],
            "viable_channels_remaining": 0,
            "status": "OK",
            "skip_reason": None,
            "saturation_windows_flagged": 0
        }
        
        text_lines.append(f"\nSHANK {shank_id}")
        text_lines.extend(bad_report)
        
        # Determine viable channels post-bad-channel-drop
        chan_ids = shank_rec.get_channel_ids()
        clean_ids = [c for i, c in enumerate(chan_ids) if i not in bad_local]
        clean_rec = shank_rec.select_channels(clean_ids)
        
        # Cheap saturation scan
        if sat_cfg.get("enabled", True):
            print(f"  Scanning shank {shank_id} for saturation severity...")
            severity = scan_saturation_fraction_per_channel(clean_rec, fs, cfg_for_check)
            
            hopeless_local_idx = [ch for ch, frac in severity.items() if frac > hopeless_thresh]
            if hopeless_local_idx:
                clean_chan_ids = clean_rec.get_channel_ids()
                hopeless_ids = [clean_chan_ids[i] for i in hopeless_local_idx]
                
                shank_info["saturated_hopeless_channels"] = hopeless_ids
                text_lines.append(f"  Excluded due to hopeless saturation (>{hopeless_thresh:.0%}): {hopeless_ids}")
                
                # Further refine viable channels
                clean_ids = [c for c in clean_chan_ids if c not in hopeless_ids]
                clean_rec = clean_rec.select_channels(clean_ids)
                
        shank_info["viable_channels_remaining"] = clean_rec.get_num_channels()
        text_lines.append(f"  Viable channels remaining: {shank_info['viable_channels_remaining']}")
        
        # Shank skip decision logic
        if shank_info["viable_channels_remaining"] < min_chans:
            shank_info["status"] = "SKIPPED"
            shank_info["skip_reason"] = f"Remaining channels ({shank_info['viable_channels_remaining']}) < min_channels_to_sort_shank ({min_chans})"
            text_lines.append(f"  STATUS: {shank_info['status']} - {shank_info['skip_reason']}")
            print(f"  --> SKIPPING Shank {shank_id}: {shank_info['skip_reason']}")
        else:
            shank_info["status"] = "PASS"
            text_lines.append(f"  STATUS: {shank_info['status']}")
            
            # Run precise saturation scan ONLY if the shank passed
            if sat_cfg.get("enabled", True):
                print(f"  Running precise saturation scan on remaining {shank_info['viable_channels_remaining']} channel(s)...")
                per_channel_sat = detect_saturation_windows_per_channel(clean_rec, fs, cfg_for_check)
                n_windows = sum(len(w) for w in per_channel_sat.values())
                shank_info["saturation_windows_flagged"] = n_windows
                text_lines.append(f"  Precise saturation windows flagged: {n_windows}")
        
        report_data["shanks"][shank_id] = shank_info

    # Save artifacts
    json_path = os.path.join(day_output_dir, "health_report.json")
    txt_path = os.path.join(day_output_dir, "health_report.txt")
    
    with open(json_path, "w") as f:
        json.dump(report_data, f, indent=2)
        
    with open(txt_path, "w") as f:
        f.write("\n".join(text_lines) + "\n")
        
    print(f"\n  [PASS] Reports written to {day_output_dir}")
    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Standalone Health Check for Ephys Pipeline.")
    parser.add_argument("--env", required=True, choices=["local", "fox", "biotin"])
    parser.add_argument("--animal", required=True, help="Animal ID to check")
    parser.add_argument("--date", required=True, help="Date string (YYYYMMDD)")
    parser.add_argument("--with-staging", action="store_true", help="Do not skip local staging.")
    
    args = parser.parse_args()
    cfg = load_config(args.env)
    
    success = generate_health_report(cfg, args.animal, args.date, skip_staging=not args.with_staging)
    if not success:
        raise SystemExit(1)