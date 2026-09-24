#!/usr/bin/env bash
# Auto-generated CHIA environment activation script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source conda
if [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/miniconda3/etc/profile.d/conda.sh"
elif [ -f "$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
    source "$HOME/anaconda3/etc/profile.d/conda.sh"
fi

conda activate chia_env

export CHIA_ROOT="$SCRIPT_DIR"
export PYTHONPATH="$CHIA_ROOT:$PYTHONPATH"
export PATH="$HOME/miniconda3/envs/chia_env/bin:$CHIA_ROOT/tools/oss-cad-suite/bin:$PATH"
export RAY_TMPDIR="/tmp"
export RAY_memory_usage_threshold=0.90
export RAY_memory_monitor_refresh_ms=0
export THIS_MACHINE=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$THIS_MACHINE" ] && export THIS_MACHINE="127.0.0.1"

# GCP Vertex AI configuration.
# Put your own settings in .env.local (untracked) -- do not commit credentials:
#   export GOOGLE_CLOUD_PROJECT="your-project"
#   export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/your-key.json"
# or just run: gcloud auth application-default login
if [ -f "$CHIA_ROOT/.env.local" ]; then
    # shellcheck disable=SC1091
    source "$CHIA_ROOT/.env.local"
fi
export GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
export GOOGLE_GENAI_USE_VERTEXAI="True"
if [ -z "${GOOGLE_CLOUD_PROJECT:-}" ]; then
    echo "WARNING: GOOGLE_CLOUD_PROJECT is unset - SVA synthesis will fail."
    echo "         Set it in .env.local (see comment above)."
fi

echo "=========================================================="
echo "⚡ CHIA Formal Verification Environment Active!"
echo "• Python Environment: $(which python)"
echo "• Yosys / SymbiYosys: $(which yosys 2>/dev/null || echo 'tools/oss-cad-suite')"
echo "• GCP Vertex Project: ${GOOGLE_CLOUD_PROJECT:-<unset>} ($GOOGLE_CLOUD_LOCATION)"
echo "=========================================================="
