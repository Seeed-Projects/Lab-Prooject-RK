#!/usr/bin/env bash
# 实时流水线唯一入口: preflight → XVF3800 采集(或 --wav 文件) → 容器内流水线
# → stdout JSONL 事件流 + transcript 实时落盘 + WebSocket 广播
# 在 RK3588 主机上运行。Ctrl-C 结束会话, 自动从容器取回产物。
#
# 用法:
#   ./run.sh --seconds 300 --out-dir /tmp/ovs_realtime      # 录 5 分钟
#   ./run.sh --seconds 0   --out-dir /tmp/ovs_realtime      # 持续运行直到 Ctrl-C
#   ./run.sh --wav /tmp/test.wav --out-dir /tmp/ovs_rt      # 文件输入(16k 单声道 wav)
#   ./run.sh --ws-port 0 ...                                # 关闭 WebSocket 广播
#   ./run.sh --language en --out-dir /tmp/ovs_rt            # 指定 ASR 语言(仅 zh/en, 默认 zh)
#   ./run.sh --recluster-every 5                            # 每 5 段全量重聚类(默认 3)
#   ./run.sh --cluster-method eigengap                      # 切回官方 3D-Speaker 谱聚类(默认 silhouette)
#   ./run.sh --loopback assets/loopback_demo_2spk_40s.wav --language en
#       # 兜底验证: wav 直送流水线 + 扬声器同步播放(不依赖麦克风拾音, 最大音量)
#   ./run.sh --loopback xx.wav --play-dev plughw:rockchipes8311,0
#       # 改从 RK 板载 ES8311(耳机口)播放; 默认 hw:$CARD,0(XVF3800 扬声器口)
#   ./run.sh --no-summarize                               # 关闭会话后 LLM 总结(--seconds>0 默认自动总结)
# 会话结束后, LLM 总结以 {"type":"summary",...} 事件推送给所有 WebSocket 订阅者,
# 推送完毕才关闭广播服务; 订阅者因此能看到完整会话 + 总结, 再收到关帧。
set -uo pipefail

# docker 可能不在调用方(如后端服务进程)的 PATH 中: 探测常见安装位置并自动补齐
if ! command -v docker >/dev/null 2>&1; then
  for d in /usr/bin /usr/local/bin /snap/bin /usr/local/sbin /opt/bin; do
    if [[ -x "$d/docker" ]]; then
      export PATH="$d:$PATH"
      echo "[env] docker 不在 PATH, 已自动补上: $d/docker"
      break
    fi
  done
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
CONTAINER="${CONTAINER:-openvoicestream}"
CARD="${CARD:-Array}"
ASR_URL="${ASR_URL:-http://127.0.0.1:8621}"
CAPTURE_PERIOD_US="${CAPTURE_PERIOD_US:-100000}"
CAPTURE_BUFFER_US="${CAPTURE_BUFFER_US:-2000000}"
CAMPPLUS_SHA="aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2"
VAD_SHA="6b99cbfd39246b6706f98ec13c7c50c6b299181f2474fa05cbc8046acc274396"

SECONDS_TO_RECORD=60
OUT_DIR="/tmp/ovs_realtime"
WAV=""
RECLUSTER_EVERY=3
CLUSTER_METHOD=""
LOOPBACK_WAV=""
PLAY_DEV=""
WS_PORT=8765
LANGUAGE="zh"
SUMMARY="auto"
LLM_URL="${LLM_URL:-http://127.0.0.1:8001}"
SKIP_PREFLIGHT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --seconds) SECONDS_TO_RECORD="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --wav) WAV="$2"; shift 2 ;;
    --recluster-every) RECLUSTER_EVERY="$2"; shift 2 ;;
    --cluster-method) CLUSTER_METHOD="$2"; shift 2 ;;
    --loopback) LOOPBACK_WAV="$2"; shift 2 ;;
    --play-dev) PLAY_DEV="$2"; shift 2 ;;
    --ws-port) WS_PORT="$2"; shift 2 ;;
    --language|--lang) LANGUAGE="$2"; shift 2 ;;
    --summarize) SUMMARY="on"; shift ;;
    --no-summarize) SUMMARY="off"; shift ;;
    --llm-url) LLM_URL="$2"; shift 2 ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    -h|--help) grep '^# ' "$0" | sed 's/^# //'; exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

mkdir -p "$OUT_DIR" 2>/dev/null || true
export CAPTURE_HEARTBEAT="$OUT_DIR/capture_heartbeat"
export DOWNMIX_HEARTBEAT="$OUT_DIR/downmix_heartbeat"
export AUDIO_LEVEL_PATH="$OUT_DIR/audio_level.json"
PIPELINE_STAGES="$OUT_DIR/pipeline_stages"
mkdir -p "$PIPELINE_STAGES" 2>/dev/null || true
if [[ -w "$OUT_DIR" ]]; then
  exec > >(tee -a "$OUT_DIR/run.log") 2>&1
  echo "[realtime] 本次运行日志: $OUT_DIR/run.log"
fi

case "$LANGUAGE" in
  zh|en) ;;
  *) echo "错误: --language 只支持 zh(中文) 或 en(英文), 收到: '$LANGUAGE'"; exit 1 ;;
esac

# ── 兜底模式准备: wav 直送流水线 + XVF3800 扬声器同步播放 ──
if [[ -n "$LOOPBACK_WAV" ]]; then
  [[ -f "$LOOPBACK_WAV" ]] || { echo "错误: --loopback 音频不存在: $LOOPBACK_WAV"; exit 1; }
  command -v aplay >/dev/null || { echo "错误: 缺少 aplay(alsa-utils)"; exit 1; }
  arecord -l 2>/dev/null | grep -q "$CARD" \
    || { echo "错误: 找不到声卡 '$CARD'(扬声器播放需要) → 检查 USB 连接"; exit 1; }
  python3 - "$LOOPBACK_WAV" <<'PYCONV'
import audioop, sys, wave
src = sys.argv[1]
w = wave.open(src)
rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
data = w.readframes(w.getnframes()); w.close()
if width != 2:
    data = audioop.lin2lin(data, width, 2)
if rate != 16000:
    data, _ = audioop.ratecv(data, 2, ch, rate, 16000, None)
mono = audioop.tomono(data, 2, 1, 1) if ch == 2 else data
stereo = data if ch == 2 else audioop.tostereo(mono, 2, 1, 1)
for path, d, c in (("/tmp/rt_loopback_mono.wav", mono, 1),
                   ("/tmp/rt_loopback_play.wav", stereo, 2)):
    o = wave.open(path, "wb")
    o.setnchannels(c); o.setsampwidth(2); o.setframerate(16000)
    o.writeframes(d); o.close()
print(f"[loopback] 已转换 {src}: {len(mono)/2/16000:.1f}s → 16kHz mono(流水线) + stereo(扬声器)")
PYCONV
  WAV=/tmp/rt_loopback_mono.wav
fi

ok()   { echo "[preflight]   ✓ $1"; }
bad()  { echo "[preflight]   ✗ $1"; FAIL=1; }

preflight() {
  echo "[preflight] 检查运行环境 ..."
  FAIL=0
  # 0. docker 命令本身
  if ! command -v docker >/dev/null 2>&1; then
    bad "找不到 docker 命令(已探测 PATH 与 /usr/bin /usr/local/bin /snap/bin /usr/local/sbin /opt/bin)→ 安装 docker 后 ./bootstrap.sh; 若 docker 装在其它位置, export PATH 后重试"
  fi
  # 1. 容器在跑
  if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    ok "容器 $CONTAINER 运行中"
  elif command -v docker >/dev/null 2>&1; then
    bad "容器 $CONTAINER 未运行 → docker start $CONTAINER (或 ./bootstrap.sh)"
  fi
  # 2. ASR 服务健康(/health 应返回 200)
  code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$ASR_URL/health" 2>/dev/null || true)"
  [[ -z "$code" ]] && code=000
  if [[ "$code" == "200" ]]; then ok "ASR 服务 $ASR_URL 健康 (/health 200)"
  elif [[ "$code" != "000" ]]; then ok "ASR 服务 $ASR_URL 有响应 (HTTP $code, 非 /health 200, 留意)"
  else bad "ASR 服务 $ASR_URL 无响应 → ./bootstrap.sh"; fi
  # 3. 容器内模型 sha256
  camp="$(docker exec "$CONTAINER" sha256sum /tmp/campplus.onnx 2>/dev/null | awk '{print $1}')"
  if [[ "$camp" == "$CAMPPLUS_SHA" ]]; then ok "CAM++ 模型 sha256 一致"; else bad "CAM++ 模型缺失或校验失败 → ./bootstrap.sh"; fi
  vad="$(docker exec "$CONTAINER" sha256sum /opt/asr/models/vad/silero_vad.onnx 2>/dev/null | awk '{print $1}')"
  if [[ "$vad" == "$VAD_SHA" ]]; then ok "Silero VAD 模型 sha256 一致"; else bad "Silero VAD 模型缺失或校验失败 → ./bootstrap.sh"; fi
  # 4. 麦克风(仅实时采集模式需要)
  if [[ -z "$WAV" ]]; then
    if arecord -l 2>/dev/null | grep -q "$CARD"; then ok "XVF3800 声卡 '$CARD' 在位"; else bad "找不到声卡 '$CARD' → 检查 USB 连接 (docs/device_connection.md)"; fi
    command -v python3 >/dev/null && ok "python3 (host_downmix)" || bad "缺少 python3"
  fi
  # 5. LLM 总结服务(可选, 不阻塞: 缺失时会话后跳过总结)
  if [[ "$SUMMARY" != "off" ]]; then
    lcode="$(curl -s -m 3 -o /dev/null -w '%{http_code}' "$LLM_URL/health" 2>/dev/null || echo 000)"
    if [[ "$lcode" == "200" ]]; then ok "LLM 总结服务 $LLM_URL 健康"
    else echo "[preflight]   ℹ LLM 总结服务 $LLM_URL 未运行(会话后将跳过总结; 部署: docker compose -f deploy/docker-compose.llm-summary.yml up -d)"; fi
  fi
  if [[ $FAIL -ne 0 ]]; then
    echo "[preflight] 未通过, 先修复上述问题(环境安装: ./bootstrap.sh)"
    exit 1
  fi
  echo "[preflight] 全部通过"
}

[[ $SKIP_PREFLIGHT -eq 1 ]] || preflight

echo "[realtime] 上传流水线到容器 ..."
docker cp "$HERE/src/realtime_pipeline.py" "$CONTAINER:/tmp/realtime_pipeline.py"
docker cp "$HERE/src/ws_broadcast.py" "$CONTAINER:/tmp/ws_broadcast.py"
docker exec "$CONTAINER" mkdir -p "$OUT_DIR"

CTRL_PORT=$((WS_PORT + 1))
if [[ "$WS_PORT" -gt 0 ]]; then
  # 清理可能残留的旧广播 daemon(上次异常退出留下的孤儿进程), best-effort
  docker exec "$CONTAINER" /opt/venv/bin/python /tmp/ws_broadcast.py \
    --send-control --control-port "$CTRL_PORT" --cmd close >/dev/null 2>&1 || true
  # 常驻广播 daemon: 生命周期与流水线解耦, 会话结束后继续存活, 直到总结事件推送完毕
  docker exec -d "$CONTAINER" sh -c \
    "/opt/venv/bin/python /tmp/ws_broadcast.py --daemon --port $WS_PORT --control-port $CTRL_PORT --idle-timeout 0 >>/tmp/ws_broadcast.log 2>&1"
  READY=0
  for _ in $(seq 1 20); do
    if docker exec "$CONTAINER" /opt/venv/bin/python /tmp/ws_broadcast.py \
        --send-control --control-port "$CTRL_PORT" --cmd ping 2>/dev/null | grep -q '"ok": true'; then
      READY=1; break
    fi
    sleep 0.25
  done
  if [[ $READY -eq 1 ]]; then
    echo "[realtime] 广播 daemon 就绪 (ws :$WS_PORT / 控制 127.0.0.1:$CTRL_PORT)"
  else
    echo "[realtime] ⚠ 广播 daemon 未就绪, 流水线将回退进程内广播(订阅者会在会话结束时断开)"
  fi
fi

PIPE_ARGS="--out-dir $OUT_DIR --recluster-every $RECLUSTER_EVERY --events-file $OUT_DIR/events.jsonl --ws-port $WS_PORT --language $LANGUAGE"
[[ -n "$CLUSTER_METHOD" ]] && PIPE_ARGS="$PIPE_ARGS --cluster-method $CLUSTER_METHOD"
[[ "$WS_PORT" -gt 0 ]] && PIPE_ARGS="$PIPE_ARGS --ws-control-port $CTRL_PORT"

if [[ "$WS_PORT" -gt 0 ]]; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo "[realtime] WebSocket 订阅地址: ws://${IP:-<RK3588-IP>}:${WS_PORT} (任意标准 WebSocket 客户端)"
fi

PLAY_PID=""
if [[ -n "$LOOPBACK_WAV" ]]; then
  amixer -c "$CARD" sset 'PCM',0 60,60 >/dev/null 2>&1 \
    && echo "[loopback] 扬声器音量已设为最大 60,60(默认最大音量播放)" \
    || echo "[loopback] ⚠ 无法设置扬声器音量(不影响流水线)"
  PLAY_DEV="${PLAY_DEV:-hw:${CARD},0}"
  trap '[[ -n "${PLAY_PID:-}" ]] && kill "$PLAY_PID" 2>/dev/null' EXIT
  aplay -D "$PLAY_DEV" -q /tmp/rt_loopback_play.wav >/dev/null 2>&1 &
  PLAY_PID=$!
  echo "[loopback] 扬声器开始播放($PLAY_DEV), 同时 wav 直送流水线 ..."
fi

if [[ -n "$WAV" ]]; then
  echo "[realtime] 文件输入模式: $WAV (拷入容器)"
  docker cp "$WAV" "$CONTAINER:/tmp/rt_input.wav" || { echo "[realtime] ✗ 无法读取 $WAV"; exit 1; }
  docker exec "$CONTAINER" /opt/venv/bin/python /tmp/realtime_pipeline.py $PIPE_ARGS --wav /tmp/rt_input.wav
else
  DUR_FLAG=""
  [[ "$SECONDS_TO_RECORD" -gt 0 ]] && DUR_FLAG="-d $SECONDS_TO_RECORD"
  rm -f "$PIPELINE_STAGES"/*.json "$OUT_DIR/pipeline_diagnostics.json"
  : >"$OUT_DIR/arecord.stderr.log"
  : >"$OUT_DIR/host_downmix.stderr.log"
  echo "[realtime] XVF3800 实时采集 ${SECONDS_TO_RECORD}s(0=持续), Ctrl-C 结束 ..."
  echo "[realtime] ALSA capture period=${CAPTURE_PERIOD_US}us buffer=${CAPTURE_BUFFER_US}us downmix=v2-buffered"
  trap 'echo "[realtime] 收到中断, 等待流水线收尾 ..."' INT
  write_stage_state() {
    local stage="$1" pid="$2" alive="$3" returncode="$4" started_at="$5" exited_at="$6"
    local signal_json=null sigpipe=false tmp
    if [[ "$returncode" != "null" && "$returncode" -ge 128 ]]; then
      signal_json=$((returncode - 128))
    fi
    [[ "$returncode" == "141" ]] && sigpipe=true
    tmp="$PIPELINE_STAGES/.${stage}.${BASHPID}.tmp"
    printf '{"stage":"%s","pid":%s,"alive":%s,"returncode":%s,"exit_signal":%s,"sigpipe":%s,"started_at":%s,"exited_at":%s}\n' \
      "$stage" "$pid" "$alive" "$returncode" "$signal_json" "$sigpipe" "$started_at" "$exited_at" >"$tmp"
    mv -f "$tmp" "$PIPELINE_STAGES/$stage.json"
  }
  run_arecord() {
    arecord -D "hw:${CARD},0" -f S16_LE -r 16000 -c 2 -t raw -q \
      --period-time "$CAPTURE_PERIOD_US" --buffer-time "$CAPTURE_BUFFER_US" \
      $DUR_FLAG 2>>"$OUT_DIR/arecord.stderr.log" &
    local child_pid=$! started_at exited_at rc
    started_at="$(date +%s.%N)"
    write_stage_state arecord "$child_pid" true null "$started_at" null
    wait "$child_pid"; rc=$?
    exited_at="$(date +%s.%N)"
    write_stage_state arecord "$child_pid" false "$rc" "$started_at" "$exited_at"
    return "$rc"
  }
  run_downmix() {
    python3 "$HERE/src/host_downmix.py" <&0 2>>"$OUT_DIR/host_downmix.stderr.log" &
    local child_pid=$! started_at exited_at rc
    started_at="$(date +%s.%N)"
    write_stage_state host_downmix "$child_pid" true null "$started_at" null
    wait "$child_pid"; rc=$?
    exited_at="$(date +%s.%N)"
    write_stage_state host_downmix "$child_pid" false "$rc" "$started_at" "$exited_at"
    return "$rc"
  }
  run_docker_exec() {
    docker exec -i "$CONTAINER" /opt/venv/bin/python /tmp/realtime_pipeline.py $PIPE_ARGS <&0 &
    local child_pid=$! started_at exited_at rc
    started_at="$(date +%s.%N)"
    write_stage_state docker_exec "$child_pid" true null "$started_at" null
    wait "$child_pid"; rc=$?
    exited_at="$(date +%s.%N)"
    write_stage_state docker_exec "$child_pid" false "$rc" "$started_at" "$exited_at"
    return "$rc"
  }
  run_arecord | run_downmix | run_docker_exec
  PIPE_STATUS=("${PIPESTATUS[@]}")
  python3 - "$OUT_DIR/pipeline_diagnostics.json" \
    "${PIPE_STATUS[0]:-}" "${PIPE_STATUS[1]:-}" "${PIPE_STATUS[2]:-}" "$$" <<'PYPIPE'
import json
import sys
import time

path, arecord_rc, downmix_rc, docker_rc, shell_pid = sys.argv[1:]
pipeline = {}
for name, raw in (
    ("arecord", arecord_rc),
    ("host_downmix", downmix_rc),
    ("docker_exec", docker_rc),
):
    try:
        returncode = int(raw)
    except ValueError:
        returncode = None
    pipeline[name] = {
        "returncode": returncode,
        "exit_signal": returncode - 128 if returncode is not None and returncode >= 128 else None,
        "sigpipe": returncode == 141,
    }
with open(path, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "timestamp": time.time(),
            "run_sh_pid": int(shell_pid),
            "pipeline": pipeline,
        },
        handle,
        indent=2,
    )
    handle.write("\n")
PYPIPE
fi

if [[ -n "$PLAY_PID" ]]; then
  wait "$PLAY_PID" 2>/dev/null || true
  echo "[loopback] 扬声器播放结束(音量保持最大 60,60)"
  trap - EXIT
fi

echo "[realtime] 取回产物 ..."
mkdir -p "$OUT_DIR"
for f in transcript.txt transcript.jsonl segments.jsonl events.jsonl realtime_stats.json; do
  docker cp "$CONTAINER:$OUT_DIR/$f" "$OUT_DIR/$f" 2>/dev/null || true
done
echo "[realtime] 完成: $OUT_DIR/{transcript.txt,transcript.jsonl,segments.jsonl,events.jsonl,realtime_stats.json}"

# ── 会话后 LLM 总结(--seconds>0 自动; --summarize 强制; --no-summarize 关闭)──
SUMMARY_STATUS="skipped"
SUMMARY_REASON="未请求总结"
DO_SUMMARY=0
if [[ "$SUMMARY" == "on" ]]; then DO_SUMMARY=1
elif [[ "$SUMMARY" == "auto" && "$SECONDS_TO_RECORD" -gt 0 ]]; then DO_SUMMARY=1; fi
if [[ $DO_SUMMARY -eq 1 ]]; then
  lcode="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$LLM_URL/health" 2>/dev/null || echo 000)"
  if [[ "$lcode" == "200" ]]; then
    python3 "$HERE/src/summarize.py" --out-dir "$OUT_DIR" --llm-url "$LLM_URL" \
      --prompt-file "$HERE/prompts/meeting_summary.md"
    rc=$?
    if [[ $rc -eq 0 ]]; then
      SUMMARY_STATUS="ok"
    elif [[ $rc -eq 2 ]]; then
      SUMMARY_REASON="无可识别的对话内容"
    else
      SUMMARY_STATUS="failed"; SUMMARY_REASON="总结生成失败 (rc=$rc, 详见 run.log)"
    fi
  else
    SUMMARY_REASON="LLM 总结服务 $LLM_URL 不可用 (HTTP $lcode)"
    echo "[realtime] ⚠ LLM 总结服务 $LLM_URL 不可用(HTTP $lcode), 跳过总结 → docker compose -f deploy/docker-compose.llm-summary.yml up -d"
  fi
elif [[ "$SUMMARY" == "off" ]]; then
  SUMMARY_REASON="--no-summarize 已指定"
else
  SUMMARY_REASON="持续模式(--seconds 0)不自动总结"
fi

# ── 把 summary 事件推送给 WebSocket 订阅者, 然后关闭广播 daemon ──
if [[ "$WS_PORT" -gt 0 ]]; then
  SUMMARY_LINE="$(python3 - "$OUT_DIR" "$SUMMARY_STATUS" "$SUMMARY_REASON" <<'PY'
import json, os, sys
out_dir, status, reason = sys.argv[1], sys.argv[2], sys.argv[3]
ev = {"type": "summary", "status": status}
path = os.path.join(out_dir, "summary.md")
if status == "ok" and os.path.exists(path):
    ev["text"] = open(path, encoding="utf-8").read().rstrip()
elif reason:
    ev["reason"] = reason
print(json.dumps(ev, ensure_ascii=False))
PY
)"
  if docker exec "$CONTAINER" /opt/venv/bin/python /tmp/ws_broadcast.py \
      --send-control --control-port "$CTRL_PORT" --cmd emit --line "$SUMMARY_LINE" >/dev/null 2>&1; then
    echo "[realtime] summary 事件已推送给 WebSocket 订阅者 (status=$SUMMARY_STATUS)"
  else
    echo "[realtime] ⚠ summary 推送 WebSocket 失败(不影响产物文件, summary.md 在 $OUT_DIR)"
  fi
  docker exec "$CONTAINER" /opt/venv/bin/python /tmp/ws_broadcast.py \
    --send-control --control-port "$CTRL_PORT" --cmd close >/dev/null 2>&1 || true
fi
