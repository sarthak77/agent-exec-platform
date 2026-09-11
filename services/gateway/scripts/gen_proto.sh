#!/usr/bin/env bash
# Regenerates the gRPC/protobuf Python stubs from proto/ into src/aep/...
# (a top-level package matching the proto package `aep.gateway.v1` so the
# generated files' own cross-imports resolve without rewriting).
# Run after editing service.proto.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run python -m grpc_tools.protoc \
  -I proto \
  --python_out=src \
  --pyi_out=src \
  --grpc_python_out=src \
  proto/aep/gateway/v1/service.proto

# grpc_tools.protoc does not always emit __init__.py at every package level.
find src/aep -type d -exec touch {}/__init__.py \;

echo "Generated stubs under src/aep/gateway/v1/"
