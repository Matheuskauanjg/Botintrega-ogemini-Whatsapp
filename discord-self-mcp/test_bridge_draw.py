"""Offline regression tests for contextual image generation (no API calls)."""
import base64
import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace

# CI runs dependency-free unit tests; API calls are not made here.
for module_name in ("discord", "httpx", "fastapi"):
    if importlib.util.find_spec(module_name) is None:
        fake = types.ModuleType(module_name)
        if module_name == "fastapi":
            fake.Depends = lambda dependency: dependency
        sys.modules[module_name] = fake

import bridge_draw as draw


class DrawTests(unittest.TestCase):
    def test_explicit_command(self):
        match = draw.COMMAND_RE.match("!desenhar um dragão e uma nave")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "um dragão e uma nave")

    def test_natural_command(self):
        match = draw.COMMAND_RE.match("Greed, desenha a conversa de ontem")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "a conversa de ontem")

    def test_not_command(self):
        self.assertIsNone(draw.COMMAND_RE.match("greed fala comigo"))

    def test_gemini_steps_image(self):
        raw = b"\x89PNG\r\n\x1a\nfake image"
        data = base64.b64encode(raw).decode()
        payload = {
            "id": "int_test",
            "steps": [
                {"type": "model_output", "content": [
                    {"type": "text", "text": "feito"},
                    {"type": "image", "mime_type": "image/png", "data": data},
                ]},
            ],
        }
        image, ext = draw._extract_image(payload)
        self.assertEqual(raw, image)
        self.assertEqual("png", ext)

    def test_gemini_legacy_image(self):
        raw = b"jpeg"
        payload = {"outputs": [
            {"type": "image", "mime_type": "image/jpeg", "data": base64.b64encode(raw).decode()}
        ]}
        self.assertEqual(draw._extract_image(payload), (raw, "jpg"))

    def test_context_masks_credentials(self):
        message = SimpleNamespace(
            content="!desenhar uma espada",
            author=SimpleNamespace(display_name="Pessoa"),
        )
        prompt = draw._context_prompt([
            {"name": "Greed", "content": "vamos lutar com espadas"},
            {"name": "Jogador", "content": "senha: minhatestepass123 https://example.com/segredo"}
        ], message, "")
        self.assertIn("espadas", prompt)
        self.assertNotIn("minhatestepass123", prompt)
        self.assertNotIn("https://example.com/segredo", prompt)


if __name__ == "__main__":
    unittest.main()
