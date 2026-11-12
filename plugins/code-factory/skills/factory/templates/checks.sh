#!/usr/bin/env bash
# The gate. fx runs this inside the job's clone of the project after every build; reviewers only
# run when it exits 0. It must be non-interactive, must not rewrite files (no --fix,
# no snapshot updates) and must exit non-zero on any failure.
#
# FACTORY_TODO: /factory init replaces this file with the repository's real checks
# (install if needed, lint, type check, tests, build) and deletes this line.
#
# Env from fx: FACTORY_JOB (job id), FACTORY_PORT (the job's base port).
set -euo pipefail
echo "checks.sh in the factory repository is not configured yet: run /factory init" >&2
exit 1
