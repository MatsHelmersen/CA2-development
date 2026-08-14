"""
io_utils.py - raw-data loading, probe binding, path resolution, and
session metadata for ephys_pipeline.

Scope (per ARCHITECTURE.md Sec.3): loading OpenEphys recordings, attaching
the probe, resolving output paths, and writing session_boundaries.json.

Explicitly OUT of scope (belongs elsewhere):
  - Kilosort4 / sorting                    -> ap_sorter.py
  - bad-channel detection, saturation scan -> artifact_cleaning.py / quality_control.py
  - unit assessment / classification       -> quality_control.py
  - existing-output (shank_*_ks4) checks   -> ap_sorter.py (sorter-specific)

Config is read via config_loader.load_config(env) - see that module and
ARCHITECTURE.md Sec.4 for the base.yaml / <env>.yaml merge contract. No
CONFIG dict is hardcoded here.

RESOLVED: config_loader.CONFIG_DIR was `Path(__file__).resolve().parent /
"config"`, which mismatched the src/config_loader.py + ephys_pipeline/config/
layout in ARCHITECTURE.md Sec.3. Fixed to `.parent.parent / "config"` by
the user directly in config_loader.py (2024 session) - no longer an issue.

CORRECTED (previously mis-flagged as a bug): I had assumed aux_channel_ids
values were 0-indexed positional integers (per base.yaml's comment) being
compared against string channel labels, and flagged that as a type
mismatch. User verified against real recordings: the values are actually
matched directly and correctly against recording.get_channel_ids() when
listed 1-indexed (i.e. true channel + 1), matching how OpenEphys reports
channel labels. The removal logic itself (direct `in` comparison) was
never wrong - only base.yaml's comment ("0-indexed, raw recording channel
order") is inaccurate and should be corrected to describe 1-indexed
OpenEphys channel labels. Not changed here (base.yaml is outside this
module) - flagging for the user to update the comment.
"""

import os
import glob
import json
import shutil
from pathlib import Path

import numpy as np

import spikeinterface.full as si
import probeinterface as pi

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

from config_loader import load_config  # sibling module in src/ - see KNOWN ISSUE above


# Repo root, used to resolve base.yaml's `probe.json_relative_path` when no
# environment-specific probe_json_override is set. Assumes this file lives
# at <repo_root>/src/io_utils.py (per ARCHITECTURE.md Sec.3). If that's not
# where io_utils.py ends up, this needs revisiting alongside issue #1 above.
REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================================
# PROBE
# ============================================================================

def resolve_probe_json_path(cfg: dict) -> Path:
    """
    paths.probe_json_override (env yaml) wins if set - must be an absolute
    path per the fox.yaml/local.yaml/biotin.yaml convention. Otherwise fall
    back to base.yaml's probe.json_relative_path, resolved relative to
    REPO_ROOT (per base.yaml's own comment: "relative to the repo").
    """
    override = cfg.get("paths", {}).get("probe_json_override")
    if override:
        return Path(override)
    rel = cfg.get("probe", {}).get("json_relative_path")
    if not rel:
        raise KeyError(
            "Config has neither paths.probe_json_override nor probe.json_relative_path set."
        )
    return REPO_ROOT / rel


def load_probe(cfg: dict):
    """
    Load the probe geometry via probeinterface and assign default device
    channel indices if the JSON didn't specify them.

    NOTE: base.yaml's comment states this must point at the CORRECTED
    sitemap (previous version had a ~9x vertical-scale error, true min
    contact pitch ~24-30 um - see ARCHITECTURE.md Sec.1). This function
    does not itself verify correctness of the geometry, only that a file
    exists and loads.
    """
    probe_json = resolve_probe_json_path(cfg)
    if not probe_json.exists():
        raise FileNotFoundError(
            f"Probe JSON not found at {probe_json}. Confirm this points at the "
            f"corrected ASSY-350-H20 sitemap (see ARCHITECTURE.md Sec.1)."
        )
    probe = pi.read_probeinterface(probe_json).probes[0]
    if probe.device_channel_indices is None:
        probe.set_device_channel_indices(np.arange(probe.get_contact_count()))
    return probe


def bind_probe(recording, probe, animal_id: str, cfg: dict):
    """
    Remove EMG/ECoG aux channels (animal-specific, config-driven) BEFORE
    attaching the probe - set_probe requires (or silently mis-maps,
    depending on SI version) the channel count to match the probe's
    contact count. This is a channel-IDENTITY fix (wrong number/kind of
    channels), not a bad-channel EXCLUSION - bad-channel detection stays
    out of this module (see quality_control.py / artifact_cleaning.py).

    Returns (recording_with_probe, aux_ids_removed).
    """
    aux_ids = cfg.get("aux_channel_ids", {}).get(str(animal_id), [])
    if aux_ids:
        all_ids = recording.get_channel_ids()
        keep_ids = [c for c in all_ids if c not in aux_ids]
        recording = recording.select_channels(keep_ids)

    n_chans = recording.get_num_channels()
    if n_chans != probe.get_contact_count():
        raise ValueError(
            f"Channel count mismatch after EMG/ECoG removal: recording has {n_chans} "
            f"channels, probe expects {probe.get_contact_count()}. Check "
            f"cfg['aux_channel_ids'] for animal {animal_id}."
        )

    return recording.set_probe(probe, group_mode="by_shank"), aux_ids


# ============================================================================
# PATHS / SESSION DISCOVERY
# ============================================================================

def get_day_output_dir(cfg: dict, animal_id: str, date_str: str, session_name: str = None) -> str:
    """
    Output dir for a given animal/day (or single session, in individual-
    session mode). cfg['paths']['output_base_path'] decouples output from
    raw-data location (ARCHITECTURE.md Sec.3, stated as a deliberate
    requirement); falls back to base_path if unset (write alongside raw
    data - previous default behaviour).
    """
    root = cfg["paths"].get("output_base_path") or cfg["paths"]["base_path"]
    if session_name is not None:
        return os.path.join(root, animal_id, "Raw_data", date_str, session_name)
    return os.path.join(root, animal_id, "Raw_data", date_str)


def find_sessions(cfg: dict, animal_filter: str = None) -> dict:
    """
    Walk <base_path>/<AnimalID>/Raw_data/<YYYYMMDD>/<YYYYMMDD_NNN> and group
    session folders by (AnimalID, YYYYMMDD) so same-day sessions can be
    concatenated. Returns dict: (animal_id, date_str) -> sorted list of paths.

    NOTE (unchanged from sort_batch.py, not re-derived): ordering relies
    entirely on the zero-padded NNN suffix sorting correctly. This does
    NOT cross-check OpenEphys's own recorded start timestamps - if a
    session was ever renumbered manually, or NNN doesn't reflect true
    chronological order, concatenation order will be silently wrong. This
    was an acknowledged gap in the original script and is still open
    (ARCHITECTURE.md Sec.7 doesn't list it explicitly - worth adding).
    """
    base_path = cfg["paths"]["base_path"]
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
        grouped[key] = sorted(grouped[key])  # NNN order -> chronological, per caveat above

    return grouped


def stage_sessions_locally(cfg: dict, animal_id: str, date_str: str, session_paths: list) -> list:
    """
    Copy raw OpenEphys session folders to local scratch
    (cfg['paths']['stage_raw_locally']) before reading, when base_path is a
    network/UNC mount. A single sequential copy is usually far more
    efficient over SMB/CIFS than the scattered reads si.read_openephys
    performs for every downstream step.

    Returns the paths to actually read from: staged paths if staging is
    configured, otherwise session_paths unchanged.
    """
    stage_root = cfg["paths"].get("stage_raw_locally")
    if not stage_root:
        return list(session_paths)

    staged_root = os.path.join(stage_root, animal_id, date_str)
    os.makedirs(staged_root, exist_ok=True)
    work_paths = []
    stage_iter = tqdm(session_paths, desc="  Staging to scratch", unit="session") if tqdm else session_paths
    for p in stage_iter:
        dest = os.path.join(staged_root, os.path.basename(p))
        if not os.path.isdir(dest):
            shutil.copytree(p, dest)
        work_paths.append(dest)
    return work_paths


# ============================================================================
# LOADING + CONCATENATION
# ============================================================================

def load_day_recording(cfg: dict, session_paths: list, concatenate: bool = True):
    """
    Read each session with si.read_openephys and, if concatenate=True and
    there is more than one session, concatenate in path order (chronological
    per the NNN-sort caveat in find_sessions()). Does NOT stage, attach the
    probe, or remove aux channels - call stage_sessions_locally() first if
    needed, and bind_probe() after.

    Returns (recording, session_frame_counts: list[int], fs: float).

    Raises RuntimeError if concatenate=False and len(session_paths) > 1,
    matching sort_batch.py's original behaviour: individual-session mode
    (e.g. probe/DV moved between sessions - never concatenate across a
    depth change, per ARCHITECTURE.md Sec.6) must be handled by looping
    this function once per session at the caller level, not by silently
    picking one session here.
    """
    stream_name = cfg["recording"]["stream_name"]
    block_index = cfg["recording"]["block_index"]

    recordings = []
    session_frame_counts = []
    for p in session_paths:
        rec = si.read_openephys(p, stream_name=stream_name, block_index=block_index)
        recordings.append(rec)
        session_frame_counts.append(rec.get_num_frames())

    if concatenate and len(recordings) > 1:
        recording = si.concatenate_recordings(recordings)
    elif len(recordings) > 1:
        raise RuntimeError(
            "concatenate=False with multiple sessions must be handled by looping "
            "load_day_recording per-session at the caller level, not inside this function."
        )
    else:
        recording = recordings[0]

    fs = recording.get_sampling_frequency()
    return recording, session_frame_counts, fs


def _read_ttl_info(session_path: str, cfg: dict):
    """
    Read TTL onset/offset/count from the event stream matching
    cfg['recording']['stream_name'], for one raw session folder.

    CAVEAT (unchanged - ARCHITECTURE.md Sec.5): OpenEphys event timestamps
    are not guaranteed zero-referenced to the continuous recording's
    sample 0 (SpikeInterface GH #3300, still open as of my training data -
    verify against current SI docs if this matters for your alignment).
    Sanity-check the first TTL onset against your camera trigger script's
    expected delay before trusting downstream video/spike alignment.
    """
    block_index = cfg["recording"]["block_index"]
    stream_name = cfg["recording"]["stream_name"]
    try:
        events = si.read_openephys_event(session_path, block_index=block_index)
        event_channels = list(events.channel_ids)
        # Match against the acquisition board stream specifically (per your
        # directory structure: .../events/Acquisition_Board-100.
        # acquisition_board/TTL) rather than blindly taking the first event
        # channel - other event streams may exist (other Record Nodes,
        # other devices) and index 0 is not guaranteed to be the TTL line.
        match_key = stream_name.split("#")[-1]
        matched = [c for c in event_channels if match_key in str(c)]
        if not matched:
            # OpenEphys names event-stream channels independently of the
            # continuous stream (e.g. "Acquisition Board TTL Input" vs the
            # continuous stream's "Acquisition_Board-100.acquisition_board")
            # - match_key above will never hit for event channels named this
            # way. Fall back to matching "ttl" in the channel name before
            # giving up and taking the first channel.
            matched = [c for c in event_channels if "ttl" in str(c).lower()]
        if not matched and event_channels:
            print(f"    Warning: no event channel matched '{match_key}' or contained 'TTL' for "
                  f"{os.path.basename(session_path)}; found {event_channels}. "
                  f"Falling back to the first one - verify this is really the TTL line.")
            matched = [event_channels[0]]
        if matched:
            ev = events.get_events(channel_id=matched[0], segment_index=0)
            if len(ev) > 0:
                return {
                    "channel_id": str(matched[0]),
                    "first_onset_s": float(ev[0]["time"]),
                    "last_offset_s": float(ev[-1]["time"] + ev[-1]["duration"]),
                    "n_events": int(len(ev)),
                }
    except Exception as ev_err:
        print(f"    Warning: could not read TTL events for {os.path.basename(session_path)}: {ev_err}")
    return None


def build_session_metadata(cfg: dict, session_paths: list, session_frame_counts: list, fs: float) -> list:
    """
    Build the per-session metadata list matching the session_boundaries.json
    "sessions" contract (ARCHITECTURE.md Sec.5): frame offsets into the
    concatenated recording, durations, and TTL info per session.
    """
    session_metadata = []
    cumulative_offset = 0
    for p, n_frames in zip(session_paths, session_frame_counts):
        session_metadata.append({
            "session_path": p,
            "frame_offset_in_concatenated": cumulative_offset,
            "n_frames": n_frames,
            "duration_s": n_frames / fs,
            "ttl": _read_ttl_info(p, cfg),
        })
        cumulative_offset += n_frames
    return session_metadata


def write_session_boundaries_json(day_output_dir: str, fs: float, session_metadata: list) -> str:
    """Write session_boundaries.json per the ARCHITECTURE.md Sec.5 contract."""
    os.makedirs(day_output_dir, exist_ok=True)
    out_path = os.path.join(day_output_dir, "session_boundaries.json")
    with open(out_path, "w") as f:
        json.dump({"sampling_frequency": fs, "sessions": session_metadata}, f, indent=2)
    return out_path


# ============================================================================
# CONVENIENCE WRAPPER
# ============================================================================

def prepare_day(cfg: dict, animal_id: str, date_str: str, session_paths: list, probe,
                 concatenate: bool = True) -> dict:
    """
    Everything this module owns, in one call - mirrors what process_day()
    used to do in sort_batch.py up to (but not including) bad-channel
    detection / saturation scanning / Kilosort4 / phy export, which now
    belong to artifact_cleaning.py, ap_sorter.py, and quality_control.py.

    Steps: stage-to-scratch (if configured) -> read + concatenate OpenEphys
    sessions -> build session metadata (incl. TTL) -> write
    session_boundaries.json -> remove aux channels -> attach probe.

    Returns dict:
        recording               : probe-attached, aux-channels-removed
                                   recording, NOT yet split by shank -
                                   that's the caller's job, e.g.
                                   recording.split_by("group")
        session_metadata         : list matching session_boundaries.json's
                                    "sessions" contract
        fs                        : sampling frequency
        aux_ids                   : aux channel ids removed for this animal
        day_output_dir            : resolved output dir
        session_boundaries_path   : path written
    """
    individual_session_mode = (not concatenate) and len(session_paths) == 1
    day_output_dir = get_day_output_dir(
        cfg, animal_id, date_str,
        session_name=os.path.basename(session_paths[0]) if individual_session_mode else None,
    )

    work_paths = stage_sessions_locally(cfg, animal_id, date_str, session_paths)

    recording, session_frame_counts, fs = load_day_recording(cfg, work_paths, concatenate=concatenate)

    # session_metadata records the ORIGINAL (unstaged) paths - that's what
    # identifies the session to the rest of the pipeline and to you; the
    # staged copy is an implementation detail of this particular run.
    session_metadata = build_session_metadata(cfg, session_paths, session_frame_counts, fs)
    boundaries_path = write_session_boundaries_json(day_output_dir, fs, session_metadata)

    recording, aux_ids = bind_probe(recording, probe, animal_id, cfg)

    return {
        "recording": recording,
        "session_metadata": session_metadata,
        "fs": fs,
        "aux_ids": aux_ids,
        "day_output_dir": day_output_dir,
        "session_boundaries_path": boundaries_path,
    }


# ============================================================================
# SELF-CHECK (control/verification for this module only)
# ============================================================================
#
# Deliberately narrow in scope: this checks that io_utils.py's own
# responsibilities (config, probe, paths, session discovery) are correctly
# wired, WITHOUT reading any raw traces, running Kilosort4, or doing
# anything expensive. A proper end-to-end health check (readable data,
# probe geometry sanity, disk space, GPU visibility, etc.) belongs in
# health_check.py - see the suggested prompt for that module below.

def self_check(cfg: dict, animal_filter: str = None) -> list:
    """
    Run a series of cheap, read-only checks against the given config and
    report PASS/WARN/FAIL for each. Returns a list of
    (level: "PASS"|"WARN"|"FAIL", message: str) tuples; does not raise on
    its own (a FAIL is reported, not thrown) so you get the full picture
    in one run rather than stopping at the first problem.

    Checks performed:
      1. base_path exists and is readable
      2. output_base_path (if set) exists or is creatable
      3. stage_raw_locally (if set) exists or is creatable
      4. probe JSON resolves and loads, contact count matches expectation (128)
      5. find_sessions() finds at least one animal/day group
      6. aux_channel_ids keys match discovered animal IDs (catches typos'd
         animal IDs that silently result in "no aux channels configured")
    """
    results = []

    def check(level, msg):
        results.append((level, msg))
        print(f"  [{level}] {msg}")

    print(f"\n{'='*70}\nio_utils.py self-check (env={cfg.get('_env', '?')})\n{'='*70}")

    # 1. base_path
    base_path = cfg.get("paths", {}).get("base_path")
    if base_path and os.path.isdir(base_path):
        check("PASS", f"base_path exists: {base_path}")
    else:
        check("FAIL", f"base_path missing or unreadable: {base_path}")

    # 2. output_base_path
    output_base = cfg.get("paths", {}).get("output_base_path")
    if output_base:
        if os.path.isdir(output_base):
            check("PASS", f"output_base_path exists: {output_base}")
        else:
            try:
                os.makedirs(output_base, exist_ok=True)
                check("WARN", f"output_base_path did not exist, created it: {output_base}")
            except Exception as e:
                check("FAIL", f"output_base_path missing and not creatable: {output_base} ({e})")
    else:
        check("PASS", "output_base_path unset - output will be written alongside raw data")

    # 3. stage_raw_locally
    stage_root = cfg.get("paths", {}).get("stage_raw_locally")
    if stage_root:
        if os.path.isdir(stage_root):
            check("PASS", f"stage_raw_locally exists: {stage_root}")
        else:
            try:
                os.makedirs(stage_root, exist_ok=True)
                check("WARN", f"stage_raw_locally did not exist, created it: {stage_root}")
            except Exception as e:
                check("FAIL", f"stage_raw_locally set but not creatable: {stage_root} ({e})")
    else:
        check("PASS", "stage_raw_locally unset - reading directly from base_path")

    # 4. probe
    try:
        probe = load_probe(cfg)
        n_contacts = probe.get_contact_count()
        n_shanks = probe.get_shank_count()
        if n_contacts == 128:
            check("PASS", f"probe loaded: {n_contacts} contacts, {n_shanks} shanks")
        else:
            check("WARN", f"probe loaded but contact count is {n_contacts}, expected 128 for ASSY-350-H20")
    except Exception as e:
        check("FAIL", f"probe failed to load: {e}")
        probe = None

    # 5. session discovery
    try:
        grouped = find_sessions(cfg, animal_filter=animal_filter)
        if grouped:
            n_sessions = sum(len(v) for v in grouped.values())
            check("PASS", f"found {len(grouped)} animal/day group(s), {n_sessions} session(s) total")
            discovered_animals = {a for (a, d) in grouped.keys()}
        else:
            check("FAIL", "no animal/day groups found - check base_path and directory naming "
                          "(<base_path>/<AnimalID>/Raw_data/<YYYYMMDD>/<YYYYMMDD>_<NNN>)")
            discovered_animals = set()
    except Exception as e:
        check("FAIL", f"find_sessions() raised: {e}")
        discovered_animals = set()

    # 6. aux_channel_ids sanity - configured animals vs. discovered animals
    aux_cfg = cfg.get("aux_channel_ids", {})
    configured_animals = set(aux_cfg.keys())
    if discovered_animals:
        unconfigured = discovered_animals - configured_animals
        if unconfigured:
            check("WARN", f"animal(s) with no aux_channel_ids entry (fine if they genuinely have "
                          f"no EMG/ECoG channels, but verify): {sorted(unconfigured)}")
        else:
            check("PASS", "every discovered animal has an aux_channel_ids entry")
        stale = configured_animals - discovered_animals
        if stale:
            check("WARN", f"aux_channel_ids has entries for animal(s) not found on disk "
                          f"(typo, or animal simply not run yet): {sorted(stale)}")

    n_fail = sum(1 for level, _ in results if level == "FAIL")
    n_warn = sum(1 for level, _ in results if level == "WARN")
    print(f"\n{n_fail} FAIL, {n_warn} WARN, {sum(1 for l,_ in results if l=='PASS')} PASS\n")
    return results


def check_day(cfg: dict, animal_id: str, date_str: str, skip_staging: bool = True) -> bool:
    """
    Heavier, opt-in check: actually runs prepare_day() on ONE animal/day and
    reports what came back, without doing anything downstream (no
    bad-channel detection, no KS4). This is the thing to run after touching
    io_utils.py itself, or after a new animal/recording day shows up, before
    trusting it in a full batch run.

    skip_staging=True (default) bypasses stage_raw_locally even if configured,
    so a spot-check doesn't wait on a full network copy - pass False to
    verify staging itself.

    Reports: session count, per-session frame count and duration, TTL
    presence per session, final channel/shank count post aux-removal, and
    whether session_boundaries.json round-trips through json.load(). Returns
    True if no problems found, False otherwise (does not raise).
    """
    print(f"\n{'='*70}\ncheck_day: {animal_id} / {date_str} (env={cfg.get('_env', '?')})\n{'='*70}")
    ok = True

    grouped = find_sessions(cfg, animal_filter=animal_id)
    key = (animal_id, date_str)
    if key not in grouped:
        print(f"  [FAIL] no sessions found for {animal_id}/{date_str} - check the date string "
              f"and that find_sessions() sees this animal at all.")
        return False
    session_paths = grouped[key]
    print(f"  Found {len(session_paths)} session(s): {[os.path.basename(p) for p in session_paths]}")

    cfg_for_check = cfg
    if skip_staging and cfg.get("paths", {}).get("stage_raw_locally"):
        cfg_for_check = dict(cfg)
        cfg_for_check["paths"] = dict(cfg["paths"])
        cfg_for_check["paths"]["stage_raw_locally"] = None
        print("  (staging disabled for this check - pass skip_staging=False to test staging itself)")

    try:
        probe = load_probe(cfg_for_check)
    except Exception as e:
        print(f"  [FAIL] probe failed to load: {e}")
        return False

    try:
        result = prepare_day(cfg_for_check, animal_id, date_str, session_paths, probe)
    except Exception as e:
        print(f"  [FAIL] prepare_day() raised: {e}")
        return False

    rec = result["recording"]
    print(f"  [PASS] recording loaded: {rec.get_num_channels()} channels, "
          f"{rec.get_num_frames()} frames, {rec.get_num_frames() / result['fs']:.1f} s, "
          f"fs={result['fs']} Hz")

    expected_chans_after_aux = probe.get_contact_count()
    if rec.get_num_channels() != expected_chans_after_aux:
        print(f"  [WARN] channel count ({rec.get_num_channels()}) != probe contact count "
              f"({expected_chans_after_aux}) after probe binding - unexpected, investigate.")
        ok = False
    if result["aux_ids"]:
        print(f"  [PASS] removed {len(result['aux_ids'])} aux channel(s): {result['aux_ids']}")
    else:
        print(f"  [WARN] no aux_channel_ids configured for {animal_id} - confirm this animal "
              f"genuinely has no EMG/ECoG channels wired.")

    for sess in result["session_metadata"]:
        name = os.path.basename(sess["session_path"])
        ttl_str = f"{sess['ttl']['n_events']} TTL event(s)" if sess["ttl"] else "NO TTL EVENTS FOUND"
        flag = "PASS" if sess["ttl"] else "WARN"
        print(f"  [{flag}] {name}: {sess['duration_s']:.1f}s, {ttl_str}")
        if not sess["ttl"]:
            ok = False

    # round-trip session_boundaries.json
    try:
        with open(result["session_boundaries_path"]) as f:
            reloaded = json.load(f)
        assert reloaded["sampling_frequency"] == result["fs"]
        assert len(reloaded["sessions"]) == len(result["session_metadata"])
        print(f"  [PASS] session_boundaries.json written and round-trips: "
              f"{result['session_boundaries_path']}")
    except Exception as e:
        print(f"  [FAIL] session_boundaries.json did not round-trip: {e}")
        ok = False

    print(f"\n{'OK' if ok else 'PROBLEMS FOUND'} - see PASS/WARN/FAIL lines above.\n")
    return ok


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Debug/control tools for io_utils.py.")
    parser.add_argument("--env", required=True, choices=["local", "fox", "biotin"])
    parser.add_argument("--animal", default=None)
    parser.add_argument("--check", action="store_true",
                         help="Run self_check(): cheap, read-only validation of config/probe/paths/"
                              "session-discovery. Fast, no raw data is read.")
    parser.add_argument("--check-day", nargs=2, metavar=("ANIMAL_ID", "DATE"),
                         help="Run check_day(): actually loads ONE animal/day (OpenEphys read, "
                              "probe binding, session_boundaries.json write) and reports on it. "
                              "Slower than --check, but tells you it actually works end-to-end.")
    parser.add_argument("--with-staging", action="store_true",
                         help="With --check-day, don't bypass stage_raw_locally - actually test the "
                              "staging copy too (slower).")
    args = parser.parse_args()

    cfg = load_config(args.env)

    if args.check_day:
        animal_id, date_str = args.check_day
        passed = check_day(cfg, animal_id, date_str, skip_staging=not args.with_staging)
        raise SystemExit(0 if passed else 1)
    elif args.check:
        results = self_check(cfg, animal_filter=args.animal)
        if any(level == "FAIL" for level, _ in results):
            raise SystemExit(1)
    else:
        grouped = find_sessions(cfg, animal_filter=args.animal)
        for (animal_id, date_str), paths in grouped.items():
            print(f"{animal_id} / {date_str}: {len(paths)} session(s)")
            for p in paths:
                print(f"  {os.path.basename(p)}")
