#!/usr/bin/env python3
"""实时说话人识别 + ASR 转录流水线(容器内运行)

输入: stdin 或 --wav 文件的 16kHz S16_LE 单声道原始音频(XVF3800 采集)
输出(实时): stdout JSONL 事件流(同一事件流可经 --ws-port WebSocket 广播给订阅者)
  {"type":"turn", idx, start, end, speaker, speaker_state, text, ...}   每段话音结束后 ~1-4s 内输出
  {"type":"relabel", num_speakers, mapping}                             周期性重聚类后的标签修正
输出(落盘, --out-dir): transcript.txt(实时更新) + 结束时写出
  segments.jsonl(与批处理 Stage 1 兼容, 可直接接 stage2_cluster.py 复聚类)
  transcript.jsonl / cluster_info.json / realtime_stats.json

方法: 流式 Silero VAD(512 样本窗, 状态机) → 段级 Qwen3 ASR(HTTP) + CAM++ embedding
→ 在线 provisional 标签(最近质心) + 每 N 段全量重聚类(默认 k-means+silhouette 选 k,
对少段数稳定; 可切回官方 3D-Speaker 谱聚类 eigengap, 纯 numpy 移植, 见 lib),
按时间重叠稳定说话人编号。
"""
import argparse
import io
import json
import queue
import signal
import sys
import threading
import time
import uuid
import wave
from collections import defaultdict
from pathlib import Path

import numpy as np
import onnxruntime as ort

SAMPLE_RATE = 16000
WINDOW = 512  # silero 16k 窗口


# ── 官方谱聚类的纯 numpy 移植(与 lib/official_cluster.py SpectralCluster 一致) ──

def _kmeans(X, k, seed=42, n_init=5, iters=100):
    rng = np.random.default_rng(seed)
    best = None
    for _ in range(n_init):
        centers = [X[rng.integers(len(X))]]
        for _ in range(1, k):
            d = np.min(np.stack([((X - c) ** 2).sum(1) for c in centers]), axis=0)
            if d.sum() <= 0:
                centers.append(X[rng.integers(len(X))])
                continue
            centers.append(X[rng.choice(len(X), p=d / d.sum())])
        C = np.stack(centers)
        assign = np.zeros(len(X), dtype=int)
        for _ in range(iters):
            assign = ((X[:, None, :] - C[None]) ** 2).sum(-1).argmin(1)
            newC = np.stack([X[assign == j].mean(0) if (assign == j).any() else C[j]
                             for j in range(k)])
            if np.allclose(newC, C):
                break
            C = newC
        inertia = float(((X - C[assign]) ** 2).sum())
        if best is None or inertia < best[0]:
            best = (inertia, assign)
    return best[1]


def spectral_cluster(embs, pval=0.012, min_num_spks=1, max_num_spks=15, oracle_num=None):
    n = len(embs)
    if n <= 1:
        return np.zeros(n, dtype=int)
    X = embs / (np.linalg.norm(embs, axis=1, keepdims=True) + 1e-8)
    M = X @ X.T
    n_elems = min(int((1 - pval) * n), n - 6)  # min_pnum=6
    for i in range(n):
        M[i, np.argsort(M[i])[:n_elems]] = 0.0
    M = 0.5 * (M + M.T)
    np.fill_diagonal(M, 0.0)
    L = -M
    L[np.diag_indices(n)] = np.abs(M).sum(1)
    w, v = np.linalg.eigh(L)
    if oracle_num:
        k = min(oracle_num, n)
    else:
        hi = min(max_num_spks + 1, n)
        lo = max(min_num_spks - 1, 0)
        gaps = np.diff(w[lo:hi])
        k = int(np.argmax(gaps)) + min_num_spks if len(gaps) else 1
        k = min(k, n)
    return _kmeans(v[:, :k], k)


def filter_minor_cluster(labels, x, min_cluster_size=4):
    cset = np.unique(labels)
    csize = np.array([(labels == i).sum() for i in cset])
    minor = cset[csize <= min_cluster_size]
    if len(minor) == 0:
        return labels
    major = cset[csize > min_cluster_size]
    if len(major) == 0:
        return np.zeros_like(labels)
    centers = np.stack([x[labels == i].mean(0) for i in major])
    centers = centers / (np.linalg.norm(centers, axis=1, keepdims=True) + 1e-8)
    for i in range(len(labels)):
        if labels[i] in minor:
            xn = x[i] / (np.linalg.norm(x[i]) + 1e-8)
            labels[i] = major[int(np.argmax(centers @ xn))]
    return labels


def merge_by_cos(labels, x, cos_thr=0.8):
    while True:
        cset = np.unique(labels)
        if len(cset) <= 1:
            break
        centers = np.stack([x[labels == i].mean(0) for i in cset])
        centers = centers / (np.linalg.norm(centers, axis=1, keepdims=True) + 1e-8)
        aff = np.triu(centers @ centers.T, 1)
        idx = np.unravel_index(int(np.argmax(aff)), aff.shape)
        if aff[idx] < cos_thr:
            break
        labels[labels == cset[idx[1]]] = cset[idx[0]]
    return labels


def official_cluster(embs, pval=0.012, max_num_spks=15, min_cluster_size=4, mer_cos=0.8,
                     oracle_num=None):
    """官方 CommonClustering(spectral) 的等价 numpy 实现。"""
    labels = spectral_cluster(embs, pval=pval, max_num_spks=max_num_spks, oracle_num=oracle_num)
    labels = filter_minor_cluster(labels, embs, min_cluster_size)
    if mer_cos is not None:
        labels = merge_by_cos(labels, embs, mer_cos)
    return labels


def silhouette_score(Xn, labels):
    """归一化 embedding 上的平均 silhouette(余弦距离 = 1 - cos)。"""
    lab = np.asarray(labels)
    if len(np.unique(lab)) < 2:
        return -1.0
    sims = Xn @ Xn.T
    vals = []
    for i in range(len(Xn)):
        same = lab == lab[i]
        same[i] = False
        if not same.any():
            vals.append(0.0)
            continue
        a = (1.0 - sims[i][same]).mean()
        b = min((1.0 - sims[i][lab == c]).mean() for c in np.unique(lab) if c != lab[i])
        vals.append((b - a) / max(a, b))
    return float(np.mean(vals))


# 短段(<2.5s) embedding 噪声下, 同一人 3 段实测 silhouette 噪声底 ≤0.035;
# 真实双人分离实测 ≥0.211。取 0.1 为接受阈值, 两者之间留 3x 余量。
SIL_ACCEPT_MIN = 0.1


def silhouette_cluster(embs, max_num_spks=15, mer_cos=0.8, oracle_num=None):
    """k-means + silhouette 选 k(直接在归一化余弦 embedding 上)。

    eigengap 谱聚类在段数少(<~10)时 p-pruning 后相似度图仍近全连接,
    eigengap 倾向高估 k(把弱簇拆成单点), 再经小簇过滤把真实少数说话人
    全部并掉 → 坍缩为 1 簇。silhouette 直接度量划分质量, 小样本稳定:
    仅当 silhouette > SIL_ACCEPT_MIN 才接受 k>=2, 否则判 1 人;
    mer_cos 保留为防过分裂兜底。
    """
    n = len(embs)
    if n <= 1:
        return np.zeros(n, dtype=int)
    X = embs / (np.linalg.norm(embs, axis=1, keepdims=True) + 1e-8)
    if oracle_num:
        k = min(oracle_num, n)
        labels = _kmeans(X, k) if k > 1 else np.zeros(n, dtype=int)
    else:
        best_labels, best_score = np.zeros(n, dtype=int), SIL_ACCEPT_MIN
        for k in range(2, min(max_num_spks, n - 1) + 1):
            labels = _kmeans(X, k)
            if len(np.unique(labels)) < 2:
                continue
            score = silhouette_score(X, labels)
            if score > best_score:
                best_labels, best_score = labels, score
        labels = best_labels
    if mer_cos is not None:
        labels = merge_by_cos(labels, embs, mer_cos)
    return labels


# ── 工具 ──

def wav_bytes(audio_f32: np.ndarray, sr: int = SAMPLE_RATE) -> bytes:
    pcm = (np.clip(audio_f32, -1.0, 1.0) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def call_asr(url: str, wav: bytes, language: str = "zh", timeout: float = 120.0):
    import urllib.request
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="seg.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n").encode() + wav + \
           f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{url}/asr?language={language}", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[realtime] ASR call failed: {e}", file=sys.stderr, flush=True)
        return None


def fmt_time(t: float) -> str:
    t = int(t)
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


# ── 流式 VAD 状态机 ──

class StreamingVad:
    def __init__(self, model_path, threshold=0.5, min_silence_ms=500, min_speech_ms=500,
                 max_speech_s=12.0, speech_pad_ms=200):
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        self.session = ort.InferenceSession(model_path, sess_options=so,
                                            providers=["CPUExecutionProvider"])
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.sr_arr = np.array(SAMPLE_RATE, dtype=np.int64)
        self.threshold = threshold
        self.win_s = WINDOW / SAMPLE_RATE
        self.min_silence_w = max(1, int(min_silence_ms / 1000 / self.win_s))
        self.min_speech_w = max(1, int(min_speech_ms / 1000 / self.win_s))
        self.max_speech_w = int(max_speech_s / self.win_s)
        self.pad_samples = int(speech_pad_ms / 1000 * SAMPLE_RATE)
        self.in_speech = False
        self.cur_start_w = 0
        self.speech_w = 0
        self.silence_w = 0

    def prob(self, chunk_f32: np.ndarray) -> float:
        out, self.state = self.session.run(None, {
            "input": chunk_f32[np.newaxis].astype(np.float32),
            "state": self.state, "sr": self.sr_arr})
        return float(np.asarray(out).ravel()[0])

    def process_window(self, win_idx: int, prob: float):
        """返回需要 finalize 的区域列表 [(start_s, end_s), ...]"""
        out = []
        if not self.in_speech:
            if prob >= self.threshold:
                self.in_speech = True
                self.cur_start_w = win_idx
                self.speech_w, self.silence_w = 1, 0
            return out
        if prob >= self.threshold:
            self.speech_w += 1
            self.silence_w = 0
        else:
            self.silence_w += 1
        length_w = win_idx - self.cur_start_w + 1
        if length_w >= self.max_speech_w:
            out.append(self._region(win_idx + 1))
            self.in_speech = False
            return out
        if self.silence_w >= self.min_silence_w:
            if self.speech_w >= self.min_speech_w:
                out.append(self._region(win_idx - self.silence_w + 1))
            self.in_speech = False
        return out

    def flush(self, total_windows: int):
        if self.in_speech and self.speech_w >= self.min_speech_w:
            return [self._region(total_windows)]
        return []

    def _region(self, end_w):
        return (self.cur_start_w * self.win_s, end_w * self.win_s)


# ── 主流程 ──

class RealtimePipeline:
    def __init__(self, args, broker=None):
        self.args = args
        self.broker = broker
        self.vad = StreamingVad(args.vad_model, args.vad_threshold, args.vad_min_silence,
                                args.vad_min_speech, args.vad_max_speech, args.vad_speech_pad)
        from voxedge.capabilities.speaker_embedding import SpeakerEmbedder
        self.embedder = SpeakerEmbedder(args.campplus_model, num_threads=args.threads)
        self.out_dir = Path(args.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.seg_q = queue.Queue()
        self.segments = []          # dict: start,end,audio_keep(bool),text,emb,label
        self.next_speaker_id = 1
        self.cluster_state = {"centroids": [], "labels": np.array([], dtype=int)}
        self.turn_count = 0
        self.stats = {"asr_ms": [], "emb_ms": [], "cluster_ms": [], "emit_delay_s": []}
        self._stop = threading.Event()

    # ── 输出 ──
    def emit(self, obj):
        line = json.dumps(obj, ensure_ascii=False)
        with self.lock:
            print(line, flush=True)
            if self.args.events_file:
                with open(self.args.events_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        if self.broker is not None:
            try:
                self.broker.publish(line)
            except Exception as e:
                print(f"[realtime] ws publish failed: {e}", file=sys.stderr, flush=True)

    def write_transcript(self):
        lines = ["转录结果|实时", "", "文字记录:", ""]
        for s in sorted(self.segments, key=lambda x: x["start"]):
            if s.get("label") is None or not s.get("text"):
                continue
            lines.append(f"说话人 {s['label']} {fmt_time(s['start'])} ")
            lines.append(s["text"])
            lines.append("")
        (self.out_dir / "transcript.txt").write_text("\n".join(lines), encoding="utf-8")

    # ── 段处理(worker 线程) ──
    def process_segment(self, start, end, audio):
        t_vad_done = time.perf_counter()
        wav = wav_bytes(audio)
        t0 = time.perf_counter()
        asr_res = call_asr(self.args.asr_url, wav, self.args.language)
        asr_ms = (time.perf_counter() - t0) * 1000
        text = (asr_res or {}).get("text", "")
        t0 = time.perf_counter()
        try:
            emb = self.embedder.compute(audio, SAMPLE_RATE)
            emb = np.asarray(emb, dtype=np.float32).ravel()
        except Exception as e:
            print(f"[realtime] embedding failed: {e}", file=sys.stderr, flush=True)
            emb = None
        emb_ms = (time.perf_counter() - t0) * 1000

        seg = {"start": round(start, 3), "end": round(end, 3), "text": text,
               "emb": emb, "label": None, "asr_ms": round(asr_ms, 1),
               "emb_ms": round(emb_ms, 1)}
        with self.lock:
            idx = len(self.segments)
            self.segments.append(seg)
            seg["idx"] = idx
            # provisional 标签: 最近质心
            if emb is not None and len(self.cluster_state["centroids"]) > 0:
                en = emb / (np.linalg.norm(emb) + 1e-8)
                sims = np.stack(self.cluster_state["centroids"]) @ en
                seg["label"] = int(np.argmax(sims)) + 1
                seg["speaker_state"] = "provisional"
            else:
                seg["label"] = self.next_speaker_id
                seg["speaker_state"] = "provisional_new"
                if emb is not None:
                    self.cluster_state["centroids"].append(
                        emb / (np.linalg.norm(emb) + 1e-8))
                self.next_speaker_id += 1
            self.stats["asr_ms"].append(asr_ms)
            self.stats["emb_ms"].append(emb_ms)
            self.turn_count += 1

        self.emit({"type": "turn", "idx": idx, "start": seg["start"], "end": seg["end"],
                   "speaker": seg["label"], "speaker_state": seg["speaker_state"],
                   "text": text, "asr_ms": seg["asr_ms"], "emb_ms": seg["emb_ms"],
                   "emit_delay_s": round(time.perf_counter() - t_vad_done, 2)})
        self.write_transcript()

        if self.turn_count % self.args.recluster_every == 0:
            self.recluster()

    def recluster(self):
        with self.lock:
            embs_idx = [i for i, s in enumerate(self.segments) if s["emb"] is not None]
            if len(embs_idx) < 2:
                return
            embs = np.stack([self.segments[i]["emb"] for i in embs_idx])
            prev_labels = np.array([self.segments[i]["label"] or 0 for i in embs_idx])
            durs = np.array([self.segments[i]["end"] - self.segments[i]["start"] for i in embs_idx])
        t0 = time.perf_counter()
        if self.args.cluster_method == "silhouette":
            new_labels = silhouette_cluster(embs, max_num_spks=self.args.max_speakers,
                                            mer_cos=self.args.mer_cos)
        else:
            new_labels = official_cluster(embs, pval=self.args.pval,
                                          max_num_spks=self.args.max_speakers,
                                          min_cluster_size=self.args.min_cluster_size,
                                          mer_cos=self.args.mer_cos)
        # 稳定编号: 按时间重叠把新簇映射回旧说话人编号
        overlap = defaultdict(float)
        for pl, nl, d in zip(prev_labels, new_labels, durs):
            overlap[(int(pl), int(nl))] += d
        old_of_new, used_old = {}, set()
        for (pl, nl), d in sorted(overlap.items(), key=lambda kv: -kv[1]):
            if nl not in old_of_new and pl not in used_old and pl > 0 and d > 0:
                old_of_new[nl] = pl
                used_old.add(pl)
        mapping, next_id = {}, self.next_speaker_id
        for nl in np.unique(new_labels):
            if int(nl) not in old_of_new:
                old_of_new[int(nl)] = next_id
                next_id += 1
            mapping[int(nl)] = old_of_new[int(nl)]
        with self.lock:
            for j, i in enumerate(embs_idx):
                self.segments[i]["label"] = mapping[int(new_labels[j])]
                self.segments[i]["speaker_state"] = "final"
            self.next_speaker_id = next_id
            # 更新质心状态
            cents = []
            for spk in sorted({s["label"] for s in self.segments if s["emb"] is not None}):
                m = np.stack([s["emb"] for s in self.segments
                              if s["label"] == spk and s["emb"] is not None]).mean(0)
                cents.append(m / (np.linalg.norm(m) + 1e-8))
            self.cluster_state = {"centroids": cents,
                                  "speakers": sorted({s["label"] for s in self.segments
                                                      if s["emb"] is not None})}
            self.stats["cluster_ms"].append((time.perf_counter() - t0) * 1000)
            k = len(np.unique(new_labels))
        self.emit({"type": "relabel", "num_speakers": k,
                   "mapping": {str(a): int(b) for a, b in mapping.items()},
                   "cluster_ms": round((time.perf_counter() - t0) * 1000, 1)})
        self.write_transcript()

    # ── 音频读取主循环 ──
    def run(self, audio_iter):
        worker = threading.Thread(target=self._worker, daemon=True)
        worker.start()
        buf = np.zeros(0, dtype=np.float32)
        win_idx = 0
        pad_w = max(1, int(self.vad.pad_samples / WINDOW))
        pre = []            # 非语音窗滚动缓冲(最多 pad_w 个), 作区域头部 pad
        cur_audio = []      # 当前语音区域的窗
        self._pending = None  # [start_s, end_s, parts, tail_need]

        def close_pending():
            if self._pending is None:
                return
            st, en, parts, _ = self._pending
            self._pending = None
            audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
            if len(audio) >= int(0.3 * SAMPLE_RATE):
                self.seg_q.put((st, en, audio))

        def finalize_region(st, en):
            nonlocal pre, cur_audio
            parts = []
            if pre:
                pre_cat = np.concatenate(pre)
                if len(pre_cat) > self.vad.pad_samples:
                    pre_cat = pre_cat[-self.vad.pad_samples:]
                parts.append(pre_cat)
            parts.extend(cur_audio)
            cur_audio, pre = [], []
            self._pending = [max(0.0, st - self.vad.pad_samples / SAMPLE_RATE),
                             en + self.vad.pad_samples / SAMPLE_RATE, parts, pad_w]

        for chunk in audio_iter:
            buf = np.concatenate([buf, chunk])
            while len(buf) >= WINDOW:
                win = buf[:WINDOW]
                buf = buf[WINDOW:]
                prob = self.vad.prob(win)
                was_in = self.vad.in_speech
                regions = self.vad.process_window(win_idx, prob)
                now_in = self.vad.in_speech
                if self._pending is not None and now_in:
                    close_pending()
                for (st, en) in regions:
                    finalize_region(st, en)
                if was_in or now_in:
                    cur_audio.append(win)
                else:
                    if self._pending is not None:
                        self._pending[2].append(win)
                        self._pending[3] -= 1
                        if self._pending[3] <= 0:
                            close_pending()
                    pre.append(win)
                    if len(pre) > pad_w:
                        pre.pop(0)
                win_idx += 1
        for (st, en) in self.vad.flush(win_idx):
            finalize_region(st, en)
        close_pending()
        self.seg_q.put(None)
        worker.join()
        self.finalize_outputs()

    def _worker(self):
        while True:
            item = self.seg_q.get()
            if item is None:
                break
            st, en, audio = item
            self.process_segment(st, en, audio)
        self.recluster()  # 收尾最终聚类

    def finalize_outputs(self):
        with self.lock:
            segs_out = []
            for s in self.segments:
                segs_out.append({"idx": s["idx"], "start": s["start"], "end": s["end"],
                                 "duration": round(s["end"] - s["start"], 3),
                                 "text": s["text"], "normalized_text": s["text"],
                                 "asr_ms": s["asr_ms"], "embedding_ms": s["emb_ms"],
                                 "embedding": s["emb"].tolist() if s["emb"] is not None else None,
                                 "speaker": s["label"]})
            with (self.out_dir / "segments.jsonl").open("w", encoding="utf-8") as f:
                for r in segs_out:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            merged = []
            for s in sorted(self.segments, key=lambda x: x["start"]):
                if not s.get("text"):
                    continue
                if merged and merged[-1]["speaker"] == s["label"] and \
                   s["start"] - merged[-1]["end"] <= 2.0:
                    merged[-1]["end"] = s["end"]
                    merged[-1]["text"] += s["text"]
                else:
                    merged.append({"start": s["start"], "end": s["end"],
                                   "speaker": s["label"], "text": s["text"]})
            with (self.out_dir / "transcript.jsonl").open("w", encoding="utf-8") as f:
                for i, m in enumerate(merged):
                    f.write(json.dumps({"idx": i, "start": round(m["start"], 3),
                                        "end": round(m["end"], 3),
                                        "duration": round(m["end"] - m["start"], 3),
                                        "speaker": m["speaker"], "text": m["text"]},
                                       ensure_ascii=False) + "\n")
            json.dump({"num_segments": len(self.segments),
                       "num_turns": len(merged),
                       "num_speakers": len({s["label"] for s in self.segments}),
                       "recluster_every": self.args.recluster_every,
                       "asr_ms_mean": round(float(np.mean(self.stats["asr_ms"] or [0])), 1),
                       "emb_ms_mean": round(float(np.mean(self.stats["emb_ms"] or [0])), 1),
                       "cluster_ms": [round(x, 1) for x in self.stats["cluster_ms"]],
                       "params": {"cluster_method": self.args.cluster_method,
                                  "pval": self.args.pval, "mer_cos": self.args.mer_cos,
                                  "max_speakers": self.args.max_speakers,
                                  "min_cluster_size": self.args.min_cluster_size}},
                      (self.out_dir / "realtime_stats.json").open("w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            self.write_transcript()
        self.emit({"type": "session", "event": "end",
                   "num_segments": len(self.segments),
                   "num_speakers": len({s["label"] for s in self.segments})})


def stdin_audio_iter(block_s=0.032):
    n = int(SAMPLE_RATE * block_s)
    while True:
        raw = sys.stdin.buffer.read(n * 2)
        if not raw:
            break
        pcm = np.frombuffer(raw[:len(raw) // 2 * 2], dtype=np.int16)
        yield pcm.astype(np.float32) / 32768.0


def wav_audio_iter(path):
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1, \
            "wav 必须是 16kHz 单声道"
        while True:
            raw = w.readframes(512)
            if not raw:
                break
            yield np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def main():
    ap = argparse.ArgumentParser(description="Realtime speaker + ASR pipeline")
    ap.add_argument("--asr-url", default="http://127.0.0.1:8621")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--campplus-model", default="/tmp/campplus.onnx")
    ap.add_argument("--vad-model", default="/opt/asr/models/vad/silero_vad.onnx")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--events-file", default="", help="可选: JSONL 事件同时落盘")
    ap.add_argument("--wav", default="", help="测试用: 从 16k 单声道 WAV 文件喂入(代替 stdin)")
    ap.add_argument("--vad-threshold", type=float, default=0.5)
    ap.add_argument("--vad-min-silence", type=int, default=500)
    ap.add_argument("--vad-min-speech", type=int, default=500)
    ap.add_argument("--vad-max-speech", type=float, default=12.0)
    ap.add_argument("--vad-speech-pad", type=int, default=200)
    ap.add_argument("--recluster-every", type=int, default=3,
                    help="每 N 段重聚类一次(默认 3: 实测 60s 双人会话 13s 首次分离)")
    ap.add_argument("--cluster-method", choices=["silhouette", "eigengap"],
                    default="silhouette",
                    help="silhouette: k-means+silhouette 选 k(默认, 少段数稳定); "
                         "eigengap: 官方 3D-Speaker 谱聚类(pval/min-cluster-size 生效)")
    ap.add_argument("--pval", type=float, default=0.012)
    ap.add_argument("--mer-cos", type=float, default=0.8)
    ap.add_argument("--min-cluster-size", type=int, default=4)
    ap.add_argument("--max-speakers", type=int, default=15)
    ap.add_argument("--ws-port", type=int, default=0,
                    help="WebSocket 广播端口(0=关闭);订阅者收到与 stdout 相同的 JSONL 事件流")
    ap.add_argument("--ws-host", default="0.0.0.0")
    ap.add_argument("--ws-control-port", type=int, default=0,
                    help="外部广播 daemon 控制端口(>0 时优先把事件投递给已常驻的 daemon, "
                         "不可达则回退进程内广播); 由 run.sh 自动指定")
    args = ap.parse_args()

    broker = None
    broker_external = False
    if args.ws_port > 0:
        from ws_broadcast import WebSocketBroker, ControlPublisher
        if args.ws_control_port > 0 and ControlPublisher.probe("127.0.0.1", args.ws_control_port):
            # 广播 daemon 已由 run.sh 常驻: 流水线退出后订阅者保持连接, 等待 summary 事件
            broker = ControlPublisher("127.0.0.1", args.ws_control_port)
            broker_external = True
            print(f"[ws] 外部广播 daemon 就绪(控制端口 {args.ws_control_port})",
                  file=sys.stderr, flush=True)
        else:
            if args.ws_control_port > 0:
                print("[ws] ⚠ 控制端口不可达, 回退进程内广播(流水线退出即断开订阅者)",
                      file=sys.stderr, flush=True)
            broker = WebSocketBroker(args.ws_host, args.ws_port)
            broker.start()

    pipe = RealtimePipeline(args, broker=broker)
    pipe.emit({"type": "session", "event": "start", "sample_rate": SAMPLE_RATE,
               "asr_url": args.asr_url, "recluster_every": args.recluster_every})
    audio = wav_audio_iter(args.wav) if args.wav else stdin_audio_iter()
    try:
        pipe.run(audio)
    finally:
        if broker is not None and not broker_external:
            broker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
