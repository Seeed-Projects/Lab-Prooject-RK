import asyncio
import unittest
from unittest.mock import patch

from app.inventory_voice import (
    InventoryVoiceAssistant,
    build_structured_answer,
    fallback_answer,
    keyword_intent,
)


class FakeVoice:
    def __init__(self):
        self.events = []

    async def subscribe_events(self):
        return asyncio.Queue()

    async def unsubscribe_events(self, queue):
        del queue

    def publish_event(self, payload):
        self.events.append(payload)


class FakeRetailSingle:
    def inventory_snapshot(self):
        return {
            "source": "recamera",
            "sequence": 9,
            "data_valid": True,
            "data_age_seconds": 0.2,
            "total_count": 4,
            "type_count": 3,
            "present_products": [
                {"id": 1, "name": "Product 1", "count": 1},
                {"id": 2, "name": "Product 2", "count": 1},
            ],
            "present_product_counts": {"Product 1": 2, "Product 2": 1},
            "missing_products": [{"id": 3, "name": "Product 3"}],
        }


class InventoryVoiceTests(unittest.IsolatedAsyncioTestCase):
    def test_keyword_intents_cover_reference_questions(self):
        self.assertEqual(keyword_intent("How many bottles are there?")["intent"], "COUNT_TOTAL")
        self.assertEqual(keyword_intent("How many types are on the shelf?")["intent"], "COUNT_TYPES")
        self.assertEqual(keyword_intent("Which products are missing?")["intent"], "LIST_MISSING_PRODUCTS")

    def test_fallback_uses_snapshot_facts(self):
        snapshot = FakeRetailSingle().inventory_snapshot()
        self.assertEqual(
            fallback_answer({"intent": "COUNT_TOTAL"}, snapshot),
            "There are 4 bottles on the shelf.",
        )
        self.assertEqual(
            fallback_answer({"intent": "COUNT_TYPES"}, snapshot),
            "There are 3 different product types on the shelf.",
        )

    def test_structured_answers_keep_numeric_facts_typed(self):
        snapshot = FakeRetailSingle().inventory_snapshot()
        total = build_structured_answer({"intent": "COUNT_TOTAL"}, snapshot)
        self.assertEqual(total["answer_type"], "number")
        self.assertIsInstance(total["value"], int)
        self.assertEqual(total["value"], 4)
        missing = build_structured_answer({"intent": "COUNT_MISSING"}, {**snapshot, "missing_count": 1})
        self.assertIsInstance(missing["value"], int)
        self.assertEqual(missing["value"], 1)

    def test_chinese_recamera_names_are_rendered_in_english(self):
        snapshot = {
            **FakeRetailSingle().inventory_snapshot(),
            "present_products": [{"id": 1, "name": "商品 1", "count": 2}],
            "present_product_counts": {"商品 1": 2},
            "missing_products": [
                {"id": 2, "name": "商品 2"},
                {"id": 6, "name": "商品 6"},
            ],
        }
        missing = build_structured_answer({"intent": "LIST_MISSING_PRODUCTS"}, snapshot)
        self.assertEqual(missing["items"], ["Product 2", "Product 6"])
        self.assertEqual(missing["answer"], "Products needing restock: Product 2, Product 6.")
        self.assertNotRegex(missing["answer"], r"[\u3400-\u9fff]")

        count = build_structured_answer(
            {"intent": "PRODUCT_COUNT", "product_name": "Product 1"}, snapshot
        )
        self.assertEqual(count["product_name"], "Product 1")
        self.assertEqual(count["answer"], "There are 2 Product 1 bottles on the shelf.")

    async def test_turn_publishes_grounded_answer(self):
        voice = FakeVoice()
        assistant = InventoryVoiceAssistant(sales_voice=voice, retail_single=FakeRetailSingle())
        assistant._classify = lambda question: {"intent": "COUNT_TOTAL", "product_name": None, "confidence": 1.0}
        assistant._generate_answer = lambda question, intent, snapshot: fallback_answer(intent, snapshot)

        async def run_inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        # The test validates grounding and event structure, not Python's
        # executor shutdown behavior. Keep it deterministic on the device.
        with patch("app.inventory_voice.asyncio.to_thread", side_effect=run_inline):
            await assistant._answer_turn({"idx": 1}, "How many bottles are there?")

        self.assertEqual(len(voice.events), 1)
        self.assertEqual(voice.events[0]["type"], "inventory.answer")
        self.assertEqual(voice.events[0]["answer"], "There are 4 bottles on the shelf.")
        self.assertEqual(voice.events[0]["data"]["sequence"], 9)


if __name__ == "__main__":
    unittest.main()
