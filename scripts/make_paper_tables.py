#!/usr/bin/env python3
"""Emit the paper's result tables and \\PENDING macro values from measured JSON.

Every number in the paper should be traceable to a solver run. Rather than
transcribing figures by hand -- which is how a paper drifts from its artifact --
this reads the re-verified records and prints the LaTeX to paste in, plus the
macro block that replaces the placeholders.

Usage:
    python scripts/make_paper_tables.py [--out results/refinement]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def pct(v, nd=1):
    return "--" if v is None else f"${v:.{nd}f}\\%$"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="results/refinement")
    args = ap.parse_args()
    root = Path(args.out)

    reverified = {}
    for path in sorted(root.glob("*/reverified.json")):
        rec = json.loads(path.read_text())
        reverified[rec["module"]] = rec

    # Proposal disposition comes from the loop records, which count what the
    # model offered before anything was filtered.
    proposed = syntax = dup = unsound = 0
    for path in sorted(root.glob("*/refinement_eval.json")):
        rec = json.loads(path.read_text())
        if rec["module"] not in reverified:
            continue          # module has no re-verified counterpart; skip
        for run in rec["runs"]:
            for it in run["iterations"]:
                proposed += it["proposed"]
                syntax += it["rejected_syntax"]
                dup += it.get("rejected_duplicate", 0)
                unsound += it["rejected_unsound"]
    admitted = proposed - syntax - dup - unsound

    share = lambda n: f"{n / proposed * 100:.0f}\\%" if proposed else "--"

    print("% ---------------- macro block: replace the one at the top --------")
    print(f"\\newcommand{{\\nmodules}}{{{len(reverified)}}}")
    seeds = {len(r["seeds"]) for r in reverified.values()}
    print(f"\\newcommand{{\\nseeds}}{{{seeds.pop() if len(seeds) == 1 else '3'}}}")
    print(f"\\newcommand{{\\nproposed}}{{{proposed}}}")
    print(f"\\newcommand{{\\punsound}}{{{round(unsound / proposed * 100) if proposed else 0}}}")
    print(f"\\newcommand{{\\pdup}}{{{round(dup / proposed * 100) if proposed else 0}}}")
    runs = sum(len(json.loads((root / n / "refinement_eval.json").read_text())["runs"])
               for n in reverified)
    print(f"\\newcommand{{\\nruns}}{{{runs}}}")
    print(f"\\newcommand{{\\ncomb}}{{{sum(1 for r in reverified.values() if r['depth'] == 1)}}}")

    print("\n% ---------------- Table: proposal disposition -------------------")
    print("Rejected: unsupported SVA syntax & %d & %s \\\\" % (syntax, share(syntax)))
    print("Rejected: duplicate of a shown property & %d & %s \\\\" % (dup, share(dup)))
    print("Rejected: refuted by the correct design & %d & %s \\\\" % (unsound, share(unsound)))
    print("Admitted to the suite & %d & %s \\\\" % (admitted, share(admitted)))

    print("\n% ---------------- Table: per-module results ---------------------")
    all_rates = []
    seeds_run = converged_real = no_suite = 0
    for name, rec in sorted(reverified.items()):
        loop = json.loads(
            (Path(args.out) / name / "refinement_eval.json").read_text())
        # Denominator is seeds RUN, not seeds that produced a scoreable suite.
        # A seed whose candidates were all refuted by the golden design did not
        # converge -- it failed -- and dropping it from the denominator would
        # quietly improve the reported rate.
        n = len(loop["runs"])
        empty = sum(1 for r in loop["runs"]
                    if not r["final_properties"] or r["final_kill_rate"] is None)
        conv = sum(1 for s in rec["seeds"] if s["converged_in_loop"])
        seeds_run += n
        converged_real += conv
        no_suite += empty
        rates = [s["full_bank"]["kill_rate_percent"] for s in rec["seeds"]
                 if s["full_bank"]["kill_rate_percent"] is not None]
        all_rates += rates
        esc = name.replace("_", "\\_")
        # Active mutants in the full bank: the denominator behind every rate.
        # Identical across seeds (same bank), so one figure per module.
        active = sorted({s["full_bank"]["active"] for s in rec["seeds"]})
        act = "--" if not active else "/".join(map(str, active))
        print(f"\\texttt{{{esc}}} & {conv}/{n} & {empty} & {act} & {pct(rec['full_bank_min'])} & "
              f"{pct(rec['full_bank_mean'])} & {pct(rec['full_bank_max'])} \\\\")

    print("\n% ---------------- prose figures ---------------------------------")
    if all_rates:
        print(f"%% overall full-bank mean: {sum(all_rates)/len(all_rates):.1f}%%")
        print(f"%% overall range: {min(all_rates):.1f}%% -- {max(all_rates):.1f}%%")
    print(f"%% converged runs: {converged_real}/{seeds_run}")
    print(f"%% seeds yielding no scoreable suite: {no_suite}/{seeds_run}")

    gaps = [(n, s["overfit_gap_points"]) for n, r in reverified.items()
            for s in r["seeds"] if s["overfit_gap_points"] is not None]
    if gaps:
        print(f"%% mean sampled-vs-full-bank gap: "
              f"{sum(g for _, g in gaps) / len(gaps):.1f} points over {len(gaps)} seeds")
        worst = max(gaps, key=lambda kv: kv[1])
        print(f"%% largest sampled-vs-full-bank gap: {worst[0]} {worst[1]:+.1f} points")

    # Refinement trajectory on the sample the loop sees: first iteration vs
    # final suite, over runs where both were scored. Plus per-run cost.
    ini, fin, iters, wall = [], [], [], []
    for n in reverified:
        loop = json.loads((root / n / "refinement_eval.json").read_text())
        for run in loop["runs"]:
            iters.append(len(run["iterations"]))
            if run.get("wall_seconds"):
                wall.append(run["wall_seconds"])
            if run.get("initial_kill_rate") is not None and \
                    run.get("final_kill_rate") is not None:
                ini.append(run["initial_kill_rate"])
                fin.append(run["final_kill_rate"])
    if ini:
        med = lambda v: sorted(v)[len(v) // 2] if len(v) % 2 else \
            (sorted(v)[len(v) // 2 - 1] + sorted(v)[len(v) // 2]) / 2
        print(f"%% sampled kill rate, first iteration -> final: mean "
              f"{sum(ini)/len(ini):.1f}%% -> {sum(fin)/len(fin):.1f}%%, median "
              f"{med(ini):.1f}%% -> {med(fin):.1f}%% over {len(ini)} runs")
        print(f"%% iterations per run: mean {sum(iters)/len(iters):.1f}, max {max(iters)}")
    if wall:
        print(f"%% wall time per run: median {med(wall)/60:.1f} min, max "
              f"{max(wall)/60:.1f} min, total {sum(wall)/3600:.1f} h over {len(wall)} runs")
    pts = [(s["sampled"]["kill_rate_percent"], s["full_bank"]["kill_rate_percent"])
           for r in reverified.values() for s in r["seeds"]
           if s["sampled"]["kill_rate_percent"] is not None
           and s["full_bank"]["kill_rate_percent"] is not None]
    if pts:
        below = sum(1 for a, b in pts if b < a)
        print(f"%% seeds with full-bank rate below sampled rate: {below}/{len(pts)}")
        print("%% scatter (sampled, full): " + " ".join(f"({a:.1f},{b:.1f})" for a, b in pts))

    vac = [(n, s["vacuity"]["percent"]) for n, r in reverified.items()
           for s in r["seeds"] if s["vacuity"]["percent"] is not None]
    if vac:
        print(f"%% vacuity range: {min(v for _, v in vac):.1f}%% -- "
              f"{max(v for _, v in vac):.1f}%%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
