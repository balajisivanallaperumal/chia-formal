#!/usr/bin/env bash
# Fetch the RTL this project is evaluated against, pinned to exact commits.
#
# These trees total ~2 GB, so they are not committed. Each is cloned at the
# same revision used for the reported results -- a moving branch would make
# the numbers irreproducible.
#
# Usage:  ./scripts/fetch_benchmarks.sh [--shallow]
#   --shallow  fetch only the pinned commit (much faster, no history)

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SHALLOW=0
[ "${1:-}" = "--shallow" ] && SHALLOW=1

# path <TAB> url <TAB> commit
read -r -d '' PINS <<'EOF' || true
examples/formal_verification/soc_cores/chipyard	https://github.com/ucb-bar/chipyard.git	371ab92dd09917b6ab05d58bf762b173c52f4224
examples/formal_verification/soc_cores/cv32e40p	https://github.com/openhwfoundation/cv32e40p.git	6033d2b1be3295ec774d17ac4cf226faacfdeb08
examples/formal_verification/soc_cores/cva6	https://github.com/openhwfoundation/cva6.git	81245a47fad8fe1a5d562d953ef2662e099def76
examples/formal_verification/soc_cores/Flute	https://github.com/bluespec/Flute.git	9b2b056527429e509c719c417a588b3ea594949b
examples/formal_verification/soc_cores/hackatdac18	https://github.com/HACK-EVENT/hackatdac18.git	60534635d40f397d71b45be511d954af2c0c7b3d
examples/formal_verification/soc_cores/hackatdac19	https://github.com/HACK-EVENT/hackatdac19.git	57e7b2109c1ea2451914878df2e6ca740c2dcf34
examples/formal_verification/soc_cores/hackatdac21	https://github.com/HACK-EVENT/hackatdac21.git	bcae7aba7f9daee8ad2cfd47b997ac7ad6611034
examples/formal_verification/soc_cores/Hazard3	https://github.com/Wren6991/Hazard3.git	8af992930f71a69b0e06c38734c1094f41a05ca0
examples/formal_verification/soc_cores/ibex	https://github.com/lowRISC/ibex.git	e9f55342edbd27e9e17a0e41b1c95a81abb5eac8
examples/formal_verification/soc_cores/Piccolo	https://github.com/bluespec/Piccolo.git	8a80b63af7e0036833836c91fc95f18f74c5fafa
examples/formal_verification/soc_cores/picorv32	https://github.com/YosysHQ/picorv32.git	ef203c2b0a3fb793280f5114941416c425c5b461
examples/formal_verification/soc_cores/rocket-chip	https://github.com/chipsalliance/rocket-chip.git	ece7b9ad544b07df39cd13b6fd7236562a26355d
examples/formal_verification/soc_cores/serv	https://github.com/olofk/serv.git	f200eb2ed7b69ac1c6b8eddd47654522aeee5ce8
examples/formal_verification/soc_cores/Toooba	https://github.com/bluespec/Toooba.git	81f926f863a591478638ee9e182ac4b6fbf86d5a
benchmarks/hackatdac18	https://github.com/HACK-EVENT/hackatdac18.git	60534635d40f397d71b45be511d954af2c0c7b3d
benchmarks/hackatdac19	https://github.com/HACK-EVENT/hackatdac19.git	57e7b2109c1ea2451914878df2e6ca740c2dcf34
benchmarks/hackatdac21	https://github.com/HACK-EVENT/hackatdac21.git	bcae7aba7f9daee8ad2cfd47b997ac7ad6611034
benchmarks/verification-benchmarks	https://github.com/HWSec-UNC/verification-benchmarks.git	f5da736829b4020fe76ec1d13cd2761199efde31
EOF

while IFS=$'\t' read -r path url sha; do
  [ -z "$path" ] && continue
  if [ -d "$path/.git" ]; then
    have=$(git -C "$path" rev-parse HEAD 2>/dev/null || echo none)
    if [ "$have" = "$sha" ]; then
      echo "ok       $path"
      continue
    fi
    echo "checkout $path -> ${sha:0:8}"
    git -C "$path" fetch --quiet origin "$sha" 2>/dev/null || git -C "$path" fetch --quiet origin
    git -C "$path" checkout --quiet "$sha"
    continue
  fi
  mkdir -p "$(dirname "$path")"
  if [ "$SHALLOW" = "1" ]; then
    echo "clone    $path (shallow, ${sha:0:8})"
    git init --quiet "$path"
    git -C "$path" remote add origin "$url"
    git -C "$path" fetch --quiet --depth 1 origin "$sha"
    git -C "$path" checkout --quiet FETCH_HEAD
  else
    echo "clone    $path (${sha:0:8})"
    git clone --quiet "$url" "$path"
    git -C "$path" checkout --quiet "$sha"
  fi
done <<< "$PINS"

echo
echo "All benchmark sources are at their pinned revisions."
