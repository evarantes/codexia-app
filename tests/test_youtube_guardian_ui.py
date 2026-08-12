import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "app" / "static" / "index.html"


class YouTubeGuardianSimplifiedUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX.read_text(encoding="utf-8")

    def test_primary_panel_uses_plain_language_and_clear_navigation(self):
        self.assertIn("Custos e Resultados", self.html)
        self.assertIn("youtubeSubTab = 'guardian'; fetchGuardianOverview()", self.html)
        self.assertIn("Como está hoje?", self.html)
        self.assertIn("Gasto real no período", self.html)
        self.assertIn("Custo médio por vídeo", self.html)
        self.assertIn("Produções e custos", self.html)
        self.assertIn("Receitas e despesas", self.html)
        self.assertIn("Detalhes técnicos e simulações", self.html)

    def test_real_estimated_and_simulated_values_are_explained_separately(self):
        self.assertIn("os totais abaixo usam somente produções e lançamentos reais", self.html)
        self.assertIn("Simulações não aparecem nesta lista", self.html)
        self.assertIn("Eles não entram nos cartões de gasto, custo médio ou resultado financeiro", self.html)
        self.assertIn("guardianEstimatedAverage", self.html)
        self.assertIn("simulation_summary", self.html)

    def test_roi_is_not_shown_as_a_fake_loss_without_revenue(self):
        self.assertIn("guardianHasFinancialResult", self.html)
        self.assertIn("Ainda não calculado", self.html)
        self.assertIn("Registre uma receita para acompanhar o retorno", self.html)

    def test_budget_and_api_balance_have_actionable_explanations(self):
        self.assertIn("guardianBudgetStatusLabel", self.html)
        self.assertIn("Saldo das APIs", self.html)
        self.assertIn("sem inventar um saldo inexistente", self.html)
        self.assertIn("O que fazer:", self.html)

    def test_legacy_panel_was_removed_after_local_validation(self):
        self.assertNotIn("false && youtubeSubTab === 'guardian'", self.html)
        self.assertNotIn("Controle Financeiro e Eficiência", self.html)
        self.assertEqual(1, self.html.count("v-if=\"youtubeSubTab === 'guardian'\" class=\"space-y-5\""))


if __name__ == "__main__":
    unittest.main()
