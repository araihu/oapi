#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
mkdir -p .tools
revision=56318e36ec4d1afff6bf4592e38645ed75df78be
metadata="$(go version -m .tools/manja 2>/dev/null || true)"
if [[ ! -x .tools/manja || ( "$metadata" != *"$revision"* && "$metadata" != *"${revision:0:12}"* ) ]]; then
  GOEXPERIMENT=jsonv2 GOWORK=off GOFLAGS=-trimpath GOBIN="$PWD/.tools" go install "github.com/araihu/manja/cmd/manja@$revision"
fi
npm ci --no-audit --no-fund
python3 -c 'import yaml; assert yaml.__version__ == "6.0.3", "Install requirements.txt in a virtualenv"'
