import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from report_formatter import (  # noqa: E402
    WHATSAPP_LIMIT,
    accounts_in_report,
    assemble_report,
    describe_kpi_direction,
    format_metric,
    format_number_br,
    render_account_whatsapp,
    render_whatsapp,
    scope_report_to_account,
)


class ReportFormatterTest(unittest.TestCase):
    def test_formats_brazilian_numbers(self):
        self.assertEqual(format_number_br(1234567.89), "1.234.567,89")
        self.assertEqual(format_metric(12.34, "currency"), "R$ 12,34")
        self.assertEqual(format_metric(3.95, "percent"), "3,95%")
        self.assertEqual(format_metric(3.53, "ratio"), "3,53")

    def test_kpi_direction_respects_higher_and_lower(self):
        self.assertIn(
            "direção favorável", describe_kpi_direction(2.0, 1.5, "higher")
        )
        self.assertIn(
            "direção desfavorável", describe_kpi_direction(30.0, 25.0, "lower")
        )
        self.assertIn(
            "direção desfavorável", describe_kpi_direction(1.0, 1.5, "higher")
        )
        self.assertIn(
            "direção favorável", describe_kpi_direction(20.0, 25.0, "lower")
        )

    def test_uses_smallest_profile_window_and_nested_ads(self):
        now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config").mkdir()
            (base / "dashboards").mkdir()
            profile = {
                "windows": [14, 4, 7],
                "ad_account_id": "CONTA_DE_TESTE",
                "primary_kpi": "roas",
                "kpis": [
                    {
                        "key": "roas",
                        "label": "ROAS",
                        "target": 2,
                        "better": "higher",
                        "format": "ratio",
                    }
                ],
            }
            payload = {
                "generated_at": (now - timedelta(hours=2)).isoformat(),
                "window_days": 4,
                "ad_account_id": "CONTA_DE_TESTE",
                "currency": "BRL",
                "primary_kpi": "roas",
                "kpis_summary": {
                    "roas": {
                        "value": 3.5,
                        "target": 2,
                        "delta_pct": 75,
                        "status": "green",
                    }
                },
                "spend_total": 1234.5,
                "campaigns": [
                    {
                        "name": "Campanha A",
                        "spend": 1234.5,
                        "metrics": {"roas": 3.5},
                        "adsets": [
                            {
                                "ads": [
                                    {
                                        "ad_id": "ad_exemplo",
                                        "ad_name": "Criativo A",
                                        "spend": 300,
                                        "decide": "SCALE",
                                        "decide_reason": "ROAS acima da meta",
                                    }
                                ]
                            }
                        ],
                    }
                ],
            }
            (base / "config" / "meta_perfil.json").write_text(json.dumps(profile))
            (base / "dashboards" / "paid-traffic-4d.json").write_text(
                json.dumps(payload)
            )

            report = assemble_report(operation_dir=base, now=now)

        self.assertEqual(report["window"], 4)
        self.assertEqual(report["spend"], "R$ 1.234,50")
        self.assertFalse(report["stale"])
        self.assertEqual(report["scale_ads"][0]["name"], "Criativo A")
        self.assertIn("direção favorável", report["kpis"][0]["direction"])

    def test_scope_report_to_account_isolates_campaigns_and_spend(self):
        now = datetime(2026, 8, 26, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config").mkdir()
            (base / "dashboards").mkdir()
            profile = {
                "windows": [1],
                "ad_account_ids": ["act_A", "act_B"],
                "primary_kpi": "cost_per_msg",
                "kpis": [
                    {
                        "key": "cost_per_msg",
                        "label": "Custo por mensagem",
                        "target": 3.5,
                        "better": "lower",
                        "format": "currency",
                    }
                ],
                "decide_exclude_tags": [],
            }
            payload = {
                "generated_at": now.isoformat(),
                "window_days": 1,
                "currency": "BRL",
                "primary_kpi": "cost_per_msg",
                "kpis_summary": {},
                "spend_total": 30,
                "accounts": [
                    {
                        "ad_account_id": "act_A",
                        "account_name": "Conta A",
                        "account_status": 1,
                        "account_status_label": "ATIVA",
                        "spend": 20,
                    },
                    {
                        "ad_account_id": "act_B",
                        "account_name": "Conta B",
                        "account_status": 1,
                        "account_status_label": "ATIVA",
                        "spend": 10,
                    },
                ],
                "campaigns": [
                    {
                        "name": "Campanha A1",
                        "account_id": "act_A",
                        "account_name": "Conta A",
                        "spend": 20,
                        "effective_status": "ACTIVE",
                        "metrics": {"cpm": 5.0},
                        "raws": {"impressions": 1000, "clicks": 10},
                        "decide": "SCALE",
                    },
                    {
                        "name": "Campanha B1",
                        "account_id": "act_B",
                        "account_name": "Conta B",
                        "spend": 10,
                        "effective_status": "ACTIVE",
                        "metrics": {"cpm": 8.0},
                        "raws": {"impressions": 500, "clicks": 5},
                        "decide": "KILL",
                    },
                ],
            }
            (base / "config" / "meta_perfil.json").write_text(json.dumps(profile))
            (base / "dashboards" / "paid-traffic-1d.json").write_text(json.dumps(payload))
            report = assemble_report(operation_dir=base, now=now)

        accounts = accounts_in_report(report)
        self.assertEqual({a["id"] for a in accounts}, {"act_A", "act_B"})

        scoped_a = scope_report_to_account(report, "act_A")
        self.assertEqual(scoped_a["account_name"], "Conta A")
        self.assertEqual(len(scoped_a["active_campaigns"]), 1)
        self.assertEqual(scoped_a["active_campaigns"][0]["name"], "Campanha A1")
        self.assertEqual(scoped_a["active_campaigns"][0]["decision"], "Escalar")
        self.assertEqual(scoped_a["spend"], "R$ 20,00")

        scoped_b = scope_report_to_account(report, "act_B")
        self.assertEqual(len(scoped_b["active_campaigns"]), 1)
        self.assertEqual(scoped_b["active_campaigns"][0]["decision"], "Pausar")

        message_a = render_account_whatsapp(scoped_a)
        self.assertIn("Conta A", message_a)
        self.assertIn("Escalar", message_a)
        self.assertNotIn("Campanha B1", message_a)
        self.assertLessEqual(len(message_a), WHATSAPP_LIMIT)

    def test_stale_data_is_highlighted(self):
        now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config").mkdir()
            (base / "dashboards").mkdir()
            (base / "config" / "meta_perfil.json").write_text(
                json.dumps({"windows": [7], "kpis": [], "primary_kpi": "cpl"})
            )
            (base / "dashboards" / "paid-traffic-7d.json").write_text(
                json.dumps(
                    {
                        "generated_at": (now - timedelta(hours=27)).isoformat(),
                        "window_days": 7,
                        "kpis_summary": {},
                        "campaigns": [],
                        "spend_total": 0,
                    }
                )
            )
            report = assemble_report(operation_dir=base, now=now)
        self.assertTrue(report["stale"])
        self.assertIn("DADO DEFASADO", report["warning"])

    def test_missing_json_reports_unavailable_without_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "config").mkdir()
            (base / "config" / "meta_perfil.json").write_text(
                json.dumps({"windows": [4], "kpis": [], "primary_kpi": "cpl"})
            )
            report = assemble_report(operation_dir=base)
        self.assertFalse(report["available"])
        self.assertIsNone(report["spend"])
        self.assertIn("não existe", report["warning"])
        self.assertIn("não inventou números", report["warning"])

    def test_whatsapp_never_exceeds_limit(self):
        active_campaigns = [
            {
                "name": "Campanha " + ("longa " * 20) + str(index),
                "account_name": "Conta de Teste",
                "spend": "R$ 1.000,00",
                "cpm_display": "R$ 12,34",
                "ctr_display": "1,23%",
                "cpa_display": "R$ 45,67",
                "decision": "Manter",
                "decision_emoji": "🟡",
            }
            for index in range(30)
        ]
        report = {
            "available": True,
            "warning": "",
            "account": "CONTA_DE_TESTE",
            "account_name": "Conta de Teste",
            "window": 7,
            "generated_display": "29/07/2026 às 08:05",
            "spend": "R$ 10.000,00",
            "kpis": [
                {
                    "status": "green",
                    "label": "ROAS",
                    "value_display": "3,53",
                    "target_display": "2,00",
                }
            ],
            "active_campaigns": active_campaigns,
            "footer": "Dados do coletor local. Para pedir na hora, rode o formatter.",
        }
        rendered = render_whatsapp(report)
        self.assertLessEqual(len(rendered), WHATSAPP_LIMIT)
        self.assertIn("Relatório resumido", rendered)

    def test_whatsapp_uses_portuguese_decision_labels(self):
        report = {
            "available": True,
            "warning": "",
            "account": "CONTA_DE_TESTE",
            "account_name": "Conta de Teste",
            "window": 7,
            "generated_display": "29/07/2026 às 08:05",
            "spend": "R$ 100,00",
            "kpis": [],
            "active_campaigns": [
                {
                    "name": "Campanha Escalar",
                    "account_name": "Conta de Teste",
                    "spend": "R$ 50,00",
                    "cpm_display": "R$ 10,00",
                    "ctr_display": "2,00%",
                    "cpa_display": "R$ 5,00",
                    "decision": "Escalar",
                    "decision_emoji": "📈",
                },
                {
                    "name": "Campanha Pausar",
                    "account_name": "Conta de Teste",
                    "spend": "R$ 50,00",
                    "cpm_display": "R$ 20,00",
                    "ctr_display": "0,50%",
                    "cpa_display": "R$ 25,00",
                    "decision": "Pausar",
                    "decision_emoji": "🛑",
                },
            ],
            "footer": "Dados do coletor local.",
        }
        rendered = render_whatsapp(report)
        self.assertIn("Escalar", rendered)
        self.assertIn("Pausar", rendered)
        self.assertNotIn("SCALE", rendered)
        self.assertNotIn("KILL", rendered)


if __name__ == "__main__":
    unittest.main()
