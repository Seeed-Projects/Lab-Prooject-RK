from __future__ import annotations

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib import request as urllib_request

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .domain import HubError
from .events import EventBus
from .process_manager import ProcessManager
from .registry import DemoRegistry
from .resources import ResourceManager
from .runtime_supervisor import RuntimeSupervisor
from .voice_analysis import analysis_requires_english_rewrite, normalize_voice_analysis
from .inventory_voice import InventoryVoiceAssistant
from .go2rtc_bridge import Go2RtcBridge
from .piper_tts import PiperTTS, PiperTTSUnavailable
from .paths import BACKEND_ROOT, FRONTEND_ROOT, MULTI_CAMERA_ROOT, VOICE_PIPELINE_ROOT
from adapters.recamera_adapter import ReCameraAdapter
from adapters.hardware_status_detector import HardwareStatusDetector
from adapters.multi_camera_adapter import MultiCameraAdapter
from adapters.retail_single_adapter import RetailSingleAdapter
from adapters.sales_voice_adapter import SalesVoiceAdapter
from controllers.showcase_controller import ShowcaseSwitchController
from demo_manager import DemoManager


BASE_DIR = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(os.getenv("DEMO_HUB_PLUGIN_DIR", BASE_DIR / "plugins"))
DATA_DIR = Path(os.getenv("DEMO_HUB_DATA_DIR", BASE_DIR / "var"))


@asynccontextmanager
async def lifespan(application: FastAPI):
    registry = DemoRegistry(PLUGIN_DIR)
    registry.scan()
    bus = EventBus()
    resources = ResourceManager()
    manager = ProcessManager(registry, DATA_DIR, bus, resources)
    demo_manager = DemoManager(manager)
    sales_voice = SalesVoiceAdapter(project_root=VOICE_PIPELINE_ROOT, out_dir=DATA_DIR / "sales_voice")
    recamera = ReCameraAdapter(device_ip=os.getenv("RECAMERA_IP", "192.168.42.1"))
    live_shelf = MultiCameraAdapter(
        project_root=BACKEND_ROOT,
        runner=BACKEND_ROOT / "tools" / "live_shelf_8stream.py",
        config=MULTI_CAMERA_ROOT / "configs" / "runtime.json",
        inference_root=MULTI_CAMERA_ROOT,
    )
    retail_single = RetailSingleAdapter(
        backend_root=BACKEND_ROOT,
        rtsp_url=os.getenv("RECAMERA_RTSP_URL", recamera.detected_rtsp),
        websocket_url=os.getenv("RECAMERA_WS_URL", recamera.upstream_ws),
        capture_python=MULTI_CAMERA_ROOT / ".venv" / "bin" / "python",
    )
    go2rtc = Go2RtcBridge(source_url=retail_single.source_url, config_path=BASE_DIR / "run" / "go2rtc.yaml")
    inventory_voice = InventoryVoiceAssistant(
        sales_voice=sales_voice,
        retail_single=retail_single,
    )
    piper_tts = PiperTTS()
    hardware_detector = HardwareStatusDetector(
        sales_voice=sales_voice,
        recamera=recamera,
        retail_single=retail_single,
    )
    runtime_supervisor = RuntimeSupervisor(
        sales_voice=sales_voice,
        live_shelf=live_shelf,
        retail_single=retail_single,
        hardware_detector=hardware_detector,
        backend_port=int(os.getenv("DEMO_HUB_PORT", "8080")),
        frontend_port=int(os.getenv("DEMO_HUB_FRONTEND_PORT", "5173")),
    )
    showcase_controller = ShowcaseSwitchController(
        live_shelf=live_shelf,
        sales_voice=sales_voice,
        recamera=recamera,
        retail_single=retail_single,
        process_manager=manager,
        runtime_supervisor=runtime_supervisor,
        inventory_voice=inventory_voice,
    )
    application.state.registry = registry
    application.state.bus = bus
    application.state.resources = resources
    application.state.manager = manager
    application.state.demo_manager = demo_manager
    application.state.sales_voice = sales_voice
    application.state.recamera = recamera
    application.state.live_shelf = live_shelf
    application.state.retail_single = retail_single
    application.state.go2rtc = go2rtc
    application.state.hardware_detector = hardware_detector
    application.state.runtime_supervisor = runtime_supervisor
    application.state.inventory_voice = inventory_voice
    application.state.piper_tts = piper_tts
    application.state.showcase_controller = showcase_controller
    runtime_supervisor.start()
    await inventory_voice.start()
    await go2rtc.start()
    yield
    await runtime_supervisor.stop()
    await inventory_voice.stop()
    await piper_tts.stop()
    await showcase_controller.stop()
    with suppress(Exception):
        await showcase_controller.wait_for_idle(timeout=10)
    await live_shelf.stop()
    await retail_single.stop_runtime()
    await retail_single.stop_prewarm()
    await sales_voice.stop_runtime()
    await go2rtc.stop()
    await hardware_detector.stop()
    await manager.shutdown()


app = FastAPI(title="RK3588 Demo Hub", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", f"req_{uuid.uuid4().hex}")
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(HubError)
async def hub_error_handler(request: Request, exc: HubError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"request_id": getattr(request.state, "request_id", None), "detail": str(exc)},
    )


def manager(request: Request) -> ProcessManager:
    return request.app.state.manager


@app.get("/api/v1/health")
async def health(request: Request):
    return {"request_id": request.state.request_id, "status": "ok"}


@app.get("/api/v1/ready")
async def ready(request: Request):
    registry: DemoRegistry = request.app.state.registry
    return {
        "request_id": request.state.request_id,
        "status": "ready" if registry.list() else "not_ready",
        "demo_count": len(registry.list()),
        "registry_errors": registry.errors,
    }


@app.get("/api/v1/demos")
async def list_demos(request: Request):
    registry: DemoRegistry = request.app.state.registry
    process_manager = manager(request)
    demos = []
    for manifest in registry.list():
        active = [
            process_manager.public_run(r)
            for r in process_manager.list_for_demo(manifest.name)
            if r.state in {"starting", "running", "stopping"}
        ]
        demos.append({"manifest": manifest.public_dict(), "active_runs": active})
    return {"request_id": request.state.request_id, "demos": demos, "registry_errors": registry.errors}


@app.get("/api/v1/demos/{demo_id}")
async def get_demo(demo_id: str, request: Request):
    manifest = request.app.state.registry.get(demo_id)
    return {"request_id": request.state.request_id, "manifest": manifest.public_dict()}


@app.post("/api/v1/demos/{demo_id}/runs", status_code=202)
async def start_demo(demo_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(422, "request body must be an object")
    parameters = body.get("parameters", {})
    if not isinstance(parameters, dict):
        raise HTTPException(422, "parameters must be an object")
    run = await manager(request).start(demo_id, parameters)
    return {"request_id": request.state.request_id, "run": manager(request).public_run(run)}


@app.post("/api/v1/demos/{demo_id}/switch")
async def switch_demo(demo_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(422, "request body must be an object")
    parameters = body.get("parameters", {})
    if not isinstance(parameters, dict):
        raise HTTPException(422, "parameters must be an object")
    status = await request.app.state.demo_manager.switch_demo(demo_id, parameters)
    return {"request_id": request.state.request_id, "switch": status.public_dict()}


@app.get("/api/v1/switch/status")
async def switch_status(request: Request):
    return {
        "request_id": request.state.request_id,
        "switch": request.app.state.demo_manager.get_status(),
    }


@app.get("/api/v1/demos/{demo_id}/runs")
async def list_runs(demo_id: str, request: Request):
    process_manager = manager(request)
    return {"request_id": request.state.request_id, "runs": [process_manager.public_run(r) for r in process_manager.list_for_demo(demo_id)]}


@app.get("/api/v1/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    process_manager = manager(request)
    return {"request_id": request.state.request_id, "run": process_manager.public_run(process_manager.get(run_id))}


@app.post("/api/v1/runs/{run_id}/stop", status_code=202)
async def stop_run(run_id: str, request: Request):
    run = await manager(request).stop(run_id)
    return {"request_id": request.state.request_id, "run": manager(request).public_run(run)}


@app.get("/api/v1/runs/{run_id}/logs")
async def get_logs(
    run_id: str,
    request: Request,
    stream: str = Query("all", pattern="^(all|stdout|stderr)$"),
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
):
    result = manager(request).logs(run_id, stream, offset, limit)
    result["request_id"] = request.state.request_id
    return result


@app.get("/api/v1/runs/{run_id}/events")
async def get_events(run_id: str, request: Request, after_seq: int = Query(0, ge=0)):
    manager(request).get(run_id)
    return {
        "request_id": request.state.request_id,
        "events": request.app.state.bus.replay(run_id, after_seq),
    }


@app.get("/api/v1/runs/{run_id}/artifacts")
async def list_artifacts(run_id: str, request: Request):
    return {"request_id": request.state.request_id, "artifacts": manager(request).artifacts(run_id)}


@app.get("/api/v1/runs/{run_id}/artifacts/{name}")
async def get_artifact(run_id: str, name: str, request: Request):
    path, media_type = manager(request).artifact(run_id, name)
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.get("/api/v1/resources")
async def get_resources(request: Request):
    return {"request_id": request.state.request_id, "resources": request.app.state.resources.snapshot()}


@app.post("/api/v1/sales-voice/start", status_code=202)
async def start_sales_voice(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    language = body.get("language", "en") if isinstance(body, dict) else "en"
    try:
        status = await request.app.state.runtime_supervisor.request_voice(language=language)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"request_id": request.state.request_id, "status": status}


@app.post("/api/v1/sales-voice/stop", status_code=202)
async def stop_sales_voice(request: Request):
    return {"request_id": request.state.request_id, "status": await request.app.state.sales_voice.stop()}


@app.get("/api/v1/sales-voice/status")
async def sales_voice_status(request: Request):
    return {
        "request_id": request.state.request_id,
        "status": request.app.state.sales_voice.status(),
        "runtime": request.app.state.runtime_supervisor.status()["runtimes"]["voice"],
    }


@app.get("/api/v1/runtime/status")
async def runtime_status(request: Request):
    return {
        "request_id": request.state.request_id,
        "status": request.app.state.runtime_supervisor.status(),
    }


@app.websocket("/api/v1/sales-voice/stream")
async def sales_voice_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        await websocket.app.state.sales_voice.forward_websocket(websocket.send_json)
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.get("/api/v1/inventory-voice/tts/status")
async def inventory_voice_tts_status(request: Request):
    return {"request_id": request.state.request_id, "status": request.app.state.piper_tts.status()}


@app.post("/api/v1/inventory-voice/tts/speak")
async def inventory_voice_tts_speak(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        raise HTTPException(422, "text must be a string")
    try:
        status = await request.app.state.piper_tts.speak(text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except PiperTTSUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"request_id": request.state.request_id, "status": status}


@app.post("/api/v1/sales-voice/summary")
async def sales_voice_summary(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    turns = body.get("turns", []) if isinstance(body, dict) else []
    if not isinstance(turns, list) or not turns:
        raise HTTPException(422, "turns must be a non-empty array")
    transcript = "\n".join(
        f"Speaker {turn.get('speaker', '?')}: {turn.get('text', '')}"
        for turn in turns
        if isinstance(turn, dict) and turn.get("text")
    )
    if not transcript.strip():
        raise HTTPException(422, "turns contain no transcript text")

    prompt = (
        "You are a sales assistant demo summarizer. Return JSON only with string keys "
        "summary, customer_intent, and recommendation. Always write all three values in "
        "clear, concise English, regardless of the transcript language. Do not use markdown."
    )
    payload = {
        "model": "rkllm-model",
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "/no_think\n" + transcript[-5000:]},
        ],
        "max_tokens": 500,
        "temperature": 0.2,
        "stream": False,
    }

    def call_llm(llm_payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(llm_payload).encode("utf-8")
        req = urllib_request.Request(
            os.getenv("LLM_SUMMARY_URL", "http://127.0.0.1:8001").rstrip("/") + "/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib_request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read())

    try:
        result = await asyncio.to_thread(call_llm, payload)
        text = result["choices"][0]["message"]["content"]
        analysis = normalize_voice_analysis(text)
        if analysis_requires_english_rewrite(analysis):
            rewrite_payload = {
                **payload,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Rewrite the supplied sales analysis as JSON only. Use keys summary, "
                            "customer_intent, recommendation. Every value must be English."
                        ),
                    },
                    {"role": "user", "content": json.dumps(analysis, ensure_ascii=False)},
                ],
                "temperature": 0.0,
            }
            rewritten = await asyncio.to_thread(call_llm, rewrite_payload)
            text = rewritten["choices"][0]["message"]["content"]
            analysis = normalize_voice_analysis(text)
            if analysis_requires_english_rewrite(analysis):
                raise ValueError("LLM analysis was not returned in English")
    except Exception as exc:
        fallback = {
            "summary": "The conversation was captured, but detailed AI analysis is temporarily unavailable.",
            "intent": "Pending AI analysis",
            "recommendation": "Review the latest transcript and follow up with the customer.",
        }
        return {
            "request_id": request.state.request_id,
            "status": "fallback",
            "error": str(exc),
            "analysis": fallback,
            "summary": fallback,
        }
    return {
        "request_id": request.state.request_id,
        "status": "ok",
        "analysis": analysis,
        "text": text,
    }


@app.get("/api/v1/recamera/status")
async def recamera_status(request: Request):
    return {"request_id": request.state.request_id, "status": await request.app.state.recamera.status()}


@app.get("/api/v1/hardware/status")
async def hardware_status(request: Request):
    return {"request_id": request.state.request_id, "status": request.app.state.hardware_detector.status()}


@app.websocket("/api/v1/recamera/stream")
async def recamera_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        await websocket.app.state.recamera.forward_websocket(websocket.send_json)
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.get("/api/v1/shelf/video")
async def shelf_video():
    browser_path = MULTI_CAMERA_ROOT / "outputs" / "restock_demo_rk3588_browser.mp4"
    original_path = MULTI_CAMERA_ROOT / "outputs" / "restock_demo_rk3588.mp4"
    video_path = browser_path if browser_path.is_file() else original_path
    if not video_path.is_file():
        raise HTTPException(404, f"output video missing: {video_path}")
    return FileResponse(video_path, media_type="video/mp4", filename=video_path.name)


@app.get("/api/v1/shelf/status")
async def shelf_status(request: Request):
    browser_path = MULTI_CAMERA_ROOT / "outputs" / "restock_demo_rk3588_browser.mp4"
    original_path = MULTI_CAMERA_ROOT / "outputs" / "restock_demo_rk3588.mp4"
    video_path = browser_path if browser_path.is_file() else original_path
    return {
        "request_id": request.state.request_id,
        "status": {
            "video_ready": video_path.is_file(),
            "video_path": str(video_path),
            "video_url": "/api/v1/shelf/video",
            "validated_average_fps": 6.5,
            "models_loaded": 2,
            "runtime": "RKNN Lite / RK3588 NPU",
        },
    }


@app.post("/api/v1/shelf/live/start", status_code=202)
async def shelf_live_start(request: Request):
    return {"request_id": request.state.request_id, "status": await request.app.state.live_shelf.start()}


@app.post("/api/v1/shelf/live/stop", status_code=202)
async def shelf_live_stop(request: Request):
    return {"request_id": request.state.request_id, "status": await request.app.state.live_shelf.stop()}


@app.get("/api/v1/shelf/live/status")
async def shelf_live_status(request: Request):
    return {"request_id": request.state.request_id, "status": request.app.state.live_shelf.status()}


@app.get("/api/v1/shelf/live.jpg")
async def shelf_live_jpg(request: Request):
    path = request.app.state.live_shelf.frame_path()
    if not path.is_file():
        raise HTTPException(404, "live preview frame not ready")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/v1/shelf/live/{camera_id}.jpg")
async def shelf_live_camera_jpg(camera_id: int, request: Request):
    if camera_id < 1 or camera_id > 4:
        raise HTTPException(404, "camera id must be 1..4")
    path = request.app.state.live_shelf.out_dir / f"latest_{camera_id}.jpg"
    if not path.is_file():
        raise HTTPException(404, "live preview frame not ready")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/v1/shelf/live.mjpeg")
async def shelf_live_mjpeg(request: Request):
    return await shelf_live_camera_mjpeg(1, request)


@app.get("/api/v1/shelf/live/{camera_id}.mjpeg")
async def shelf_live_camera_mjpeg(camera_id: int, request: Request):
    if camera_id < 1 or camera_id > 4:
        raise HTTPException(404, "camera id must be 1..4")
    async def stream():
        last_mtime = 0.0
        last_data: bytes | None = None
        while True:
            path = request.app.state.live_shelf.out_dir / f"latest_{camera_id}.jpg"
            if path.is_file():
                try:
                    stat = path.stat()
                    if stat.st_mtime != last_mtime or last_data is None:
                        last_data = path.read_bytes()
                        last_mtime = stat.st_mtime
                    if last_data:
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Cache-Control: no-store\r\n\r\n"
                            + last_data
                            + b"\r\n"
                        )
                except FileNotFoundError:
                    pass
            await asyncio.sleep(0.04)

    return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.websocket("/api/v1/shelf/live/events")
async def shelf_live_events(websocket: WebSocket):
    await websocket.accept()
    try:
        await websocket.app.state.live_shelf.events(websocket.send_json)
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.post("/api/v1/retail-single/start", status_code=202)
async def retail_single_start(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    url = body.get("url")
    if url is not None and not isinstance(url, str):
        raise HTTPException(422, "url must be a string")
    return {"request_id": request.state.request_id, "status": await request.app.state.retail_single.start(url=url)}


@app.post("/api/v1/retail-single/stop", status_code=202)
async def retail_single_stop(request: Request):
    return {"request_id": request.state.request_id, "status": await request.app.state.retail_single.stop()}


@app.get("/api/v1/retail-single/status")
async def retail_single_status(request: Request):
    status = request.app.state.retail_single.status()
    bridge = request.app.state.go2rtc
    status["webrtc"] = bridge.status()
    status["webrtc_ready"] = await bridge.health()
    # Keep go2rtc bound to localhost and proxy signaling through Backend so a
    # browser on another machine never tries to connect to its own 127.0.0.1.
    status["webrtc_url"] = "/api/v1/retail-single/webrtc/ws?src=recamera_detected" if status["webrtc_ready"] else None
    return {"request_id": request.state.request_id, "status": status}


@app.get("/api/v1/retail-single/webrtc/status")
async def retail_single_webrtc_status(request: Request):
    bridge = request.app.state.go2rtc
    ready = await bridge.health()
    return {
        "request_id": request.state.request_id,
        "status": bridge.status() | {"ready": ready},
    }


@app.websocket("/api/v1/retail-single/webrtc/ws")
async def retail_single_webrtc_ws(websocket: WebSocket):
    await websocket.accept()
    bridge = websocket.app.state.go2rtc
    if not await bridge.health():
        await websocket.close(code=1013, reason="go2rtc is not ready")
        return
    try:
        import websockets

        async with websockets.connect(bridge.websocket_url, proxy=None, ping_interval=None) as upstream:
            async def browser_to_go2rtc():
                while True:
                    message = await websocket.receive_text()
                    await upstream.send(message)

            async def go2rtc_to_browser():
                async for message in upstream:
                    await websocket.send_text(message)

            tasks = {
                asyncio.create_task(browser_to_go2rtc()),
                asyncio.create_task(go2rtc_to_browser()),
            }
            _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception:
        with suppress(Exception):
            await websocket.close(code=1011, reason="WebRTC signaling failed")


@app.get("/api/v1/retail-single/frame.jpg")
async def retail_single_frame(request: Request):
    path = request.app.state.retail_single.frame_path()
    if not path.is_file():
        raise HTTPException(404, "retail single frame not ready")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/v1/retail-single/video")
async def retail_single_video(request: Request):
    path = request.app.state.retail_single.latest_video_path()
    if not path.is_file():
        raise HTTPException(404, "retail single video not ready")
    return FileResponse(path, media_type="video/mp4")


@app.get("/api/v1/retail-single/mjpeg")
async def retail_single_mjpeg(request: Request):
    adapter = request.app.state.retail_single

    async def stream():
        last_data: bytes | None = None
        last_mtime = 0.0
        while True:
            path = adapter.frame_path()
            if path.is_file():
                try:
                    mtime = path.stat().st_mtime
                    if mtime != last_mtime:
                        data = path.read_bytes()
                        if data:
                            last_data = data
                            last_mtime = mtime
                            yield (
                                b"--frame\r\n"
                                b"Content-Type: image/jpeg\r\n"
                                b"Cache-Control: no-store\r\n\r\n"
                                + last_data
                                + b"\r\n"
                            )
                except (FileNotFoundError, OSError):
                    pass
            await asyncio.sleep(0.04)

    return StreamingResponse(stream(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.websocket("/api/v1/retail-single/events")
async def retail_single_events(websocket: WebSocket):
    await websocket.accept()
    try:
        await websocket.app.state.retail_single.forward_websocket(websocket.send_json)
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.post("/api/v1/showcase/switch", status_code=202)
async def showcase_switch(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    target = body.get("demo") if isinstance(body, dict) else None
    if target not in {"single", "multi", "voice", "none"}:
        raise HTTPException(422, "demo must be one of: single, multi, voice, none")
    parameters = body if isinstance(body, dict) else {}
    try:
        result = await request.app.state.showcase_controller.switch(target, parameters)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"request_id": request.state.request_id, "showcase": result}


@app.post("/api/v1/showcase/stop", status_code=202)
async def showcase_stop(request: Request):
    result = await request.app.state.showcase_controller.stop()
    return {"request_id": request.state.request_id, "showcase": result}


@app.get("/api/v1/showcase/status")
async def showcase_status(request: Request):
    active = [
        request.app.state.manager.public_run(run)
        for run in request.app.state.manager.runs.values()
        if run.state in {"starting", "running", "stopping"}
    ]
    return {
        "request_id": request.state.request_id,
        "showcase": request.app.state.showcase_controller.get_status(),
        "live_shelf": request.app.state.live_shelf.status(),
        "sales_voice": request.app.state.sales_voice.status(),
        "process_runs": active,
    }


def event_matches(event: dict[str, Any], filters: dict[str, Any]) -> bool:
    mappings = (("demo_ids", "demo_id"), ("run_ids", "run_id"), ("channels", "channel"))
    return all(not filters.get(field) or event[key] in filters[field] for field, key in mappings)


@app.websocket("/api/v1/stream")
async def stream(websocket: WebSocket):
    await websocket.accept()
    bus: EventBus = websocket.app.state.bus
    queue = await bus.subscribe()
    filters: dict[str, Any] = {"demo_ids": [], "run_ids": [], "channels": []}
    receiver: asyncio.Task[Any] | None = None
    try:
        message = await websocket.receive_json()
        if message.get("action") != "subscribe":
            await websocket.close(code=1008, reason="first message must subscribe")
            return
        for field in filters:
            value = message.get(field, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                await websocket.close(code=1008, reason=f"invalid {field}")
                return
            filters[field] = set(value)
        after_seq = int(message.get("after_seq", 0))
        for run_id in filters["run_ids"]:
            for event in bus.replay(run_id, after_seq):
                if event_matches(event, filters):
                    await websocket.send_json(event)
        await websocket.send_json({"type": "hub.subscribed", "filters": {k: sorted(v) for k, v in filters.items()}})
        receiver = asyncio.create_task(websocket.receive_text())
        while True:
            event_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({event_task, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if receiver in done:
                event_task.cancel()
                receiver.result()
                receiver = asyncio.create_task(websocket.receive_text())
                continue
            event = event_task.result()
            if event_matches(event, filters):
                await websocket.send_json(event)
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        if receiver:
            receiver.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await receiver
        await bus.unsubscribe(queue)


# Serve the Vite production build from the same origin as REST and WebSocket.
FRONTEND_DIST = FRONTEND_ROOT / "dist"
if FRONTEND_DIST.is_dir() and (FRONTEND_DIST / "index.html").is_file():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
