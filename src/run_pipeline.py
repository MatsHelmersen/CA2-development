"""
run_pipeline.py - CLI entry point for ephys_pipeline.

Scope (per ARCHITECTURE.md Sec.3, Sec.4 OPEN ISSUE): wires io_utils.py /
health_check.py / ap_sorter.py / quality_control.py together behind one
CLI, using config_loader.load_config(env) - the same merged-config
pattern every other module already uses. This RESOLVES the Sec.4 OPEN
ISSUE (a previous version of this file read a non-existent
config/config.yaml with a different key schema) - there is no such file
in the project as of this session, so this is a fresh implementation,
not a migration.

This module does NOT reimplement any pipeline logic. Every subcommand is
a thin argument-resolution + delegation layer over an existing public
function in another module. If a subcommand's behaviour looks wrong,
the bug is almost certainly in the module it delegates to, not here.

============================================================================
DESIGN DECISIONS THIS SESSION (flagged per project convention)
============================================================================

1. SUBCOMMANDS: `health-check` (wraps health_check.py's --preflight /
   --report), `sort` (wraps ap_sorter.process_animal_day), `qc` (wraps
   quality_control.process_animal_day), `phy-export` (see module-level
   caveat above - NOT a cheaper path than `qc`), `run-all` (new -
   sequences report-mode health-check -> sort -> qc for one animal/day,
   for local end-to-end testing/convenience), `list-jobs` and the
   --job-list/--job-index flags (new - Slurm array support, see #2),
   and `check` (new - aggregates every module's self_check() in one
   call, distinct from health-check --preflight which is environment/
   hardware-level, not module-wiring-level - see #3).

2. SLURM ARRAY / --animal/--date SINGLE-UNIT-OF-WORK INTERFACE: every
   day-scoped subcommand (health-check --report, sort, qc, phy-export,
   run-all) accepts EITHER `--animal ID --date YYYYMMDD [--session-name
   NAME]` directly, OR `--job-list PATH --job-index N`. The latter reads
   a CSV manifest (written by `list-jobs`) of one row per (animal_id,
   date_str) and selects row N - designed so a single Slurm array task
   script can be:

     python run_pipeline.py --env fox run-all \\
         --job-list jobs.csv --job-index $SLURM_ARRAY_TASK_ID

   with `--array=0-$(($(wc -l < jobs.csv)-2))` sized directly off
   `list-jobs`'s own printed suggestion. The manifest is deliberately
   DAY-level, not session-level: `--session-name` (for a day where the
   probe/drive moved mid-day, ARCHITECTURE.md Sec.6) is NOT expressible
   via --job-list and must be run as an explicit one-off
   --animal/--date/--session-name invocation instead. This is a
   deliberate scope limit, not an oversight - session-level splitting is
   the exception (a handful of days per project), not the batch-array
   common case, and folding it into the manifest schema would complicate
   the common path for a rare one. --job-list validates this: passing
   --session-name together with --job-list is a hard error, not a
   silent ignore.

3. `check` vs `health-check --preflight`: DELIBERATELY KEPT SEPARATE,
   not merged. `health-check --preflight` (health_check.py, unchanged)
   checks environment/hardware readiness: GPU/CUDA, disk space, package
   versions, probe geometry, aux-channel coverage. The new `check`
   subcommand here instead aggregates io_utils.self_check() +
   artifact_cleaning.self_check() + ap_sorter.self_check() +
   quality_control.self_check() - config-key presence and (with
   --animal/--date) per-day file/output presence for EACH module's own
   stated dependencies. These answer different questions ("is this
   machine ready to run KS4" vs "is this day's config/output internally
   consistent across modules") and calling `check` does not replace
   running `health-check --preflight` before a Fox batch submission, or
   vice versa.

4. NOT ADDED: a single "assess every animal/day found on disk in one
   call" loop for sort/qc. Deliberately out of scope - true batch
   processing on Fox should be a Slurm job array (ARCHITECTURE.md Sec.2:
   "one task per animal/day, not a single sequential process"), which is
   exactly what --job-list/--job-index is for. A local "loop over
   everything sequentially" mode would encourage running a real batch
   outside Slurm on Fox and was intentionally left out; for local/biotin
   ad-hoc work, loop over `list-jobs`' output yourself (e.g. a one-line
   shell for-loop) rather than this script doing it silently.

5. EXIT CODES: every subcommand returns 0 on full success, 1 if any
   FAIL/error was reported (matches every other module's __main__
   convention - `raise SystemExit(1 if ... else 0)`). `run-all` returns
   1 if ANY stage failed, and by default stops at the first failed
   stage (pass --continue-on-error to run remaining stages anyway - e.g.
   to still attempt qc's stale-report detection even after a sort
   error on one shank).

Config is read via config_loader.load_config(env) exclusively - no
CONFIG dict, no other config file, is read anywhere in this module.
"""

import argparse
import copy
import csv
import os
import sys
from pathlib import Path

# This file lives at <repo_root>/run_pipeline.py (NOT under src/ - see
# ARCHITECTURE.md Sec.3's directory tree), unlike every other module,
# which assumes <repo_root>/src/<module>.py. REPO_ROOT is therefore
# this file's own parent, not parent.parent.
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config_loader import load_config
from src.io_utils import find_sessions, get_day_output_dir
from src import health_check
from src import ap_sorter
from src import quality_control
from src import artifact_cleaning
from src import io_utils as io_utils_module


# ============================================================================
# JOB-LIST (Slurm array) HELPERS
# ============================================================================

def cmd_list_jobs(cfg: dict, args) -> int:
    """
    Enumerate every (animal_id, date_str) group found under
    cfg["paths"]["base_path"] (optionally filtered by --animal) and write
    a CSV manifest: job_index,animal_id,date_str,n_sessions. job_index is
    0-based and stable for a given directory tree snapshot (sorted by
    (animal_id, date_str)) - re-run this whenever new raw data is added,
    since indices are not guaranteed stable across additions/removals.
    """
    grouped = find_sessions(cfg, animal_filter=args.animal)
    if not grouped:
        print("No animal/day groups found - check paths.base_path and directory naming.")
        return 1

    rows = sorted(grouped.keys())  # deterministic (animal_id, date_str) order
    with open(args.out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["job_index", "animal_id", "date_str", "n_sessions"])
        for i, (animal_id, date_str) in enumerate(rows):
            writer.writerow([i, animal_id, date_str, len(grouped[(animal_id, date_str)])])

    print(f"Wrote {len(rows)} job(s) to {args.out}")
    print(f"\nSlurm array usage:")
    print(f"  #SBATCH --array=0-{len(rows) - 1}")
    print(f"  python run_pipeline.py --env fox run-all "
          f"--job-list {args.out} --job-index $SLURM_ARRAY_TASK_ID")
    return 0


def _load_job_list(path: str) -> list:
    if not os.path.exists(path):
        raise SystemExit(f"--job-list file not found: {path}. Run "
                          f"'python run_pipeline.py --env {{env}} list-jobs' first.")
    jobs = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if "animal_id" not in (reader.fieldnames or []) or "date_str" not in (reader.fieldnames or []):
            raise SystemExit(f"--job-list file {path} is missing required columns "
                              f"'animal_id'/'date_str' - was it written by "
                              f"'run_pipeline.py list-jobs'?")
        for row in reader:
            jobs.append((row["animal_id"], row["date_str"]))
    return jobs


def resolve_animal_date(args) -> tuple:
    """
    Resolve (animal_id, date_str, session_name) from EITHER
    --animal/--date[/--session-name] OR --job-list/--job-index, per
    Design Decision 2. Raises SystemExit with an actionable message on
    any ambiguous/invalid combination rather than guessing.
    """
    job_list = getattr(args, "job_list", None)
    job_index = getattr(args, "job_index", None)
    session_name = getattr(args, "session_name", None)

    if job_list is not None:
        if job_index is None:
            raise SystemExit("--job-list requires --job-index (e.g. --job-index $SLURM_ARRAY_TASK_ID).")
        if args.animal or args.date:
            raise SystemExit("--job-list/--job-index and --animal/--date are mutually exclusive - "
                              "pick one way to specify the animal/day.")
        if session_name:
            raise SystemExit("--session-name is not supported together with --job-list (the manifest "
                              "is day-level only, per run_pipeline.py's design - see module docstring "
                              "Design Decision 2). Re-run this day with explicit "
                              "--animal/--date/--session-name instead.")
        jobs = _load_job_list(job_list)
        if not (0 <= job_index < len(jobs)):
            raise SystemExit(f"--job-index {job_index} out of range for {len(jobs)} job(s) in {job_list} "
                              f"(valid range: 0-{len(jobs) - 1}).")
        animal_id, date_str = jobs[job_index]
        return animal_id, date_str, None

    if not args.animal or not args.date:
        raise SystemExit("Provide --animal and --date, or --job-list and --job-index.")
    return args.animal, args.date, session_name


def _add_job_selection_args(sp, shank: bool = False, with_staging: bool = True):
    sp.add_argument("--animal", default=None, help="Animal ID.")
    sp.add_argument("--date", default=None, help="Date string YYYYMMDD.")
    sp.add_argument("--session-name", default=None,
                     help="Restrict to a single session (e.g. '20231105_002') instead of the "
                          "whole day - forces individual-session mode. Not usable with --job-list.")
    sp.add_argument("--job-list", default=None,
                     help="CSV manifest from 'list-jobs'. Combine with --job-index. Mutually "
                          "exclusive with --animal/--date.")
    sp.add_argument("--job-index", type=int, default=None,
                     help="0-based row in --job-list to select (e.g. $SLURM_ARRAY_TASK_ID).")
    if shank:
        sp.add_argument("--shank", default=None, help="Restrict to this shank ID only.")
    if with_staging:
        sp.add_argument("--with-staging", action="store_true",
                         help="Don't bypass stage_raw_locally - actually test the staging copy too.")


# ============================================================================
# SUBCOMMAND: health-check
# ============================================================================

def cmd_health_check(cfg: dict, args) -> int:
    if args.preflight:
        results = health_check.run_preflight(cfg, animal_filter=args.animal,
                                              check_ordering=args.check_ordering)
        return 1 if any(level == "FAIL" for level, _ in results) else 0

    # --report
    animal_id, date_str, session_name = resolve_animal_date(args)
    ok = health_check.generate_health_report(
        cfg, animal_id, date_str, skip_staging=not args.with_staging,
        run_spectral_check=args.spectral_check, session_name=session_name,
    )
    return 0 if ok else 1


# ============================================================================
# SUBCOMMAND: sort
# ============================================================================

def _print_summary(title: str, summary: dict) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)
    for key, lines in summary.items():
        label = key.upper()
        print(f"{label}: {len(lines)}")
        for line in lines:
            print(f"  - {line}")


def cmd_sort(cfg: dict, args) -> int:
    animal_id, date_str, session_name = resolve_animal_date(args)
    summary = ap_sorter.process_animal_day(
        cfg, animal_id, date_str, session_name=session_name, shank_filter=args.shank,
        skip_staging=not args.with_staging, existing_output_action=args.existing_output_action,
        dry_run=args.dry_run,
    )
    _print_summary("SORT SUMMARY" + (" (DRY RUN)" if args.dry_run else ""), summary)
    return 1 if summary["errors"] else 0


# ============================================================================
# SUBCOMMAND: qc
# ============================================================================

def cmd_qc(cfg: dict, args) -> int:
    animal_id, date_str, session_name = resolve_animal_date(args)
    summary = quality_control.process_animal_day(
        cfg, animal_id, date_str, session_name=session_name, shank_filter=args.shank,
        skip_staging=not args.with_staging, dry_run=args.dry_run,
        skip_phy_export=args.skip_phy_export, skip_plots=args.skip_plots,
    )
    _print_summary("QC SUMMARY" + (" (DRY RUN)" if args.dry_run else ""), summary)
    return 1 if summary["errors"] else 0


# ============================================================================
# SUBCOMMAND: phy-export
# ============================================================================

def cmd_phy_export(cfg: dict, args) -> int:
    print("NOTE: phy-export is not a cheaper path than 'qc' - Phy export needs the same "
          "SortingAnalyzer (waveforms/templates) that unit-metric computation builds, so this "
          "recomputes the full assessment. See run_pipeline.py's module docstring if you want "
          "a genuinely decoupled re-export (would require a quality_control.py change).")
    animal_id, date_str, session_name = resolve_animal_date(args)
    summary = quality_control.process_animal_day(
        cfg, animal_id, date_str, session_name=session_name, shank_filter=args.shank,
        skip_staging=not args.with_staging, dry_run=False,
        skip_phy_export=False, skip_plots=not args.with_plots,
    )
    _print_summary("PHY-EXPORT SUMMARY", summary)
    return 1 if summary["errors"] else 0


# ============================================================================
# SUBCOMMAND: run-all
# ============================================================================

def cmd_run_all(cfg: dict, args) -> int:
    animal_id, date_str, session_name = resolve_animal_date(args)
    overall_ok = True

    stages = []
    if not args.skip_health_check:
        stages.append("health-check")
    if not args.skip_sort:
        stages.append("sort")
    if not args.skip_qc:
        stages.append("qc")
    print(f"run-all: {animal_id}/{date_str}"
          f"{f'/{session_name}' if session_name else ''} - stages: {', '.join(stages) or '(none)'}")

    if not args.skip_health_check:
        ok = health_check.generate_health_report(
            cfg, animal_id, date_str, skip_staging=not args.with_staging,
            run_spectral_check=args.spectral_check, session_name=session_name,
        )
        overall_ok = overall_ok and ok
        if not ok and not args.continue_on_error:
            print("run-all: health-check failed - stopping (pass --continue-on-error to proceed anyway).")
            return 1

    if not args.skip_sort:
        summary = ap_sorter.process_animal_day(
            cfg, animal_id, date_str, session_name=session_name, shank_filter=args.shank,
            skip_staging=not args.with_staging, existing_output_action=args.existing_output_action,
            dry_run=args.dry_run,
        )
        _print_summary("SORT SUMMARY" + (" (DRY RUN)" if args.dry_run else ""), summary)
        ok = not summary["errors"]
        overall_ok = overall_ok and ok
        if not ok and not args.continue_on_error:
            print("run-all: sort reported error(s) - stopping (pass --continue-on-error to proceed anyway).")
            return 1

    if not args.skip_qc:
        summary = quality_control.process_animal_day(
            cfg, animal_id, date_str, session_name=session_name, shank_filter=args.shank,
            skip_staging=not args.with_staging, dry_run=args.dry_run,
            skip_phy_export=args.skip_phy_export, skip_plots=args.skip_plots,
        )
        _print_summary("QC SUMMARY" + (" (DRY RUN)" if args.dry_run else ""), summary)
        ok = not summary["errors"]
        overall_ok = overall_ok and ok

    return 0 if overall_ok else 1


# ============================================================================
# SUBCOMMAND: check (aggregated module self-checks)
# ============================================================================

def cmd_check(cfg: dict, args) -> int:
    day_output_dir = None
    if args.animal and args.date:
        day_output_dir = get_day_output_dir(cfg, args.animal, args.date, session_name=args.session_name)
    elif args.animal or args.date:
        raise SystemExit("--animal and --date must be given together for 'check' (or neither, for a "
                          "config-only check).")

    all_results = []
    sections = [
        ("io_utils", io_utils_module.self_check(cfg, animal_filter=args.animal)),
        ("artifact_cleaning", artifact_cleaning.self_check(cfg, day_output_dir=day_output_dir)),
        ("ap_sorter", ap_sorter.self_check(cfg, day_output_dir=day_output_dir)),
        ("quality_control", quality_control.self_check(cfg, day_output_dir=day_output_dir)),
    ]
    for name, results in sections:
        all_results.extend(results)

    n_fail = sum(1 for level, _ in all_results if level == "FAIL")
    n_warn = sum(1 for level, _ in all_results if level == "WARN")
    print(f"\n{'=' * 70}\nAGGREGATE: {n_fail} FAIL, {n_warn} WARN, "
          f"{sum(1 for l, _ in all_results if l == 'PASS')} PASS across "
          f"{len(sections)} module(s)\n{'=' * 70}")
    return 1 if n_fail else 0


# ============================================================================
# CLI
# ============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ephys_pipeline CLI entry point. --env must precede the subcommand.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # One-time: see what's out there, generate a Slurm array manifest
  python run_pipeline.py --env fox list-jobs --out jobs.csv

  # Environment-level pre-flight before submitting Fox jobs
  python run_pipeline.py --env fox health-check --preflight --check-ordering

  # Per-day signal quality report
  python run_pipeline.py --env local health-check --report --animal 213868 --date 20231105

  # Sort, then assess, for one day
  python run_pipeline.py --env local sort --animal 213868 --date 20231105
  python run_pipeline.py --env local qc   --animal 213868 --date 20231105

  # Full local end-to-end run for one day
  python run_pipeline.py --env local run-all --animal 213868 --date 20231105

  # Slurm array task (one line in your sbatch script)
  python run_pipeline.py --env fox run-all --job-list jobs.csv --job-index $SLURM_ARRAY_TASK_ID

  # Aggregated module self-check
  python run_pipeline.py --env local check --animal 213868 --date 20231105
""",
    )
    parser.add_argument("--env", required=True, choices=["local", "fox", "biotin"])
    sub = parser.add_subparsers(dest="command", required=True)

    # --- list-jobs ---
    sp = sub.add_parser("list-jobs", help="Write a Slurm-array-friendly CSV manifest of animal/day groups.")
    sp.add_argument("--animal", default=None, help="Restrict to a single AnimalID (default: all found).")
    sp.add_argument("--out", default="jobs.csv", help="Output CSV path (default: jobs.csv).")
    sp.set_defaults(func=cmd_list_jobs)

    # --- health-check ---
    sp = sub.add_parser("health-check", help="Wraps health_check.py: --preflight or --report.")
    mode = sp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--report", action="store_true")
    sp.add_argument("--check-ordering", action="store_true",
                     help="(--preflight) Also cross-check NNN folder order against timestamps.")
    sp.add_argument("--spectral-check", action="store_true",
                     help="(--report) Run the periodic-discharge spectral diagnostic.")
    _add_job_selection_args(sp)
    sp.set_defaults(func=cmd_health_check)

    # --- sort ---
    sp = sub.add_parser("sort", help="Wraps ap_sorter.py: run Kilosort4 per shank.")
    _add_job_selection_args(sp, shank=True)
    sp.add_argument("--existing-output-action", choices=["skip", "overwrite", "prompt"], default="skip")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_sort)

    # --- qc ---
    sp = sub.add_parser("qc", help="Wraps quality_control.py: post-sort unit assessment.")
    _add_job_selection_args(sp, shank=True)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--skip-phy-export", action="store_true")
    sp.add_argument("--skip-plots", action="store_true")
    sp.set_defaults(func=cmd_qc)

    # --- phy-export ---
    sp = sub.add_parser("phy-export",
                         help="Force a Phy export for one animal/day (NOT cheaper than 'qc' - see "
                              "module docstring).")
    _add_job_selection_args(sp, shank=True)
    sp.add_argument("--with-plots", action="store_true",
                     help="Also generate QC PNGs (skipped by default here to save time).")
    sp.set_defaults(func=cmd_phy_export)

    # --- run-all ---
    sp = sub.add_parser("run-all", help="health-check --report -> sort -> qc, in sequence, for one animal/day.")
    _add_job_selection_args(sp, shank=True)
    sp.add_argument("--spectral-check", action="store_true")
    sp.add_argument("--existing-output-action", choices=["skip", "overwrite", "prompt"], default="skip")
    sp.add_argument("--dry-run", action="store_true", help="Applies to the sort and qc stages.")
    sp.add_argument("--skip-phy-export", action="store_true")
    sp.add_argument("--skip-plots", action="store_true")
    sp.add_argument("--skip-health-check", action="store_true")
    sp.add_argument("--skip-sort", action="store_true")
    sp.add_argument("--skip-qc", action="store_true")
    sp.add_argument("--continue-on-error", action="store_true",
                     help="Run remaining stages even if an earlier stage reported an error.")
    sp.set_defaults(func=cmd_run_all)

    # --- check ---
    sp = sub.add_parser("check", help="Aggregate io_utils/artifact_cleaning/ap_sorter/quality_control self_check().")
    sp.add_argument("--animal", default=None)
    sp.add_argument("--date", default=None)
    sp.add_argument("--session-name", default=None)
    sp.set_defaults(func=cmd_check)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    cfg = load_config(args.env)
    exit_code = args.func(cfg, args)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
