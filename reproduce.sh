#!/usr/bin/env bash
# One entry point for reproducing the paper's results.
#
#   ./reproduce.sh tables            regenerate every table and number in the paper
#                                    from the committed JSON (seconds; no tools needed)
#   ./reproduce.sh setup             fetch the toolchain and benchmark RTL
#   ./reproduce.sh reverify [MOD..]  re-prove the committed suites from scratch and
#                                    compare with the committed results (no LLM needed)
#   ./reproduce.sh loop              run the loop once on the arbiter design (needs Vertex AI)
#   ./reproduce.sh cluster           same, on a CHIA cluster: chia up -> job submit -> down
#
# Run from anywhere; paths are resolved against the repository root.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
ROOT="$PWD"

OSS_DATE="2026-08-22"
OSS_TGZ="oss-cad-suite-linux-x64-${OSS_DATE//-/}.tgz"
OSS_URL="https://github.com/YosysHQ/oss-cad-suite-build/releases/download/${OSS_DATE}/${OSS_TGZ}"

need_tools() {
    export PATH="$ROOT/tools/oss-cad-suite/bin:$PATH"
    for t in yosys sby; do
        command -v "$t" >/dev/null || {
            echo "$t not found. Run ./reproduce.sh setup first." >&2; exit 1; }
    done
}

cmd="${1:-help}"; shift || true
case "$cmd" in
tables)
    python scripts/make_paper_tables.py --out results/refinement
    ;;

setup)
    if [ ! -x tools/oss-cad-suite/bin/yosys ]; then
        echo "downloading oss-cad-suite ${OSS_DATE} (the version used for the paper)"
        mkdir -p tools
        curl -L --fail -o "tools/${OSS_TGZ}" "$OSS_URL"
        tar -xzf "tools/${OSS_TGZ}" -C tools
    fi
    ./scripts/fetch_benchmarks.sh --shallow
    # Rocket and BOOM modules are Chipyard-generated Verilog, which a plain clone
    # does not contain. The generated files used in the paper are vendored here
    # at their original paths, so no Chipyard build is needed.
    cp -rn vendor/. ./
    echo "setup done. Python deps: pip install -r requirements.txt"
    ;;

reverify)
    need_tools
    # Work on a copy so the committed results are never overwritten.
    rm -rf outputs/reproduce
    mkdir -p outputs/reproduce
    for d in results/refinement/*/; do
        m=$(basename "$d")
        if [ $# -gt 0 ] && [[ ! " $* " =~ " $m " ]]; then continue; fi
        [ -f "$d/refinement_eval.json" ] || continue
        mkdir -p "outputs/reproduce/$m"
        cp "$d/refinement_eval.json" "outputs/reproduce/$m/"
    done
    # Same protocol as the paper: 24-mutant sample, BMC depth 12
    # (combinational modules are detected and checked at depth 1).
    python scripts/reverify_results.py --out outputs/reproduce --budget 24 --depth 12 \
        ${WORKERS:+--workers "$WORKERS"}
    python - <<'EOF'
import json, pathlib
ok = bad = 0
for new in sorted(pathlib.Path("outputs/reproduce").glob("*/reverified.json")):
    old = pathlib.Path("results/refinement") / new.parent.name / "reverified.json"
    a, b = json.loads(old.read_text()), json.loads(new.read_text())
    key = lambda r: {s["seed"]: (s["full_bank"]["killed"], s["full_bank"]["active"])
                     for s in r["seeds"]}
    if key(a) == key(b):
        ok += 1
    else:
        bad += 1
        print(f"DIFFERS  {a['module']}: committed {key(a)}  reproduced {key(b)}")
print(f"\nfull-bank results match for {ok} module(s), differ for {bad}")
raise SystemExit(1 if bad else 0)
EOF
    ;;

loop)
    need_tools
    : "${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT (and Vertex AI credentials) first}"
    python examples/formal_verification/formal_verification_loop.py \
        --dut examples/formal_verification/designs/arbiter.v --top arbiter --seeds 1 --out outputs/demo
    echo "record: outputs/demo/arbiter/refinement_eval.json"
    ;;

cluster)
    need_tools
    ./examples/formal_verification/submit_formal_loop.sh examples/formal_verification/designs/arbiter.v arbiter 1
    ;;

*)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
