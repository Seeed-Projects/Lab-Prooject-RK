# 设备连接方式

## 拓扑

```
XVF3800 USB 麦克风阵列 ──USB──▶ RK3588 (seeed@192.168.14.71)
                                   ├── 主机: arecord 采集 + src/host_downmix.py 下混 + run.sh
                                   └── 容器 openvoicestream: VAD + ASR + CAM++ + 聚类
                                       (src/realtime_pipeline.py + src/ws_broadcast.py)
```

实时流水线只需要 XVF3800 + RK3588;不依赖 LLM 服务、不依赖 ESP32。

## XVF3800 麦克风阵列

| 项 | 值 |
|---|---|
| 型号 | reSpeaker XVF3800 4-Mic Array |
| USB VID/PID | `2886:001A` |
| ALSA 设备 | RK3588 `card 5: Array`(卡名 `Array`, 重启后稳定) |
| 原生格式 | 16 kHz, S16_LE, **双声道** |

注意:直接以单声道打开 `hw:Array,0` 会报 `Channels count non available`。
`src/host_downmix.py` 的做法是录双声道后用 python3 标准库下混为单声道,不依赖
asound.conf;如其他程序要以单声道直接采集,需安装 ALSA plug 配置
(RK 项目 `~/OpenVoiceStream/deploy/asound.xvf3800.conf`)。

首次使用须录 5s 真实语音核验:非静音、无削顶、RMS 足以触发 VAD。

## RK3588

| 项 | 值 |
|---|---|
| SSH | `ssh seeed@192.168.14.71`(已配免密;sudo 需密码) |
| RK 侧项目 | `services/voice-pipeline` |
| 容器 | `openvoicestream`(docker, 健康状态 `docker ps` 可见) |
| 容器 python | `/opt/venv/bin/python`(numpy 1.26.4 / onnxruntime 1.28.0 / av 18.0.0) |

### 容器内服务与文件

| 项 | 值 |
|---|---|
| Qwen3 ASR 服务(生产) | `http://127.0.0.1:8621`,健康检查 `GET /asr/capabilities` |
| 流水线临时 ASR 实例 | `http://127.0.0.1:8622`(批处理评测 `eval/stage1_rk.sh` 自动起停, 实时模式不用) |
| 会议总结 LLM(可选) | `http://127.0.0.1:8001`,健康检查 `GET /health`,容器 `rkllm-summary` |
| CAM++ 模型 | `/tmp/campplus.onnx`(28,281,164 B, sha256 `aa3cfc16…ba2`, 中英双语版, 见 docs/models.md) |
| Silero VAD 模型 | `/opt/asr/models/vad/silero_vad.onnx`(2,313,010 B) |

临时 ASR 实例跑在 :8622 是为了不动生产配置;脚本结束前自动 `pkill` 清理。

### ASR-only 部署变体(asr-realtime)

镜像 `openvoicestream:asr-realtime` 与 `openvoicestream:intranet-led` 为同一镜像
(同 image id)的 retag,差异只在启动配置:剔除 LLM/TTS/灯控/代理相关环境变量,
`ASR_MAX_NEW_TOKENS=256`。离线部署见 `deploy/restore.sh` +
`deploy/docker-compose.asr-realtime.yml`(bundle 在 `deploy/bundle/`,约 2.4 GB)。

### 健康检查

```bash
ssh seeed@192.168.14.71 'docker ps --filter name=openvoicestream --format "{{.Names}} {{.Status}}"'
# 期望: openvoicestream Up X hours (healthy)
ssh seeed@192.168.14.71 'docker exec openvoicestream curl -s http://127.0.0.1:8621/asr/capabilities'
ssh seeed@192.168.14.71 'arecord -l | grep -A1 Array'
```

## 文件传输约定

RK 网络偶发不稳:
- 小文件(<5MB): `scp` 直传;
- 大文件(segments.jsonl、音频): 一律 `gzip -c` 流式传输,落地后 `sha256sum` 两端比对;
- 互联网下载代理(如需): `http://192.168.15.137:7897`。

## 开发机(仅批处理评测需要, 实时流水线不依赖)

批处理评测链路(仓库 `eval/`)在开发机运行:python3 ≥ 3.10 +
`numpy scipy scikit-learn fastcluster`(`python3 -m venv .venv &&
.venv/bin/pip install numpy scipy scikit-learn fastcluster`)。
