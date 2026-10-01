#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
python -m scripts.build_contracts
python -m scripts.sync_contracts
