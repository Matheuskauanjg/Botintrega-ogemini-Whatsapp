import os
import unittest
from unittest.mock import patch

from greed_persona import dogao_guidance, dogao_targeted, normalize


class GreedPersonaTests(unittest.TestCase):
    def test_accent_and_case(self):
        self.assertEqual(normalize('  DOGÃO   '), 'dogao')
        self.assertTrue(dogao_targeted('O Dogão', '10', 'Greed, oi'))

    def test_does_not_flirt_with_everyone(self):
        self.assertFalse(dogao_targeted('Ana', '11', 'oi Greed'))
        self.assertFalse(dogao_targeted('Ana', '11', 'o Dogão entrou na call'))
        self.assertFalse(dogao_targeted('dogadinho', '11', 'oi'))

    def test_explicit_request(self):
        self.assertTrue(dogao_targeted('Ana', '11', 'Greed, flerta com o Dogão'))
        self.assertTrue(dogao_targeted('Ana', '11', 'manda uma cantada pro dogao'))

    def test_configured_discord_id(self):
        with patch.dict(os.environ, {'DOGAO_USER_ID': '123456789'}, clear=False):
            self.assertTrue(dogao_targeted('Membro', '123456789', 'fala Greed'))
            self.assertFalse(dogao_targeted('Membro', 'other', 'fala Greed'))

    def test_can_disable(self):
        with patch.dict(os.environ, {'DOGAO_FLIRT_ENABLED': 'false'}, clear=False):
            self.assertEqual(dogao_guidance('dogão', '1', 'oi'), '')

    def test_respect_no(self):
        result = dogao_guidance('Dogão', '10', 'para de flertar')
        self.assertIn('respeite', result)
        self.assertNotIn('Use no máximo uma cantada', result)

    def test_guidance_is_brief_and_non_explicit(self):
        result = dogao_guidance('Dogão', '10', 'oi greed')
        self.assertIn('pergunta real', result)
        self.assertIn('sem spam', result)


if __name__ == '__main__':
    unittest.main()
