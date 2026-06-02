import os
from pathlib import Path
import tempfile
import unittest

from foodbrain_assistant.config import load_settings


class ConfigTest(unittest.TestCase):
    def test_load_settings_reads_dotenv_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "FOODBRAIN_GROCY_BASE_URL=http://grocy.local/",
                        "FOODBRAIN_GROCY_API_KEY=from-file",
                        "FOODBRAIN_EXPIRY_WINDOW_DAYS=3",
                        "FOODBRAIN_TOP_INGREDIENT_LIMIT=5",
                    ]
                ),
                encoding="utf-8",
            )

            settings = load_settings(env_file)

        self.assertEqual(settings.grocy_base_url, "http://grocy.local")
        self.assertEqual(settings.grocy_api_key, "from-file")
        self.assertEqual(settings.expiry_window_days, 3)
        self.assertEqual(settings.top_ingredient_limit, 5)

    def test_environment_variables_override_dotenv_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "FOODBRAIN_GROCY_API_KEY=from-file\n",
                encoding="utf-8",
            )

            old_value = os.environ.get("FOODBRAIN_GROCY_API_KEY")
            os.environ["FOODBRAIN_GROCY_API_KEY"] = "from-env"
            try:
                settings = load_settings(env_file)
            finally:
                if old_value is None:
                    os.environ.pop("FOODBRAIN_GROCY_API_KEY", None)
                else:
                    os.environ["FOODBRAIN_GROCY_API_KEY"] = old_value

        self.assertEqual(settings.grocy_api_key, "from-env")


if __name__ == "__main__":
    unittest.main()