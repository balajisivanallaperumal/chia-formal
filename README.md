# CHIA-Formal: Mutation-Guided Assertion Refinement

An agentic loop that synthesizes SystemVerilog Assertions with an LLM, measures
how many injected faults they actually catch, and feeds the surviving faults
back to the model until the assertion suite catches everything.

The model only ever **proposes**. Every property is admitted or discarded by a
solver, and every number in every report is measured by a solver run.

---

## The loop

```
        Gemini proposes assertions
                  |
                  v
        SOUNDNESS GATE  --  does each one hold on the CORRECT design?
                  |         (anything refuted here is discarded)
                  v
        Inject single-line faults into the RTL (mutants)
                  |
                  v
        Model-check every assertion against every mutant
                  |
        survivors? --yes--> return them to the model as RTL diffs --+
                  |                                                 |
                  no                                                |
                  v                                                 |
          measured kill rate   <---------------------------------- +
```

Three gates keep the score honest, and each exists because the loop fails
without it:

| Gate | Failure it prevents |
|---|---|
| **Soundness** | A property false on the correct design is refuted by *every* mutant, scoring a perfect 100% while meaning nothing. |
| **Equivalence** | A mutant provably indistinguishable from the original is not a fault; counting it against the suite understates coverage. |
| **Vacuity** | A property whose trigger is unreachable passes for free and detects nothing. |

---

## Reproducing the paper

Everything goes through `./reproduce.sh`:

| Command | Needs | Time | What it does |
|---|---|---|---|
| `./reproduce.sh tables` | Python only | seconds | Regenerates every table and number in the paper from the committed JSON. |
| `./reproduce.sh setup` | network | ~15 min | Downloads oss-cad-suite 2026-08-22 (the version used) into `tools/` and the benchmark RTL at pinned commits. |
| `./reproduce.sh reverify [module ...]` | toolchain | ~6 min for 3 small modules; 1.5 to 2 h for all 22 | Re-proves every committed suite from scratch in `outputs/reproduce/`, then checks the full-bank results against the committed ones. No LLM or credentials needed. |
| `./reproduce.sh loop` | toolchain + Vertex AI | a few min | Runs the loop once on the arbiter reference design. |
| `./reproduce.sh cluster` | toolchain + Vertex AI | a few min | The same, on a CHIA cluster (`chia up`, `chia job submit`, `chia down`). |

A quick check: `./reproduce.sh reverify arbiter IBuf cv32e40p_popcnt`.
`reverify` never overwrites the committed results. It exits non-zero if any
full-bank result differs.

The Rocket and BOOM modules are Chipyard-generated Verilog, which a plain clone
of Chipyard does not contain. The 18 generated files used in the paper are
vendored under `vendor/` at their original paths, and `setup` copies them into
place, so no Chipyard build is needed. All paths in the result files are
relative to the repository root.

## Quickstart

```bash
# 1. Toolchain and credentials
source activate_chia.sh          # expects .env.local, see "Credentials" below

# 2. Benchmark RTL (~2 GB, pinned to exact upstream commits)
./scripts/fetch_benchmarks.sh --shallow

# 3. Run the loop locally
python examples/formal_verification/formal_verification_loop.py \
    --dut examples/formal_verification/designs/arbiter.v --top arbiter --seeds 3
```

On a cluster, the (property, mutant) grading grid — one independent solver call
per pair, and the dominant cost — is dispatched to workers advertising the
`formal` resource:

```bash
chia up -y examples/formal_verification/cluster.yaml
chia job submit --working-dir . -- \
    python examples/formal_verification/formal_verification_loop.py \
        --dut examples/formal_verification/designs/arbiter.v --top arbiter --seeds 3 --dispatch ray
chia down -y examples/formal_verification/cluster.yaml
```

or `./examples/formal_verification/submit_formal_loop.sh examples/formal_verification/designs/arbiter.v arbiter 3`
to do all three.

Output lands in `outputs/refinement/<module>/refinement_eval.json`: the per-iteration
trajectory, the final assertions, and an **independent re-verification** that
re-proves every property and recomputes the kill rate from a fresh mutant bank.
The run fails loudly if that re-check disagrees with the loop.

---

## Layout

Following the upstream CHIA convention, reusable machinery lives in the
framework package and the loop lives under `examples/`.

```
.
├── reproduce.sh                  one entry point: tables, setup, reverify, loop, cluster
├── chia/                         CHIA framework (upstream), plus:
│   └── formal/                   this project's subsystem
│       ├── refinement_loop.py        the closed loop
│       ├── empirical_metrics.py      measurement, the three gates, cluster dispatch
│       ├── multifile_proof.py        package-aware BMC; local and ChiaFunction entry points
│       ├── mutation_node.py          single-fault mutant bank + equivalence miters
│       ├── vacuity_node.py           antecedent reachability (vacuity gate)
│       ├── vertex_ai_synthesizer.py  Gemini property synthesis
│       ├── native_assertions.py      in-tree (human) assertion inventory and repair
│       └── bug_hunt.py               apply a hardened suite to suspect RTL
├── examples/formal_verification/ the CHIA loop
│   ├── formal_verification_loop.py   driver: refine, verify, record
│   ├── human_vs_loop.py              grade ibex's in-tree assertions vs the loop
│   ├── bug_hunt_loop.py              harden on clean RTL, apply to suspect RTL
│   ├── cluster.yaml                  worker pool advertising the "formal" resource
│   ├── submit_formal_loop.sh         chia up -> job submit -> down
│   ├── env.yml                       worker environment
│   ├── designs/                      reference designs (arbiter.v, fifo.v)
│   └── soc_cores/                    benchmark RTL, fetched by setup (not committed)
├── scripts/
│   ├── fetch_benchmarks.sh           pinned benchmark checkout
│   ├── screen_modules.py             pick modules the harness can check
│   ├── reverify_results.py           re-grade saved suites under one protocol
│   └── make_paper_tables.py          measured JSON -> LaTeX
├── results/                      the paper's results (committed)
│   ├── PAPER_CLAIMS.md               every number in the paper -> its source
│   ├── refinement/                   main results: 22 modules, 41 runs
│   ├── resample_ablation/            rotating-sample ablation
│   ├── human_baseline/               ibex human baseline
│   └── bug_hunt/                     Hack@DAC bug hunt (CWE-1240)
├── outputs/                      new runs land here (gitignored)
└── vendor/                       Chipyard-generated RTL for the Rocket and BOOM modules
```

---

## Credentials

Nothing is read from the repository. Put your settings in `.env.local`
(untracked):

```bash
export GOOGLE_CLOUD_PROJECT="your-project"
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/your-key.json"
```

or run `gcloud auth application-default login`.

---

## Results

All results in the paper are committed here and every number is listed with its
source in `results/PAPER_CLAIMS.md`. See "Reproducing the paper" above for how to
regenerate and re-prove them.

Headline figures (22 modules from Rocket, BOOM, CV32E40P and SERV plus two
reference designs, 41 runs):

| | |
|---|---|
| Properties proposed | 1849 |
| ... refuted by the correct design | 673 (36%) |
| ... duplicates | 416 (22%) |
| Sampled kill rate, first iteration -> refined | mean 39.1% -> 73.9% |
| Full-bank kill rate | 0.3% to 100%, mean 54.5% |
| Sampled vs full-bank gap | mean 19.4 points, max 70 (`cv32e40p_alu`: 81.0% vs 11.0%) |
| ibex in-tree assertions gradeable | 14 of 185 |
| Median wall time per run | 1.2 min |

The gap is the main finding: a suite tuned on a 24-mutant sample scores much
lower on the full bank, and rotating the sample each round does not close it
(`results/resample_ablation/`).

---

## Known limitations

Stated plainly, because the point of this project is to not overstate results.

- **Bounded model checking only.** `k`-induction is not run. A passing property
  holds up to the configured depth and says nothing beyond it.
- **Combinational modules are checked at depth 1.** With no state, that check
  is exhaustive rather than bounded.
- **Bug hunting is two-phase.** The loop assumes the design under test is
  correct — that is what the soundness gate enforces. To hunt bugs, harden a
  suite against a clean reference first, then apply it to the suspect version
  (`chia/formal/bug_hunt.py`, `examples/formal_verification/bug_hunt_loop.py`).
- **Evaluated on small modules.** Results are for designs of tens to a few
  hundred lines.
- **Commercial tools not used.** Everything runs on SymbiYosys, Yosys and Z3.

### Invalid prior results

Reports and `out_*` directories produced before 2026-09-21 are **invalid** and
excluded from this repository. The harness emitted assertions inside
`` `ifdef FORMAL `` but never defined `FORMAL`, so the preprocessor stripped
every property before the solver ran: `assert(1'b0)` reported
`PASS (Formally Proven)`. Their kill-rate, vacuity and pruning figures were
constants in the report generator rather than measurements. They need
regenerating, not citing.

---

## Licence

BSD-3-Clause. Built on [CHIA](https://github.com/ucb-bar/chia) (UC Berkeley
Architecture Research).
