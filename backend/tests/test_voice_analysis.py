import unittest

from app.voice_analysis import (
    analysis_requires_english_rewrite,
    normalize_voice_analysis,
)


EXPECTED = {
    "summary": "The customer asked about the product.",
    "intent": "Product inquiry",
    "recommendation": "Share product details.",
}


class VoiceAnalysisTests(unittest.TestCase):
    def test_normal_object(self):
        value = {
            "summary": EXPECTED["summary"],
            "customer_intent": EXPECTED["intent"],
            "recommendation": EXPECTED["recommendation"],
        }
        self.assertEqual(normalize_voice_analysis(value), EXPECTED)

    def test_json_string_and_double_stringified_json(self):
        value = (
            '"{\\"summary\\":\\"The customer asked about the product.\\",'
            '\\"intent\\":\\"Product inquiry\\",'
            '\\"recommendation\\":\\"Share product details.\\"}"'
        )
        self.assertEqual(normalize_voice_analysis(value), EXPECTED)

    def test_markdown_fenced_json(self):
        value = """```json
        {"summary":"The customer asked about the product.",
         "intent":"Product inquiry",
         "recommendation":"Share product details."}
        ```"""
        self.assertEqual(normalize_voice_analysis(value), EXPECTED)

    def test_nested_message_content(self):
        value = {"data": {"message": {"content": {
            "conversation_summary": EXPECTED["summary"],
            "customerIntent": EXPECTED["intent"],
            "next_step": EXPECTED["recommendation"],
        }}}}
        self.assertEqual(normalize_voice_analysis(value), EXPECTED)

    def test_openai_choices_wrapper(self):
        value = {"choices": [{"message": {"content": EXPECTED}}]}
        self.assertEqual(normalize_voice_analysis(value), EXPECTED)

    def test_plain_text_is_only_used_as_summary(self):
        result = normalize_voice_analysis("A short natural-language summary.")
        self.assertEqual(result["summary"], "A short natural-language summary.")
        self.assertNotIn("[object Object]", result.values())

    def test_non_english_analysis_requires_rewrite(self):
        self.assertTrue(analysis_requires_english_rewrite({
            "summary": "客户对产品感兴趣。",
            "intent": "Product inquiry",
            "recommendation": "Follow up.",
        }))
        self.assertTrue(analysis_requires_english_rewrite({
            "summary": "The customer asked about the product.",
            "intent": "Product inquiry",
            "recommendation": "Отправьте подробную информацию.",
        }))
        self.assertFalse(analysis_requires_english_rewrite(EXPECTED))


if __name__ == "__main__":
    unittest.main()
