#!/usr/bin/env bash
# 部署脚本:本地构建前端 → rsync 后端代码与前端产物到服务器 → 重启服务
#
# 用法:
#   scripts/deploy.sh                    # 部署到默认服务器(ubuntu@134.175.68.24,/opt/edream)
#   DEPLOY_HOST=root@1.2.3.4 scripts/deploy.sh
#   DEPLOY_SKIP_FRONTEND=1 scripts/deploy.sh   # 只更新后端(前端没改时)
#
# 服务器侧要求(首次部署已就绪):/opt/edream + .env + venv + systemd 服务 edream.service
# 仅更新 .env 配置(如切换 Casdoor/COS)后也执行本脚本即可,重启会生效。

set -euo pipefail

DEPLOY_HOST="${DEPLOY_HOST:-ubuntu@134.175.68.24}"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/edream}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_TMP="/tmp/edream-upload"

cd "$(dirname "$0")/.."
ROOT_DIR=$(pwd)

SSH_CMD="ssh -i $SSH_KEY -o BatchMode=yes"
RSYNC_CMD="rsync -az --delete --exclude=__pycache__ -e \"$SSH_CMD\""

echo "==> 目标: ${DEPLOY_HOST}:${DEPLOY_DIR}"

if [ -z "${DEPLOY_SKIP_FRONTEND:-}" ]; then
  # 找 node(兼容 nvm 不在 PATH 的环境)
  if ! command -v npm >/dev/null 2>&1; then
    for candidate in "$HOME"/.nvm/versions/node/*/bin; do
      [ -x "$candidate/npm" ] && export PATH="$candidate:$PATH" && break
    done
  fi
  command -v npm >/dev/null 2>&1 || { echo "!! 找不到 npm,请先安装 Node 或用 DEPLOY_SKIP_FRONTEND=1 跳过前端"; exit 1; }

  echo "==> 构建前端"
  (cd frontend && [ -d node_modules ] || npm install; npm run build)
fi

echo "==> 上传产物(服务器临时目录)"
$SSH_CMD "$DEPLOY_HOST" "rm -rf $REMOTE_TMP && mkdir -p $REMOTE_TMP/frontend-dist"
eval rsync -az --delete --exclude=__pycache__ -e "'$SSH_CMD'" \
  "$ROOT_DIR/backend" "$ROOT_DIR/main.py" "$ROOT_DIR/pyproject.toml" "$DEPLOY_HOST:$REMOTE_TMP/"
eval rsync -az --delete -e "'$SSH_CMD'" \
  "$ROOT_DIR/frontend/dist/" "$DEPLOY_HOST:$REMOTE_TMP/frontend-dist/"

echo "==> 落盘 ${DEPLOY_DIR} 并重启服务"
$SSH_CMD "$DEPLOY_HOST" "DEPLOY_DIR=$DEPLOY_DIR bash -s" <<'REMOTE'
set -e
sudo mkdir -p "$DEPLOY_DIR/frontend/dist"
sudo rsync -a --delete /tmp/edream-upload/backend /tmp/edream-upload/main.py /tmp/edream-upload/pyproject.toml "$DEPLOY_DIR/"
sudo rsync -a --delete /tmp/edream-upload/frontend-dist/ "$DEPLOY_DIR/frontend/dist/"
sudo chown -R "$(stat -c %U "$DEPLOY_DIR")":"$(stat -c %G "$DEPLOY_DIR")" "$DEPLOY_DIR"
cd "$DEPLOY_DIR"
.venv/bin/pip install -q .
sudo systemctl restart edream.service
rm -rf /tmp/edream-upload
sleep 2
systemctl is-active edream.service
curl -sf http://127.0.0.1:8000/api/health && echo
REMOTE

echo "==> 部署完成 ✓"
