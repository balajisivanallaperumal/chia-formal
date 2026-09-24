#!/usr/bin/env python3
"""CHIA loop: mutation-guided refinement of LLM-synthesized assertions.

An LLM proposes SystemVerilog Assertions, a model checker grades them against a
bank of single-fault mutants, and the mutants that survive are returned to the
model until the suite catches them all. The model only proposes; every verdict
comes from the solver.

Runs on the head node and dispatches the (property, mutant) grid to workers
advertising the ``formal`` resource. That grid is one independent solver call
per pair and dominates the runtime, so it is what the cluster is for.

    # local
    python examples/formal_verification/formal_verification_loop.py \
        --dut examples/formal_verification/designs/arbiter.v --top arbiter --seeds 3

    # on a cluster
    chia up -y examples/formal_verification/cluster.yaml
    chia job submit --working-dir . -- \
        python examples/formal_verification/formal_verification_loop.py \
        --dut examples/formal_verification/designs/arbiter.v --top arbiter --seeds 3 --dispatch ray

Every figure written to the JSON record is re-derived after the loop finishes,
independently of the loop's own bookkeeping, and a disagreement is a hard
failure.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from chia.formal.multifile_proof import OSS_CAD_BIN, check_formal_proof  # noqa: E402
from chia.formal.empirical_metrics import (  # noqa: E402
    detect_clock_reset,
    ensure_formal_cluster,
    measure_mutation_coverage,
    measure_vacuity,
    solve_minimal_basis,
)
from chia.formal.refinement_loop import refine_until_killed  # noqa: E402
from chia.formal.state_def import Verdict  # noqa: E402

log = logging.getLogger("refinement-eval")


def independent_verification(dut, top, rtl, properties, clk, rst_n, depth, workers,
                             budget=0):
    """Re-derive the headline numbers without trusting the loop's bookkeeping.

    Re-proves every property against golden RTL and recomputes coverage from a
    fresh mutant bank. A discrepancy means the loop is miscounting, which is
    exactly the failure this project exists to catch.

    Args:
        budget: Mutant cap. Must match the loop's, or the two are grading
            against different populations and any comparison between them is
            meaningless -- the check would report a mismatch that says nothing
            about the loop's arithmetic.
    """
    soundness = []
    for pid, code in properties:
        verdict, detail, _, _ = check_formal_proof(
            dut, top, code, clk, rst_n, depth)
        soundness.append({
            "property_id": pid,
            "verdict": verdict.value,
            "sound": verdict is Verdict.PASS,
            "detail": detail[:200],
            "code": " ".join(code.splitlines()[-1].split()),
        })
    sound = [(s["property_id"], code) for s, (_, code)
             in zip(soundness, properties) if s["sound"]]

    coverage = measure_mutation_coverage(
        dut_path=dut, module_name=top, golden_rtl=rtl, properties=sound,
        proof_fn=check_formal_proof, clk=clk, rst_n=rst_n, bmc_depth=depth,
        budget=budget, workers=workers, oss_cad_bin=OSS_CAD_BIN,
        classify_equivalents=True)
    basis = solve_minimal_basis(coverage)
    vacuity = measure_vacuity(
        dut, top, rtl, "\n".join(c for _, c in sound),
        check_formal_proof, clk, rst_n, depth)

    return {
        "properties_checked": len(properties),
        "properties_sound": len(sound),
        "soundness": soundness,
        "kill_rate_percent": coverage.kill_rate,
        "kill_rate": coverage.kill_rate_str,
        "generated": coverage.generated,
        "stillborn": coverage.stillborn,
        "equivalent": coverage.equivalent,
        "killed_equivalent": coverage.killed_equivalent,
        "active": coverage.active,
        "killed": coverage.killed,
        "survivors": [
            {"line": m.line_no, "operator": m.operator, "original": m.original,
             "mutated": m.mutated}
            for m in coverage.sampled_mutants if m.mid in set(coverage.survivors)
        ],
        "minimal_basis": {
            "candidates": basis.candidates,
            "selected": len(basis.selected_ids),
            "redundancy_percent": basis.redundancy_pct,
            "coverage_preserved": basis.coverage_preserved,
        },
        "vacuity": {
            "antecedents": vacuity.antecedents,
            "reachable": vacuity.reachable,
            "vacuous": vacuity.vacuous,
            "percent": vacuity.vacuity_pct,
            "status": vacuity.status,
            "unreachable_antecedents": vacuity.unreached,
            "error": vacuity.error,
        },
    }


def _write_record(out_dir, args, dut, runs):
    """Write the module record, reflecting however many seeds have finished."""
    for r in runs:
        bodies = [fp["assertion"] for fp in r["final_properties"]]
        r["suite_size"] = len(bodies)
        r["suite_distinct"] = len(set(bodies))

    rates = [r["final_kill_rate"] for r in runs if r["final_kill_rate"] is not None]
    record = {
        "module": args.top,
        "source": str(dut),
        "bmc_depth": args.depth,
        "seeds_requested": args.seeds,
        "seeds_completed": len(runs),
        "seeds": args.seeds,
        "converged_seeds": sum(1 for r in runs if r["converged"]),
        "kill_rate_min": min(rates) if rates else None,
        "kill_rate_max": max(rates) if rates else None,
        "kill_rate_mean": sum(rates) / len(rates) if rates else None,
        "all_verifications_agree": all(r["verification_agrees"] for r in runs),
        "runs": runs,
    }
    (out_dir / "refinement_eval.json").write_text(json.dumps(record, indent=2))
    return record


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dut", required=True)
    ap.add_argument("--top", required=True)
    ap.add_argument("--clk", default="clk")
    ap.add_argument("--rst_n", default="rst_n")
    ap.add_argument("--depth", type=int, default=16)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--max-iterations", type=int, default=6)
    ap.add_argument("--budget", type=int, default=0,
                    help="mutant cap; 0 runs the whole bank")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--out", default="outputs/refinement")
    ap.add_argument("--dispatch", choices=("local", "ray"), default="local",
                    help="grade on this host, or fan out across CHIA workers")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for noisy in ("MutationEngineNode", "EmpiricalMetrics", "httpx"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    if args.dispatch == "ray":
        # Before any ChiaFunction call: a local call auto-initialises Ray, and
        # the `formal` resource cannot be added to a running cluster.
        log.info("connected to a CHIA cluster with %d formal slots",
                 ensure_formal_cluster())

    dut = Path(args.dut).resolve()
    rtl = dut.read_text()

    # Stateless logic needs no unrolling: depth 1 covers every input
    # combination, making the result a complete proof rather than a bounded
    # one. Unrolling further only multiplies solver work.
    _cr = detect_clock_reset(rtl)
    if _cr.combinational and args.depth != 1:
        log.info("%s is combinational; using depth 1 (exhaustive)", args.top)
        args.depth = 1
    out_dir = Path(args.out).resolve() / args.top
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = []
    for seed in range(args.seeds):
        log.info("=== %s seed %d/%d ===", args.top, seed + 1, args.seeds)
        started = time.time()
        result = refine_until_killed(
            dut_path=dut, module_name=args.top, golden_rtl=rtl,
            proof_fn=check_formal_proof, clk=args.clk, rst_n=args.rst_n,
            bmc_depth=args.depth, budget=args.budget, workers=args.workers,
            oss_cad_bin=OSS_CAD_BIN, max_iterations=args.max_iterations,
            dispatch=args.dispatch)

        verification = independent_verification(
            dut, args.top, rtl, result.final_properties,
            args.clk, args.rst_n, args.depth, args.workers, budget=args.budget)

        # Separately, grade the final suite against the ENTIRE mutant bank.
        # The loop optimises against a capped sample; this reports how well the
        # resulting suite generalises to faults it was never shown.
        full_bank = independent_verification(
            dut, args.top, rtl, result.final_properties,
            args.clk, args.rst_n, args.depth, args.workers, budget=0) \
            if args.budget else verification

        loop_rate = result.final_kill_rate
        indep_rate = verification["kill_rate_percent"]
        agrees = (loop_rate is None and indep_rate is None) or (
            loop_rate is not None and indep_rate is not None
            and abs(loop_rate - indep_rate) < 0.05)
        if not agrees:
            log.error("MISMATCH: loop reported %s, independent check %s",
                      loop_rate, indep_rate)

        runs.append({
            "seed": seed,
            "converged": result.converged,
            "stop_reason": result.stop_reason,
            "iterations": [asdict(i) for i in result.iterations],
            "initial_kill_rate": result.initial_kill_rate,
            "final_kill_rate": loop_rate,
            "loop_seconds": result.seconds,
            "independent_verification": verification,
            "full_bank_evaluation": full_bank,
            "verification_agrees": agrees,
            "final_properties": [
                {"property_id": pid,
                 "assertion": " ".join(code.splitlines()[-1].split()),
                 "code": code}
                for pid, code in result.final_properties
            ],
            "wall_seconds": time.time() - started,
        })
        log.info("seed %d: %s -> %s (converged=%s, independent=%s)",
                 seed, f"{result.initial_kill_rate:.1f}%" if result.initial_kill_rate is not None else "n/a",
                 f"{loop_rate:.1f}%" if loop_rate is not None else "n/a",
                 result.converged, verification["kill_rate"])

        # Persist after every seed. A seed costs tens of minutes, and writing
        # only at the end means an external timeout discards every seed that
        # did finish -- the results exist in the log but not in any form a
        # report can consume.
        _write_record(out_dir, args, dut, runs)

    record = _write_record(out_dir, args, dut, runs)
    path = out_dir / "refinement_eval.json"

    print("\n" + "=" * 72)
    print(f"{args.top}: {record['converged_seeds']}/{args.seeds} seeds converged")
    scoreable = sum(1 for r in runs if r["final_kill_rate"] is not None)
    if scoreable < len(runs):
        print(f"{len(runs) - scoreable}/{len(runs)} seeds produced no scoreable "
              f"suite (every candidate refuted by the golden design)")
    if record["kill_rate_min"] is not None:
        print(f"final kill rate: min {record['kill_rate_min']:.1f}%  "
              f"mean {record['kill_rate_mean']:.1f}%  max {record['kill_rate_max']:.1f}%")
    print(f"independent verification agrees on every seed: "
          f"{record['all_verifications_agree']}")
    print(f"written: {path}")
    return 0 if record["all_verifications_agree"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
