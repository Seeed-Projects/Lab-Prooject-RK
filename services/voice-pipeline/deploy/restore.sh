#!/usr/bin/env bash
# 离线部署恢复(全新 RK3588 设备): 校验 → 导入镜像 → 恢复模型卷 → 放置运行时文件
# 用法: ./restore.sh [bundle_dir]     (默认: 本脚本同级 bundle/)
# 环境变量: OVS_DEPLOY_DIR(运行时文件落点, 默认 $HOME/ovs-asr-deploy), FORCE=1 强制覆盖
# 恢复完成后: docker compose -f docker-compose.asr-realtime.yml up -d
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="${1:-$SCRIPT_DIR/bundle}"
DEPLOY_DIR="${OVS_DEPLOY_DIR:-$HOME/ovs-asr-deploy}"
IMAGE_TAR="openvoicestream-asr-realtime.tar.gz"
VOL_TAR="rk-asr-models.tar.gz"
VOLUME_NAME="rk-asr-models"
IMAGE_NAME="openvoicestream:asr-realtime"
LLM_IMAGE_TAR="rkllm-summary-qwen3-1.7b-w8a8.tar.gz"
LLM_IMAGE_NAME="ghcr.io/hanzo-huang/rkllm-docker/qwen3-1.7b:w8a8-rk3588"
SUDO=""
[[ $EUID -ne 0 ]] && SUDO="sudo"

fail() { echo "[restore] ✗ $1"; exit 1; }
ok()   { echo "[restore] ✓ $1"; }

if ! command -v docker >/dev/null 2>&1; then
  bash "$SCRIPT_DIR/install_docker_offline.sh" "$BUNDLE_DIR" || fail "docker 不可用且离线安装失败"
fi

echo "[restore] bundle 目录: $BUNDLE_DIR"
for f in "$IMAGE_TAR" "$VOL_TAR" "$LLM_IMAGE_TAR" MANIFEST.json rk_manifest.json SHA256SUMS; do
  [[ -f "$BUNDLE_DIR/$f" ]] || fail "缺少 $BUNDLE_DIR/$f"
done
[[ -d "$BUNDLE_DIR/configs" ]] || fail "缺少 $BUNDLE_DIR/configs/"

echo "[restore] 1/4 SHA256 校验"
(cd "$BUNDLE_DIR" && sha256sum -c SHA256SUMS) || fail "sha256 校验失败, 文件可能损坏"
ok "校验通过"

echo "[restore] 2/4 导入镜像 $IMAGE_NAME"
if docker image inspect "$IMAGE_NAME" >/dev/null 2>&1 && [[ "${FORCE:-0}" != "1" ]]; then
  ok "镜像已存在, 跳过 (FORCE=1 可强制重新导入)"
else
  docker load -i "$BUNDLE_DIR/$IMAGE_TAR" || fail "docker load 失败"
  docker image inspect "$IMAGE_NAME" >/dev/null 2>&1 || fail "导入后仍找不到镜像 $IMAGE_NAME"
  ok "镜像已导入"
fi

echo "[restore] 2b/4 导入会议总结 LLM 镜像 $LLM_IMAGE_NAME"
if docker image inspect "$LLM_IMAGE_NAME" >/dev/null 2>&1 && [[ "${FORCE:-0}" != "1" ]]; then
  ok "LLM 镜像已存在, 跳过 (FORCE=1 可强制重新导入)"
else
  docker load -i "$BUNDLE_DIR/$LLM_IMAGE_TAR" || fail "docker load 失败(LLM 镜像)"
  docker image inspect "$LLM_IMAGE_NAME" >/dev/null 2>&1 || fail "导入后仍找不到镜像 $LLM_IMAGE_NAME"
  ok "LLM 镜像已导入"
fi

echo "[restore] 3/4 恢复模型卷 $VOLUME_NAME"
VOL_SHA="$(sha256sum "$BUNDLE_DIR/$VOL_TAR" | awk '{print $1}')"
MARKER="$DEPLOY_DIR/.rk-asr-models.sha"
if docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1 \
   && [[ -f "$MARKER" ]] && [[ "$(cat "$MARKER")" == "$VOL_SHA" ]] && [[ "${FORCE:-0}" != "1" ]]; then
  ok "卷已存在且与 bundle 一致, 跳过 (FORCE=1 可强制覆盖)"
else
  docker volume create "$VOLUME_NAME" >/dev/null || fail "docker volume create 失败"
  VOL_DATA="/var/lib/docker/volumes/$VOLUME_NAME/_data"
  [[ -d "$VOL_DATA" ]] || fail "找不到卷目录 $VOL_DATA"
  echo "[restore]   解压模型卷(需要 root, 约数 GB, 请耐心等待)..."
  $SUDO tar xzf "$BUNDLE_DIR/$VOL_TAR" -C "$VOL_DATA" || fail "卷解压失败"
  $SUDO chown -R root:root "$VOL_DATA" 2>/dev/null || true
  mkdir -p "$DEPLOY_DIR"
  echo "$VOL_SHA" > "$MARKER"
  ok "模型卷已恢复"
fi

echo "[restore] 4/4 放置运行时文件 → $DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR/runtime" "$DEPLOY_DIR/configs"
cp -f "$BUNDLE_DIR/MANIFEST.json"    "$DEPLOY_DIR/runtime/MANIFEST.json"    || fail "MANIFEST.json 复制失败"
cp -f "$BUNDLE_DIR/rk_manifest.json" "$DEPLOY_DIR/runtime/rk_manifest.json" || fail "rk_manifest.json 复制失败"
cp -rf "$BUNDLE_DIR/configs/." "$DEPLOY_DIR/configs/" || fail "configs 复制失败"
ok "运行时文件就位"

echo
echo "[restore] DONE — 启动服务:"
echo "  docker compose -f $SCRIPT_DIR/docker-compose.asr-realtime.yml up -d"
echo "  curl -s http://127.0.0.1:8621/health   # 应返回 200"
echo "  # 可选: 会议总结 LLM"
echo "  docker compose -f $SCRIPT_DIR/docker-compose.llm-summary.yml up -d"
echo "  curl -s http://127.0.0.1:8001/health   # 应返回 200"
