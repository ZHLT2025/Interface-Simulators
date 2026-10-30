#!/usr/bin/env bash
# 新五墙 interface-simulators-5w 的唯一 compose 入口（U01 V1.2 D-7 第 4 步）。
#
#   deploy/5w/compose-5w.sh config
#   deploy/5w/compose-5w.sh up -d
#   deploy/5w/compose-5w.sh ps
#   deploy/5w/compose-5w.sh down          # 只停本项目；绝不加 -v 除非是本步回退
#
# 守卫：必须在新工作树 /root/project/portal-5w/interface-simulators 内运行。
# 旧工作树 /root/project/portal-20261022/interface-simulators 只读，任何写/起停都被拒绝。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
EXPECT_ROOT="/root/project/portal-5w/interface-simulators"
PROJECT="interface-simulators-5w"
ENV_FILE="deploy/5w/.env.5w"
OVERRIDE="deploy/5w/compose.5w.override.yaml"

# ---- 工作树守卫 ----------------------------------------------------------
if [[ "$ROOT" != "$EXPECT_ROOT" ]]; then
  echo "REFUSED: 本脚本只能在 $EXPECT_ROOT 内运行（当前 $ROOT）。" >&2
  echo "         旧工作树 /root/project/portal-20261022/interface-simulators 为只读保护对象。" >&2
  exit 2
fi
branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
if [[ "$branch" != 5w/* ]]; then
  echo "REFUSED: 期望 5w/* 分支，实际 '$branch'。" >&2
  exit 2
fi
for f in docker-compose.yml "$OVERRIDE" "$ENV_FILE"; do
  [[ -f "$ROOT/$f" ]] || { echo "REFUSED: 缺少 $f（.env.5w 见 deploy/5w/.env.5w.example 与 README.md）。" >&2; exit 2; }
done
[[ -f "$ROOT/deploy/5w/secrets/mosquitto-passwd" ]] || {
  echo "REFUSED: 缺少 deploy/5w/secrets/mosquitto-passwd（见 README.md 第 3 步）。" >&2; exit 2; }

# ---- 危险参数守卫 --------------------------------------------------------
# down -v 会删除本项目卷；本栈只有绑定挂载，-v 只在「本步回退」时由人工显式确认后使用。
if [[ "${1:-}" == "down" ]]; then
  for a in "$@"; do
    if [[ "$a" == "-v" || "$a" == "--volumes" ]]; then
      if [[ "${SIM5W_CONFIRM_DOWN_V:-}" != "yes" ]]; then
        echo "REFUSED: down -v 需 SIM5W_CONFIRM_DOWN_V=yes（只用于本步回退，只删本项目对象）。" >&2
        exit 2
      fi
    fi
  done
fi

cd "$ROOT"
exec docker compose -p "$PROJECT" \
  -f docker-compose.yml -f "$OVERRIDE" \
  --env-file "$ENV_FILE" "$@"
