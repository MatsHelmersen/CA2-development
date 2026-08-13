#!/bin/bash
# sort_one_day.sbatch
#
# Submits ONE Kilosort4 sorting run (one animal/day) as a Slurm job on
# Fox's GPU (accel) partition. This is the unit of work for batch mode -
# for many animal/days, submit many of these (a "job array"), one per
# unit of work, so they run in parallel across Fox's GPU nodes instead of
# looping through them one at a time in a single process.
#
# Usage:
#   sbatch sort_one_day.sbatch <animal_id> <date>
#
# Check status:
#   squeue --me
# Cancel:
#   scancel <job_id>

#SBATCH --account=ec31
#SBATCH --job-name=ks4_sort
#SBATCH --partition=accel
#SBATCH --gpus=1
#SBATCH --cpus-per-task=8
#SBATCH --mem-per-cpu=4G
#SBATCH --time=04:00:00          # adjust once you know a realistic runtime for one day's data
#SBATCH --output=/fp/projects01/ec31/boccara-group/logs/sort_%j.out

set -o errexit   # stop the script on any error, rather than continuing silently
set -o nounset   # error if a variable is used before being set

module purge     # start from a clean environment - avoids picking up stray loaded modules

# activate your conda/venv environment (adjust path to wherever
# environment.yml gets installed - conda env create -f environment.yml once,
# then this activates it for every job)
source ~/miniconda3/etc/profile.d/conda.sh
conda activate ephys_pipeline

ANIMAL_ID=$1
DATE=$2

cd /fp/projects01/ec31/boccara-group/ephys_pipeline
python run_pipeline.py sort --env fox --animal "${ANIMAL_ID}" --date "${DATE}"

# NOTE: this script does not yet exist as a job ARRAY (--array=...), which
# is the actual mechanism for submitting many animal/days at once without
# writing one script per day. That's the next step once run_pipeline.py
# has a --animal/--date interface that processes exactly one unit of work
# per invocation - worth building that interface deliberately for this.
