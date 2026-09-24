#!/usr/bin/env python3
"""Re-grade saved assertion suites, offline, under one consistent protocol.

The refinement loop optimises against a capped mutant sample, so runs made with
different caps are not comparable: a suite graded on 40 of 113 mutants and one
graded on all 113 are answering different questions. This re-grades every saved
suite identically, straight from the recorded property text, so no LLM is
called and no loop is re-run.

It reports two numbers per seed:

* **sampled** -- the same budget the loop optimised against, which is the
  figure the loop's own bookkeeping should reproduce.
* **full bank** -- every mutant, including those the loop never saw. The gap
  between the two is overfitting to the sample, and on some modules it is
  large.

Usage:
    python scripts/reverify_results.py --out outputs/refinement [--budget 40]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from chia.formal.multifile_proof import OSS_CAD_BIN, check_formal_proof  # noqa: E402
from chia.formal.empirical_metrics import (  # noqa: E402
    detect_clock_reset,
    measure_mutation_coverage,
    measure_vacuity,
    solve_minimal_basis,
)
from chia.formal.state_def import Verdict  # noqa: E402

log = logging.getLogger("reverify")


def grade(dut: Path, top: str, rtl: str, properties, clk, rst_n, depth,
          workers, budget, cache):
    """Soundness-gate a suite and measure what it catches."""
    sound = []
    for pid, code in properties:
        verdict, _, _, _ = check_formal_proof(dut, top, code, clk, rst_n, depth)
        if verdict is Verdict.PASS:
            sound.append((pid, code))

    coverage = measure_mutation_coverage(
        dut_path=dut, module_name=top, golden_rtl=rtl, properties=sound,
        proof_fn=check_formal_proof, clk=clk, rst_n=rst_n, bmc_depth=depth,
        budget=budget, workers=workers, oss_cad_bin=OSS_CAD_BIN,
        classify_equivalents=True, equivalence_cache=cache)
    basis = solve_minimal_basis(coverage)
    return sound, coverage, basis


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="outputs/refinement")
    ap.add_argument("--budget", type=int, default=40,
                    help="the cap the loop optimised against")
    ap.add_argument("--depth", type=int, default=12)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--only", nargs="*", default=None, help="module names")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for noisy in ("MutationEngineNode", "EmpiricalMetrics", "httpx"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    records = sorted(Path(args.out).glob("*/refinement_eval.json"))
    summary = []

    for path in records:
        record = json.loads(path.read_text())
        module = record["module"]
        if args.only and module not in args.only:
            continue
        # Sources are stored relative to the repository root so records are
        # portable; absolute paths from older records still work.
        source = record["source"]
        dut = Path(source) if Path(source).is_absolute() else REPO / source
        if not dut.exists():
            log.warning("skipping %s: source missing at %s", module, dut)
            continue
        rtl = dut.read_text(errors="ignore")
        cr = detect_clock_reset(rtl)
        # A combinational module has no clock port. Passing a name it does not
        # declare fails elaboration for every property, so the soundness gate
        # rejects the whole suite and the module reports "no active mutants" --
        # which looks like a weak suite rather than a harness error.
        clk = cr.clk or "none"
        rst = cr.rst or "none"
        depth = 1 if cr.combinational else args.depth

        # Equivalence depends only on the design, so one cache serves every
        # seed of a module and each miter is paid for once.
        cache: dict = {}
        seeds = []
        for run in record["runs"]:
            props = [(fp["property_id"], fp["code"]) for fp in run["final_properties"]]
            if not props:
                continue
            _, sampled, s_basis = grade(dut, module, rtl, props, clk, rst,
                                        depth, args.workers, args.budget, cache)
            _, full, f_basis = grade(dut, module, rtl, props, clk, rst,
                                     depth, args.workers, 0, cache)
            vac = measure_vacuity(dut, module, rtl,
                                  "\n".join(c for _, c in props),
                                  check_formal_proof, clk, rst, depth)
            seeds.append({
                "seed": run["seed"],
                "converged_in_loop": run["converged"],
                "properties": len(props),
                "properties_distinct": len({fp["assertion"] for fp in run["final_properties"]}),
                "sampled": {
                    "budget": args.budget,
                    "kill_rate_percent": sampled.kill_rate,
                    "kill_rate": sampled.kill_rate_str,
                    "active": sampled.active,
                    "killed": sampled.killed,
                },
                "full_bank": {
                    "generated": full.generated,
                    "stillborn": full.stillborn,
                    "equivalent": full.equivalent,
                    "active": full.active,
                    "killed": full.killed,
                    "kill_rate_percent": full.kill_rate,
                    "kill_rate": full.kill_rate_str,
                    "minimal_basis": len(f_basis.selected_ids),
                    "redundancy_percent": f_basis.redundancy_pct,
                },
                "vacuity": {
                    "percent": vac.vacuity_pct,
                    "status": vac.status,
                    "unreachable": vac.unreached,
                },
                "overfit_gap_points": (
                    None if sampled.kill_rate is None or full.kill_rate is None
                    else round(sampled.kill_rate - full.kill_rate, 1)),
            })
            s = seeds[-1]
            log.info("%s seed %d: sampled %s | full bank %s | gap %s",
                     module, run["seed"], s["sampled"]["kill_rate"],
                     s["full_bank"]["kill_rate"], s["overfit_gap_points"])

        full_rates = [s["full_bank"]["kill_rate_percent"] for s in seeds
                      if s["full_bank"]["kill_rate_percent"] is not None]
        entry = {
            "module": module,
            "source": source,
            "clk": clk, "rst": rst, "depth": depth,
            "seeds": seeds,
            "full_bank_min": min(full_rates) if full_rates else None,
            "full_bank_mean": sum(full_rates) / len(full_rates) if full_rates else None,
            "full_bank_max": max(full_rates) if full_rates else None,
        }
        summary.append(entry)
        (path.parent / "reverified.json").write_text(json.dumps(entry, indent=2))

    out = Path(args.out) / "REVERIFIED_SUMMARY.json"
    out.write_text(json.dumps(summary, indent=2))

    print("\n" + "=" * 86)
    print(f"{'module':<26}{'seeds':>6}{'full-bank min':>15}{'mean':>9}{'max':>9}{'max gap':>10}")
    print("-" * 86)
    for e in summary:
        gaps = [s["overfit_gap_points"] for s in e["seeds"]
                if s["overfit_gap_points"] is not None]
        f = lambda v: f"{v:.1f}%" if v is not None else "n/a"
        print(f"{e['module']:<26}{len(e['seeds']):>6}{f(e['full_bank_min']):>15}"
              f"{f(e['full_bank_mean']):>9}{f(e['full_bank_max']):>9}"
              f"{(f'{max(gaps):+.1f}' if gaps else 'n/a'):>10}")
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
