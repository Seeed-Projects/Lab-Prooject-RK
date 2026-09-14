# XVF3800 实时说话人识别 + ASR 转录流水线

XVF3800 USB 麦克风阵列录入真实说话人音频 → 流式 VAD 切段 → 段级 ASR 转录 + CAM++
说话人 embedding → 在线说话人聚类 → 结构化的"说话人 + 文本"结果**实时输出**。
纯音频流水线:**不含 LLM、不含 ESP32 灯控**。

```
XVF3800 USB 麦克风 (16kHz 双声道)
   │  arecord → host_downmix.py 下混单声道              [RK3588 主机]
   ▼
流式 Silero VAD → 段级 Qwen3 ASR + CAM++ embedding      [容器 openvoicestream]
   │  在线 provisional 标签 + 每 10 段官方谱聚类全量重聚类
   ▼
实时输出: {"type":"turn","speaker":2,"text":"..."}
   ├─ stdout JSONL 事件流            (直接消费)
   ├─ ws://<RK3588-IP>:8765          (WebSocket 广播, 任意订阅者)
   └─ --out-dir/transcript.txt       (实时更新的可读转录)
```

## 快速开始(RK3588 主机上, 三步)

```bash
./bootstrap.sh                                     # 1. 一次性环境准备(幂等, 可重复执行)
./run.sh --language en --seconds 0 --out-dir /tmp/ovs_realtime   # 2. 启动实时会话(Ctrl-C 结束)
websocat ws://192.168.14.71:8765                   # 3. 局域网任意机器订阅事件流
```

话音结束后约 0.6–1.5s(短段)stdout 即输出第一条
`{"type":"turn", idx, start, end, speaker, text, ...}` 事件;
说话人编号跨重聚类保持稳定,历史标签追溯修正。

## 部署到新设备(离线 bundle)

`deploy/bundle/` 是完全离线的部署包(约 4.5 GB):容器镜像
`openvoicestream:asr-realtime` 的 gzip、ASR 模型卷 `rk-asr-models` 的 gzip、
会议总结 LLM 镜像 `qwen3-1.7b:w8a8-rk3588` 的 gzip、
**docker 29.7.2 + compose v5.4.0 离线安装包(aarch64 静态二进制)**、
运行时 manifest/configs 与 SHA256SUMS。全新 RK3588 上(无需联网、无需预装 docker):

```bash
cd deploy
./restore.sh                                          # docker 缺失时先离线安装 → sha256 校验 → docker load(ASR+LLM 镜像) → 恢复模型卷 → 放运行时文件(幂等)
docker compose -f docker-compose.asr-realtime.yml up -d
curl -s http://127.0.0.1:8621/asr/capabilities        # 200 即 ASR 服务就绪
docker compose -f docker-compose.llm-summary.yml up -d   # 可选: 会议总结 LLM(:8001)
curl -s http://127.0.0.1:8001/health                  # 200 即 LLM 就绪
```

该变体含 ASR(Qwen3 RKLLM + Silero VAD + RK 运行时)+ 可选会议总结 LLM
(Qwen3-1.7B W8A8, NPU),无 TTS/灯控,`ASR_MAX_NEW_TOKENS=256`(长句不截断)。
若本机已有容器但缺镜像/卷,`./bootstrap.sh` 检测到 bundle 会自动走 restore 并起 compose。

## 常用命令

| 命令 | 说明 |
|---|---|
| `./run.sh --seconds 60` | 录 1 分钟后自动结束,当前展示效果最佳方案 |
| `./run.sh --seconds 0` | 持续运行直到 Ctrl-C |
| `./run.sh --wav /tmp/test.wav` | 文件输入(16kHz 单声道 wav),无麦克风时的冒烟测试 |
| `./run.sh --ws-port 0` | 关闭 WebSocket 广播 |
| `./run.sh --ws-port 9000` | 改广播端口(默认 8765) |
| `./run.sh --language en` | 指定 ASR 语言: `zh`(中文, 默认)或 `en`(英文), 其他值拒绝 |
| `./run.sh --recluster-every 5` | 每累计 N 段触发一次全量重聚类(默认 3, 越小分离出得越快) |
| `./run.sh --cluster-method eigengap` | 切回官方 3D-Speaker 谱聚类(默认 silhouette, 少段数更稳) |
| `./run.sh --loopback assets/loopback_demo_2spk_40s.wav --language en` | 兜底验证: wav 直送流水线, 同时 XVF3800 扬声器同步播放(不依赖麦克风拾音) |
| `./run.sh --no-summarize` | 关闭会话后 LLM 总结(`--seconds>0` 默认自动总结) |
| `./run.sh --out-dir DIR` | 产物目录(默认 /tmp/ovs_realtime) |
| `./run.sh --skip-preflight` | 跳过启动前检查(调试用) |

`run.sh` 启动前自动 preflight:容器、ASR 服务、模型 sha256、XVF3800 声卡;
任何一项失败会给出对应修复指引(多数情况是先跑 `./bootstrap.sh`)。
docker 不在调用方 PATH 中时(如被后端服务进程拉起)会自动探测
/usr/bin、/usr/local/bin、/snap/bin 等常见位置;每次运行的完整日志
落盘 `$OUT_DIR/run.log`,供上层服务读取排错。

## WebSocket 订阅(实时消费端)

订阅地址 `ws://<RK3588-IP>:8765`(`--ws-port` 修改)。标准 RFC 6455 协议:
每个 text frame 就是一个 JSONL 事件;会话内事件(session/turn/relabel)与 stdout
同构。会话结束后连接保持,LLM 总结完成时推送 `{"type":"summary","status":"ok",
"text":"<Markdown 全文>"}` 事件,推送完毕服务端才发 close 帧。服务器忽略客户端
发来的 text 帧,支持 ping/pong 保活与 close 握手;无鉴权,仅限局域网使用。

```bash
websocat ws://192.168.14.71:8765
```

```python
# pip install websockets
import asyncio, websockets
async def main():
    async with websockets.connect("ws://192.168.14.71:8765") as ws:
        async for event in ws:
            print(event)   # {"type":"turn","speaker":1,"text":"..."}
asyncio.run(main())
```

事件 schema(session / turn / relabel / summary)与全部输出文件格式见
[docs/data_interfaces.md](docs/data_interfaces.md)。

## 最终指标(1 小时 48 分钟真实会议录音, 实际 6 名说话人)

| 指标 | Test 80% | 全录音 |
|------|----------|--------|
| 自动说话人数 | **6** (实际 6) | 6 |
| 说话人准确率(时长加权) | **0.9077** | 0.9160 |
| DER (collar=0.25) | **0.5809** | 0.5545 |
| CER (concatenated, 主指标) | **0.4093** | 0.3904 |
| 话轮级说话人准确率 | 0.8989 | 0.9072 |
| 联合话轮准确率(文本+说话人) | 0.0499 | 0.0454 |

完整口径与分母见 [results/final_metrics.md](results/final_metrics.md)。
资源占用(60s 音频连续处理, 最坏工况):整机 CPU 平均 ~27%(8 核),容器 ≈2.0 核,
流水线进程内存峰值 160 MB;ASR 解码跑在 NPU(RKLLM, 三核并行, 最热核均值 ~44%)。
存储:模型共 30.6 MB + RKLLM decoder 1.53 GB,详见 final_metrics.md。

## 交付结构

```
realtime-pipeline/
├── README.md                # 本文件
├── bootstrap.sh             # 一次性环境准备(依赖/容器/模型, 幂等)
├── run.sh                   # 唯一运行入口(preflight → 采集 → 流水线 → 广播)
├── src/
│   ├── realtime_pipeline.py # 流水线主体(容器内运行)
│   ├── ws_broadcast.py      # WebSocket 广播服务(纯标准库)
│   ├── host_downmix.py      # 双声道→单声道(纯标准库)
│   └── summarize.py         # 会议总结(会话后调 LLM, 纯标准库)
├── prompts/
│   └── meeting_summary.md   # 会议总结提示词模板
├── docs/
│   ├── device_connection.md # 设备连接(XVF3800 + RK3588)
│   ├── data_interfaces.md   # 输入/输出/事件流/WebSocket 接口
│   └── models.md            # 模型与聚类算法
├── deploy/                  # ASR-only 最小部署变体(新设备/离线)
│   ├── docker-compose.asr-realtime.yml
│   ├── docker-compose.llm-summary.yml  # 会议总结 LLM(Qwen3-1.7B W8A8, 可选)
│   ├── restore.sh           # 校验 → 导入镜像 → 恢复模型卷 → 运行时文件(幂等)
│   └── bundle/              # 离线 bundle: ASR+LLM 镜像+模型卷+运行时文件+SHA256SUMS(约 4.4 GB)
└── results/
    └── final_metrics.md     # 最终指标(最优配置)
```

## 文档

- [docs/device_connection.md](docs/device_connection.md) — XVF3800 与 RK3588 的连接、容器内服务、健康检查
- [docs/data_interfaces.md](docs/data_interfaces.md) — 音频输入、JSONL 事件流、WebSocket、产物文件格式
- [docs/models.md](docs/models.md) — Qwen3 ASR / CAM++ / Silero VAD / 官方 3D-Speaker 谱聚类

## 已知限制

1. 默认聚类为 k-means+silhouette 选 k: 单人场景 silhouette≤0 不误分, 双人场景
   实测 3 段(约 13s)即可首次分离; 仅当显式 `--cluster-method eigengap` 时,
   累计 ≤4 段的说话人会被并入最近大簇(官方 min_cluster_size=4 行为);
   重聚类的标签修正始终追溯到历史段;
2. 重叠语音不支持;
3. ASR 在 worker 线程串行,长段(接近 12s 上限)会推迟后续段的 emit,不丢音频;
4. WebSocket 广播无鉴权,不要暴露到公网。
5. 会议总结为会话后批处理,约 1 分钟量级(Qwen3-1.7B W8A8, NPU),不实时;
   总结质量依赖转录质量,长会议(>1h)转录超出上下文时总结仅覆盖前部内容。

批处理评测流水线不属于本交付(位于仓库 `eval/`)。
