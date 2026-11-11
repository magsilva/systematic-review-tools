from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from openalex import configure_session


class OpenAlexConfigTests(unittest.TestCase):
    def test_file_key_precedes_environment_and_blank_or_missing_uses_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.local.ini'
            session = Mock(headers={})
            with patch.dict('os.environ', OPENALEX_API_KEY='environment-key'):
                configure_session(session, path)
                self.assertEqual(session.headers['Authorization'], 'Bearer environment-key')
                path.write_text('[openalex]\napi_key = file%key\n')
                configure_session(session, path)
                self.assertEqual(session.headers['Authorization'], 'Bearer file%key')
                path.write_text('[openalex]\napi_key =  \n')
                configure_session(session, path)
                self.assertEqual(session.headers['Authorization'], 'Bearer environment-key')

    def test_invalid_configuration_does_not_disclose_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.local.ini'
            path.write_text('secret-value-without-section\n')
            with self.assertRaises(ValueError) as caught:
                configure_session(Mock(headers={}), path)
            self.assertNotIn('secret-value', str(caught.exception))
