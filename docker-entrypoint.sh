#!/bin/sh
# Apply migrations (guarded by a Postgres advisory lock), then run the lexwatch command.
set -e
case "$1" in
  migrate|download-model) ;;
  *) lexwatch migrate ;;
esac
exec lexwatch "$@"
