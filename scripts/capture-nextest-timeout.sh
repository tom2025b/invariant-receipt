#!/usr/bin/env bash
set -euo pipefail

# Capture the first M3 parser-resilience artifact from the real runner. This
# script never edits the XML: it copies nextest's bytes and records provenance
# separately. Run it from any directory in a clean checkout.

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
config="$root/scripts/capture-nextest-timeout.toml"
artifact="$root/tests/data/nextest/timeout_named.xml"
provenance="$root/tests/data/nextest/timeout_named.provenance.txt"
expected_version=0.9.149
version_output=$(cargo nextest --version)
actual_version=$(printf '%s\n' "$version_output" | awk 'NR == 1 { print $2 }')

if [[ "$actual_version" != "$expected_version" ]]; then
    printf 'refusing capture: expected cargo-nextest %s, got %s\n' \
        "$expected_version" "$version_output" >&2
    exit 2
fi

capture_root=$(mktemp -d "${TMPDIR:-/tmp}/invariant-receipt-nextest-timeout.XXXXXX")
trap 'rm -rf -- "$capture_root"' EXIT
export CARGO_TARGET_DIR="$capture_root/target"
export IR_CAPTURE_NEXTEST_TIMEOUT=1

command=(
    cargo nextest run
    --manifest-path "$root/fixtures/rust-crate/Cargo.toml"
    --tool-config-file "invariant-receipt-capture:$config"
    --profile capture-timeout
    --retries 0
    --no-fail-fast
    --no-tests=warn
    --lib
    tests::hang
)

printf 'capture command:'
printf ' %q' "${command[@]}"
printf '\n'

set +e
"${command[@]}"
nextest_exit=$?
set -e
if [[ $nextest_exit -ne 100 ]]; then
    printf 'refusing capture: expected nextest test-run-failed exit 100, got %s\n' \
        "$nextest_exit" >&2
    exit 3
fi

mapfile -d '' reports < <(find "$CARGO_TARGET_DIR" -type f -name junit.xml -print0)
if [[ ${#reports[@]} -ne 1 ]]; then
    printf 'refusing capture: expected exactly one junit.xml, found %s\n' \
        "${#reports[@]}" >&2
    exit 4
fi
if ! grep -q 'name="tests::hang"' "${reports[0]}"; then
    printf 'refusing capture: JUnit does not contain tests::hang\n' >&2
    exit 5
fi
if ! grep -Eiq '<failure[^>]*type="[^"]*timeout' "${reports[0]}"; then
    printf 'refusing capture: tests::hang has no distinct timeout failure type\n' >&2
    exit 6
fi

# cp performs no textual transformation. Do not add a comment to the XML.
cp -- "${reports[0]}" "$artifact"
artifact_sha256=$(sha256sum "$artifact" | awk '{ print $1 }')
captured_at=$(date --iso-8601=seconds)
host=$(uname -a)
rust=$(rustc --version)
printf -v command_text '%q ' "${command[@]}"

cat >"$provenance" <<EOF
producer: cargo-nextest $expected_version
command: ${command_text% }
profile: capture-timeout
slow-timeout.period: 1s
slow-timeout.terminate-after: 1
slow-timeout.on-timeout: fail
retries: 0
captured-at: $captured_at
host: $host; $rust
artifact-sha256: $artifact_sha256
EOF

printf 'captured byte-for-byte XML: %s\n' "$artifact"
printf 'wrote provenance: %s\n' "$provenance"
printf 'sha256: %s\n' "$artifact_sha256"
