# Mutation-graded assertion refinement loop

A CHIA loop that asks an LLM (Gemini 2.5 Flash on Vertex AI) for SystemVerilog
Assertions, grades them against a bank of single-line fault mutants, and sends
the surviving mutants back to the model as RTL diffs. The model only proposes.
Every property is kept or discarded by the solver (SymbiYosys with Yosys and Z3).

Each round runs three checks:

- **Soundness.** A property must hold on the correct design, or it is discarded.
- **Equivalence.** Mutants that fail to elaborate, or are provably equivalent to
  the original, are removed from the denominator.
- **Vacuity.** An assertion's trigger must be reachable within the check depth.

The expensive part is the (property, mutant) grading grid: one independent
solver call per pair. With `--dispatch ray` that grid is sent to CHIA workers
advertising the `formal` resource (see `cluster.yaml`).

## Files

```
formal_verification_loop.py   the loop: refine, re-verify, write the JSON record
human_vs_loop.py              grade ibex's in-tree assertions and the loop on the same mutants
bug_hunt_loop.py              harden a suite on clean RTL, then apply it to suspect RTL
cluster.yaml                  CHIA cluster: head + one worker pool with the "formal" resource
submit_formal_loop.sh         chia up -> chia job submit -> chia down, in one command
env.yml                       worker environment
```

The framework code the loop uses is in `chia/formal/`. See the top-level README
for the full layout.

## Setup

From the repository root:

```bash
source activate_chia.sh                 # conda env, oss-cad-suite on PATH, .env.local
./scripts/fetch_benchmarks.sh --shallow # benchmark RTL at pinned commits (~2 GB)
```

`.env.local` needs `GOOGLE_CLOUD_PROJECT` and Vertex AI credentials. It is never
committed.

## Run on a CHIA cluster

```bash
# 1. bring up the cluster
chia up -y examples/formal_verification/cluster.yaml

# 2. check that the formal workers are up
chia status

# 3. submit the loop; --dispatch ray sends grading to the workers
chia job submit --working-dir . -- \
    python examples/formal_verification/formal_verification_loop.py \
        --dut examples/formal_verification/designs/arbiter.v --top arbiter --seeds 3 --dispatch ray

# 4. follow the job
chia job list

# 5. tear the cluster down
chia down -y examples/formal_verification/cluster.yaml
```

`./examples/formal_verification/submit_formal_loop.sh examples/formal_verification/designs/arbiter.v arbiter 3`
runs steps 1, 3 and 5 and always tears the cluster down on exit.

## Run locally

```bash
python examples/formal_verification/formal_verification_loop.py \
    --dut examples/formal_verification/designs/arbiter.v --top arbiter --seeds 3
```

Useful options: `--budget` (mutants in the tuning sample, default 24; `0` uses
the whole bank), `--max-iterations`, `--depth` (BMC depth; combinational modules
are checked at depth 1, which is exhaustive), `--workers`, `--out`.

## Output

`<out>/<top>/refinement_eval.json` (default `outputs/refinement/`) holds, per seed:

- every iteration: properties proposed, rejected as unsound, duplicate or bad
  syntax, admitted, and the kill rate on the sample;
- the final properties;
- an independent re-verification that re-proves every property and recomputes
  the kill rate on a fresh bank; the run fails if it disagrees with the loop;
- the kill rate on the full mutant bank, which is the number to report.

## Human baseline (ibex)

```bash
python examples/formal_verification/human_vs_loop.py --out outputs/human_baseline
```

This prepares the ibex tree, inventories its in-tree assertions (how many
compile to nothing under Yosys, how many need repair), and grades the gradeable
ones and the loop's suite on the same mutants.

## Bug hunting on suspect RTL

The soundness check assumes the design is correct, so the loop cannot be run on
buggy RTL directly. Harden on a clean reference first, then apply the suite:

```bash
python examples/formal_verification/bug_hunt_loop.py \
    --reference clean/fuse_mem.sv --suspect buggy/fuse_mem.sv --top fuse_mem \
    --out outputs/bug_hunt
```

For Hack@DAC 2021 the clean and buggy files come from the `fix_cwe_<n>` and
`main` branches of `soc_cores/hackatdac21`. A refutation on the suspect is a
finding with a counterexample. No findings is not proof of absence.

## Limits

Bounded model checking only (no k-induction) outside combinational modules.
Evaluated on modules of tens to a few hundred lines.
