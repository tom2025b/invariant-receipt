#!/usr/bin/env bash
# Install invariant-receipt from the COMMITTED HEAD into a virtualenv.
#
#   scripts/install.sh [VENV_DIR]      (default: .venv-tool in this repo)
#
# Refuses a dirty working tree: the installed build identity names a commit,
# and a build containing uncommitted changes would name the wrong one. The
# package is built from `git archive HEAD`, never from the working tree, and
# the generated _build.py (version, commit, build time) exists only in that
# staged copy — so a source checkout can never pick up a stale identity.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv="${1:-$here/.venv-tool}"
cd "$here"

if [ -n "$(git status --porcelain --untracked-files=all)" ]; then
  echo "install: refused — the working tree has uncommitted or untracked changes." >&2
  echo "install: the build identity must name a real commit; commit or stash first:" >&2
  git status --short >&2
  exit 1
fi

commit="$(git rev-parse HEAD)"
version="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
build_time="$(date --iso-8601=seconds)"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
git archive --format=tar HEAD | tar -x -C "$stage"
printf 'BUILD = {"version": "%s", "commit": "%s", "build_time": "%s"}\n' \
  "$version" "$commit" "$build_time" > "$stage/src/invariant_receipt/_build.py"

python3 -m venv "$venv"
"$venv/bin/pip" install --quiet "$stage"
"$venv/bin/invariant-receipt" --version
