#!/bin/sh
# Apply migrations (guarded by a Postgres advisory lock), then run the lexwatch command.
# `docker run IMAGE lexwatch --help` and `docker run IMAGE --help` work too (no migration).
set -e
if [ "$1" = "lexwatch" ]; then shift; fi
case "$1" in
  migrate|download-model|-h|--help|"") ;;
  *) lexwatch migrate ;;
esac
exec lexwatch "$@"
