"""Formatação do alerta e retry quando o Telegram recusa a mensagem.

Não chama a API real.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import aiohttp  # noqa: E402

import main as monitor  # noqa: E402
from telegram_notifier import send_telegram_alert  # noqa: E402

TITULO = "Monster_Energy Ultra_Peachy <promo> & Keen"
LINK = "https://s.shopee.com.br/an_offer_link?utm_content=a_b&x=1"


def _extra_pack() -> dict:
    return {
        "titulo": TITULO,
        "qtd": 6,
        "preco_pack": 47.90,
        "frete": None,
        "frete_obs": "Shopee API não informa frete",
        "link": LINK,
        "prime": False,
    }


class _Resposta:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return str(self._payload)

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Sessao:
    def __init__(self, status: int, payload, posts: list):
        self._status = status
        self._payload = payload
        self._posts = posts

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, json=None, headers=None):
        self._posts.append({"url": url, "json": json})
        return _Resposta(self._status, self._payload)


class AlertaTelegramTest(unittest.TestCase):
    def test_underscore_no_titulo_e_no_link_fica_literal_em_html(self) -> None:
        texto = monitor._build_alert_message(
            "Monster_Energy [9988_7766]",
            7.98,
            "shopee",
            _extra_pack(),
        )
        from telegram_notifier import escape_html

        self.assertIn(escape_html(TITULO), texto)
        self.assertIn(escape_html(LINK), texto)
        self.assertIn("_", TITULO)
        self.assertIn("_", LINK)
        self.assertIn("<b>Preço por lata:</b>", texto)
        self.assertIn("<i>Monster · limiar", texto)
        # O Markdown antigo abria itálico com "_" e a API recusava a entidade.
        self.assertNotIn("*Preço por lata:*", texto)
        self.assertNotIn("<promo>", texto)
        self.assertIn("&lt;promo&gt;", texto)
        self.assertIn("&amp;", texto)

    def test_alerta_de_lata_também_escapa(self) -> None:
        texto = monitor._build_alert_message(
            "Monster_Ultra <lata>",
            8.49,
            "pao_de_acucar",
        )
        self.assertIn("Monster_Ultra &lt;lata&gt;", texto)
        self.assertIn("<b>Preço encontrado:</b>", texto)
        self.assertNotIn("*Preço encontrado:*", texto)

    def test_envio_usa_html_e_devolve_false_quando_a_api_recusa(self) -> None:
        posts: list = []

        def sessao(*args, **kwargs):
            return _Sessao(
                400,
                "Bad Request: can't parse entities",
                posts,
            )

        with patch("telegram_notifier.aiohttp.ClientSession", sessao):
            ok = asyncio.run(
                send_telegram_alert("token", "123", "<b>oi</b> Monster_Ultra")
            )
        self.assertFalse(ok)
        self.assertEqual(posts[0]["json"]["parse_mode"], "HTML")
        self.assertIn("Monster_Ultra", posts[0]["json"]["text"])
        self.assertIn("bot", posts[0]["url"])

    def test_envio_ok_somente_com_ok_true(self) -> None:
        def sessao_ok(*args, **kwargs):
            return _Sessao(200, {"ok": True, "result": {}}, [])

        def sessao_falso(*args, **kwargs):
            return _Sessao(200, {"ok": False, "description": "chat not found"}, [])

        with patch("telegram_notifier.aiohttp.ClientSession", sessao_ok):
            self.assertTrue(asyncio.run(send_telegram_alert("t", "1", "oi")))
        with patch("telegram_notifier.aiohttp.ClientSession", sessao_falso):
            self.assertFalse(asyncio.run(send_telegram_alert("t", "1", "oi")))

    def test_erro_de_rede_devolve_false(self) -> None:
        class Quebrada:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def post(self, *args, **kwargs):
                raise aiohttp.ClientError("rede caiu")

        with patch("telegram_notifier.aiohttp.ClientSession", lambda *a, **k: Quebrada()):
            self.assertFalse(asyncio.run(send_telegram_alert("t", "1", "oi")))

    def test_falha_de_envio_nao_grava_o_preco(self) -> None:
        chave = "shopee::Monster_Energy Pipeline Punch [9988_7766]"
        historico = {chave: 9.0}

        async def falhou(*args, **kwargs):
            return False

        with patch("main.send_telegram_alert", falhou):
            asyncio.run(
                monitor._registrar_preco_monster(
                    historico,
                    site_id="shopee",
                    nome="Monster_Energy Pipeline Punch [9988_7766]",
                    preco=7.98,
                    telegram_token="token",
                    telegram_chat_id="123",
                    extra=_extra_pack(),
                )
            )
        self.assertEqual(historico[chave], 9.0)

    def test_envio_ok_grava_e_preco_acima_do_limiar_nao_envia(self) -> None:
        historico = {"amazon::Pack [B0]": 10.0}
        enviados: list[str] = []

        async def ok(token, chat, mensagem):
            enviados.append(mensagem)
            return True

        with patch("main.send_telegram_alert", ok):
            asyncio.run(
                monitor._registrar_preco_monster(
                    historico,
                    site_id="amazon",
                    nome="Pack [B0]",
                    preco=8.0,
                    telegram_token="token",
                    telegram_chat_id="123",
                    extra={
                        "titulo": "Monster_Pack",
                        "qtd": 6,
                        "preco_pack": 48.0,
                        "frete": 0,
                        "frete_obs": "frete grátis",
                        "link": "https://www.amazon.com.br/dp/B0_X",
                        "prime": True,
                    },
                )
            )
        self.assertEqual(historico["amazon::Pack [B0]"], 8.0)
        self.assertEqual(len(enviados), 1)
        self.assertIn("Monster_Pack", enviados[0])
        self.assertIn("https://www.amazon.com.br/dp/B0_X", enviados[0])

        async def nao_deveria(*args, **kwargs):
            raise AssertionError("preço acima do limiar não alerta")

        with patch("main.send_telegram_alert", nao_deveria):
            asyncio.run(
                monitor._registrar_preco_monster(
                    historico,
                    site_id="amazon",
                    nome="Pack [B0]",
                    preco=9.2,
                    telegram_token="token",
                    telegram_chat_id="123",
                )
            )
        self.assertEqual(historico["amazon::Pack [B0]"], 9.2)

    def test_preco_igual_nao_reenvia(self) -> None:
        historico = {"tauste::Energético Monster Lata 473ml": 8.49}

        async def nao_deveria(*args, **kwargs):
            raise AssertionError("preço estável não alerta")

        with patch("main.send_telegram_alert", nao_deveria):
            asyncio.run(
                monitor._registrar_preco_monster(
                    historico,
                    site_id="tauste",
                    nome="Energético Monster Lata 473ml",
                    preco=8.49,
                    telegram_token="token",
                    telegram_chat_id="123",
                )
            )
        self.assertEqual(historico["tauste::Energético Monster Lata 473ml"], 8.49)

    def test_uma_falha_nao_impede_o_proximo_alerta(self) -> None:
        historico = {
            "shopee::A [1]": 9.0,
            "amazon::B [2]": 9.0,
        }
        chamadas = {"n": 0}

        async def falha_a_primeira(*args, **kwargs):
            chamadas["n"] += 1
            return chamadas["n"] > 1

        with patch("main.send_telegram_alert", falha_a_primeira):
            asyncio.run(
                monitor._registrar_preco_monster(
                    historico,
                    site_id="shopee",
                    nome="A [1]",
                    preco=7.5,
                    telegram_token="t",
                    telegram_chat_id="1",
                    extra=_extra_pack(),
                )
            )
            asyncio.run(
                monitor._registrar_preco_monster(
                    historico,
                    site_id="amazon",
                    nome="B [2]",
                    preco=8.0,
                    telegram_token="t",
                    telegram_chat_id="1",
                    extra={
                        "titulo": "Monster Energy",
                        "qtd": 6,
                        "preco_pack": 48.0,
                        "frete": 0.0,
                        "frete_obs": "frete grátis",
                        "link": "https://www.amazon.com.br/dp/B2",
                        "prime": True,
                    },
                )
            )
        self.assertEqual(historico["shopee::A [1]"], 9.0)
        self.assertEqual(historico["amazon::B [2]"], 8.0)


if __name__ == "__main__":
    unittest.main()
