#!/usr/bin/env bash

exec "$(dirname "$0")/docker/check-docker-amd-gpu.sh" "$@"
