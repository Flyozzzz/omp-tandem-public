#!/bin/sh
# Diagnostic only: do not consume hook payloads or execute discovered tools.
missing=
if ! command -v uv >/dev/null 2>&1; then
    missing=uv
fi
if ! command -v omp >/dev/null 2>&1; then
    if [ -n "$missing" ]; then
        missing='uv and omp'
    else
        missing=omp
    fi
fi
if [ -n "$missing" ]; then
    printf '%s\n' "{\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",\"additionalContext\":\"OMP Tandem prerequisite missing from PATH: $missing. Use the omp-tandem setup skill (skills/setup/SKILL.md) for user-approved installation and manual provider setup. This diagnostic installed nothing and checked no credentials.\"}}"
fi
exit 0
