#!/usr/bin/env python3
"""CHIA loop: harden assertions on clean RTL, then hunt bugs in suspect RTL.

Two designs, two questions. Against the *reference* the loop asks "is this
property true?" and discards it if not. Against the *suspect* it asks "does
this property still hold?" and a refutation is a bug report with a waveform.

Running the hardening loop directly on buggy RTL cannot work: the soundness
gate would discard the very property that catches the bug, because that
property is false on the design in front of it.

    # harden on the upstream clean core, hunt in the bug-injected fork
    python examples/formal_verification/bug_hunt_loop.py \\
        --reference clean/rtl/foo.sv --suspect buggy/rtl/foo.sv --top foo \\
        --seeds 2 --out outputs/bug_hunt

    # reuse a suite from an earlier hardening run instead of re-deriving it
    python examples/formal_verification/bug_hunt_loop.py \\
        --reference clean/rtl/foo.sv --suspect buggy/rtl/foo.sv --top foo \\
        --suite outputs/refinement/foo/refinement_eval.json

A finding is a discrepancy between two designs, backed by a counterexample.
Whether it is a defect or a deliberate change is a judgement this tool does
not make. Absence of findings is not evidence of absence: the search is
bounded, and a suite only catches what it was hardened to catch.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from chia.formal.bug_hunt import hunt  # noqa: E402
from chia.formal.multifile_proof import OSS_CAD_BIN, check_formal_proof  # noqa: E402
from chia.formal.empirical_metrics import detect_clock_reset  # noqa: E402
from chia.formal.refinement_loop import refine_until_killed  # noqa: E402

log = logging.getLogger("bug-hunt")


def load_suite(path: Path):
    """Read the final properties of the best seed from a hardening record."""
    rec = json.loads(path.read_text())
    runs = [r for r in rec["runs"] if r["final_properties"]]
    if not runs:
        raise SystemExit(f"{path} contains no seed with a usable suite")
    # Strongest suite available: the one that caught the most faults.
    best = max(runs, key=lambda r: r["final_kill_rate"] or 0.0)
    log.info("reusing seed %d from %s (kill rate %s)",
             best["seed"], path, best["final_kill_rate"])
    return [(p["property_id"], p["code"]) for p in best["final_properties"]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reference", required=True, help="clean RTL")
    ap.add_argument("--suspect", required=True, help="RTL under investigation")
    ap.add_argument("--top", required=True)
    ap.add_argument("--suspect-top", default=None,
                    help="top module in the suspect, if it differs")
    ap.add_argument("--suite", default=None,
                    help="refinement_eval.json to reuse instead of hardening")
    ap.add_argument("--clk", default=None)
    ap.add_argument("--rst_n", default=None)
    ap.add_argument("--depth", type=int, default=12)
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--max-iterations", type=int, default=5)
    ap.add_argument("--budget", type=int, default=32)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--out", default="outputs/bug_hunt")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for noisy in ("MutationEngineNode", "EmpiricalMetrics", "httpx"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    reference = Path(args.reference).resolve()
    suspect = Path(args.suspect).resolve()
    for p in (reference, suspect):
        if not p.exists():
            raise SystemExit(f"missing RTL: {p}")

    rtl = reference.read_text(errors="ignore")
    cr = detect_clock_reset(rtl)
    clk = args.clk or cr.clk or "clk"
    rst = args.rst_n or cr.rst or "none"

    out_dir = Path(args.out).resolve() / args.top
    trace_dir = out_dir / "counterexamples"
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    hardening = None
    if args.suite:
        properties = load_suite(Path(args.suite))
    else:
        log.info("hardening on the reference (%d seeds)", args.seeds)
        best = None
        for seed in range(args.seeds):
            res = refine_until_killed(
                dut_path=reference, module_name=args.top, golden_rtl=rtl,
                proof_fn=check_formal_proof, clk=clk, rst_n=rst,
                bmc_depth=args.depth, budget=args.budget, workers=args.workers,
                oss_cad_bin=OSS_CAD_BIN, max_iterations=args.max_iterations)
            rate = res.final_kill_rate or 0.0
            log.info("  seed %d: kill rate %.1f%% over %d properties",
                     seed, rate, len(res.final_properties))
            if best is None or rate > (best.final_kill_rate or 0.0):
                best = res
        if best is None or not best.final_properties:
            raise SystemExit("hardening produced no usable suite; "
                             "nothing can be hunted with")
        hardening = {
            "kill_rate_percent": best.final_kill_rate,
            "properties": len(best.final_properties),
            "converged": best.converged,
            "stop_reason": best.stop_reason,
        }
        properties = best.final_properties

    log.info("applying %d properties to %s", len(properties), suspect.name)
    report = hunt(
        reference_path=reference, suspect_path=suspect, module_name=args.top,
        properties=properties, proof_fn=check_formal_proof, clk=clk,
        rst_n=rst, bmc_depth=args.depth, trace_dir=trace_dir,
        suspect_module=args.suspect_top,
    )

    record = {
        "module": args.top,
        "reference": str(reference),
        "suspect": str(suspect),
        "clk": clk, "rst": rst, "bmc_depth": args.depth,
        "hardening": hardening,
        "properties_in_suite": len(properties),
        "properties_applied": report.applied,
        "not_applicable": [{"property_id": p, "reason": r}
                           for p, r in report.not_applicable],
        "clean": report.clean,
        "inconclusive": [{"property_id": p, "detail": d}
                         for p, d in report.inconclusive],
        "findings": [{"property_id": f.property_id, "assertion": f.assertion,
                      "detail": f.detail, "counterexample": f.counterexample}
                     for f in report.findings],
        "wall_seconds": time.time() - started,
    }
    path = out_dir / "bug_hunt.json"
    path.write_text(json.dumps(record, indent=2))

    print("\n" + "=" * 78)
    print(f"BUG HUNT: {args.top}")
    print(f"  reference : {reference}")
    print(f"  suspect   : {suspect}")
    print("=" * 78)
    print(f"  {report.summary}")
    if report.not_applicable:
        print(f"  {len(report.not_applicable)} properties did not hold on the "
              f"reference and were not applied")
    for f in report.findings:
        print(f"\n  FINDING {f.property_id}")
        print(f"    {f.assertion[:110]}")
        print(f"    counterexample: {f.counterexample or 'not captured'}")
    if not report.findings:
        print("\n  No property was refuted by the suspect. This bounds the "
              "search;\n  it does not establish the design is correct.")
    print(f"\nwritten: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
