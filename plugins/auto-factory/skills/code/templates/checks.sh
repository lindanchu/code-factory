#!/usr/bin/env bash
# One repository's gate: checks/<repo>.sh in the factory repository. fx runs it inside the job's clone of
# that repository after every build; reviewers only run when every changed repository's script exits 0.
# It must be non-interactive, must not rewrite files (no --fix, no snapshot updates) and must exit
# non-zero on any failure.
#
# FACTORY_TODO: /auto-factory:code init replaces this file with the repository's real checks
# (install if needed, lint, type check, tests, build) and deletes this line.
#
# Env from fx: FACTORY_JOB (job id), FACTORY_PORT (this repository's port in the job), FACTORY_DB (its database).
# With lanes in factory.json also FACTORY_LANE_<PATH> for each folder a lane protects: where this job reads that folder of the factory
# repository (its own copy as committed, unless it holds the lane). Read it instead of the folder's own path.
set -euo pipefail
echo "$(basename "$0") in the factory repository's checks/ is not configured yet: run /auto-factory:code init" >&2
exit 1
