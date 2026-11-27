#!/usr/bin/env bash
# One repository's gate: checks/<repo>.sh in the factory repository. fx runs it inside the job's clone of
# that repository after every build; reviewers only run when every changed repository's script exits 0.
# It must be non-interactive, must not rewrite files (no --fix, no snapshot updates) and must exit
# non-zero on any failure.
#
# FACTORY_TODO: /auto-factory:code init replaces this file with the repository's real checks
# (install if needed, lint, type check, tests, build) and deletes this line.
# Tests run in the runner's parallel mode when it has one, one database per process named from FACTORY_DB, behind a
# switch at the top (PARALLEL=1) that selftest proved (init.md, "Tests in parallel").
# Each stage starts with echo "::fx-stage <name> $(date +%s)", and the e2e tests are those in $FACTORY_E2E_TESTS when it
# is set (init.md, "Stages" and "The e2e map").
#
# Env from fx: FACTORY_JOB (job id), FACTORY_PORT (this repository's port in the job), FACTORY_DB (its database).
# FACTORY_ONLY_STAGE (fx checks --only: run only these comma-separated stages, with the setup they need) and
# FACTORY_TEST_ARGS (their test runner's arguments, shell-quoted: eval "TEST_ARGS=($FACTORY_TEST_ARGS)"; init.md, "Stages").
# FACTORY_E2E_TESTS (a file: the e2e test files the job's change reaches, one per line, maybe none) or FACTORY_E2E_FULL=1
# (the whole suite), FACTORY_E2E_WHY, FACTORY_CHANGED_FILES (a file: the changed paths) and FACTORY_BASE_REF.
# With lanes in factory.json also FACTORY_LANE_<PATH> for each folder a lane protects: where this job reads that folder of the factory
# repository (its own copy as committed, unless it holds the lane). Read it instead of the folder's own path.
set -euo pipefail
echo "$(basename "$0") in the factory repository's checks/ is not configured yet: run /auto-factory:code init" >&2
exit 1
