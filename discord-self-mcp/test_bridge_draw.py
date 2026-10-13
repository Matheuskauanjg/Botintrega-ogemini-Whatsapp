"""Offline regression tests for contextual image generation (no API calls)."""
import base64
import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# CI runs dependency-free unit tests; API calls are not made here.
for module_name in ("discord", "httpx", "fastapi"):
    if importlib.util.find_spec(module_name) is None:
        fake = types.ModuleType(module_name)
        if module_name == "fastapi":
            fake.Depends = lambda dependency: dependency
        if module_name == "httpx":
            fake.HTTPError = OSError
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


    def test_nvidia_sd3_payload(self):
        url, payload = draw._nvidia_request("stabilityai/stable-diffusion-3-medium", "draw a dragon")
        self.assertTrue(url.endswith("/stabilityai/stable-diffusion-3-medium"))
        self.assertEqual(payload["prompt"], "draw a dragon")
        self.assertEqual(payload["mode"], "text-to-image")
        self.assertEqual(payload["output_format"], "jpeg")

    def test_nvidia_flux_payload(self):
        url, payload = draw._nvidia_request("black-forest-labs/flux.2-klein-4b", "draw a dragon")
        self.assertTrue(url.endswith("/black-forest-labs/flux.2-klein-4b"))
        self.assertEqual(payload["mode"], "Image Generation")
        self.assertEqual(payload["height"], 1024)
        self.assertEqual(payload["samples"], 1)

    def test_nvidia_artifact_base64(self):
        image = b"\xff\xd8\xff\xdbjpeg bytes"
        response = SimpleNamespace(
            headers={"content-type": "application/json"},
            json=lambda: {"artifacts": [
                {"finishReason": "SUCCESS", "base64": base64.b64encode(image).decode()}
            ]},
        )
        self.assertEqual(draw._extract_nvidia_image(response), (image, "jpg"))

    def test_nvidia_binary_image(self):
        image = b"\x89PNG\r\n\x1a\nimage bytes"
        response = SimpleNamespace(
            headers={"content-type": "image/png"}, content=image
        )
        self.assertEqual(draw._extract_nvidia_image(response), (image, "png"))

    def test_nvidia_reject_invalid_model(self):
        with self.assertRaises(ValueError):
            draw._nvidia_request("unknown/untrusted", "test")


class ProviderOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_nvidia_first_success(self):
        nvidia = AsyncMock(return_value=(b"nvidia_image", "jpg"))
        gemini = AsyncMock(return_value=(b"gemini_image", "png"))
        with patch.object(draw, "_generate_image_nvidia", nvidia), patch.object(draw, "_generate_image_gemini", gemini):
            result = await draw._generate_image("draw this")
        self.assertEqual(result, (b"nvidia_image", "jpg", "NVIDIA"))
        nvidia.assert_awaited_once()
        gemini.assert_not_awaited()

    async def test_fallback_to_gemini(self):
        nvidia = AsyncMock(side_effect=RuntimeError("NVIDIA unavailable"))
        gemini = AsyncMock(return_value=(b"gemini_image", "png"))
        with patch.object(draw, "_generate_image_nvidia", nvidia), patch.object(draw, "_generate_image_gemini", gemini):
            result = await draw._generate_image("draw this")
        self.assertEqual(result, (b"gemini_image", "png", "Gemini"))
        nvidia.assert_awaited_once()
        gemini.assert_awaited_once()

    async def test_both_providers_fail(self):
        nvidia = AsyncMock(side_effect=RuntimeError("NVIDIA unavailable"))
        gemini = AsyncMock(side_effect=RuntimeError("Gemini unavailable"))
        with patch.object(draw, "_generate_image_nvidia", nvidia), patch.object(draw, "_generate_image_gemini", gemini):
            with self.assertRaisesRegex(RuntimeError, "NVIDIA e Gemini falharam"):
                await draw._generate_image("draw this")


if __name__ == "__main__":
    unittest.main()
