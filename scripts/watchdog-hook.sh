#!/bin/sh
# Offline only. Missing runtimes leave the coordinator on bounded polling.
if ! command -v uv >/dev/null 2>&1; then
    exit 0
fi
output=$(uv --no-config run --offline --no-project --script "$1" --stdout 2>/dev/null)
status=$?
if [ "$status" -eq 2 ]; then
    case "$output" in
        'OMP watchdog probe '*|'OMP '*' bounded_check')
            printf '%s\n' "$output" >&2
            exit 2
            ;;
    esac
fi
exit 0
