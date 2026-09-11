import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "app" / "static" / "index.html"


class ResumeErrorMessageUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")

    def test_structured_errors_never_reach_alert_as_object_string(self):
        self.assertIn("return text === '[object Object]' ? '' : text;", self.html)
        self.assertIn(
            'throw new Error(this.formatApiError(data.detail || data.message || data, "Falha ao reiniciar."));',
            self.html,
        )
        self.assertIn(
            'const retryError = "Erro ao reiniciar: " + this.formatApiError(e && e.message ? e.message : e, "Falha ao reiniciar.");',
            self.html,
        )
        self.assertNotIn(
            'throw new Error(data.detail || data.message || "Falha ao reiniciar.");',
            self.html,
        )

    def test_resume_and_diagnostics_use_the_shared_error_formatter(self):
        self.assertIn(
            "throw new Error(this.formatApiError(data.detail || data.message || data, 'Falha ao retomar a produção.'));",
            self.html,
        )
        self.assertIn(
            "alert(this.formatApiError(e && e.message ? e.message : e, 'Falha ao retomar a produção.'));",
            self.html,
        )
        self.assertIn(
            'throw new Error(this.formatApiError(data.detail || data.message || data, "Falha ao diagnosticar."));',
            self.html,
        )

    def test_retry_stays_on_the_existing_task_checkpoint(self):
        self.assertIn("encodeURIComponent(taskId)", self.html)
        self.assertIn(
            "if (['failed', 'paused'].includes(status) && currentTaskId)",
            self.html,
        )
        self.assertIn("await this.retryStoryTask();", self.html)


if __name__ == "__main__":
    unittest.main()
