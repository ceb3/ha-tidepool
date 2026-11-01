#!/usr/bin/env bash
# Copy custom_components/tidepool to a Home Assistant host over ssh.
#
# Usage: scripts/install_dev.sh [--dry-run] <ha-host>
#
# Streams a tar of the integration (without __pycache__ or tests) over ssh, so
# the host only needs tar (no rsync). Home Assistant is NOT restarted.
set -euo pipefail

usage() {
  echo "usage: ${0##*/} [--dry-run] <ha-host>" >&2
  exit 2
}

dry_run=0
if [[ ${1:-} == "--dry-run" ]]; then
  dry_run=1
  shift
fi
[[ $# -eq 1 ]] || usage
host=$1
# Reject anything that ssh could take for an option or that is not a host name.
[[ $host =~ ^[A-Za-z0-9][A-Za-z0-9._@:-]*$ ]] || usage

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source_parent="$repo_root/custom_components"
target_parent=/config/custom_components
# The only path that is ever removed.
target=$target_parent/tidepool

[[ -d $source_parent/tidepool ]] || {
  echo "missing $source_parent/tidepool" >&2
  exit 1
}

local_cmd=(tar -C "$source_parent" --exclude=__pycache__ --exclude=tests -cf - tidepool)
remote_cmd="mkdir -p $target_parent && rm -rf $target && tar -x -C $target_parent"

if ((dry_run)); then
  printf '%q ' "${local_cmd[@]}"
  printf "| ssh -- %s '%s'\n" "$host" "$remote_cmd"
  echo "Dry run: nothing was copied."
else
  "${local_cmd[@]}" | ssh -- "$host" "$remote_cmd"
  echo "Installed custom_components/tidepool to $host:$target."
fi
echo "Home Assistant was NOT restarted; restart it yourself at a safe time."
