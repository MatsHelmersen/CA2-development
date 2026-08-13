# Master prompt — paste at the start of each module conversation

Copy this into a new conversation inside the `ephys_pipeline` Project, filling
in the two bracketed lines. Everything else stays the same each time.

---

I'm continuing work on my modular electrophysiology pipeline (`ephys_pipeline`).
Before doing anything else, read `ARCHITECTURE.md` in this Project's knowledge
— it defines the module boundaries, the config schema, and the interface
contracts (function signatures, expected inputs/outputs) that every module
must stay consistent with. Also check for any other relevant files in
Project knowledge (e.g. `config.yaml`, existing module source) before writing
new code — don't re-derive conventions from scratch if they're already
documented.

**Module for this conversation:** [e.g. `lfp_extractor.py`]

**Goal for this session:** [e.g. "Migrate the bandpass filtering and
shorted-channel QC logic from depth_resolved_lfp.py into this module,
matching the io_utils.py loading interface described in ARCHITECTURE.md"]

Ground rules for this session:
- If what I'm asking for would require changing a function signature, config
  key, or file format that another module depends on (per ARCHITECTURE.md),
  stop and flag it before proceeding — don't silently change a shared
  contract from within a single-module conversation.
- If ARCHITECTURE.md is ambiguous or silent on something this module needs,
  ask rather than guessing, and propose the addition so I can carry it back
  into ARCHITECTURE.md.
- Match the coding conventions already established elsewhere in the
  pipeline (see ARCHITECTURE.md's "Conventions" section) rather than
  introducing a new style for this module.
- At the end of the session, summarize anything that should be added to or
  changed in ARCHITECTURE.md, so I can update the Project knowledge before
  the next module conversation.
