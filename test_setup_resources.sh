#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
. "$SCRIPT_DIR/host_resource_checks.sh"

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT HUP INT TERM

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains() {
  case "$1" in
    *"$2"*) ;;
    *) fail "expected output to contain: $2" ;;
  esac
}

cat > "$TEST_DIR/meminfo" <<'EOF'
MemTotal:        8000000 kB
MemAvailable:    6144000 kB
EOF

cat > "$TEST_DIR/df" <<'EOF'
#!/usr/bin/env sh
printf 'Avail\n5000000\n'
EOF
chmod +x "$TEST_DIR/df"
PATH="$TEST_DIR:$PATH"
export PATH

if ! output=$(check_host_resources "$TEST_DIR/meminfo" /test 4096 1024 10240 1024 8589934592 2>&1); then
  fail "sufficient resources should pass"
fi
assert_contains "$output" 'Host memory: 6000 MiB available'
assert_contains "$output" 'Docker daemon memory limit: 8192 MiB'
assert_contains "$output" 'WARNING: Server-data filesystem has 4882 MiB free'
assert_contains "$output" 'WARNING: Server-data filesystem'

cat > "$TEST_DIR/meminfo" <<'EOF'
MemTotal:        800000 kB
MemAvailable:     512000 kB
EOF
if check_host_resources "$TEST_DIR/meminfo" /test 4096 1024 10240 1024 536870912 >"$TEST_DIR/output" 2>&1; then
  fail "critical memory shortage should fail"
fi
assert_contains "$(cat "$TEST_DIR/output")" 'ERROR: Host has only 500 MiB memory available'
assert_contains "$(cat "$TEST_DIR/output")" 'ERROR: Docker daemon reports a memory limit of 512 MiB'

cat > "$TEST_DIR/meminfo" <<'EOF'
MemTotal:        8000000 kB
MemAvailable:    6144000 kB
EOF
cat > "$TEST_DIR/df" <<'EOF'
#!/usr/bin/env sh
printf 'Avail\n5000000\n'
EOF
chmod +x "$TEST_DIR/df"
if ! output=$(check_host_resources "$TEST_DIR/meminfo" /test 4096 1024 10240 1024 2147483648 2>&1); then
  fail "daemon memory between critical and recommended thresholds should only warn"
fi
assert_contains "$output" 'WARNING: Docker daemon reports a memory limit of 2048 MiB'

cat > "$TEST_DIR/meminfo" <<'EOF'
MemTotal:        8000000 kB
MemAvailable:    6144000 kB
EOF
cat > "$TEST_DIR/df" <<'EOF'
#!/usr/bin/env sh
printf 'Avail\n500\n'
EOF
chmod +x "$TEST_DIR/df"
if check_host_resources "$TEST_DIR/meminfo" /test 4096 1024 10240 1024 8589934592 >"$TEST_DIR/output" 2>&1; then
  fail "critical disk shortage should fail"
fi
assert_contains "$(cat "$TEST_DIR/output")" 'ERROR: Server-data filesystem has only 0 MiB free'

echo 'All host resource preflight tests passed.'
