# ephys_pipeline

Modular electrophysiology processing pipeline: bad-channel/saturation
triage, Kilosort4 spike sorting, post-sort unit assessment, LFP extraction.
Runs locally (Windows) or on Fox (UiO HPC, Slurm).

See `ARCHITECTURE.md` (in the Claude Project knowledge, not in this repo
yet - add it here too once stable) for module boundaries and interface
contracts.

## Setup

```bash
conda env create -f environment.yml
conda activate ephys_pipeline
# then install torch separately - see the comment in environment.yml
```

Copy the probe JSON (corrected geometry) into `probe/`.
Configs are already split: `config/base.yaml` (parameters) +
`config/local.yaml` / `config/fox.yaml` (paths, select via `--env`).

## Build status

Tracking progress migrating logic out of the original monolithic
`sort_batch.py` into `src/` modules. Update this table as each module lands.

| Module | Status | Depends on | Tested against |
|---|---|---|---|
| `io_utils.py` | not started | - | - |
| `health_check.py` | not started | io_utils | - |
| `artifact_cleaning.py` | not started | health_check | - |
| `ap_sorter.py` | not started | io_utils, health_check, artifact_cleaning | - |
| `quality_control.py` | not started | ap_sorter | - |
| `lfp_extractor.py` | not started | io_utils only | - |
| `run_pipeline.py` | not started | all of the above | - |

## Testing convention

Each module should be runnable/testable in isolation before the next one
is built on top of it - see `tests/` for one test file per module. Prefer
testing against one real (small) day of data locally before trusting a
module on the cluster or at batch scale.
