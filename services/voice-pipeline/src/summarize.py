#!/usr/bin/env python3
# 会议总结: 读取会话产物 transcript.jsonl → 调本机 RKLLM 服务(OpenAI 兼容) → 生成 summary.md
# 纯标准库, 在 RK3588 主机上运行(run.sh 会话结束后自动调用)。
# 用法: python3 summarize.py --out-dir /tmp/ovs_realtime [--llm-url http://127.0.0.1:8001]
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.request

DEFAULT_PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts", "meeting_summary.md")


def fmt_ts(sec):
    mm, ss = divmod(int(sec), 60)
    return f"{mm:02d}:{ss:02d}"


def load_turns(out_dir):
    """优先 transcript.jsonl(带 start/end/speaker/text), 兜底解析 transcript.txt。"""
    turns = []
    path = os.path.join(out_dir, "transcript.jsonl")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except Exception:
                continue
            if t.get("text") and t.get("start") is not None:
                turns.append(t)
    if turns:
        return turns
    txt = os.path.join(out_dir, "transcript.txt")
    if os.path.exists(txt):
        cur = None
        for line in open(txt, encoding="utf-8"):
            m = re.match(r"^说话人\s*(\d+)\s+(\d+):(\d+)\s*$", line.strip())
            if m:
                cur = {"speaker": int(m.group(1)),
                       "start": int(m.group(2)) * 60 + int(m.group(3)), "text": ""}
                turns.append(cur)
            elif cur is not None and line.strip():
                cur["text"] = (cur["text"] + " " + line.strip()).strip()
        turns = [t for t in turns if t.get("text")]
    return turns


def build_context(turns):
    lines = ["Transcript (speaker-labeled):"]
    for t in turns:
        lines.append(f"[{fmt_ts(t['start'])}] Speaker {t.get('speaker', '?')}: {t['text']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="会议总结(RKLLM)")
    ap.add_argument("--out-dir", required=True, help="run.sh 的产物目录(含 transcript.jsonl)")
    ap.add_argument("--llm-url", default=os.environ.get("LLM_URL", "http://127.0.0.1:8001"))
    ap.add_argument("--prompt-file", default=DEFAULT_PROMPT)
    ap.add_argument("--max-tokens", type=int, default=1500)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--model", default="rkllm-model")
    args = ap.parse_args()

    turns = load_turns(args.out_dir)
    if not turns:
        print("[summarize] 无可识别的对话内容, 跳过总结")
        return 2

    with open(args.prompt_file, encoding="utf-8") as f:
        template = f.read().strip()

    duration = max(t.get("end", t["start"]) for t in turns)
    context = (f"Meeting date: {datetime.date.today().isoformat()}\n"
               f"Recording duration: {fmt_ts(duration)}\n\n"
               + build_context(turns))

    body = json.dumps({
        "model": args.model,
        "messages": [
            {"role": "system", "content": template},
            {"role": "user", "content": "/no_think\n" + context},
        ],
        "max_tokens": args.max_tokens,
        "temperature": 0.2,
        "stream": False,
    }).encode()
    req = urllib.request.Request(args.llm_url.rstrip("/") + "/v1/chat/completions",
                                 data=body, headers={"Content-Type": "application/json"})
    print(f"[summarize] 请求 LLM: {len(turns)} 个话轮, max_tokens={args.max_tokens} (约需 1 分钟) ...", flush=True)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            res = json.loads(resp.read())
    except Exception as e:
        print(f"[summarize] ✗ LLM 请求失败: {e}")
        return 1
    dt = time.time() - t0
    try:
        text = res["choices"][0]["message"]["content"]
    except Exception:
        print(f"[summarize] ✗ 响应格式异常: {str(res)[:300]}")
        return 1
    usage = res.get("usage") or {}
    out = os.path.join(args.out_dir, "summary.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")
    cps = usage.get("completion_tokens")
    rate = f", ≈{cps / dt:.1f} tok/s" if cps and dt else ""
    print(f"[summarize] ✓ {dt:.1f}s 完成, 输出 {cps or '?'} tokens{rate} → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
