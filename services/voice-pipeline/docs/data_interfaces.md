# 数据接口说明

实时流水线的输入、事件流、WebSocket 订阅与产物文件格式。

## 音频输入

| 通道 | 格式 |
|---|---|
| XVF3800 实时采集(run.sh 默认) | `arecord -D hw:Array,0 -f S16_LE -r 16000 -c 2`,经 `src/host_downmix.py` 下混为 16kHz 单声道后喂入容器 |
| `--wav` 文件输入 | 16kHz 单声道 S16 WAV(其他格式先用 ffmpeg 转:`ffmpeg -i in.m4a -ar 16000 -ac 1 out.wav`) |

容器内流水线消费的是 16kHz S16_LE 单声道原始音频(stdin 或 wav 文件)。

## 实时事件流(stdout / WebSocket / events.jsonl 三通道同构)

每行(每个 WebSocket text frame)一个 JSON 事件:

| type | 时机 | 字段 |
|---|---|---|
| `session` | 会话起止 | event=start/end; start 带 sample_rate, asr_url, recluster_every;end 带 num_segments, num_speakers |
| `turn` | 每段话音结束后 ~0.6–5s | idx, start, end, speaker, speaker_state, text, asr_ms, emb_ms, emit_delay_s |
| `relabel` | 每 N 段(默认 3)重聚类后 | num_speakers, mapping(新簇→稳定说话人编号), cluster_ms |
| `summary` | 会话结束后 LLM 总结完成,由 run.sh 推送(**仅 WebSocket 通道**,不在 stdout/events.jsonl) | status=ok/skipped/failed;ok 带 text(Markdown 全文),skipped/failed 带 reason |

- `speaker`:1 起的整数编号,跨重聚类保持稳定(按时间重叠匹配)。
- `speaker_state`:`provisional` / `provisional_new` = 最近质心快速赋值;
  重聚类后相关段以 `relabel` 事件整体修正,transcript 落盘文件反映最终标签。
- 时间戳来自采样计数(窗索引 × 32ms ± 200ms pad),不是墙钟。

## WebSocket 订阅

| 项 | 值 |
|---|---|
| 端点 | `ws://<RK3588-IP>:8765`(`--ws-port` 修改,`--ws-port 0` 关闭) |
| 协议 | RFC 6455,服务端→客户端 text frame;每帧一个 JSONL 事件(与 stdout 逐字节同构) |
| 订阅语义 | 连接成功后收到之后产生的全部事件;不补发连接前的历史(历史看 events.jsonl);会话结束后连接保持,收到 `summary` 事件后服务端才发 close 帧 |
| 生命周期 | 广播 daemon 由 run.sh 在容器内常驻启动,会话结束后继续存活(最长 15 分钟 idle 保护),推送完 summary 事件后主动关闭 |
| 客户端→服务端 | text/binary 帧被忽略;ping 自动回 pong;支持 close 握手 |
| 容量/鉴权 | 最多 32 个并发订阅者;无鉴权,仅限局域网 |
| 实现 | `src/ws_broadcast.py`(纯标准库;daemon 广播 + 127.0.0.1 控制端口,run.sh 经其推送 summary/关流;daemon 不可达时流水线回退进程内广播) |

## 产物文件(--out-dir,会话结束时从容器取回)

| 文件 | 说明 |
|---|---|
| `transcript.txt` | 人类可读转录,**实时更新**(与参考文本同格式) |
| `transcript.jsonl` | 合并话轮结构化结果(最终交付格式,见下) |
| `segments.jsonl` | VAD 段级中间结果(含 embedding),可再聚类 |
| `events.jsonl` | 完整事件流存档(与 stdout 相同) |
| `realtime_stats.json` | 段数/说话人数/ASR/embedding/聚类耗时统计 |
| `summary.md` | 会议总结(Markdown 四节结构),会话后 LLM 生成,同时以 `summary` 事件推送给 WebSocket 订阅者;未部署 LLM 或无 `--seconds` 时不产生 |

### transcript.jsonl(对外交付格式)

每行一个合并话轮(相邻同说话人、间隔 ≤2.0s 的段合并):

| 字段 | 类型 | 说明 |
|---|---|---|
| `idx` | int | 话轮序号 |
| `start` / `end` / `duration` | float | 起止/时长(秒) |
| `speaker` | int | 说话人编号,**1 起**,按首次出现排序 |
| `text` | str | 该话轮 ASR 文本 |

### segments.jsonl

每行一个 VAD 语音段:

| 字段 | 类型 | 说明 |
|---|---|---|
| `idx` | int | 段序号 |
| `start` / `end` / `duration` | float | 起止/时长(秒) |
| `text` | str | ASR 转录文本 |
| `asr_ms` / `embedding_ms` | float | ASR / embedding 耗时 |
| `embedding` | float[192] | CAM++ 说话人 embedding |

### transcript.txt

```
转录结果|实时

文字记录:

说话人 1 00:00 
这个麦克风是 XVF3800 ...

说话人 2 00:12 
好, 现在换我说话 ...
```

## 参考文本格式(仅评测场景用)

```
<任意头部>
文字记录:
说话人 1 00:00 
功能都可以做。
说话人 2 00:40 
...
```

`说话人 <id> <MM:SS 或 HH:MM:SS>` 行 + 紧随其后的文本行。参考只有话轮起点时间,
终点用下一话轮起点近似。评测链路(批处理)不属于本交付。
