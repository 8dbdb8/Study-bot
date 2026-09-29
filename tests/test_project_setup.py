import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ProjectSetupFilesTests(unittest.TestCase):
    def read_file(self, relative_path):
        return (PROJECT_ROOT / relative_path).read_text(
            encoding="utf-8"
        )

    def test_requirements_include_runtime_dependencies(self):
        requirements = self.read_file("requirements.txt")

        for package in (
            "aiohttp==",
            "discord.py==",
            "python-dotenv==",
        ):
            with self.subTest(package=package):
                self.assertIn(package, requirements)

    def test_env_example_contains_only_placeholder(self):
        env_example = self.read_file(".env.example")

        self.assertIn(
            "DISCORD_TOKEN=replace_with_your_discord_bot_token",
            env_example,
        )

    def test_start_script_is_portable(self):
        script = self.read_file("start_studybot.bat")

        self.assertIn('cd /d "%~dp0"', script)
        self.assertNotIn("C:\\Users\\", script)
        self.assertIn("setup_studybot.bat", script)
        self.assertIn(
            "DISCORD_TOKEN=replace_with_your_discord_bot_token",
            script,
        )

    def test_setup_script_preserves_existing_env(self):
        script = self.read_file("setup_studybot.bat")

        self.assertIn('if not exist ".env"', script)
        self.assertIn(
            'pip install -r requirements.txt',
            script,
        )
        self.assertIn("sys.version_info >= (3, 11)", script)
        self.assertIn("ollama pull qwen3:8b", script)

    def test_readme_documents_first_run(self):
        readme = self.read_file("README.md")

        for text in (
            "setup_studybot.bat",
            "start_studybot.bat",
            "DISCORD_TOKEN",
            "勉強部屋",
            "勉強ログ",
            "/sglog",
        ):
            with self.subTest(text=text):
                self.assertIn(text, readme)


if __name__ == "__main__":
    unittest.main()
