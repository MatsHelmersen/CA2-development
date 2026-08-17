"""
ap_sorter.py - Kilosort4 execution, one shank at a time, for ephys_pipeline.

Scope (per ARCHITECTURE.md Sec.9): chains io_utils.prepare_day() -> split by
shank -> bad-channel / hopeless-saturation exclusion -> saturation muting
(artifact_cleaning.py) -> si.run_sorter("kilosort4", ...). Also owns the
existing-output check (shank_*_ks4/params.py present) - explicitly excluded
from io_utils.py's scope as sorter-specific.

Explicitly OUT of scope:
  - Bad channel / saturation / discharge DETECTION -> health_check.py
  - Saturation muting MECHANISM (the lazy wrapper itself) -> artifact_cleaning.py
  - Unit assessment / classification / phy export     -> quality_control.py
  - LFP extraction                                     -> lfp_extractor.py

============================================================================
DESIGN DECISIONS MADE THIS SESSION (flagged per ARCHITECTURE.md ground
rules - read before assuming either of these is "just how it works")
============================================================================

1. EXCLUSION SOURCE: this module reads bad-channel / hopeless-saturation
   exclusions back from health_report.json rather than re-running
   health_check.py's detection functions. Chosen over re-detection because:
     (a) detection (bad-channel scan + coarse/precise saturation scan) is
         the expensive part of health_check.py --report and re-running it
         here would double that cost for every sort;
     (b) health_report.json is meant to be inspected by a human before
         sorting (that's the whole point of --report being a separate,
         earlier step) - re-detecting here could silently produce a
         DIFFERENT exclusion set than what was reviewed, if anything about
         the recording or config changed between the two runs. Reading the
         report back makes the reviewed report the actual source of truth.
   Cost of this choice: ap_sorter.py now hard-depends on health_report.json
   existing for the exact animal/day(/session) being sorted -
   load_health_report() raises FileNotFoundError with an actionable message
   if it's missing, mirroring artifact_cleaning.load_saturation_windows()'s
   existing pattern for the same kind of dependency.

   MOVED THIS SESSION (flagged, not silent): load_health_report(),
   get_shank_exclusion_ids(), and apply_health_report_exclusions() - plus
   a new composing function, reconstruct_clean_recording_for_shank(),
   which chains exclusion -> min-channel check -> mute_before_sorting-
   gated saturation muting in the one correct order - now live in
   artifact_cleaning.py, not here. Reason: quality_control.py (next
   module) needs to reconstruct the IDENTICAL clean+muted recording that
   KS4 actually sorted, in order to build a SortingAnalyzer against the
   same waveforms KS4 saw - not a similar-but-independently-reimplemented
   recording. Keeping this logic in ap_sorter.py would have meant
   quality_control.py either importing sorting-specific internals from
   the sorter module (backwards dependency - ap_sorter.py should not be
   a dependency of the assessment module) or reimplementing the
   exclusion+muting order itself, risking drift between what was sorted
   and what gets assessed. This module now calls
   artifact_cleaning.reconstruct_clean_recording_for_shank() and no
   longer defines its own copies of the three functions above - see
   artifact_cleaning.py's module docstring ("SCOPE CHANGE") for the
   mirrored note there, and ARCHITECTURE.md Sec.4c/Sec.5/Sec.9.

2. INTERFACE CHANGE REQUIRED IN health_check.py (flagged, not silently
   made without calling it out): health_report.json's existing
   "bad_reasons" field is keyed by LOCAL channel index into shank_rec as
   it existed inside health_check.py's process at report time - not a
   channel_id. That local index is not safe to reuse against whatever
   recording object THIS module builds in a separate process, days later,
   potentially against a config with different exclusions already applied
   - the exact instability artifact_cleaning.py's module docstring already
   identified and fixed for saturation_windows.json (see its "NEW CONTRACT"
   section). bad_reasons had the same latent bug, just never consumed
   cross-process until now. RESOLUTION: health_check.py now additionally
   writes "bad_channel_ids" (channel-ID-keyed list) per shank, additive/
   non-breaking. This module reads bad_channel_ids, not bad_reasons.
   bad_reasons is left in health_report.json unchanged (still useful for
   the human-readable report). If you are running against an OLDER
   health_report.json written before this fix, bad_channel_ids will be
   absent/empty and bad-channel exclusion will silently do nothing here -
   re-run health_check.py --report to regenerate it before sorting.

3. mute_before_sorting GATING: artifact_cleaning.mute_saturation_for_shank()
   deliberately does not consult cfg["saturation_detection"]
   ["mute_before_sorting"] itself (see that module's own docstring - this
   is a documented design choice there, not a bug). UPDATED THIS SESSION:
   the flag check itself now lives in
   artifact_cleaning.reconstruct_clean_recording_for_shank() (moved out
   of ap_sorter.py along with the rest of the exclusion/muting sequence -
   see "SCOPE CHANGE" note at the top of artifact_cleaning.py), not here.
   This module (ap_sorter.py) simply calls
   reconstruct_clean_recording_for_shank() and trusts whatever it
   returns; it does not check the flag itself any more. If
   mute_before_sorting is False, saturation windows are left in the data
   and Kilosort4 sees them un-muted. This is NOT the same thing as
   saturation_detection.enabled=False upstream in health_check.py (that
   controls whether windows are DETECTED at all; mute_before_sorting
   controls whether detected windows are APPLIED).

4. EXISTING-OUTPUT CHECK is now per-SHANK (shank_{id}_ks4/params.py present),
   not per-day like sort_batch.py's original day-level check - this module
   processes one shank at a time and a partial day (some shanks sorted,
   others not, e.g. after a crash) should be resumable shank-by-shank
   rather than all-or-nothing.

Config is read via config_loader.load_config(env). No thresholds are
hardcoded here; ks4_params and sorting.min_channels_to_sort_shank come
from cfg. existing_output_action ("skip"|"overwrite"|"prompt") is a CLI
flag, not (yet) a base.yaml key - defaults to "skip" since Fox batch runs
via Slurm cannot prompt interactively. See end-of-session summary re:
whether to promote this to a config key.
"""

import copy
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import spikeinterface.full as si

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# Assumes this file lives at <repo_root>/src/ap_sorter.py per ARCHITECTURE.md Sec.3.
from src.config_loader import load_config
from src.io_utils import find_sessions, prepare_day, load_probe, select_session
from src import artifact_cleaning


# ============================================================================
# EXISTING-OUTPUT CHECK (per shank - sorter-specific, out of io_utils.py's scope)
# ============================================================================

def existing_shank_output_present(day_output_dir: str, shank_id) -> bool:
    """
    True if a completed Kilosort4 run already exists for this shank -
    mirrors sort_batch.py's existing_output_present() heuristic (params.py
    inside the shank_*_ks4 folder means KS4 actually ran and finished
    writing its params, not just that the folder was created), but scoped
    to one shank rather than "any shank_*_ks4 folder in this day" - this
    module can resume a partially-sorted day shank-by-shank.
    """
    params_path = os.path.join(day_output_dir, f"shank_{shank_id}_ks4", "sorter_output", "params.py")
    return os.path.exists(params_path)


# ============================================================================
# READ BACK health_report.json / saturation_windows.json
# ============================================================================
#
# load_health_report(), get_shank_exclusion_ids(), apply_health_report_
# exclusions(), and the composing reconstruct_clean_recording_for_shank()
# now live in artifact_cleaning.py (imported below as
# artifact_cleaning.load_health_report etc.) - MOVED this session so
# quality_control.py can share exactly the same exclusion+muting logic
# rather than reimplementing it. See this module's docstring header and
# artifact_cleaning.py's "SCOPE CHANGE" docstring note for the full
# rationale. Nothing in this section is defined locally any more.

# ============================================================================
# PER-SHANK EXCLUSION + MUTING + SORT
# ============================================================================


def sort_one_shank(cfg: dict, shank_id, shank_rec, health_report: dict, saturation_windows: dict,
                    day_output_dir: str, ks4_params: dict, existing_output_action: str = "skip",
                    dry_run: bool = False) -> dict:
    """
    Reconstruct this shank's clean+muted recording via
    artifact_cleaning.reconstruct_clean_recording_for_shank() (health_report.json
    exclusions -> min-channel re-check -> saturation muting gated on
    cfg["saturation_detection"]["mute_before_sorting"] - originally
    "Design Decision 3", now implemented in artifact_cleaning.py so
    quality_control.py can reuse it), then run Kilosort4 on the result.

    Returns a result dict with at least a "status" key: one of "SORTED",
    "SKIPPED", "ERROR". Does not raise on ordinary skip/error conditions -
    those are reported in the returned dict so a caller looping over
    shanks can continue past a single bad shank; genuinely unexpected
    exceptions from si.run_sorter are caught, logged, and returned as
    status="ERROR" rather than propagating (matches sort_batch.py's
    per-day error handling, scoped down to per-shank here).
    """
    # Reconstruction (exclusions -> min-channel re-check -> mute_before_sorting-
    # gated saturation muting) now lives in artifact_cleaning.py so
    # quality_control.py can call the identical logic later - see this
    # module's docstring and artifact_cleaning.py's "SCOPE CHANGE" note.
    try:
        recon = artifact_cleaning.reconstruct_clean_recording_for_shank(
            cfg, shank_id, shank_rec, health_report, saturation_windows)
    except Exception as e:
        msg = f"reconstruct_clean_recording_for_shank raised: {e}"
        print(f"  [FAIL] Shank {shank_id}: {msg}")
        traceback.print_exc()
        return {"status": "ERROR", "message": msg}

    if recon["status"] == "MISSING":
        msg = recon["message"] + " Not sorting."
        print(f"  [FAIL] Shank {shank_id}: {msg}")
        return {"status": "ERROR", "message": msg}

    if recon["status"] != "PASS":
        # "SKIPPED" (health_check.py already marked this shank SKIPPED) or
        # "SKIPPED_MIN_CHANNELS" (report is stale relative to this
        # recording) both mean: don't sort.
        msg = recon["message"] + " Not sorting."
        print(f"  [SKIP] Shank {shank_id}: {msg}")
        return {"status": "SKIPPED", "message": msg}

    if recon["missing_ids"]:
        print(f"    Warning: shank {shank_id} health_report.json references "
              f"{len(recon['missing_ids'])} channel(s) not present in the current "
              f"recording (already excluded upstream, or the report is stale "
              f"relative to this recording): {sorted(recon['missing_ids'])}")

    clean_rec = recon["recording"]
    print(f"  Shank {shank_id}: {recon['message']}")

    out_folder = os.path.join(day_output_dir, f"shank_{shank_id}_ks4")

    if existing_shank_output_present(day_output_dir, shank_id):
        if existing_output_action == "skip":
            print(f"  Shank {shank_id}: existing output at {out_folder} - skipping "
                  f"(existing_output_action=skip).")
            return {"status": "SKIPPED", "message": "existing output present"}
        elif existing_output_action == "prompt":
            resp = input(f"  Shank {shank_id}: sorted output already exists at {out_folder}. "
                         f"[s]kip / [o]verwrite / [c]ancel batch > ").strip().lower()
            if resp in ("s", ""):
                return {"status": "SKIPPED", "message": "existing output present (user skip)"}
            if resp == "c":
                raise KeyboardInterrupt
            # else: fall through to overwrite
        # "overwrite" (or post-prompt fallthrough): proceed, remove_existing_folder=True below

    if dry_run:
        print(f"  [DRY RUN] Shank {shank_id}: would sort {clean_rec.get_num_channels()} "
              f"channel(s) -> {out_folder} (no Kilosort4 call made).")
        return {"status": "DRY_RUN", "n_channels": clean_rec.get_num_channels(), "folder": out_folder}

    print(f"  Shank {shank_id}: sorting {clean_rec.get_num_channels()} channel(s) with Kilosort4...")
    try:
        sorting = si.run_sorter(
            "kilosort4", clean_rec, folder=out_folder,
            remove_existing_folder=True,
            **ks4_params,
        )
    except Exception as e:
        msg = f"si.run_sorter raised: {e}"
        print(f"  [FAIL] Shank {shank_id}: {msg}")
        traceback.print_exc()
        return {"status": "ERROR", "message": msg}

    n_units = len(sorting.get_unit_ids())
    print(f"  [PASS] Shank {shank_id}: {n_units} unit(s) -> {out_folder}")
    return {"status": "SORTED", "n_units": n_units, "folder": out_folder}


# ============================================================================
# PER-DAY / PER-SESSION ORCHESTRATION
# ============================================================================

def write_ap_sorter_log(day_output_dir: str, animal_id: str, date_str: str, shank_results: dict) -> str:
    """
    Write a small ap_sorter_log.json recording per-shank sort outcomes for
    this run. NOTE: this is this module's OWN bookkeeping file, not a
    cross-module contract - it is NOT the same thing as run_summary.csv
    (that's quality_control.py's job per ARCHITECTURE.md Sec.5/Sec.3, and
    is built from unit-level assessment metrics this module doesn't
    compute). Flagged here as a new file this module writes; if you want
    it formalised as a documented cross-module contract, add it to
    ARCHITECTURE.md Sec.3/Sec.5 explicitly - until then, treat it as
    debug/resume bookkeeping only.
    """
    log_path = os.path.join(day_output_dir, "ap_sorter_log.json")
    payload = {
        "animal_id": animal_id,
        "date_str": date_str,
        "generated_at": datetime.now().isoformat(),
        "shanks": {str(k): v for k, v in shank_results.items()},
    }
    with open(log_path, "w") as f:
        json.dump(payload, f, indent=2)
    return log_path


def process_animal_day(cfg: dict, animal_id: str, date_str: str, session_name: str = None,
                        shank_filter: str = None, skip_staging: bool = True,
                        existing_output_action: str = "skip", dry_run: bool = False) -> dict:
    """
    Load one animal/day (or single session), read back health_report.json
    and saturation_windows.json, split by shank, and sort every PASS shank
    (or just shank_filter, if given) via sort_one_shank().

    Returns a summary dict: {"sorted": [...], "skipped": [...], "errors": [...]}
    of human-readable strings, matching the style of sort_batch.py's batch
    summary. Does not raise for ordinary per-shank problems (those are
    caught inside sort_one_shank); a KeyboardInterrupt from a "cancel
    batch" prompt does propagate.
    """
    session_tag = f" / session {session_name}" if session_name else ""
    print(f"\n{'='*70}\nap_sorter: {animal_id} / {date_str}{session_tag}\n{'='*70}")

    summary = {"sorted": [], "skipped": [], "errors": []}

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

    try:
        probe = load_probe(cfg_for_run)
        result = prepare_day(cfg_for_run, animal_id, date_str, session_paths, probe,
                              concatenate=concatenate)
    except Exception as e:
        msg = f"Data preparation failed: {e}"
        print(f"  [FAIL] {msg}")
        traceback.print_exc()
        summary["errors"].append(f"{animal_id}/{date_str}: {msg}")
        return summary

    rec = result["recording"]
    day_output_dir = result["day_output_dir"]

    try:
        health_report = artifact_cleaning.load_health_report(day_output_dir)
    except FileNotFoundError as e:
        print(f"  [FAIL] {e}")
        summary["errors"].append(f"{animal_id}/{date_str}: {e}")
        return summary

    # health_check.generate_health_report() always writes saturation_windows.json
    # alongside health_report.json (possibly with an empty "shanks" dict) -
    # see ARCHITECTURE.md Sec.5. If health_report.json exists but
    # saturation_windows.json does not, that's not "nothing flagged", it's
    # an inconsistent output directory (e.g. hand-edited, or written by an
    # older health_check.py that predates the saturation_windows.json
    # contract) - treated as fatal rather than silently proceeding as if
    # no saturation had ever been checked.
    try:
        saturation_windows = artifact_cleaning.load_saturation_windows(day_output_dir)
    except FileNotFoundError as e:
        print(f"  [FAIL] {e}")
        summary["errors"].append(f"{animal_id}/{date_str}: {e}")
        return summary

    recording_split = rec.split_by("group")
    ks4_params = {k: v for k, v in cfg_for_run.get("ks4_params", {}).items() if v is not None}

    shank_results = {}
    for shank_id, shank_rec in recording_split.items():
        if shank_filter is not None and str(shank_id) != str(shank_filter):
            continue
        try:
            res = sort_one_shank(
                cfg_for_run, shank_id, shank_rec, health_report, saturation_windows,
                day_output_dir, ks4_params, existing_output_action=existing_output_action,
                dry_run=dry_run,
            )
        except KeyboardInterrupt:
            print("\nBatch cancelled by user.")
            raise
        shank_results[shank_id] = res

        tag = f"{animal_id}/{date_str}{session_tag} shank {shank_id}"
        if res["status"] in ("SORTED", "DRY_RUN"):
            summary["sorted"].append(f"{tag}: {res.get('n_units', res.get('n_channels', '?'))}"
                                      f"{' units' if res['status'] == 'SORTED' else ' channels (dry run)'}")
        elif res["status"] == "SKIPPED":
            summary["skipped"].append(f"{tag}: {res.get('message', '')}")
        else:
            summary["errors"].append(f"{tag}: {res.get('message', '')}")

    if not dry_run and shank_results:
        log_path = write_ap_sorter_log(day_output_dir, animal_id, date_str, shank_results)
        print(f"\n  Wrote {log_path}")

    return summary


# ============================================================================
# SELF-CHECK (module-local verification, per ARCHITECTURE.md Sec.8)
# ============================================================================

def self_check(cfg: dict, day_output_dir: str = None) -> list:
    """
    Cheap, read-only checks for this module's own responsibilities: config
    keys it reads, and (if day_output_dir given) that health_report.json
    has the bad_channel_ids field this module now depends on, and that
    saturation_windows.json is present alongside it. Does not load any raw
    recording or run Kilosort4.
    """
    results = []

    def check(level, msg):
        results.append((level, msg))
        print(f"  [{level}] {msg}")

    print(f"\n{'='*70}\nap_sorter.py self-check (env={cfg.get('_env', '?')})\n{'='*70}")

    ks4_cfg = cfg.get("ks4_params", {})
    if ks4_cfg:
        check("PASS", f"ks4_params present (torch_device={ks4_cfg.get('torch_device')})")
    else:
        check("FAIL", "cfg['ks4_params'] missing or empty")

    min_chans = cfg.get("sorting", {}).get("min_channels_to_sort_shank")
    if min_chans is not None:
        check("PASS", f"sorting.min_channels_to_sort_shank={min_chans}")
    else:
        check("WARN", "cfg['sorting']['min_channels_to_sort_shank'] not set - "
                       "sort_one_shank() will fall back to a hardcoded default of 2.")

    sat_cfg = cfg.get("saturation_detection", {})
    check("PASS" if "mute_before_sorting" in sat_cfg else "WARN",
          f"saturation_detection.mute_before_sorting="
          f"{sat_cfg.get('mute_before_sorting', '<unset, defaults to True>')}")

    if day_output_dir is not None:
        hr_path = os.path.join(day_output_dir, "health_report.json")
        if not os.path.exists(hr_path):
            check("FAIL", f"health_report.json not found at {hr_path} - run "
                          f"health_check.py --report for this day first.")
        else:
            try:
                with open(hr_path) as f:
                    hr = json.load(f)
                shanks = hr.get("shanks", {})
                n_pass = sum(1 for s in shanks.values() if s.get("status") == "PASS")
                n_missing_ids_field = sum(
                    1 for s in shanks.values()
                    if s.get("status") == "PASS" and "bad_channel_ids" not in s)
                check("PASS", f"health_report.json loaded: {len(shanks)} shank(s), "
                              f"{n_pass} PASS")
                if n_missing_ids_field:
                    check("WARN", f"{n_missing_ids_field} PASS shank(s) have no "
                                  f"'bad_channel_ids' field - this health_report.json "
                                  f"predates the bad_channel_ids fix (see ap_sorter.py "
                                  f"module docstring, Design Decision 2). Bad-channel "
                                  f"exclusion will silently do nothing for those shanks "
                                  f"until health_check.py --report is re-run.")
            except Exception as e:
                check("FAIL", f"health_report.json at {hr_path} failed to parse: {e}")

        sw_path = os.path.join(day_output_dir, "saturation_windows.json")
        if os.path.exists(sw_path):
            check("PASS", f"saturation_windows.json present alongside health_report.json")
        else:
            check("FAIL", f"saturation_windows.json NOT found at {sw_path} even though "
                          f"health_report.json exists - inconsistent output directory "
                          f"(health_check.py always writes both together). Re-run "
                          f"health_check.py --report.")
    else:
        check("PASS", "no day_output_dir given - skipped health_report.json / "
                       "saturation_windows.json checks (pass --day-output-dir, or "
                       "--animal/--date, to validate a specific day)")

    n_fail = sum(1 for level, _ in results if level == "FAIL")
    n_warn = sum(1 for level, _ in results if level == "WARN")
    print(f"\n{n_fail} FAIL, {n_warn} WARN, "
          f"{sum(1 for l, _ in results if l == 'PASS')} PASS\n")
    return results


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    import argparse

    from src.io_utils import get_day_output_dir

    parser = argparse.ArgumentParser(
        description="Kilosort4 execution per shank for ephys_pipeline. Reads exclusions "
                    "back from health_report.json (health_check.py --report must be run "
                    "first) rather than re-detecting them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Cheap config-only check:
  python ap_sorter.py --env local --check

  # Check that a specific day's health_report.json / saturation_windows.json
  # are present and well-formed before attempting to sort it:
  python ap_sorter.py --env local --check --animal 213868 --date 20231105

  # Dry run: see which shanks would be sorted and with how many channels,
  # without actually calling Kilosort4:
  python ap_sorter.py --env local --run --animal 213868 --date 20231105 --dry-run

  # Sort every PASS shank for one animal/day:
  python ap_sorter.py --env local --run --animal 213868 --date 20231105

  # Sort only shank 2, for a day where the probe/drive moved between
  # sessions (single-session mode, matching health_check.py --report
  # --session-name for the same animal/date/session):
  python ap_sorter.py --env local --run --animal 213868 --date 20231105 --session-name 20231105_002 --shank 2

  # Re-run after a crash, overwriting whatever partial output exists:
  python ap_sorter.py --env fox --run --animal 213868 --date 20231105 --existing-output-action overwrite
""",
    )
    parser.add_argument("--env", required=True, choices=["local", "fox", "biotin"])

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                       help="Run self_check(): cheap, read-only validation of config, and "
                            "(if a day is specified) health_report.json/saturation_windows.json.")
    mode.add_argument("--run", action="store_true",
                       help="Actually sort. Requires --animal and --date.")

    day_group = parser.add_mutually_exclusive_group()
    day_group.add_argument("--day-output-dir", default=None,
                            help="(--check only) Day output directory to validate directly. "
                                 "Mutually exclusive with --animal/--date.")
    day_group.add_argument("--animal", default=None,
                            help="Animal ID. Required for --run; optional for --check "
                                 "(combine with --date to validate a specific day's reports).")

    parser.add_argument("--date", default=None, help="Date string YYYYMMDD.")
    parser.add_argument("--session-name", default=None,
                         help="Restrict to a single session (e.g. '20231105_002') rather than "
                              "the whole day - forces individual-session mode, matching "
                              "health_check.py --report --session-name for the same "
                              "animal/date. Only used with --animal/--date.")
    parser.add_argument("--shank", default=None,
                         help="Sort only this shank ID (e.g. '2') instead of every PASS "
                              "shank for the day.")
    parser.add_argument("--with-staging", action="store_true",
                         help="(--run) Don't bypass stage_raw_locally - actually test the "
                              "staging copy too (slower).")
    parser.add_argument("--existing-output-action", choices=["skip", "overwrite", "prompt"],
                         default="skip",
                         help="What to do if shank_<id>_ks4/params.py already exists for a "
                              "shank. Default 'skip' (safe for unattended/Slurm batch runs - "
                              "'prompt' will hang a non-interactive job).")
    parser.add_argument("--dry-run", action="store_true",
                         help="(--run) Load data, apply exclusions and muting, print what "
                              "would be sorted and with how many channels, but do not "
                              "actually call Kilosort4.")

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
            existing_output_action=args.existing_output_action, dry_run=args.dry_run,
        )
        print("\n" + "=" * 70)
        print("AP_SORTER SUMMARY" + (" (DRY RUN)" if args.dry_run else ""))
        print("=" * 70)
        print(f"Sorted:  {len(summary['sorted'])}")
        for line in summary["sorted"]:
            print(f"  OK   {line}")
        print(f"Skipped: {len(summary['skipped'])}")
        for line in summary["skipped"]:
            print(f"  SKIP {line}")
        print(f"Errors:  {len(summary['errors'])}")
        for line in summary["errors"]:
            print(f"  FAIL {line}")
        raise SystemExit(1 if summary["errors"] else 0)
