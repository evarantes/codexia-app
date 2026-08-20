from __future__ import annotations

import json
import unittest

from sqlalchemy.orm.attributes import set_committed_value

from app import _openai_credit_recovery_result
from app.models import VideoTask


class OpenAICreditResumeTests(unittest.TestCase):
    def _failed_task(self, *, code="OPENAI_NO_CREDIT", provider="openai"):
        task = VideoTask(id="credit-recovery-test")
        set_committed_value(task, "status", "failed")
        task.result_json = json.dumps(
            {
                "payload": {"mode": "topic", "force_reuse_assets": True},
                "provider_error": {
                    "provider": provider,
                    "code": code,
                    "message": "OpenAI sem saldo.",
                },
            },
            ensure_ascii=False,
        )
        return task

    def test_explicit_same_task_retry_is_credit_recovery_confirmation(self):
        task = self._failed_task()
        task.status = "processing"
        task.message = "Retomada preparada com reaproveitamento dos ativos; aguardando worker CX33..."

        result = _openai_credit_recovery_result(task)

        self.assertIsInstance(result, dict)
        self.assertEqual(result["provider_error"]["code"], "OPENAI_NO_CREDIT")

    def test_automatic_transition_does_not_clear_credit_lock(self):
        task = self._failed_task()
        task.status = "processing"
        task.message = "Executor automático reiniciando etapa."

        self.assertIsNone(_openai_credit_recovery_result(task))

    def test_other_provider_error_is_not_treated_as_recharge(self):
        task = self._failed_task(code="OPENAI_RATE_LIMIT")
        task.status = "processing"
        task.message = "Retomada preparada com reaproveitamento dos ativos; aguardando worker CX33..."

        self.assertIsNone(_openai_credit_recovery_result(task))


if __name__ == "__main__":
    unittest.main()
