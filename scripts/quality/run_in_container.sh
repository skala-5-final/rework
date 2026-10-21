#!/usr/bin/env bash
# compose 내부 네트워크에서 측정 스크립트를 실행한다 (MCP 서버는 밖에 포트를 열지 않는다).
#   scripts/quality/run_in_container.sh mcp_checks.py <job_id>
set -euo pipefail
cd "$(dirname "$0")/../.."
version="$(git rev-parse --short HEAD)$(git diff --quiet || echo '+미커밋변경')"
docker compose run --rm --no-deps -T \
  -v "$PWD/scripts:/app/scripts" -v "$PWD/var/quality:/app/var/quality" -v "$PWD/tests:/app/tests" \
  -e REWORK_CODE_VERSION="$version" -w /app/scripts/quality \
  worker python "$@"
