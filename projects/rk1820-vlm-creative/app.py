import base64
import json
import mimetypes
import os
import time

import httpx
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, 'static')
RKLLM_BASE_URL = os.getenv('RKLLM_BASE_URL', 'http://127.0.0.1:8080').rstrip('/')
MAX_UPLOAD_MB = int(os.getenv('MAX_UPLOAD_MB', '15'))

app = FastAPI(title='RK1820 Vision Story Studio')
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')

@app.get('/')
async def root():
    return FileResponse(os.path.join(STATIC_DIR, 'index.html'))

@app.get('/api/health')
async def health():
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f'{RKLLM_BASE_URL}/v1/models')
        elapsed_ms = (time.perf_counter() - started) * 1000
        return {
            'ok': resp.status_code < 500,
            'status_code': resp.status_code,
            'latency_ms': round(elapsed_ms, 2),
            'base_url': RKLLM_BASE_URL,
        }
    except Exception as exc:
        return JSONResponse(status_code=503, content={'ok': False, 'error': str(exc), 'base_url': RKLLM_BASE_URL})

def sse(event, data):
    return f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'

@app.post('/api/vlm')
async def vlm(image: UploadFile = File(...), prompt: str = Form(...), n_predict: int = Form(256)):
    n_predict = max(32, min(int(n_predict), 512))
    prompt = (prompt or '').strip()
    if not prompt:
        return JSONResponse(status_code=400, content={'error': 'Prompt cannot be empty.'})

    image_bytes = await image.read()
    if not image_bytes:
        return JSONResponse(status_code=400, content={'error': 'Image is empty.'})
    if len(image_bytes) > MAX_UPLOAD_MB * 1024 * 1024:
        return JSONResponse(status_code=413, content={'error': f'Image exceeds {MAX_UPLOAD_MB} MB upload limit.'})

    mime = image.content_type or mimetypes.guess_type(image.filename or '')[0] or 'image/jpeg'
    if not mime.startswith('image/'):
        return JSONResponse(status_code=400, content={'error': 'Only image files are supported.'})

    image_b64 = base64.b64encode(image_bytes).decode('ascii')
    payload = {
        'model': 'Qwen2.5-VL',
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{image_b64}'}},
                {'type': 'text', 'text': prompt},
            ],
        }],
        'stream': True,
        'extra_body': {
            'n_keep': 0,
            'cache_prompt': False,
            'id_slot': 0,
            'n_predict': n_predict,
        },
    }

    async def generate():
        request_start = time.perf_counter()
        first_token_at = None
        output_chars = 0
        stream_chunks = 0
        yield sse('start', {'model': 'Qwen2.5-VL-3B_prune', 'n_predict': n_predict, 'filename': image.filename})
        try:
            timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream('POST', f'{RKLLM_BASE_URL}/v1/chat/completions', json=payload, headers={'Authorization': 'Bearer sk-no-key-required'}) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        yield sse('error', {'status_code': resp.status_code, 'message': body.decode('utf-8', errors='replace')[:2000]})
                        return
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
                            finish_reason = choices[0].get('finish_reason')
                        except Exception:
                            continue
                        if content:
                            now = time.perf_counter()
                            if first_token_at is None:
                                first_token_at = now
                            output_chars += len(content)
                            stream_chunks += 1
                            yield sse('delta', {'content': content})
                        if finish_reason:
                            yield sse('finish_reason', {'reason': finish_reason})

            end = time.perf_counter()
            ttft_ms = ((first_token_at - request_start) * 1000) if first_token_at else None
            total_ms = (end - request_start) * 1000
            generation_seconds = (end - first_token_at) if first_token_at else None
            chars_per_second = output_chars / generation_seconds if generation_seconds and generation_seconds > 0 else None
            yield sse('metrics', {
                'ttft_ms': round(ttft_ms, 2) if ttft_ms is not None else None,
                'total_ms': round(total_ms, 2),
                'output_chars': output_chars,
                'stream_chunks': stream_chunks,
                'chars_per_second': round(chars_per_second, 2) if chars_per_second is not None else None,
            })
            yield sse('done', {'ok': True})
        except Exception as exc:
            yield sse('error', {'message': str(exc)})

    return StreamingResponse(generate(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
