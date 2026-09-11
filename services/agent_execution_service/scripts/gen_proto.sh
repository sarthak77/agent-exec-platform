#!/usr/bin/env bash
# Regenerates the gRPC/protobuf Python stubs from proto/ into src/aep/...
# (a top-level package matching each proto's own package, e.g.
# `aep.agent_execution.v1`, so the generated files' own cross-imports resolve
# without rewriting). Includes a vendored copy of job_svc's service.proto
# (see proto/aep/job/v1/service.proto) so this service can build a client stub
# for it — CreateTask submits every task as a job in job_svc. No shared proto
# package exists across services yet. Run after editing either service.proto.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run python -m grpc_tools.protoc \
  -I proto \
  --python_out=src \
  --pyi_out=src \
  --grpc_python_out=src \
  proto/aep/agent_execution/v1/service.proto \
  proto/aep/job/v1/service.proto

# grpc_tools.protoc does not always emit __init__.py at every package level.
find src/aep -type d -exec touch {}/__init__.py \;

echo "Generated stubs under src/aep/agent_execution/v1/ and src/aep/job/v1/"
