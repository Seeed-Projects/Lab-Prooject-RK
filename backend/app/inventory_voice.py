from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any
from urllib import request as urllib_request


INTENTS = {
    "COUNT_TOTAL",
    "COUNT_TYPES",
    "COUNT_MISSING",
    "LIST_PRESENT_PRODUCTS",
    "LIST_MISSING_PRODUCTS",
    "PRODUCT_STATUS",
    "PRODUCT_COUNT",
    "STOCK_OVERVIEW",
    "UNSUPPORTED",
}


class InventoryVoiceAssistant:
    """Ground Voice turns in the live ReCamera inventory snapshot."""

    def __init__(self, *, sales_voice: Any, retail_single: Any):
        self.sales_voice = sales_voice
        self.retail_single = retail_single
        self.llm_url = os.getenv("LLM_SUMMARY_URL", "http://127.0.0.1:8001").rstrip("/")
        self.model = os.getenv("RKLLM_MODEL", "rkllm-model")
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._enabled = False
        self._last_turn_key: tuple[Any, ...] | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._queue = await self.sales_voice.subscribe_events()
        self._task = asyncio.create_task(self._run(), name="inventory-voice-assistant")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._queue is not None:
            await self.sales_voice.unsubscribe_events(self._queue)
            self._queue = None

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not enabled:
            self._last_turn_key = None

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            if not self._enabled or event.get("type") != "turn":
                continue
            text = str(event.get("text") or "").strip()
            if not text:
                continue
            key = (event.get("idx"), event.get("start"), event.get("end"), text)
            if key == self._last_turn_key:
                continue
            self._last_turn_key = key
            try:
                await self._answer_turn(event, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._publish_answer(event, text, "I could not complete the shelf query right now.", "ERROR", error=str(exc))

    async def _answer_turn(self, turn: dict[str, Any], question: str) -> None:
        snapshot = self.retail_single.inventory_snapshot()
        if not snapshot.get("data_valid"):
            reason = snapshot.get("invalid_reason")
            if reason == "NO_DATA":
                answer = "I cannot reach the ReCamera inventory data right now."
            elif reason == "NOT_INITIALIZED":
                answer = "The ReCamera is still registering products. Please try again in a moment."
            else:
                answer = "The latest ReCamera inventory data is stale. Please try again in a moment."
            self._publish_answer(
                turn,
                question,
                answer,
                "DATA_UNAVAILABLE",
                snapshot=snapshot,
                structured={"answer_type": "unavailable", "value": None},
            )
            return

        intent = await asyncio.to_thread(self._classify, question)
        # Inventory facts are computed locally from the authoritative snapshot.
        # The LLM may classify a question, but it must not invent or calculate
        # quantities in the final answer.
        structured = build_structured_answer(intent, snapshot)
        answer = structured["answer"]
        self._publish_answer(
            turn,
            question,
            answer,
            "OK",
            intent=intent,
            snapshot=snapshot,
            structured=structured,
        )

    def _classify(self, question: str) -> dict[str, Any]:
        # Handle common shelf questions deterministically so an unavailable or
        # weak LLM cannot misclassify the demo's core interactions.
        direct = keyword_intent(question)
        if direct["intent"] != "UNSUPPORTED":
            return direct
        prompt = (
            "You classify questions for a retail shelf inventory assistant. Return JSON only "
            "with keys intent, product_name, confidence. Supported intents are: "
            "COUNT_TOTAL, COUNT_TYPES, COUNT_MISSING, LIST_PRESENT_PRODUCTS, LIST_MISSING_PRODUCTS, "
            "PRODUCT_STATUS, PRODUCT_COUNT, STOCK_OVERVIEW, UNSUPPORTED. "
            "Use the question language, but never answer the question."
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": question},
            ],
            "temperature": 0.0,
            "max_tokens": 120,
            "stream": False,
        }
        try:
            value = self._call_llm(payload)
            parsed = _extract_json(value)
            if isinstance(parsed, dict):
                intent = str(parsed.get("intent") or "UNSUPPORTED").upper()
                if intent in INTENTS:
                    return {
                        "intent": intent,
                        "product_name": str(parsed.get("product_name") or "").strip() or None,
                        "confidence": float(parsed.get("confidence", 0.0) or 0.0),
                    }
        except Exception:
            pass
        return keyword_intent(question)

    def _generate_answer(self, question: str, intent: dict[str, Any], snapshot: dict[str, Any]) -> str:
        facts = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        prompt = (
            "You are a grounded retail shelf assistant. Answer in clear concise English. "
            "Use only the supplied JSON facts. Never invent a product, number, or stock state. "
            "If the question is unsupported, explain what inventory questions you can answer. "
            "Return only the answer text, without markdown.\n\n"
            f"Question: {question}\nIntent: {json.dumps(intent, ensure_ascii=False)}\nFacts: {facts}"
        )
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 180,
            "stream": False,
        }
        try:
            value = self._call_llm(payload)
            text = _extract_message_text(value)
            if text:
                return text.strip().strip("`")
        except Exception:
            pass
        return fallback_answer(intent, snapshot)

    def _call_llm(self, payload: dict[str, Any]) -> Any:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(
            f"{self.llm_url}/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib_request.urlopen(req, timeout=20) as response:
            return json.loads(response.read())

    def _publish_answer(
        self,
        turn: dict[str, Any],
        question: str,
        answer: str,
        status: str,
        *,
        intent: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        error: str | None = None,
        structured: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "type": "inventory.answer",
            "question": question,
            "answer": answer,
            "status": status,
            "intent": intent or {"intent": "DATA_UNAVAILABLE"},
            "source": "recamera",
            "timestamp": time.time(),
            "turn_idx": turn.get("idx"),
        }
        if structured:
            for key in ("answer_type", "value", "unit", "items", "metrics", "product_name", "product_status"):
                if key in structured:
                    payload[key] = structured[key]
        if snapshot is not None:
            payload["data"] = snapshot
        if error:
            payload["error"] = error
        self.sales_voice.publish_event(payload)


def keyword_intent(question: str) -> dict[str, Any]:
    text = normalize_question(question)
    if any(term in text for term in (
        "how many types", "what types", "which types", "different types",
        "number of types", "几种", "多少种",
    )):
        return {"intent": "COUNT_TYPES", "product_name": None, "confidence": 0.7}
    if any(term in text for term in (
        "how many are missing", "how many missing", "missing count",
        "number of missing", "how many out of stock", "how many products are missing",
    )):
        return {"intent": "COUNT_MISSING", "product_name": None, "confidence": 0.8}
    if any(term in text for term in (
        "how many bottles", "how many products", "how many items", "how many do we have",
        "total", "total count", "count on the shelf", "多少个", "几个",
    )):
        return {"intent": "COUNT_TOTAL", "product_name": None, "confidence": 0.7}
    if any(term in text for term in ("missing", "out of stock", "restock", "replenish", "shortage", "缺货")):
        return {"intent": "LIST_MISSING_PRODUCTS", "product_name": None, "confidence": 0.7}
    if any(term in text for term in (
        "what is on", "what products", "what bottles", "which products are on",
        "available products", "currently available", "on the shelf", "有哪些",
    )):
        return {"intent": "LIST_PRESENT_PRODUCTS", "product_name": None, "confidence": 0.6}
    if any(term in text for term in ("overview", "summarize the shelf", "shelf status", "shelf summary")):
        return {"intent": "STOCK_OVERVIEW", "product_name": None, "confidence": 0.7}
    product_match = re.search(r"how many (.+?) bottles?(?: are there| do we have| on the shelf)?$", text)
    if product_match and product_match.group(1).strip() not in {"", "bottles", "products", "items"}:
        return {"intent": "PRODUCT_COUNT", "product_name": product_match.group(1).strip(), "confidence": 0.8}
    status_match = re.search(r"(?:is|are) (.+?) (?:available|in stock|on the shelf)$", text)
    if status_match:
        return {"intent": "PRODUCT_STATUS", "product_name": status_match.group(1).strip(), "confidence": 0.8}
    return {"intent": "UNSUPPORTED", "product_name": None, "confidence": 0.0}


def normalize_question(question: str) -> str:
    """Normalize spoken text without changing product names used for lookup."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s'-]", " ", str(question or "").lower())).strip()


def contains_cjk(value: Any) -> bool:
    return bool(re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", str(value or "")))


def english_product_name(value: Any, product_id: Any = None) -> str:
    """Return a customer-facing English name without changing inventory identity."""
    name = str(value or "").strip()
    if name and not contains_cjk(name):
        return name
    identifier = product_id
    if identifier in (None, ""):
        match = re.search(r"\d+", name)
        identifier = match.group(0) if match else None
    return f"Product {identifier}" if identifier not in (None, "") else "Unknown product"


def build_structured_answer(intent: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Render a deterministic, typed answer from ReCamera facts."""
    kind = str(intent.get("intent") or "UNSUPPORTED").upper()
    if kind == "COUNT_TOTAL":
        value = int(snapshot.get("total_count", 0) or 0)
        return {"answer_type": "number", "value": value, "unit": "bottles", "answer": f"There are {value} bottles on the shelf."}
    if kind == "COUNT_TYPES":
        value = int(snapshot.get("type_count", 0) or 0)
        return {"answer_type": "number", "value": value, "unit": "product types", "answer": f"There are {value} different product types on the shelf."}
    if kind == "COUNT_MISSING":
        value = int(snapshot.get("missing_count", 0) or 0)
        return {"answer_type": "number", "value": value, "unit": "missing products", "answer": f"There are {value} missing products."}
    if kind in {"LIST_PRESENT_PRODUCTS", "LIST_MISSING_PRODUCTS"}:
        if kind == "LIST_PRESENT_PRODUCTS":
            rows = snapshot.get("present_products") or []
            if rows:
                items = list(dict.fromkeys(
                    english_product_name(item.get("name"), item.get("id"))
                    for item in rows if isinstance(item, dict)
                ))
            else:
                items = [english_product_name(name) for name in (snapshot.get("present_product_counts") or {})]
            label = "Currently available products"
            answer = f"{label}: {', '.join(items) if items else 'none'}."
        else:
            items = [
                english_product_name(item.get("name"), item.get("id"))
                for item in (snapshot.get("missing_products") or [])
                if isinstance(item, dict)
            ]
            answer = f"Products needing restock: {', '.join(items) if items else 'none'}."
        return {"answer_type": "list", "items": items, "answer": answer}
    if kind == "STOCK_OVERVIEW":
        metrics = {
            "total_count": int(snapshot.get("total_count", 0) or 0),
            "type_count": int(snapshot.get("type_count", 0) or 0),
            "missing_count": int(snapshot.get("missing_count", 0) or 0),
        }
        answer = (
            f"There are {metrics['total_count']} bottles across {metrics['type_count']} product types. "
            f"{metrics['missing_count']} products need restocking."
        )
        return {"answer_type": "overview", "metrics": metrics, "answer": answer}
    if kind in {"PRODUCT_STATUS", "PRODUCT_COUNT"} and intent.get("product_name"):
        requested = str(intent["product_name"]).strip()
        resolved, count, present = resolve_product(snapshot, requested)
        if resolved is None:
            requested_display = english_product_name(requested)
            return {
                "answer_type": "status", "value": None, "product_name": requested_display,
                "product_status": "UNKNOWN",
                "answer": f"I could not find {requested_display} among the registered shelf products.",
            }
        if kind == "PRODUCT_COUNT":
            return {
                "answer_type": "number", "value": count, "unit": "bottles", "product_name": resolved or requested,
                "product_status": "IN_STOCK" if present else "OUT_OF_STOCK",
                "answer": f"There are {count} {resolved or requested} bottles on the shelf.",
            }
        status = "in stock" if present else "out of stock"
        return {
            "answer_type": "status", "value": present, "product_name": resolved or requested,
            "product_status": "IN_STOCK" if present else "OUT_OF_STOCK",
            "answer": f"{resolved or requested} is currently {status}.",
        }
    return {
        "answer_type": "text",
        "answer": "I can answer questions about bottle counts, product types, available products, and restocking status.",
    }


def resolve_product(snapshot: dict[str, Any], requested: str) -> tuple[str | None, int, bool]:
    target = normalize_question(requested)
    counts = snapshot.get("present_product_counts") or {}
    records: list[tuple[str, str, int, bool]] = []
    for item in snapshot.get("present_products") or []:
        if not isinstance(item, dict):
            continue
        original = str(item.get("name") or "").strip()
        display = english_product_name(original, item.get("id"))
        count = int(counts.get(original, item.get("count", 0)) or 0)
        records.append((original, display, count, True))
    for item in snapshot.get("missing_products") or []:
        if not isinstance(item, dict):
            continue
        original = str(item.get("name") or "").strip()
        records.append((original, english_product_name(original, item.get("id")), 0, False))
    if not records:
        records.extend((str(name), english_product_name(name), int(count or 0), True) for name, count in counts.items())
    for original, display, count, present in records:
        if target in {normalize_question(original), normalize_question(display)}:
            return display, count, present
    for original, display, count, present in records:
        candidates = (normalize_question(original), normalize_question(display))
        if target and any(target in candidate or candidate in target for candidate in candidates if candidate):
            return display, count, present
    return None, 0, False


def fallback_answer(intent: dict[str, Any], snapshot: dict[str, Any]) -> str:
    kind = intent.get("intent")
    if kind == "COUNT_TOTAL":
        return f"There are {snapshot['total_count']} bottles on the shelf."
    if kind == "COUNT_TYPES":
        return f"There are {snapshot['type_count']} different product types on the shelf."
    if kind == "COUNT_MISSING":
        return f"There are {int(snapshot.get('missing_count', 0) or 0)} missing products."
    if kind == "LIST_PRESENT_PRODUCTS":
        rows = snapshot.get("present_products") or []
        names = (
            [english_product_name(item.get("name"), item.get("id")) for item in rows if isinstance(item, dict)]
            if rows else [english_product_name(name) for name in snapshot.get("present_product_counts", {})]
        )
        return "Currently available: " + (", ".join(names) if names else "none") + "."
    if kind == "LIST_MISSING_PRODUCTS":
        names = [
            english_product_name(item.get("name"), item.get("id"))
            for item in snapshot.get("missing_products", []) if isinstance(item, dict)
        ]
        return "Products needing restock: " + (", ".join(names) if names else "none") + "."
    if kind in {"PRODUCT_STATUS", "PRODUCT_COUNT"} and intent.get("product_name"):
        target = intent["product_name"].lower()
        rows = [item for item in snapshot.get("present_products", []) if item["name"].lower() == target]
        if rows:
            return f"{intent['product_name']} is currently available."
        return f"I could not find {intent['product_name']} among the currently available products."
    return "I can answer questions about bottle counts, product types, available products, and restocking status."


def _extract_json(value: Any) -> Any:
    text = _extract_message_text(value)
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _extract_message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    choices = value.get("choices")
    if isinstance(choices, list) and choices:
        return _extract_message_text(choices[0])
    message = value.get("message")
    if isinstance(message, dict):
        return _extract_message_text(message.get("content"))
    content = value.get("content")
    return content if isinstance(content, str) else ""
