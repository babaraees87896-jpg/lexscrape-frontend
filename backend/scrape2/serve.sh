#!/usr/bin/env bash
# Dashboard: http://localhost:8080/
# Same as live.sh — api_login + poll all 103 games

set -e
cd "$(dirname "$0")"
exec ./live.sh "$@"
