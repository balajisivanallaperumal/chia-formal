#!/usr/bin/env python3
"""Compare a design's own human-written assertions against the refined loop.

Work on machine-generated assertions reports a kill rate with nothing to
compare it to. lowRISC's ibex ships 185 in-tree assertions written and reviewed
over years, which makes the missing reference point available -- if they can be
made to reach a solver at all.

Getting them there is most of the work, and what it takes is itself a result:

* slang predefines ``SYNTHESIS``, and lowRISC's ``prim_assert.sv`` dispatches
  ``VERILATOR -> SYNTHESIS -> YOSYS``, so the dummy macros win and every
  ``\\`ASSERT`` compiles to nothing regardless of ``-DYOSYS``.
* ``ASSERT_KNOWN``'s Yosys body is empty by design -- X-checks are vacuous
  under 2-state formal. 39% of ibex's assertions are of that kind.
* The Yosys ``\\`ASSERT`` expands to an *immediate* assert, where ``|->`` is
  illegal; 28% of ibex's assertions use it, and ``\\`ASSERT_IF`` embeds one in
  the macro *definition*.

Both suites are graded on the *same* mutant bank, with mutation excluded from
assertion lines so neither suite is scored against faults injected into its own
specification.

Modules whose assertions do not hold on golden ibex under this harness are
reported separately and excluded, not counted as misses: those assertions are
valid under environment constraints lowRISC's own FPV setup supplies and this
harness does not. Scoring them as failures would understate the human baseline.

Usage:
    python examples/formal_verification/human_vs_loop.py \\
        --tree /path/to/prepared_ibex --out outputs/human_baseline
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

from chia.formal.empirical_metrics import (  # noqa: E402
    detect_clock_reset,
    measure_mutation_coverage,
    stratified_sample,
)
from chia.formal.multifile_proof import OSS_CAD_BIN, check_formal_proof  # noqa: E402
from chia.formal.mutation_node import MutationEngineNode  # noqa: E402
from chia.formal.native_assertions import (  # noqa: E402
    LOWRISC_FORMAL_DEFINES,
    classify_assertions,
    prepare_native_tree,
)
from chia.formal.refinement_loop import refine_until_killed  # noqa: E402
from chia.formal.state_def import Verdict  # noqa: E402

log = logging.getLogger("human-vs-loop")


def grade_human_suite(path, top, clk, rst, mutants, golden_rtl, tree, depth, workers):
    """Grade the design's own assertions against a mutant bank.

    The module is compiled with its assertions active and nothing injected, so
    a FAIL is that suite catching the fault.
    """
    import concurrent.futures

    defines = list(LOWRISC_FORMAL_DEFINES)

    def one(m):
        mutated = MutationEngineNode.materialize(m, golden_rtl)
        try:
            v, _, _, _ = check_formal_proof(
                path, top, "", clk, rst, depth, defines=defines,
                extra_search_dirs=[tree], rtl_override=mutated)
        except Exception as exc:
            log.debug("human grading failed on %s: %s", m.mid, exc)
            return m.mid, Verdict.ERROR
        return m.mid, v

    killed, stillborn = set(), set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for mid, v in pool.map(one, mutants):
            if v is Verdict.FAIL:
                killed.add(mid)
            elif v is Verdict.ERROR:
                stillborn.add(mid)
    return killed, stillborn


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ibex", default="examples/formal_verification/soc_cores/ibex")
    ap.add_argument("--tree", default=None, help="prepared tree (built if absent)")
    ap.add_argument("--modules", nargs="*", default=[
        "ibex_register_file_ff", "ibex_register_file_fpga",
        "ibex_fetch_fifo", "ibex_decoder", "ibex_branch_predict"])
    ap.add_argument("--budget", type=int, default=24)
    ap.add_argument("--depth", type=int, default=8)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--max-iterations", type=int, default=4)
    ap.add_argument("--out", default="outputs/human_baseline")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    for noisy in ("MutationEngineNode", "EmpiricalMetrics", "httpx"):
        logging.getLogger(noisy).setLevel(logging.ERROR)

    IB = Path(args.ibex).resolve()
    tree = Path(args.tree).resolve() if args.tree else Path(args.out).resolve() / "_tree"
    if not (tree / "ibex_pkg.sv").exists():
        stats = prepare_native_tree(
            [IB / "rtl", IB / "vendor/lowrisc_ip/ip/prim/rtl",
             IB / "vendor/lowrisc_ip/dv/sv/dv_utils"], tree)
        log.info("prepared tree: %s", stats)

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    records = []

    for top in args.modules:
        src = tree / f"{top}.sv"
        if not src.exists():
            log.warning("skipping %s: not in prepared tree", top)
            continue
        rtl = src.read_text(errors="ignore")
        cr = detect_clock_reset(rtl)
        if not cr.clk:
            log.warning("skipping %s: no clock", top)
            continue
        inv = classify_assertions((IB / "rtl" / f"{top}.sv").read_text(errors="ignore"))
        log.info("=== %s: %s ===", top, inv.summary())

        # Admissibility: assertions that do not hold on golden are excluded,
        # not scored as misses.
        v, detail, _, _ = check_formal_proof(
            src, top, "", cr.clk, cr.rst, args.depth,
            defines=list(LOWRISC_FORMAL_DEFINES), extra_search_dirs=[tree])
        if v is not Verdict.PASS:
            log.warning("%s: human suite not admissible under this harness (%s)",
                        top, str(v).replace("Verdict.", ""))
            records.append({"module": top, "admissible": False,
                            "verdict": v.value, "detail": detail[:200],
                            "inventory": inv.__dict__ | {"detail": None}})
            continue

        started = time.time()
        engine = MutationEngineNode(oss_cad_bin=OSS_CAD_BIN, top=top)
        bank = engine.generate(rtl)
        sample = stratified_sample(bank, args.budget)

        human_killed, human_stillborn = grade_human_suite(
            src, top, cr.clk, cr.rst or "none", sample, rtl, tree,
            args.depth, args.workers)

        # The loop never sees the human assertions: it is graded on the same
        # design with them compiled out, which is the default define set.
        result = refine_until_killed(
            dut_path=src, module_name=top, golden_rtl=rtl,
            proof_fn=check_formal_proof, clk=cr.clk, rst_n=cr.rst or "none",
            bmc_depth=args.depth, budget=args.budget, workers=args.workers,
            oss_cad_bin=OSS_CAD_BIN, max_iterations=args.max_iterations)
        loop_cov = result.final_coverage
        loop_killed = set()
        if loop_cov:
            for p in loop_cov.per_property:
                loop_killed |= p.killed

        active = [m.mid for m in sample if m.mid not in human_stillborn]
        n = len(active) or 1
        rec = {
            "module": top,
            "admissible": True,
            "inventory": {"total": inv.total, "gradeable": inv.gradeable,
                          "x_checks": inv.x_checks, "plain": inv.plain,
                          "implications": inv.implications},
            "mutants_sampled": len(sample),
            "active": len(active),
            "human_killed": len(human_killed),
            "human_kill_rate": len(human_killed) / n * 100,
            "loop_killed": len(loop_killed),
            "loop_kill_rate": len(loop_killed) / n * 100,
            "loop_properties": len(result.final_properties),
            "both": len(human_killed & loop_killed),
            "human_only": sorted(human_killed - loop_killed),
            "loop_only": sorted(loop_killed - human_killed),
            "neither": len(set(active) - human_killed - loop_killed),
            "seconds": time.time() - started,
        }
        records.append(rec)
        log.info("%s: human %.1f%% (%d), loop %.1f%% (%d), both %d, "
                 "human-only %d, loop-only %d",
                 top, rec["human_kill_rate"], rec["human_killed"],
                 rec["loop_kill_rate"], rec["loop_killed"], rec["both"],
                 len(rec["human_only"]), len(rec["loop_only"]))
        (out_dir / "human_vs_loop.json").write_text(json.dumps(records, indent=2))

    (out_dir / "human_vs_loop.json").write_text(json.dumps(records, indent=2))
    usable = [r for r in records if r.get("admissible")]
    print("\n" + "=" * 78)
    print(f"{'module':<26}{'human':>9}{'loop':>9}{'both':>7}{'h-only':>8}{'l-only':>8}")
    print("-" * 78)
    for r in usable:
        print(f"{r['module']:<26}{r['human_kill_rate']:>8.1f}%{r['loop_kill_rate']:>8.1f}%"
              f"{r['both']:>7}{len(r['human_only']):>8}{len(r['loop_only']):>8}")
    skipped = [r for r in records if not r.get("admissible")]
    if skipped:
        print(f"\nexcluded ({len(skipped)}): human assertions not admissible under "
              f"this harness -> {', '.join(r['module'] for r in skipped)}")
    print(f"\nwritten: {out_dir / 'human_vs_loop.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
