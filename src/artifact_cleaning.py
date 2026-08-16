"""
artifact_cleaning.py - lazy artifact-muting preprocessing for ephys_pipeline.

Scope (per ARCHITECTURE.md Sec.9, Sec.5, Sec.7):
  - Consume saturation windows detected upstream by health_check.py
    (read from saturation_windows.json - see "NEW CONTRACT" below) and
    apply per-(channel, sample-range) zeroing as a lazy SpikeInterface
    preprocessing wrapper, BEFORE Kilosort4 runs.
  - Provide a stub for the ~1 kHz periodic-discharge artifact
    (ARCHITECTURE.md Sec.7) that raises NotImplementedError until the
    spatial pattern (phase-locked across a shank vs. per-channel) has
    been characterised. This module does not decide that question; it
    only refuses to silently do nothing once discharge hits are present.

Explicitly OUT of scope (belongs elsewhere):
  - Detecting saturation windows / bad channels / discharge peaks -> health_check.py
  - Kilosort4 / sorting itself (si.run_sorter call)                -> ap_sorter.py
  - Unit assessment / classification                               -> quality_control.py

============================================================================
SCOPE CHANGE (flagged, not silent) - health_report.json reconstruction
moved here from ap_sorter.py
============================================================================

Previously, "read health_report.json exclusions back and apply them" lived
entirely in ap_sorter.py (load_health_report / get_shank_exclusion_ids /
apply_health_report_exclusions), and saturation muting lived here. Both
were only ever called together, in the same order, inside
ap_sorter.sort_one_shank(). That order (bad-channel + hopeless-saturation
exclusion, THEN mute_before_sorting-gated saturation muting) is exactly
what Kilosort4 saw for a given shank - and it is exactly what
quality_control.py will need to reconstruct too, so that the
SortingAnalyzer/waveforms it computes are built against the SAME samples
KS4 actually clustered on, not a similar-but-independently-reimplemented
recording.

RESOLUTION: load_health_report(), get_shank_exclusion_ids(), and
apply_health_report_exclusions() have been moved here verbatim (same
signatures, same behaviour), and a new function,
reconstruct_clean_recording_for_shank(), composes them with
mute_saturation_for_shank() in the one correct order. This is now the
SINGLE SOURCE OF TRUTH for "what recording did KS4 actually see (or
would it see) for this shank" - ap_sorter.py calls it before
si.run_sorter(), and quality_control.py must call it (with the same
cfg / health_report / saturation_windows a given sort run used) before
building a SortingAnalyzer, rather than reimplementing the exclusion +
muting order independently and risking drift between what was sorted and
what is being assessed.

ap_sorter.py has been updated to import these from here rather than
defining its own copies - see that module's docstring for the
corresponding note. This IS an interface change (functions moved between
modules); flagged here and in ARCHITECTURE.md Sec.4c/Sec.5/Sec.9 rather
than made silently, per project convention.

CLI vs. library scope, since these differ here (unlike io_utils.py and
health_check.py, whose CLIs run their main pipeline actions directly):
this module's library functions (mute_saturation_for_shank(),
load_saturation_windows(), etc.) are meant to be called FROM ap_sorter.py,
per-shank, during a sort run - they are not standalone pipeline steps with
their own CLI action. The CLI at the bottom of this file supports exactly
one action, --check (self_check() - cheap, read-only config/file
validation), and errors explicitly if you run it without --check rather
than silently printing help text.

Config is read via config_loader.load_config(env) - see ARCHITECTURE.md
Sec.4. No thresholds are hardcoded here; this module consumes
cfg["saturation_detection"] only for the window_pad_ms / hopeless_fraction
fields it needs for reporting, and does not re-derive any detection
threshold itself (it does not re-run detection - see NEW CONTRACT below).

============================================================================
INTERFACE CHANGE REQUIRED - FLAGGED, NOT SILENTLY MADE (see chat for the
decision this implements)
============================================================================

health_report.json (ARCHITECTURE.md Sec.5) stores only a saturation
WINDOW COUNT per shank (`saturation_windows_flagged: int`), not the
actual per-channel (start_sample, end_sample) windows computed by
health_check.detect_saturation_windows_per_channel(). That dict
(`per_channel_sat`) is discarded after the count is taken - there was no
way for this module to reconstruct the windows from health_report.json
alone, contradicting the Sec.9 assumption that "health_check.py detects
... and writes them to health_report.json".

RESOLUTION (user-selected, additive, does not change health_report.json):
health_check.py must be extended to ALSO write a second file,
saturation_windows.json, alongside health_report.json, for every day it
generates a report for. This module (artifact_cleaning.py) reads that
file. See NEW CONTRACT below for the exact schema. The health_check.py
edit itself is described here but is NOT made in this file - it must be
applied to health_check.py directly (see end-of-session summary for the
literal diff needed).

NEW CONTRACT - saturation_windows.json (to be written by
health_check.generate_health_report, read by this module):

    {
      "sampling_frequency": <float>,
      "shanks": {
        "<shank_id>": {
          "<channel_id>": [[start_sample, end_sample], ...],
          ...
        },
        ...
      }
    }

Keyed by ACTUAL channel_id (string, matching
recording.get_channel_ids()), not local shank-relative integer index -
deliberately different from the in-memory
`local_channel_index -> windows` contract used internally by
health_check.py (ARCHITECTURE.md Sec.5, "Per-channel saturation
windows"). Local indices are only meaningful relative to the specific
`clean_rec` health_check.py built for itself (after bad-channel and
hopeless-saturation exclusion) - by the time this module runs, in a
separate process, days later, potentially against a config or an SI
recording object with different exclusions already applied,
recording.split_by("group") -> channel selection may not reproduce that
exact index mapping. Channel IDs are stable identifiers; local indices
are not. This module maps channel_id -> local index itself, against
whatever recording it is actually given, at call time.

Only shanks with status == "PASS" in health_report.json have entries
here - SKIPPED shanks were never precisely scanned (see
health_check.py's shank viability gate) and have no windows to mute.
"""

import json
import os

import numpy as np

import spikeinterface.full as si
# BasePreprocessor / BasePreprocessorSegment are internal classes, not part
# of spikeinterface.full's flattened user-facing namespace (confirmed:
# AttributeError, 'full' has no attribute 'BasePreprocessorSegment'). I
# initially guessed spikeinterface.core as the correct location based on
# SI's documentation describing "core" as where Base* classes live - that
# guess was also WRONG for the installed version this was actually run
# against. Confirmed correct location (verified by the user against a
# real install - I have no spikeinterface available in this environment):
# spikeinterface.preprocessing.basepreprocessor.
import spikeinterface.preprocessing.basepreprocessor as si_preprocessing_base


# ============================================================================
# SATURATION MUTING - lazy preprocessing wrapper
# ============================================================================

def load_saturation_windows(day_output_dir: str) -> dict:
    """
    Read saturation_windows.json (written by health_check.py - see the
    NEW CONTRACT note in this module's docstring) for one animal/day.

    Returns the parsed dict: {"sampling_frequency": fs,
    "shanks": {shank_id: {channel_id: [[start,end],...]}}}.

    Raises FileNotFoundError with an explicit, actionable message if the
    file is missing - this deliberately does NOT fall back to "no
    windows" silently, because "no file" (health_check.py was never run,
    or was run with an older version that doesn't write this file yet)
    and "file exists, shank had zero windows" are different situations
    and should not be conflated. A day that was never health-checked
    should not be silently sorted as if it were clean.
    """
    path = os.path.join(day_output_dir, "saturation_windows.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"saturation_windows.json not found at {path}. "
            f"Run health_check.py --report for this animal/day first "
            f"(this file is written alongside health_report.json). "
            f"If you are running an older health_check.py that predates "
            f"this contract, it needs updating - see artifact_cleaning.py "
            f"module docstring."
        )
    with open(path) as f:
        return json.load(f)


def _channel_windows_for_shank(saturation_windows: dict, shank_id, recording) -> dict:
    """
    Map the channel_id-keyed windows for one shank (as read from
    saturation_windows.json) onto LOCAL indices for the specific
    `recording` object being muted right now.

    Channels present in saturation_windows.json but absent from
    `recording` (e.g. already excluded as a bad/hopeless channel
    upstream, or a config change since the health report was generated)
    are skipped with a printed warning rather than raising - the muting
    wrapper should degrade to "mute what's still here", not crash a
    batch run over a channel that's already gone. Mismatches are worth
    knowing about, though, since they can indicate the health report is
    stale relative to the recording being sorted.

    Returns dict: local_channel_index (int) -> list of (start, end) tuples.
    """
    shank_key = str(shank_id)
    per_channel_str_keyed = saturation_windows.get("shanks", {}).get(shank_key, {})
    if not per_channel_str_keyed:
        return {}

    chan_ids = list(recording.get_channel_ids())
    id_to_local = {str(cid): i for i, cid in enumerate(chan_ids)}

    result = {}
    missing = []
    for cid_str, windows in per_channel_str_keyed.items():
        if cid_str not in id_to_local:
            missing.append(cid_str)
            continue
        local_idx = id_to_local[cid_str]
        result[local_idx] = [(int(s), int(e)) for s, e in windows]

    if missing:
        print(f"    Warning: saturation_windows.json shank {shank_key} references "
              f"{len(missing)} channel(s) not present in the current recording "
              f"(already excluded, or health report is stale): {missing}")

    return result


class _SaturationMutedSegment(si_preprocessing_base.BasePreprocessorSegment):
    """
    Per-segment trace wrapper. Zeros exactly the flagged
    (channel, sample-range) pairs, leaving every other sample -
    including other channels during the same window, and this channel
    during other windows - completely untouched.

    NOTE on channel_indices handling: the original sort_batch.py version
    re-derived absolute channel indices from a `slice` via
    `slice.indices(traces.shape[1] + slice.start)`, which is not a
    correct general slice-to-range conversion (it happens to work for
    some slice/shape combinations and not others). This version avoids
    slice arithmetic entirely by normalising channel_indices to a plain
    array up front using np.arange over the FULL channel count when
    None, and via slice.indices(n_channels_total) - the actual full
    channel count of this segment - rather than reconstructing it from
    the already-sliced traces array.
    """
    def __init__(self, parent_segment, per_channel_windows, n_channels_total):
        si_preprocessing_base.BasePreprocessorSegment.__init__(self, parent_segment)
        self.per_channel_windows = per_channel_windows
        self.n_channels_total = n_channels_total

    def get_traces(self, start_frame, end_frame, channel_indices):
        traces = self.parent_recording_segment.get_traces(
            start_frame, end_frame, channel_indices).copy()

        if channel_indices is None:
            resolved_indices = np.arange(self.n_channels_total)
        elif isinstance(channel_indices, slice):
            resolved_indices = np.arange(*channel_indices.indices(self.n_channels_total))
        else:
            resolved_indices = np.atleast_1d(channel_indices)

        if start_frame is None:
            start_frame = 0

        for out_col, ch in enumerate(resolved_indices):
            for (w_start, w_end) in self.per_channel_windows.get(int(ch), []):
                ov_start = max(w_start, start_frame)
                ov_end = min(w_end + 1, end_frame) if end_frame is not None else w_end + 1
                if ov_start < ov_end:
                    traces[ov_start - start_frame: ov_end - start_frame, out_col] = 0
        return traces


class SaturationMutedRecording(si_preprocessing_base.BasePreprocessor):
    """
    Lazy preprocessing wrapper: zeros out exactly the flagged (channel,
    sample-range) pairs from a per_channel_windows dict (LOCAL indices,
    already mapped for this specific recording - use
    mute_saturation_for_shank() below rather than constructing this
    directly unless you already have local-index windows in hand).

    Nothing is materialized until get_traces() is actually called,
    matching SpikeInterface's normal lazy-preprocessing pattern.

    IMPORT PATH (resolved): subclasses BasePreprocessor/BasePreprocessorSegment
    from spikeinterface.preprocessing.basepreprocessor (see import block at
    top of module for the history of two wrong guesses before this was
    confirmed against a real install - spikeinterface.full does not
    re-export these, and spikeinterface.core was also wrong despite SI's
    own docs suggesting "core" as the home for Base* classes). Class
    construction (import + __init__ + add_recording_segment) is confirmed
    to work against a real SpikeInterface install as of this correction.

    STILL UNVERIFIED (not the same claim as the import fix above - do not
    conflate the two): whether get_traces() actually zeros the correct
    (channel, sample) cells when called with each of the three
    channel_indices forms (None, slice, explicit array), whether
    iterating recording._recording_segments (a private attribute) is
    still the right way to enumerate segments for this SI version, and
    whether the wrapped recording behaves correctly once handed to
    si.run_sorter(). None of that has been exercised against real data
    or a real sorter call in this conversation - see end-of-session
    verification steps for concrete tests to run.

    CARRIED-FORWARD DESIGN CAVEAT (ARCHITECTURE.md Sec.7, unresolved):
    this subclasses the Base* classes directly rather than a documented
    public preprocessing function, because per-channel (as opposed to
    identical-across-all-channels) period support in SI's
    silence_periods() could not be confirmed from documentation at the
    time sort_batch.py was written. Independent of the import-path fix
    above - still worth revisiting if silence_periods() turns out to
    support this natively in your installed version, which would let
    this custom subclass be retired in favour of a documented public API.
    """
    def __init__(self, recording, per_channel_windows):
        si_preprocessing_base.BasePreprocessor.__init__(self, recording)
        n_channels_total = recording.get_num_channels()
        for parent_segment in recording._recording_segments:
            self.add_recording_segment(
                _SaturationMutedSegment(parent_segment, per_channel_windows, n_channels_total)
            )
        self._kwargs = dict(recording=recording, per_channel_windows=per_channel_windows)


def mute_saturation_for_shank(recording, shank_id, saturation_windows: dict):
    """
    Convenience entry point: given a shank's recording and the parsed
    saturation_windows.json dict (from load_saturation_windows()), map
    channel-id-keyed windows to local indices for THIS recording and
    return a lazily-muted recording.

    If this shank has no entry in saturation_windows.json (e.g. it was
    SKIPPED at the health-check stage, or the day-level saturation
    detection is disabled in config), the recording is returned
    unchanged - no-op, not an error, since "nothing flagged" and
    "nothing checked" are both valid reasons for an empty windows dict
    (the caller can distinguish the two via health_report.json's
    per-shank "status" field if that distinction matters upstream).
    """
    local_windows = _channel_windows_for_shank(saturation_windows, shank_id, recording)
    if not local_windows:
        return recording
    return SaturationMutedRecording(recording, local_windows)


def merge_windows_across_channels(per_channel_windows: dict) -> list:
    """
    Flatten per-channel windows into a single merged (start, end) list,
    sorted by start. Carried over from sort_batch.py - used downstream
    by quality_control.py's compute_saturation_overlap() (ARCHITECTURE.md
    Sec.5 "Per-channel saturation windows" contract), which needs the
    ALL-channel merged list, not the per-channel breakdown, to test
    whether a unit's spikes fall near ANY flagged window regardless of
    which channel it was flagged on.
    """
    all_windows = [w for wins in per_channel_windows.values() for w in wins]
    return sorted(all_windows)


# ============================================================================
# health_report.json READBACK + FULL SHANK RECONSTRUCTION
# (moved here from ap_sorter.py - see "SCOPE CHANGE" note in module
# docstring above for why)
# ============================================================================

def load_health_report(day_output_dir: str) -> dict:
    """
    Read health_report.json for one animal/day(/session). Raises
    FileNotFoundError with an actionable message if missing - this module
    deliberately does NOT fall back to "no exclusions" or re-run
    detection itself (mirrors load_saturation_windows()'s existing
    pattern above, for the same reason: a day that was never
    health-checked should not be silently treated as if every channel
    were clean).

    MOVED FROM ap_sorter.py (unchanged behaviour/signature) - see
    "SCOPE CHANGE" note in this module's docstring.
    """
    path = os.path.join(day_output_dir, "health_report.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"health_report.json not found at {path}. Run health_check.py --report "
            f"for this animal/day first - this module reads bad-channel and "
            f"hopeless-saturation exclusions back from that report rather than "
            f"re-detecting them (see this module's docstring, 'SCOPE CHANGE' note, "
            f"and ap_sorter.py's original Design Decision 1)."
        )
    with open(path) as f:
        return json.load(f)


def get_shank_exclusion_ids(health_report: dict, shank_id) -> tuple:
    """
    Look up one shank's entry in a loaded health_report.json dict.

    Returns (status: str, exclude_ids: set[str], detail: dict|None).
    status is "PASS", "SKIPPED", or "MISSING" (no entry at all - e.g. a
    stale health_report.json from before this shank grouping, or a
    typo'd shank_id). exclude_ids is the union of bad_channel_ids and
    saturated_hopeless_channels (channel-ID strings) - empty set if
    status is not "PASS" (a SKIPPED shank's exclusion set is meaningless;
    the whole shank is not being sorted/assessed).

    MOVED FROM ap_sorter.py (unchanged behaviour/signature).
    """
    shank_health = health_report.get("shanks", {}).get(str(shank_id))
    if shank_health is None:
        return "MISSING", set(), None

    status = shank_health.get("status", "UNKNOWN")
    if status != "PASS":
        return status, set(), shank_health

    bad_ids = set(str(c) for c in shank_health.get("bad_channel_ids", []))
    hopeless_ids = set(str(c) for c in shank_health.get("saturated_hopeless_channels", []))
    return "PASS", bad_ids | hopeless_ids, shank_health


def apply_health_report_exclusions(shank_rec, exclude_ids: set):
    """
    select_channels() to drop exclude_ids (channel-ID strings, from
    get_shank_exclusion_ids()) from shank_rec. Channels in exclude_ids
    not present in shank_rec (already gone for some other reason, or a
    stale report) are skipped with a printed warning rather than raising
    - matches this module's _channel_windows_for_shank()'s "mute what's
    still here" degradation, for the same reason: a stale report should
    warn loudly, not crash a batch run.

    Returns (clean_recording, missing_ids: set).

    MOVED FROM ap_sorter.py (unchanged behaviour/signature).
    """
    chan_ids = list(shank_rec.get_channel_ids())
    id_by_str = {str(c): c for c in chan_ids}
    present_str = set(id_by_str.keys())

    missing = exclude_ids - present_str
    keep_str = [c for c in present_str if c not in exclude_ids]
    # Preserve original channel order rather than set() order.
    keep_str_ordered = [str(c) for c in chan_ids if str(c) in keep_str]
    keep_ids = [id_by_str[c] for c in keep_str_ordered]

    return shank_rec.select_channels(keep_ids), missing


def reconstruct_clean_recording_for_shank(cfg: dict, shank_id, shank_rec,
                                           health_report: dict, saturation_windows: dict) -> dict:
    """
    Reconstruct, for one shank, the exact recording Kilosort4 was (or
    would be) handed: health_report.json exclusions (bad channels +
    hopeless-saturation channels) -> minimum-viable-channel re-check
    against THIS recording object -> saturation-window muting, gated on
    cfg["saturation_detection"]["mute_before_sorting"] (originally
    ap_sorter.py's "Design Decision 3" - the gating check itself still
    lives here now, as the one place exclusion+muting order is decided).

    THIS IS THE SINGLE SOURCE OF TRUTH for "what did KS4 actually see for
    this shank". ap_sorter.py calls this immediately before
    si.run_sorter(). quality_control.py MUST call this too - with the
    same cfg, health_report.json, and saturation_windows.json a given
    sort run used - before building a SortingAnalyzer against a shank's
    sorting output. Building the analyzer against a recording that was
    reconstructed differently (skipping the mute_before_sorting gate,
    applying exclusions in a different order, or re-deriving exclusions
    from scratch instead of reading health_report.json back) would
    silently compute waveforms/metrics against different samples than
    what KS4 actually clustered spikes on. Do not reimplement this
    exclusion+muting sequence independently elsewhere in the pipeline.

    Parameters
    ----------
    cfg               : merged config dict (reads sorting.min_channels_to_sort_shank
                         and saturation_detection.mute_before_sorting)
    shank_id          : shank identifier matching health_report.json / recording.split_by("group") keys
    shank_rec         : SpikeInterface recording for this one shank, BEFORE any
                         exclusion or muting (as returned by recording.split_by("group")[shank_id])
    health_report     : parsed health_report.json (from load_health_report())
    saturation_windows : parsed saturation_windows.json (from load_saturation_windows())

    Returns a dict with keys:
        status       : "PASS" | "SKIPPED" | "MISSING" | "SKIPPED_MIN_CHANNELS".
                       "SKIPPED_MIN_CHANNELS" is new relative to
                       get_shank_exclusion_ids()'s status values: it means
                       health_report.json said PASS, but re-applying its
                       recorded exclusions to THIS recording object
                       leaves fewer than min_channels_to_sort_shank
                       channels - i.e. the report is stale relative to
                       the recording actually being handled right now.
        recording    : reconstructed recording (clean + lazily muted if
                       applicable), or None unless status == "PASS".
        exclude_ids  : set[str] of channel IDs excluded per health_report.json.
        missing_ids  : set[str] of exclude_ids not present in shank_rec
                       (already gone upstream, or a stale report).
        muted        : bool - whether saturation muting was actually applied
                       (False if mute_before_sorting=False, or if status != "PASS").
        message      : human-readable one-line explanation, always present.
        shank_health : the raw per-shank health_report.json entry, or None
                       if status == "MISSING".
    """
    status, exclude_ids, shank_health = get_shank_exclusion_ids(health_report, shank_id)

    if status == "MISSING":
        msg = (f"No entry for shank {shank_id} in health_report.json - either a stale "
               f"report (probe/shank grouping changed) or health_check.py --report was "
               f"never run against this exact recording.")
        return {"status": "MISSING", "recording": None, "exclude_ids": set(),
                "missing_ids": set(), "muted": False, "message": msg, "shank_health": None}

    if status != "PASS":
        msg = (f"health_report.json status={status} for shank {shank_id} "
               f"({shank_health.get('skip_reason', 'no reason recorded')}).")
        return {"status": status, "recording": None, "exclude_ids": exclude_ids,
                "missing_ids": set(), "muted": False, "message": msg, "shank_health": shank_health}

    clean_rec, missing_ids = apply_health_report_exclusions(shank_rec, exclude_ids)
    if missing_ids:
        print(f"    Warning: shank {shank_id} health_report.json references "
              f"{len(missing_ids)} channel(s) not present in the current recording "
              f"(already excluded upstream, or the report is stale relative to this "
              f"recording): {sorted(missing_ids)}")

    min_chans = cfg.get("sorting", {}).get("min_channels_to_sort_shank", 2)
    if clean_rec.get_num_channels() < min_chans:
        msg = (f"After applying health_report.json exclusions, {clean_rec.get_num_channels()} "
               f"channel(s) remain on shank {shank_id} (< min_channels_to_sort_shank={min_chans}). "
               f"health_report.json recorded this shank as PASS with "
               f"{shank_health.get('viable_channels_remaining', '?')} viable channel(s) at "
               f"report time - this recording is likely stale relative to that report "
               f"(different config exclusions, or the report predates a change upstream). "
               f"Re-run health_check.py --report before sorting/assessing this shank.")
        return {"status": "SKIPPED_MIN_CHANNELS", "recording": None, "exclude_ids": exclude_ids,
                "missing_ids": missing_ids, "muted": False, "message": msg,
                "shank_health": shank_health}

    sat_cfg = cfg.get("saturation_detection", {})
    if sat_cfg.get("mute_before_sorting", True):
        clean_rec = mute_saturation_for_shank(clean_rec, shank_id, saturation_windows)
        muted = True
        msg = (f"Shank {shank_id}: {len(exclude_ids)} channel(s) excluded per "
               f"health_report.json; saturation muting applied (mute_before_sorting=True); "
               f"{clean_rec.get_num_channels()} channel(s) remain.")
    else:
        muted = False
        msg = (f"Shank {shank_id}: {len(exclude_ids)} channel(s) excluded per "
               f"health_report.json; saturation muting SKIPPED "
               f"(saturation_detection.mute_before_sorting=False) - flagged windows, if "
               f"any, are left in the data exactly as KS4 saw them.")

    return {"status": "PASS", "recording": clean_rec, "exclude_ids": exclude_ids,
            "missing_ids": missing_ids, "muted": muted, "message": msg,
            "shank_health": shank_health}


# ============================================================================
# PERIODIC DISCHARGE (~1 kHz) - STUB ONLY, NO FILTERING
# ============================================================================

class PeriodicDischargeNotCharacterisedError(NotImplementedError):
    """
    Raised by mute_periodic_discharge() unconditionally. This is a
    distinct exception type (not a bare NotImplementedError) so callers
    can catch it specifically if they want to distinguish "this artifact
    genuinely has no remediation yet" from "this function isn't written
    yet" in a try/except.
    """
    pass


def mute_periodic_discharge(recording, discharge_hits: dict, cfg: dict):
    """
    STUB. Deliberately unimplemented - see ARCHITECTURE.md Sec.7.

    health_check.detect_periodic_discharges() (--spectral-check) reports
    WHICH channels show a narrowband spectral peak and at what
    frequency/SNR, but does NOT establish whether the artifact is:
      (a) phase-locked / common-mode across channels on the same shank
          (consistent with a hardware/ground-loop origin -> candidate
          for common-average-style remediation), or
      (b) confined to individual channels (consistent with a biological
          or per-electrode origin -> remediation, if any, would need to
          be per-channel and is far more likely to also remove genuine
          signal).

    A comb/notch filter assumes continuous, stationary sinusoidal
    interference (e.g. 50/60 Hz mains hum). This artifact is described
    (ARCHITECTURE.md Sec.1, Sec.7) as a recurrent DISCHARGE - i.e.
    transient, broadband-at-onset events, not a stationary tone. Notching
    a transient in the frequency domain introduces time-domain ringing
    and will corrupt spike waveforms across a wider window than the
    artifact itself occupied. This is not a matter of tuning the filter
    better; the filter class is very likely wrong for this signal type
    (Widmann, Schroger & Maess, 2015, J Neurosci Methods, on the
    time-domain costs of frequency-domain filtering of non-stationary
    artifacts, is the standard reference for why "just notch it" is a
    bad default here - worth reading section 3 specifically before
    considering any filtering approach for this artifact).

    Suggested characterisation workflow BEFORE any filtering is written
    (not implemented here - this is what should happen in a separate,
    dedicated investigation, likely warranting its own short analysis
    script rather than being folded into this stub):
      1. Run health_check.py --report --spectral-check on 2-3 known-
         affected recordings (or a full animal/day if you don't yet
         know which days are affected).
      2. For each flagged channel/shank, check whether flagged channels
         cluster on ONE shank vs. spread across all 4 - a purely
         electrical/ground artifact affecting the headstage should show
         up on every shank; a single-shank cluster points toward a
         connector or per-shank wiring issue instead.
      3. Cross-correlate the raw (unfiltered) traces of flagged channels
         at zero lag during a flagged window. High, near-instantaneous
         correlation across channels on the same shank is consistent
         with common-mode pickup (already partially handled by KS4's
         internal CAR, do_CAR=True per config - worth checking whether
         the artifact survives CAR before building anything new).
      4. If genuinely common-mode: candidate remediation is a targeted,
         windowed common-average correction restricted to the flagged
         time ranges, NOT a persistent notch filter - the artifact
         appears to be transient (a "discharge"), not continuous.
      5. If per-channel: treat as a bad-channel/bad-window problem,
         likely folded into the existing bad-channel exclusion /
         saturation-style muting machinery rather than a new filter
         class - re-use the pattern already established in this module,
         don't invent a third mechanism.
      6. Only after (2)-(5) narrow down the mechanism should any
         filtering code be written, and it should go in this function
         (mute_periodic_discharge), replacing this stub.

    This function intentionally does nothing useful yet. It exists so
    that any future caller that reaches for "just mute the discharge
    too, it's basically the same as saturation" gets a loud, explicit
    stop with a pointer to the open question, rather than the call
    silently no-op'ing (which would be far worse: work would proceed as
    if the artifact had been handled when it had not).
    """
    raise PeriodicDischargeNotCharacterisedError(
        "Periodic discharge (~1 kHz) filtering is NOT implemented. The "
        "spatial pattern of this artifact (phase-locked/common-mode "
        "across a shank vs. per-channel) has not yet been characterised "
        "- see ARCHITECTURE.md Sec.7 and this function's docstring for "
        "the suggested characterisation workflow. Implementing a filter "
        "before that characterisation risks corrupting genuine spike "
        "waveforms (frequency-domain filtering of a transient artifact "
        "causes time-domain ringing) or missing the artifact's true "
        "extent (if it is common-mode and only muted per-channel)."
    )


# ============================================================================
# SELF-CHECK (module-local verification, per ARCHITECTURE.md Sec.8)
# ============================================================================

def self_check(cfg: dict, day_output_dir: str = None) -> list:
    """
    Cheap, read-only checks for this module's own responsibilities. Does
    NOT read raw recording data or construct any SpikeInterface wrapper
    against real traces - just verifies the saturation_windows.json
    contract can be found and parsed, and that config keys this module
    depends on are present.

    If day_output_dir is given, additionally attempts to load and
    validate that specific file's structure. Without it, only checks
    that cfg has the keys this module reads.

    Returns list of (level, message) tuples; does not raise on its own.
    """
    results = []

    def check(level, msg):
        results.append((level, msg))
        print(f"  [{level}] {msg}")

    print(f"\n{'='*70}\nartifact_cleaning.py self-check (env={cfg.get('_env', '?')})\n{'='*70}")

    sat_cfg = cfg.get("saturation_detection", {})
    if sat_cfg:
        check("PASS", f"saturation_detection config present "
                       f"(enabled={sat_cfg.get('enabled')}, "
                       f"mute_before_sorting={sat_cfg.get('mute_before_sorting')})")
    else:
        check("FAIL", "cfg['saturation_detection'] missing or empty")

    if not sat_cfg.get("mute_before_sorting", True):
        check("WARN", "mute_before_sorting=False in config - this module's muting wrapper "
                       "would be constructed but have no effect if wired into a sorter "
                       "pipeline that still calls it; confirm the caller actually checks "
                       "this flag before invoking mute_saturation_for_shank().")

    if day_output_dir is not None:
        path = os.path.join(day_output_dir, "saturation_windows.json")
        if not os.path.exists(path):
            check("FAIL", f"saturation_windows.json not found at {path} - "
                          f"run health_check.py --report for this day first")
        else:
            try:
                with open(path) as f:
                    data = json.load(f)
                if "sampling_frequency" not in data or "shanks" not in data:
                    check("FAIL", f"saturation_windows.json at {path} missing required "
                                  f"top-level keys ('sampling_frequency', 'shanks')")
                else:
                    n_shanks = len(data["shanks"])
                    n_chans_flagged = sum(len(v) for v in data["shanks"].values())
                    n_windows = sum(
                        len(wins) for shank in data["shanks"].values() for wins in shank.values()
                    )
                    check("PASS", f"saturation_windows.json loaded: {n_shanks} shank(s), "
                                  f"{n_chans_flagged} channel(s) with flagged windows, "
                                  f"{n_windows} window(s) total")
            except Exception as e:
                check("FAIL", f"saturation_windows.json at {path} failed to parse: {e}")
    else:
        check("PASS", "no day_output_dir given - skipped saturation_windows.json check "
                       "(pass --day-output-dir to validate a specific day)")

    n_fail = sum(1 for level, _ in results if level == "FAIL")
    n_warn = sum(1 for level, _ in results if level == "WARN")
    print(f"\n{n_fail} FAIL, {n_warn} WARN, "
          f"{sum(1 for l, _ in results if l == 'PASS')} PASS\n")
    return results


if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from src.config_loader import load_config
    from src.io_utils import get_day_output_dir

    parser = argparse.ArgumentParser(
        description="Debug/control tools for artifact_cleaning.py.",
        epilog="""
Examples:
  # Resolve the day output dir automatically from animal/date, matching
  # io_utils.py --check-day and health_check.py --report:
  python artifact_cleaning.py --env local --check --animal 213868 --date 20231105

  # Same, but for a single session's output dir (matches health_check.py
  # --report --session-name for the same animal/date):
  python artifact_cleaning.py --env local --check --animal 213868 --date 20231105 --session-name 20231105_002

  # Or point directly at a day output directory (e.g. if it lives outside
  # the normal animal/Raw_data/date layout, or you already have the path):
  python artifact_cleaning.py --env local --check --day-output-dir "D:/spikesorting_output/213868/Raw_data/20231105"
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--env", required=True, choices=["local", "fox", "biotin"])
    parser.add_argument("--check", action="store_true",
                         help="Run self_check(): cheap, read-only validation of config and, if "
                              "a day is specified (via --animal/--date or --day-output-dir), "
                              "saturation_windows.json structure for that day.")

    day_group = parser.add_mutually_exclusive_group()
    day_group.add_argument("--day-output-dir", default=None,
                            help="Day output directory to validate saturation_windows.json "
                                 "against directly. Mutually exclusive with --animal/--date.")
    day_group.add_argument("--animal", default=None,
                            help="Animal ID. Combine with --date; resolved to a day output "
                                 "directory via io_utils.get_day_output_dir(), the same "
                                 "resolution io_utils.py and health_check.py use - so this "
                                 "points at the same folder --report would have written "
                                 "saturation_windows.json into for this animal/day. Mutually "
                                 "exclusive with --day-output-dir.")

    parser.add_argument("--date", default=None,
                         help="Date string YYYYMMDD. Required if --animal is given.")
    parser.add_argument("--session-name", default=None,
                         help="Restrict to a single session's output directory (e.g. "
                              "'20231105_002') rather than the day-level one - matches "
                              "health_check.py --report --session-name for the same "
                              "animal/date. Only used with --animal/--date, not with "
                              "--day-output-dir (if you already have the exact path, "
                              "including any session subfolder, pass it directly).")
    args = parser.parse_args()

    if args.animal and not args.date:
        parser.error("--animal requires --date.")
    if args.date and not args.animal:
        parser.error("--date requires --animal.")
    if args.session_name and not args.animal:
        parser.error("--session-name requires --animal/--date.")

    if not args.check:
        # --check is currently the ONLY action this CLI supports - this
        # module has no "run" or "mute" subcommand of its own (muting
        # happens by calling mute_saturation_for_shank() from ap_sorter.py
        # once that module exists - see ARCHITECTURE.md Sec.9). Erring
        # explicitly here rather than silently printing the full help text,
        # since forgetting --check (e.g. running with just --animal/--date)
        # previously looked like the command had failed for an unclear
        # reason rather than "you didn't ask it to do anything."
        parser.error(
            "No action requested. --check is currently the only supported "
            "action (self_check: cheap, read-only validation of config, "
            "and optionally a specific day's saturation_windows.json). "
            "Add --check to your command, e.g.:\n"
            "  python artifact_cleaning.py --env {env} --check --animal {animal} --date {date}"
            .format(env=args.env, animal=args.animal or "ANIMAL_ID", date=args.date or "YYYYMMDD")
        )

    cfg = load_config(args.env)

    resolved_day_output_dir = args.day_output_dir
    if args.animal and args.date:
        # Same resolution io_utils.py/health_check.py use, so this points
        # at exactly the folder --report would have written
        # saturation_windows.json into for this animal/day (or
        # animal/day/session, if --session-name is given - matches
        # health_check.py --report --session-name's individual-session
        # output path).
        resolved_day_output_dir = get_day_output_dir(
            cfg, args.animal, args.date, session_name=args.session_name)

    results = self_check(cfg, day_output_dir=resolved_day_output_dir)
    if any(level == "FAIL" for level, _ in results):
        raise SystemExit(1)
