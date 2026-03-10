#!/bin/bash
# Legacy setup entrypoint.
# Use install.sh as the canonical installer.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "setup.sh is deprecated. Running install.sh instead..."
exec "$SCRIPT_DIR/install.sh" "$@"
