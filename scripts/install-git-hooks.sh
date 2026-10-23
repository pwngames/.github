#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# A stable local launcher survives branch switches. A branch missing its gate
# fails closed instead of silently disabling Git hooks.
common_dir="$(cd "$(git rev-parse --git-common-dir)" && pwd)"
hooks_dir="$common_dir/pwn-local-hooks"
mkdir -p "$hooks_dir"
cp .githooks/pre-push "$hooks_dir/pre-push"
chmod +x "$hooks_dir/pre-push" .githooks/pre-push .githooks/checks.sh
git config --local core.hooksPath "$hooks_dir"
printf 'Installed local pre-push CI checks. No remote settings changed.\n'
