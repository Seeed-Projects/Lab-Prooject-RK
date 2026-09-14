#!/usr/bin/env bash
# 一次性环境准备(幂等, 可重复执行): 宿主依赖 → 容器 → 模型 → ASR 服务 → 麦克风
# 在 RK3588 主机上运行。已完成的步骤自动跳过; 不改动 LED 生产配置。
set -uo pipefail

# docker 可能不在调用方 PATH 中: 探测常见安装位置并自动补齐
if ! command -v docker >/dev/null 2>&1; then
  for d in /usr/bin /usr/local/bin /snap/bin /usr/local/sbin /opt/bin; do
    if [[ -x "$d/docker" ]]; then
      export PATH="$d:$PATH"
      echo "[bootstrap] docker 不在 PATH, 已自动补上: $d/docker"
      break
    fi
  done
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER="${CONTAINER:-openvoicestream}"
CARD="${CARD:-Array}"
ASR_URL="${ASR_URL:-http://127.0.0.1:8621}"
MODEL_CACHE="${MODEL_CACHE:-$HOME/.ovs/models}"

CAMPPLUS_SHA="aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2"
# sherpa-onnx 官方导出的 ONNX(ModelScope 官方仓库只发 PyTorch .bin, 无 onnx)
# 中英文双语 advanced 版(训练数据含 VoxCeleb 英文), 替换原 zh-cn 版以改善英文说话人区分
CAMPPLUS_URLS=(
  "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
)
VAD_SHA="6b99cbfd39246b6706f98ec13c7c50c6b299181f2474fa05cbc8046acc274396"
# Silero VAD v5.0 固定 tag(master 已升级到 v6, sha 不匹配)
VAD_URLS=(
  "https://raw.githubusercontent.com/snakers4/silero-vad/v5.0/files/silero_vad.onnx"
  "https://cdn.jsdelivr.net/gh/snakers4/silero-vad@v5.0/files/silero_vad.onnx"
)
# 容器内目标路径(可用环境变量覆盖, 便于不动生产文件测试下载分支)
CAMPPLUS_CONTAINER_PATH="${CAMPPLUS_CONTAINER_PATH:-/tmp/campplus.onnx}"
VAD_CONTAINER_PATH="${VAD_CONTAINER_PATH:-/opt/asr/models/vad/silero_vad.onnx}"

FAIL=0
ok()  { echo "[bootstrap]   ✓ $1"; }
bad() { echo "[bootstrap]   ✗ $1"; FAIL=1; }

echo "[bootstrap] 1/7 宿主依赖"
command -v python3 >/dev/null && ok "python3" || bad "缺少 python3 (sudo apt install python3)"
if ! command -v docker >/dev/null 2>&1; then
  bash "$HERE/deploy/install_docker_offline.sh" || true
fi
command -v docker  >/dev/null && ok "docker"  || bad "缺少 docker 且离线安装失败 → 手动执行 deploy/install_docker_offline.sh 查看原因"
command -v arecord >/dev/null && ok "arecord (alsa-utils)" || bad "缺少 arecord (sudo apt install alsa-utils)"
command -v curl    >/dev/null && ok "curl" || bad "缺少 curl"

echo "[bootstrap] 2/7 容器 $CONTAINER"
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    ok "容器运行中"
  else
    echo "[bootstrap]   容器已停止, 尝试启动 ..."
    docker start "$CONTAINER" >/dev/null 2>&1 && ok "容器已启动" || bad "容器启动失败 → docker logs $CONTAINER"
  fi
else
  RESTORED=0
  for bd in "$HERE/deploy/bundle" "$HOME/ovs-asr-bundle"; do
    if [[ -f "$bd/openvoicestream-asr-realtime.tar.gz" && -f "$bd/../restore.sh" ]]; then
      echo "[bootstrap]   容器不存在, 发现离线 bundle $bd → 运行 restore.sh"
      if bash "$bd/../restore.sh" "$bd"; then RESTORED=1; else bad "restore.sh 恢复失败"; fi
      break
    fi
  done
  if [[ $RESTORED -eq 1 ]]; then
    if docker compose version >/dev/null 2>&1; then
      docker compose -f "$HERE/deploy/docker-compose.asr-realtime.yml" up -d >/dev/null 2>&1
      sleep 3
    fi
    if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
      ok "容器已从离线 bundle 恢复并创建"
    else
      bad "恢复成功但容器未创建 → 手动执行 docker compose -f deploy/docker-compose.asr-realtime.yml up -d 后重跑本脚本"
    fi
  else
    bad "容器 $CONTAINER 不存在, 也未找到离线 bundle (deploy/bundle/) → 先准备 bundle 或联系设备提供方"
  fi
fi

echo "[bootstrap] 3/7 容器内 python 依赖"
if docker exec "$CONTAINER" /opt/venv/bin/python -c "import numpy, onnxruntime" >/dev/null 2>&1; then
  ok "numpy + onnxruntime (/opt/venv)"
else
  bad "容器内 /opt/venv 缺 numpy/onnxruntime"
fi

echo "[bootstrap] 4/7 模型 (sha256 校验, 缺失则下载)"
mkdir -p "$MODEL_CACHE"
# 首次运行且未配置代理时提示一次; 不阻塞, 之后不再提醒
if [[ -z "${http_proxy:-}${https_proxy:-}${HTTP_PROXY:-}${HTTPS_PROXY:-}" ]] \
   && [[ ! -f "$MODEL_CACHE/.proxy_hint_shown" ]]; then
  echo "[bootstrap]   提示: 未检测到代理环境变量 (http_proxy/https_proxy)。"
  echo "[bootstrap]   若本机访问外网需要代理, 请先设置后重新执行本脚本:"
  echo "[bootstrap]     export http_proxy=http://192.168.15.137:7897"
  echo "[bootstrap]     export https_proxy=http://192.168.15.137:7897"
  echo "[bootstrap]   (代理地址见 docs/device_connection.md;模型已齐备或可直连时无需设置)"
  touch "$MODEL_CACHE/.proxy_hint_shown"
fi
ensure_model() {  # $1=名称 $2=容器内路径 $3=sha256 $4=缓存文件名  $5..=URLs
  local name="$1" cpath="$2" sha="$3" fname="$4"; shift 4
  local cur="$(docker exec "$CONTAINER" sha256sum "$cpath" 2>/dev/null | awk '{print $1}')"
  if [[ "$cur" == "$sha" ]]; then ok "$name 已在容器内且校验通过"; return; fi
  local cache="$MODEL_CACHE/$fname"
  if [[ ! -f "$cache" ]] || [[ "$(sha256sum "$cache" | awk '{print $1}')" != "$sha" ]]; then
    rm -f "$cache"
    local u ok_dl=0
    for u in "$@"; do
      echo "[bootstrap]   下载 $name: $u"
      if curl -fL --retry 6 --retry-delay 3 --retry-all-errors -C - -o "$cache" "$u" 2>/dev/null; then ok_dl=1; break; fi
    done
    [[ $ok_dl -eq 1 ]] || { bad "$name 下载失败(网络不通或 URL 失效), 请手动放置 $cache"; return; }
    if [[ "$(sha256sum "$cache" | awk '{print $1}')" != "$sha" ]]; then
      rm -f "$cache"; bad "$name 下载完成但 sha256 不符, 已删除"; return
    fi
  fi
  docker cp "$cache" "$CONTAINER:$cpath" && ok "$name 已装入容器" || bad "$name docker cp 失败"
}
ensure_model "CAM++ (speech_campplus_sv_zh_en_16k-common_advanced)" "$CAMPPLUS_CONTAINER_PATH" "$CAMPPLUS_SHA" campplus.onnx "${CAMPPLUS_URLS[@]}"
ensure_model "Silero VAD" "$VAD_CONTAINER_PATH" "$VAD_SHA" silero_vad.onnx "${VAD_URLS[@]}"

echo "[bootstrap] 5/7 ASR 服务"
code="$(curl -s -m 8 -o /dev/null -w '%{http_code}' "$ASR_URL/health" 2>/dev/null || echo 000)"
if [[ "$code" == "200" ]]; then
  ok "ASR $ASR_URL 健康 (/health 200)"
elif [[ "$code" != "000" ]]; then
  ok "ASR $ASR_URL 有响应 (HTTP $code, 非 /health 200, 留意)"
else
  bad "ASR $ASR_URL 无响应 → docker logs $CONTAINER 查看 uvicorn 状态(不要擅自重启生产容器)"
fi

echo "[bootstrap] 6/7 XVF3800 麦克风"
if arecord -l 2>/dev/null | grep -q "$CARD"; then
  ok "声卡 '$CARD' 在位"
else
  bad "找不到声卡 '$CARD' (USB VID:PID 2886:001A) → 检查 USB 连接, 见 docs/device_connection.md"
fi

echo "[bootstrap] 7/7 LLM 总结服务(可选组件)"
code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/health 2>/dev/null || echo 000)"
if [[ "$code" == "200" ]]; then
  ok "LLM 总结服务健康 (http://127.0.0.1:8001)"
else
  echo "[bootstrap]   ℹ 未运行(可选)。部署: docker compose -f deploy/docker-compose.llm-summary.yml up -d"
fi

echo
if [[ $FAIL -eq 0 ]]; then
  echo "[bootstrap] READY — 可以运行 ./run.sh"
else
  echo "[bootstrap] 存在问题, 见上方 ✗ 项"
fi
exit $FAIL
