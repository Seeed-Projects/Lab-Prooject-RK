# 项目模型与算法

流水线固定使用以下模型,全部本地离线运行(RK3588),不替换。

## 1. Qwen3 ASR(语音识别)— RK3588 NPU 后端

| 项 | 值 |
|---|---|
| 服务 | 容器 `openvoicestream` 内 uvicorn, `http://127.0.0.1:8621`(生产) |
| 解码器 | `decoder_qwen3.fp16.rk3588.rkllm`(RKNN, bind mount 进容器) |
| 关键参数 | `ASR_MAX_NEW_TOKENS=256`(修复 64 时的长句截断) |
| 输入 | 单段语音(建议 8–15s, VAD 在自然静音处切分) |
| 输出 | 整段中文文本(无词级时间戳) |
| 实测 | RTF ≈ 0.32(离线快于实时) |

## 2. CAM++ 说话人 embedding(3D-Speaker)

| 项 | 值 |
|---|---|
| 模型 | ModelScope `iic/speech_campplus_sv_zh_en_16k-common_advanced`(CAMPPlus, 中英双语) |
| 文件 | `/tmp/campplus.onnx`,28,281,164 B,sha256 `aa3cfc16963a10586a9393f5035d6d6b57e98d358b347f80c2a30bf4f00ceba2` |
| 运行时 | ONNX Runtime CPU,**2 线程** |
| 输入 | 16 kHz 单声道语音段 |
| 输出 | 192 维说话人 embedding(余弦相似度度量) |
| 实测 | ≈58 ms/段 |

> 原中文版 `speech_campplus_sv_zh-cn_16k-common`(sha256 `f682b514…d11`)对英文语音
> 判别力不足, 2026-08-06 换为中英双语 advanced 版; 旧模型容器内备份于
> `/tmp/campplus.zh-cn.onnx.bak`。

## 3. Silero VAD(语音活动检测)

| 项 | 值 |
|---|---|
| 文件 | `/opt/asr/models/vad/silero_vad.onnx`(2,313,010 B) |
| 参数(已固化) | threshold 0.5, min_silence 500ms, min_speech 500ms, max_speech 12s, speech_pad 200ms |
| 输出 | VAD 语音段(1035 段 / 1:48:28 录音) |

## 4. 说话人聚类 — 官方 3D-Speaker `CommonClustering`(spectral)

来源:`github.com/modelscope/3D-Speaker` `speakerlab/process/cluster.py`(Apache-2.0,
原样 vendor 到 `eval/lib/official_cluster.py`;实时模式使用同一算法在
`src/realtime_pipeline.py` 内的**纯 numpy 等价移植**, 两者同参数, 容器内无需 scipy/sklearn),参数取官方
`egs/3dspeaker/speaker-diarization/conf/diar.yaml` 默认值,**零调参**:

**实时流水线默认使用 `silhouette` 聚类**(2026-08-07 起): 直接在归一化余弦
embedding 上 k-means + silhouette 选 k(仅当 silhouette>0 才接受 k≥2,
mer_cos=0.8 兜底防过分裂)。原因: 段数少(<~10)时 p-pruning 后相似度图仍近全连接,
eigengap 高估 k 把弱簇拆成单点, 小簇过滤再把真实少数说话人并掉 → 坍缩为 1 簇
(60s 双人实测复现)。silhouette 在该数据 n=3..8 全部前缀上均给出正确二分。
`--cluster-method eigengap` 可切回下表官方行为(参数取官方默认, 零调参):

| 参数 | 值 | 说明 |
|---|---|---|
| cluster_type | spectral | 相似度 p-pruning 稀疏化 → 拉普拉斯 → eigengap 自动估 k → 谱嵌入 k-means |
| pval | 0.012 | 每行保留约 1.2% 最大相似度(稀疏化是 eigengap 生效的关键) |
| max_num_spks | 15 | 说话人数上限 |
| min_cluster_size | 4 | 小簇过滤(≤4 段并入最近大簇) |
| mer_cos | 0.8 | 质心余弦 ≥0.8 的簇合并 |

自动模式不读取任何参考人数;可选 `--oracle-k N` 先验模式必须在报告中声明来源。

## 依赖版本(RK 容器 / 开发机)

| 组件 | 版本 |
|---|---|
| 容器 numpy / onnxruntime / av | 1.26.4 / 1.28.0 / 18.0.0 |
| 开发机(eval/ 批处理评测, 实时不依赖) | python ≥3.10 + numpy / scipy / scikit-learn / fastcluster |

## 5. Qwen3-1.7B W8A8 会议总结 LLM(可选, RKLLM/NPU)

| 项 | 值 |
|---|---|
| 模型 | Qwen3-1.7B, W8A8 量化, `.rkllm` 格式(RK3588 专用) |
| 来源 | `ghcr.io/hanzo-huang/rkllm-docker/qwen3-1.7b:w8a8-rk3588`([rkllm-docker](https://github.com/Hanzo-Huang/rkllm-docker)) |
| 运行时 | RKLLM runtime v1.2.3(与 ASR 所用 `/opt/rk-runtime` 同版本) |
| 服务 | 独立容器 `rkllm-summary`, OpenAI 兼容 API `http://127.0.0.1:8001/v1/chat/completions` |
| 部署 | `docker compose -f deploy/docker-compose.llm-summary.yml up -d`(常驻) |
| 用途 | `--seconds>0` 会话结束后生成 `summary.md`(模板 `prompts/meeting_summary.md`) |
| 参考速度 | RK3576 基准 W8A8 12.57 tok/s / 稳定 RAM 2.7 GB |
| RK3588 实测 | 12.8–15.2 tok/s(1 分钟会议简洁总结 ~160 token ≈ 12–13s, 2026-08-07) |

**模型 A/B(2026-08-07, 同一份 1 分钟真实转录, 含简洁约束模板)**

| 模型 | 解码速度 | 输出 token | 总耗时 | 质量 | 结论 |
|---|---|---|---|---|---|
| **Qwen3-1.7B W8A8** | 12.8–15.2 tok/s | 157–169 | **12.2–13.2s** | 事实准确, 无幻觉 | **采用** |
| Qwen2.5-1.5B W8A8 | 17.1–20.1 tok/s | 232–361 | 13.6–18.3s | 幻觉(虚构参会人/截止时间), 无视长度约束 | 弃用 |

结论: 1.5B 单 token 更快但输出失控, 总耗时反而更长且质量不合格。
四节结构+行动项表格的输出下限 ~160 token, 12–13s 为该模板在当前 NPU 上的实际下限;
要进 10s 只能精简模板结构(如去掉行动项表格)。
