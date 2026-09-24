#!/usr/bin/env bash
# Bring up a CHIA cluster, run the refinement loop on it, tear it down.
#
#   ./examples/formal_verification/submit_formal_loop.sh examples/formal_verification/designs/arbiter.v arbiter [seeds]
#
# The loop runs on the head node; the (property, mutant) grading grid is
# dispatched to the formal_worker pool declared in cluster.yaml.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

DUT="${1:?usage: submit_formal_loop.sh <dut.v> <top> [seeds]}"
TOP="${2:?usage: submit_formal_loop.sh <dut.v> <top> [seeds]}"
SEEDS="${3:-3}"
CFG="examples/formal_verification/cluster.yaml"

if [ -z "${GOOGLE_CLOUD_PROJECT:-}" ]; then
    echo "GOOGLE_CLOUD_PROJECT is unset; property synthesis will fail." >&2
    echo "Set it in .env.local and re-source activate_chia.sh." >&2
    exit 1
fi

cleanup() { chia down -y "$CFG" || true; }
trap cleanup EXIT

chia up -y "$CFG"
chia job submit --working-dir . -- \
    python examples/formal_verification/formal_verification_loop.py \
        --dut "$DUT" --top "$TOP" --seeds "$SEEDS" --dispatch ray

echo "Results: outputs/refinement/${TOP}/refinement_eval.json"
