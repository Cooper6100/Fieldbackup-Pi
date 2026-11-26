#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
if [[ $# != 1 ]]; then
    echo 'Usage: ./scripts/package.sh ABSOLUTE_OUTPUT_DIRECTORY' >&2
    exit 2
fi
output=$(realpath -m "$1")
mkdir -p "$output"
archive=FieldBackup-v0.2.0.tar.gz
# Use only tracked files from the revision being reviewed.
git archive --format=tar --prefix=FieldBackup-v0.2.0/ HEAD \
    LICENSE README.md docs payload tests configure-smb.sh install.sh upgrade.sh rollback.sh scripts \
    | gzip -n > "$output/$archive"
(cd "$output" && sha256sum "$archive" > "$archive.sha256")
