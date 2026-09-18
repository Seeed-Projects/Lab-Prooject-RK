import json
import os
import re
import subprocess
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / 'static'
RKLLM_BASE_URL = os.getenv('RKLLM_BASE_URL', 'http://127.0.0.1:8080').rstrip('/')
MODEL_NAME = os.getenv('MODEL_NAME', 'Qwen3-1.7B')
MODEL_QUANTIZATION = os.getenv('MODEL_QUANTIZATION', 'W4A16')
SERVER_CONTEXT = int(os.getenv('SERVER_CONTEXT', '25088'))
MODEL_MAX_CONTEXT = int(os.getenv('MODEL_MAX_CONTEXT', '25088'))
CLI_PREFILL_TPS = float(os.getenv('CLI_PREFILL_TPS', '129.78'))
CLI_GENERATE_TPS = float(os.getenv('CLI_GENERATE_TPS', '114.85'))
DEVICE_LABEL = os.getenv('DEVICE_LABEL', 'RK1820')
PRODUCT_LABEL = os.getenv('PRODUCT_LABEL', 'reComputer RK3576 Dev Kit + RK1820 AI Accelerator')

app = FastAPI(title='Qwen3-1.7B Long Context Test on RK1820')
app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')


def sse(event, data):
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'


@app.get('/')
async def root():
    return FileResponse(STATIC_DIR / 'index.html')


async def count_tokens(text):
    payloads = [{'content': text}, {'prompt': text}]
    endpoints = ['/tokenize', '/v1/tokenize']
    async with httpx.AsyncClient(timeout=5.0) as client:
        for endpoint in endpoints:
            for payload in payloads:
                try:
                    resp = await client.post(f'{RKLLM_BASE_URL}{endpoint}', json=payload)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    tokens = data.get('tokens')
                    if isinstance(tokens, list):
                        return len(tokens), 'server'
                    for key in ('count', 'n_tokens', 'token_count'):
                        if isinstance(data.get(key), int):
                            return int(data[key]), 'server'
                except Exception:
                    pass
    return max(1, round(len(text) / 4.0)), 'estimated'


def run_cmd(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
        if p.returncode == 0:
            return p.stdout.strip()
    except Exception:
        pass
    return ''


def hardware_info():
    out = run_cmd(['rknn-smi', 'info'])
    if not out:
        out = run_cmd(['sudo', '-n', 'rknn-smi', 'info'])
    result = {
        'available': bool(out),
        'device': None,
        'temperature_c': None,
        'memory_used_mb': None,
        'memory_total_mb': None,
        'npu_percent': None,
        'health': None,
    }
    if not out:
        return result
    dev = re.search(r'\b(RK1820|RK1828)\b', out)
    if dev:
        result['device'] = dev.group(1)
    mem = re.search(r'(\d+)\s*/\s*(\d+)', out)
    if mem:
        result['memory_used_mb'] = int(mem.group(1))
        result['memory_total_mb'] = int(mem.group(2))
    online_line = next((x for x in out.splitlines() if 'Online' in x), '')
    device_line = next((x for x in out.splitlines() if 'RK182' in x), '')
    m = re.search(r'Online\s*\|\s*([A-Za-z]+)', online_line)
    if m:
        result['health'] = m.group(1)
    nums = re.findall(r'\b\d+\b', online_line)
    if nums:
        try:
            result['npu_percent'] = int(nums[-1])
        except Exception:
            pass
    m = re.search(r'\|\s*(\d+)\s*\|\s*\d+\s*/\s*\d+', device_line)
    if m:
        result['temperature_c'] = int(m.group(1))
    return result


@app.get('/api/health')
async def health():
    ok = False
    status = None
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f'{RKLLM_BASE_URL}/v1/models')
            status = resp.status_code
            ok = resp.status_code < 500
    except Exception:
        pass
    return {
        'server_ok': ok,
        'server_status': status,
        'latency_ms': round((time.perf_counter() - started) * 1000, 2),
        'product': PRODUCT_LABEL,
        'model': MODEL_NAME,
        'quantization': MODEL_QUANTIZATION,
        'server_context': SERVER_CONTEXT,
        'model_max_context': MODEL_MAX_CONTEXT,
        'cli_prefill_tps': CLI_PREFILL_TPS,
        'cli_generate_tps': CLI_GENERATE_TPS,
        'device_label': DEVICE_LABEL,
        'hardware': hardware_info(),
    }


async def stream_completion(messages, cache_prompt, n_predict, id_slot=0):
    payload = {
        'model': MODEL_NAME,
        'messages': messages,
        'stream': True,
        'n_keep': 0,
        'cache_prompt': bool(cache_prompt),
        'id_slot': int(id_slot),
        'n_predict': int(n_predict),
    }
    start = time.perf_counter()
    first_at = None
    output_parts = []
    finish_reason = None
    timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            'POST',
            f'{RKLLM_BASE_URL}/v1/chat/completions',
            json=payload,
            headers={'Authorization': 'Bearer sk-no-key-required'},
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise RuntimeError(f'RKLLM HTTP {resp.status_code}: ' + body.decode('utf-8', errors='replace')[:1200])
            async for line in resp.aiter_lines():
                if not line or not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if data == '[DONE]':
                    break
                try:
                    obj = json.loads(data)
                    choices = obj.get('choices') or []
                    if not choices:
                        continue
                    delta = choices[0].get('delta') or {}
                    content = delta.get('content')
                    reason = choices[0].get('finish_reason')
                except Exception:
                    continue
                if content:
                    content = content.replace('<think>', '').replace('</think>', '')
                    if content == '':
                        continue
                    content = content.replace('<think>', '').replace('</think>', '')
                    if not content:
                        continue
                    if first_at is None:
                        first_at = time.perf_counter()
                    output_parts.append(content)
                    yield {'type': 'delta', 'content': content}
                if reason:
                    finish_reason = reason
    end = time.perf_counter()
    output = ''.join(output_parts)
    output_tokens, method = await count_tokens(output)
    ttft_ms = (first_at - start) * 1000 if first_at is not None else None
    decode_seconds = max(0.001, end - first_at) if first_at is not None else None
    decode_tps = output_tokens / decode_seconds if decode_seconds else None
    yield {
        'type': 'metrics',
        'output': output,
        'metrics': {
            'ttft_ms': round(ttft_ms, 2) if ttft_ms is not None else None,
            'end_to_end_ms': round((end - start) * 1000, 2),
            'output_tokens': output_tokens,
            'token_method': method,
            'decode_tps': round(decode_tps, 2) if decode_tps is not None else None,
            'output_chars': len(output),
            'finish_reason': finish_reason,
        },
    }


async def collect_completion(messages, cache_prompt, n_predict, id_slot=0):
    out = []
    metrics = {}
    async for item in stream_completion(messages, cache_prompt, n_predict, id_slot):
        if item['type'] == 'delta':
            out.append(item['content'])
        else:
            metrics = item['metrics']
    return ''.join(out), metrics


@app.post('/api/first-turn')
async def first_turn(prompt: str = Form(...), n_predict: int = Form(192)):
    prompt = (prompt or '').strip()
    if not prompt:
        return JSONResponse(status_code=400, content={'error': 'Prompt cannot be empty.'})
    n_predict = max(16, min(int(n_predict), 512))

    async def gen():
        try:
            input_tokens, input_method = await count_tokens(prompt)
            yield sse('start', {'input_tokens': input_tokens, 'input_token_method': input_method})
            async for item in stream_completion(
                [{'role': 'user', 'content': prompt}], False, n_predict, 0
            ):
                if item['type'] == 'delta':
                    yield sse('delta', {'content': item['content']})
                else:
                    m = item['metrics']
                    m['input_tokens'] = input_tokens
                    m['input_token_method'] = input_method
                    yield sse('metrics', m)
            yield sse('done', {'ok': True})
        except Exception as exc:
            yield sse('error', {'message': str(exc)})

    return StreamingResponse(gen(), media_type='text/event-stream')


FILLER_TOPICS = [
    'Edge AI deployment requires balancing compute, memory, latency, thermal limits, software support, and update strategy.',
    'Industrial vision systems often combine cameras, preprocessing, neural inference, event filtering, and local decision logic.',
    'Robotics applications benefit from predictable response time because sensing and control loops must react within bounded latency.',
    'Local language models can reduce network dependency and keep prompts or operational data on the device when privacy matters.',
    'Model quantization reduces memory footprint and bandwidth requirements but must be evaluated against task accuracy and output quality.',
    'Embedded Linux platforms provide a flexible environment for networking, storage, containers, device management, and AI runtimes.',
    'Long-context inference stresses memory capacity and memory bandwidth because the KV cache grows as the sequence becomes longer.',
    'Real-world AI performance depends on the complete pipeline rather than a single accelerator specification or peak TOPS number.',
    'Production systems should monitor thermals, memory usage, latency, error rates, and system power under sustained workloads.',
    'Developers often compare time to first token, decode throughput, context capacity, accuracy, power, and energy per generated token.',
]


def make_filler(min_chars=180000):
    parts = []
    total = 0
    i = 1
    while total < min_chars:
        topic = FILLER_TOPICS[(i - 1) % len(FILLER_TOPICS)]
        s = (
            f'Record {i:04d}. {topic} '
            'This record is background material for a long-context retrieval benchmark. '
            f'Reference index: {100000 + i}.\n'
        )
        parts.append(s)
        total += len(s)
        i += 1
    return ''.join(parts)


HAYSTACK_SOURCE = make_filler()
NEEDLE = 'IMPORTANT REFERENCE RECORD: The access code for Project Aurora is 483271.'
NIAH_INSTRUCTION = (
    'Read the following reference material carefully. One sentence contains the access code for Project Aurora. '
    'At the end, answer the question using only the six-digit code.\n\n'
)
NIAH_QUESTION = '\n\nQuestion: What is the access code for Project Aurora? Return only the six-digit number.'


async def build_niah_prompt(target_tokens, depth_percent):
    target_tokens = max(256, min(int(target_tokens), max(256, SERVER_CONTEXT - 256)))
    depth = max(0.0, min(float(depth_percent) / 100.0, 1.0))

    async def make_prompt(filler_chars):
        filler = HAYSTACK_SOURCE[:filler_chars]
        cut = int(len(filler) * depth)
        body = filler[:cut] + '\n' + NEEDLE + '\n' + filler[cut:]
        return NIAH_INSTRUCTION + body + NIAH_QUESTION

    lo = 200
    hi = min(len(HAYSTACK_SOURCE), max(1200, target_tokens * 7))
    best = await make_prompt(min(hi, max(800, target_tokens * 4)))
    best_count, method = await count_tokens(best)

    if method == 'server':
        for _ in range(9):
            mid = (lo + hi) // 2
            candidate = await make_prompt(mid)
            cnt, _ = await count_tokens(candidate)
            if abs(cnt - target_tokens) < abs(best_count - target_tokens):
                best, best_count = candidate, cnt
            if cnt < target_tokens:
                lo = mid + 1
            else:
                hi = mid - 1
    else:
        ratio = target_tokens / max(best_count, 1)
        best = await make_prompt(max(200, int(len(best) * ratio)))
        best_count, method = await count_tokens(best)
    return best, best_count, method


@app.post('/api/niah')
async def niah(context_tokens: int = Form(2048), depth_percent: int = Form(50)):
    context_tokens = max(256, min(int(context_tokens), SERVER_CONTEXT - 256))
    depth_percent = max(0, min(int(depth_percent), 100))

    async def gen():
        try:
            prompt, actual_tokens, token_method = await build_niah_prompt(context_tokens, depth_percent)
            yield sse('start', {
                'target_context_tokens': context_tokens,
                'actual_input_tokens': actual_tokens,
                'input_token_method': token_method,
                'depth_percent': depth_percent,
            })
            output = []
            final_metrics = {}
            async for item in stream_completion(
                [{'role': 'user', 'content': prompt}], False, 32, 0
            ):
                if item['type'] == 'delta':
                    output.append(item['content'])
                    yield sse('delta', {'content': item['content']})
                else:
                    final_metrics = item['metrics']
            text = ''.join(output)
            m = re.search(r'\b(\d{6})\b', text)
            extracted = m.group(1) if m else None
            passed = extracted == '483271'
            final_metrics.update({
                'actual_input_tokens': actual_tokens,
                'input_token_method': token_method,
                'depth_percent': depth_percent,
                'answer': extracted,
                'pass': passed,
            })
            yield sse('metrics', final_metrics)
            yield sse('done', {'ok': True, 'pass': passed})
        except Exception as exc:
            yield sse('error', {'message': str(exc)})

    return StreamingResponse(gen(), media_type='text/event-stream')


MEMORY_SEED = (
    'Remember these three project facts for a later recall test. '
    'Project name: Atlas. Prototype number: 7294. Target deployment city: Osaka. '
    'Acknowledge in no more than five words.'
)
MEMORY_FILLERS = [
    'Explain why deterministic latency matters in an autonomous mobile robot. Keep the response to one short sentence.',
    'Explain one trade-off between cloud AI and edge AI for industrial cameras. Keep the response to one short sentence.',
    'State one reason quantized language models are useful on embedded hardware. Keep the response to one short sentence.',
    'Explain why memory bandwidth matters during autoregressive decoding. Keep the response to one short sentence.',
    'Describe one advantage of local inference when network connectivity is unreliable. Keep the response to one short sentence.',
    'Describe why thermal design matters during sustained NPU workloads. Keep the response to one short sentence.',
    'Explain why time to first token and decode throughput measure different aspects of LLM UX. Keep the response to one short sentence.',
    'Mention one way to evaluate an object-detection pipeline beyond raw neural-network latency. Keep the response to one short sentence.',
]


def memory_prompt(i):
    base = MEMORY_FILLERS[(i - 1) % len(MEMORY_FILLERS)]
    pad = (
        ' Additional context: the engineering team is evaluating an embedded AI system for field deployment where CPU load, '
        'accelerator memory, power stability, software updates, and predictable response time all matter. Keep the answer concise.'
    )
    return f'Round {i}. {base}{pad}'


@app.post('/api/conversation-memory')
async def conversation_memory(target_history_tokens: int = Form(8192)):
    target_history_tokens = max(512, min(int(target_history_tokens), SERVER_CONTEXT - 384))

    async def gen():
        accumulated = 0
        token_method = 'estimated'
        round_no = 0
        try:
            seed_tokens, token_method = await count_tokens(MEMORY_SEED)
            _, seed_metrics = await collect_completion(
                [{'role': 'user', 'content': MEMORY_SEED}], False, 24, 0
            )
            accumulated += seed_tokens + int(seed_metrics.get('output_tokens') or 0)
            round_no += 1
            yield sse('round', {
                'round': round_no,
                'kind': 'seed',
                'history_tokens': accumulated,
                'message': 'Stored Atlas / 7294 / Osaka',
                'ttft_ms': seed_metrics.get('ttft_ms'),
            })

            filler_index = 1
            while accumulated < target_history_tokens and round_no < 180:
                prompt = memory_prompt(filler_index)
                prompt_tokens, method = await count_tokens(prompt)
                if method == 'server':
                    token_method = 'server'
                _, metrics = await collect_completion(
                    [{'role': 'user', 'content': prompt}], True, 48, 0
                )
                accumulated += prompt_tokens + int(metrics.get('output_tokens') or 0)
                round_no += 1
                yield sse('round', {
                    'round': round_no,
                    'kind': 'filler',
                    'history_tokens': accumulated,
                    'message': f'Background conversation round {filler_index}',
                    'ttft_ms': metrics.get('ttft_ms'),
                    'decode_tps': metrics.get('decode_tps'),
                })
                filler_index += 1

            recall_prompt = (
                'Recall the project facts I gave you at the very beginning. '
                'Return exactly this format and nothing else: 7294 | Osaka'
            )
            recall_tokens, _ = await count_tokens(recall_prompt)
            answer_parts = []
            final_metrics = {}
            async for item in stream_completion(
                [{'role': 'user', 'content': recall_prompt}], True, 32, 0
            ):
                if item['type'] == 'delta':
                    answer_parts.append(item['content'])
                    yield sse('delta', {'content': item['content']})
                else:
                    final_metrics = item['metrics']
            answer = ''.join(answer_parts).strip()
            accumulated += recall_tokens + int(final_metrics.get('output_tokens') or 0)
            passed = ('7294' in answer) and ('osaka' in answer.lower())
            final_metrics.update({
                'pass': passed,
                'answer': answer,
                'rounds': round_no + 1,
                'history_tokens': accumulated,
                'history_token_method': token_method,
                'target_history_tokens': target_history_tokens,
            })
            yield sse('metrics', final_metrics)
            yield sse('done', {'ok': True, 'pass': passed})
        except Exception as exc:
            yield sse('error', {'message': str(exc)})

    return StreamingResponse(gen(), media_type='text/event-stream')
