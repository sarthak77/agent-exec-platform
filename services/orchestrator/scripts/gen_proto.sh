#!/usr/bin/env bash
# Regenerates the gRPC/protobuf Python stubs from proto/ into src/aep/...
# (a top-level package matching each proto's own package, e.g.
# `aep.orchestrator.v1`, so the generated files' own cross-imports resolve
# without rewriting). Includes a vendored copy of gateway's service.proto
# (see proto/aep/gateway/v1/service.proto) so orchestrator can build a
# client stub for it — no shared proto package exists across services yet.
# Run after editing either service.proto.
#
# Uses an isolated ephemeral env (not `uv run`) because autogen-core pins
# protobuf to [5.29.3, 5.30) and no recent grpcio-tools release supports
# that range as a runtime dep — grpcio-tools 1.68.x-1.69.x happens to
# generate code against protobuf 5.29.6, which satisfies the pin, without
# ever entering orchestrator's own dependency resolution.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run --isolated --with "grpcio-tools>=1.68,<1.70" python -m grpc_tools.protoc \
  -I proto \
  --python_out=src \
  --pyi_out=src \
  --grpc_python_out=src \
  proto/aep/orchestrator/v1/service.proto \
  proto/aep/gateway/v1/service.proto

# grpc_tools.protoc does not always emit __init__.py at every package level.
find src/aep -type d -exec touch {}/__init__.py \;

echo "Generated stubs under src/aep/orchestrator/v1/ and src/aep/gateway/v1/"
