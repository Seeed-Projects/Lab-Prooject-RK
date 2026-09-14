from __future__ import annotations

from .adapters import BaseAdapter, RetailShelfAdapter, ShelfAdapter, VoiceAdapter, XVF3800ASRAdapter
from .domain import Manifest, ValidationError


ADAPTERS = {
    "process": BaseAdapter,
    "shelf": ShelfAdapter,
    "voice": VoiceAdapter,
    "process-file": RetailShelfAdapter,
    "process-jsonl-websocket": XVF3800ASRAdapter,
}


def create_adapter(manifest: Manifest) -> BaseAdapter:
    adapter_type = manifest.adapter.get("type", "process")
    try:
        adapter_class = ADAPTERS[adapter_type]
    except KeyError as exc:
        raise ValidationError(f"unsupported adapter type: {adapter_type}") from exc
    return adapter_class(manifest)
