#!/usr/bin/env bash
# macOS Finder double-click entry point. Runs the shared launcher from the
# repo root so a Terminal window opens with visible output.
cd "$(dirname "$0")" && exec ./launch.sh "$@"
