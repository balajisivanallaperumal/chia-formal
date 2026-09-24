#!/usr/bin/env python3
"""Screen RTL modules for whether the formal harness can actually check them.

Pointing the loop at a module the harness cannot discriminate wastes an hour
and produces a row of `ERROR`. This screens candidates first with the
falsification canary: a trivially true property must PASS and a trivially
false one must FAIL. A module where both pass is not being checked at all --
usually a guard whose trigger never fires, which is exactly the vacuous-harness
failure this project exists to catch.

Modules are deduplicated by top-module name, since generated RTL is copied
verbatim across per-configuration build directories.

Usage:
    python scripts/screen_modules.py --out viable.tsv \\
        --min-lines 80 --max-lines 400 \\
        'soc_cores/Flute/src_SSITH_P2/Verilog_RTL/mk*.v'
"""
from __future__ import annotations

import argparse
import glob
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from chia.formal.multifile_proof import check_formal_proof  # noqa: E402
from chia.formal.empirical_metrics import detect_clock_reset  # noqa: E402
from chia.formal.state_def import Verdict  # noqa: E402


def top_module_of(path: Path, text: str) -> str:
    """Best guess at the module a file is named for."""
    mods = re.findall(r"^\s*module\s+(\w+)", text, re.M)
    if path.stem in mods:
        return path.stem
    return mods[0] if mods else path.stem


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("patterns", nargs="+", help="glob(s) of RTL files")
    ap.add_argument("--out", default="viable.tsv")
    ap.add_argument("--min-lines", type=int, default=60)
    ap.add_argument("--max-lines", type=int, default=600)
    ap.add_argument("--depth", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="stop after N viable")
    ap.add_argument("--max-seconds", type=float, default=0.0,
                    help="reject modules whose canary check exceeds this; "
                         "grading runs one solver call per (property, mutant) "
                         "pair, so a slow module is a slow sweep")
    args = ap.parse_args()

    paths = []
    for pat in args.patterns:
        paths.extend(Path(p) for p in glob.glob(pat, recursive=True))

    # Generated RTL is duplicated across build directories; keep one copy of
    # each module, preferring the shallowest path (the canonical source tree).
    by_module: dict[str, Path] = {}
    for p in sorted(paths, key=lambda q: (len(q.parts), str(q))):
        if not p.is_file():
            continue
        text = p.read_text(errors="ignore")
        n = len(text.splitlines())
        if not (args.min_lines <= n <= args.max_lines):
            continue
        by_module.setdefault(top_module_of(p, text), p)

    print(f"{len(by_module)} distinct modules in range "
          f"[{args.min_lines},{args.max_lines}] lines\n")
    hdr = (f"{'module':<28}{'clk':<8}{'rst':<9}{'kind':<6}"
           f"{'true':<7}{'false':<7}status")
    print(hdr); print("-" * len(hdr))

    viable = []
    for top, path in sorted(by_module.items()):
        text = path.read_text(errors="ignore")
        cr = detect_clock_reset(text)
        clk = cr.clk or "none"
        rst = cr.rst or "none"
        # Combinational modules get unguarded `always @(*)` assertions at
        # depth 1, which for stateless logic is exhaustive rather than bounded.
        if cr.combinational:
            true_prop = "always @(*) assert (1'b1);"
            false_prop = "always @(*) assert (1'b0);"
            depth = 1
        else:
            true_prop = f"always @(posedge {clk}) if ({cr.guard}) assert (1'b1);"
            false_prop = f"always @(posedge {clk}) if ({cr.guard}) assert (1'b0);"
            depth = args.depth
        import time as _time
        _t0 = _time.time()
        t_v, t_d, _, _ = check_formal_proof(
            path.resolve(), top, true_prop, clk, rst, depth)
        f_v, _, _, _ = check_formal_proof(
            path.resolve(), top, false_prop, clk, rst, depth)
        elapsed = _time.time() - _t0
        ok = t_v is Verdict.PASS and f_v is Verdict.FAIL
        if ok and args.max_seconds and elapsed > args.max_seconds:
            print(f"{top:<28}{str(cr.clk):<8}{rst:<9}{'-':<7}{'-':<7}"
                  f"skip: {elapsed:.1f}s per call exceeds "
                  f"{args.max_seconds:.0f}s budget")
            continue
        if ok:
            viable.append((str(path), top, clk, rst))
        status = ("VIABLE" if ok else
                  "elaboration fails" if t_v is Verdict.ERROR else
                  "harness not discriminating")
        kind = "comb" if cr.combinational else "seq"
        print(f"{top:<28}{clk:<8}{rst:<9}{kind:<6}"
              f"{str(t_v).replace('Verdict.', ''):<7}"
              f"{str(f_v).replace('Verdict.', ''):<7}{status}  ({elapsed:.1f}s)")
        if not ok and t_v is Verdict.ERROR:
            print(f"{'':<28}-> {t_d[:96]}")
        if args.limit and len(viable) >= args.limit:
            print(f"\nreached --limit {args.limit}")
            break

    Path(args.out).write_text("\n".join("\t".join(v) for v in viable) + "\n")
    print(f"\n{len(viable)} viable -> {args.out}")
    for _, top, _, _ in viable:
        print(f"   {top}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
