"""Mercado Livre: parsing, pack, frete e credencial. Sem rede."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import main as monitor  # noqa: E402
from config_sites import SITES_CONFIG  # noqa: E402
from ofertas_feed import (  # noqa: E402
    build_ofertas,
    observacao_from_item,
    validar_ofertas,
)
from scraper_mercado_livre import (  # noqa: E402
    MAX_PRODUTOS,
    SEARCH_LIMIT,
    TIMEOUT_S,
    calcular_preco_lata,
    extrair_frete,
    fetch_mercado_livre_ofertas,
    melhor_oferta,
    montar_ofertas,
    parse_pack_qtd,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
QUANDO = "2026-11-12T03:00:00Z"
ANTES = "2026-11-11T18:00:00Z"


def _json(nome: str):
    return json.loads((FIXTURES / nome).read_text(encoding="utf-8"))


def _itens_por_produto() -> dict:
    bruto = _json("ml_itens.json")
    return {pid: payload["results"] for pid, payload in bruto.items()}


def _ofertas_da_busca():
    busca = _json("ml_busca.json")
    return montar_ofertas(busca["results"], _itens_por_produto())


class _Resposta:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _Sessao:
    def __init__(self, resolver):
        self.headers: dict = {}
        self.chamadas: list = []
        self.resolver = resolver
        self.fechada = False

    def request(self, method, url, timeout=None, **kwargs):
        self.chamadas.append(
            {"method": method, "url": url, "timeout": timeout, **kwargs}
        )
        status, payload = self.resolver(method, url, kwargs)
        return _Resposta(status, payload)

    def close(self):
        self.fechada = True


class MercadoLivreParsingTest(unittest.TestCase):
    def test_fixtures_filtram_473_e_guardam_a_melhor_oferta(self) -> None:
        ofertas = _ofertas_da_busca()
        ids = [o["catalog_id"] for o in ofertas]
        self.assertEqual(ids, ["MLB100473", "MLB100006", "MLB100007"])

        green = ofertas[0]
        self.assertEqual(green["item_id"], "MLB60691749")
        self.assertEqual(green["qtd"], 1)
        self.assertEqual(green["preco_pack"], 22.99)
        self.assertEqual(green["frete"], 17.7)
        self.assertEqual(green["preco_lata"], 40.69)
        self.assertEqual(green["preco"], 40.69)
        self.assertEqual(
            green["link"],
            "https://www.mercadolivre.com.br/p/MLB100473",
        )
        self.assertEqual(
            green["imagem"],
            "https://http2.mlstatic.com/D_NQ_NP_monster-green.jpg",
        )
        self.assertNotIn("MLB7000001", green["link"])

        pack = ofertas[1]
        self.assertEqual(pack["qtd"], 6)
        self.assertEqual(pack["preco_pack"], 72.0)
        self.assertEqual(pack["frete"], 0.0)
        self.assertEqual(pack["preco_lata"], 12.0)
        self.assertEqual(
            pack["link"],
            "https://www.mercadolivre.com.br/monster-ultra-pack/p/MLB100006",
        )
        self.assertTrue(pack["imagem"].startswith("https://"))
        obs_pack = observacao_from_item("mercado_livre", pack, preco_anterior=None)
        self.assertIsNotNone(obs_pack)
        assert obs_pack is not None
        self.assertEqual(obs_pack["pack_qtd"], 6)
        self.assertEqual(obs_pack["preco_lata"], 12.0)
        self.assertEqual(obs_pack["frete"], 0.0)

        sem_frete = ofertas[2]
        self.assertEqual(sem_frete["qtd"], 1)
        self.assertIsNone(sem_frete["frete"])
        self.assertIn("desconhecido", sem_frete["frete_obs"])
        self.assertEqual(sem_frete["preco_lata"], 18.0)
        self.assertEqual(sem_frete["preco_pack"], 18.0)
        obs = observacao_from_item("mercado_livre", sem_frete, preco_anterior=None)
        self.assertIsNotNone(obs)
        assert obs is not None
        self.assertIsNone(obs["frete"])
        self.assertEqual(obs["preco_lata"], 18.0)
        # A marca Monster está só no atributo BRAND. O monitor exige a palavra
        # no campo ``produto``; sem ela a oferta some e a vaga do teto se perde.
        self.assertIn("monster", sem_frete["produto"].lower())
        self.assertIn("monster", sem_frete["titulo"].lower())
        self.assertTrue(monitor._produto_elegivel(sem_frete["produto"]))
        self.assertTrue(sem_frete["produto"].startswith("Monster Energético Lata "))
        self.assertFalse(ofertas[0]["produto"].lower().startswith("monster monster"))

    def test_pack_qtd_vem_do_titulo_ou_vale_1(self) -> None:
        for caso in _json("ml_packs.json"):
            with self.subTest(titulo=caso["titulo"]):
                self.assertEqual(parse_pack_qtd(caso["titulo"]), caso["qtd"])

    def test_qtd_do_item_vence_a_do_produto_e_sem_indicio_fica_1(self) -> None:
        produto = {
            "id": "MLB100473",
            "name": "Monster Energy 473ml",
            "domain_id": "MLB-ENERGY_DRINKS",
            "status": "active",
        }
        item = {
            "item_id": "MLB1",
            "title": "Monster Energy 473ml 12x",
            "price": 120.0,
            "shipping": {"free_shipping": True},
        }
        oferta = melhor_oferta(produto, [item])
        self.assertIsNotNone(oferta)
        assert oferta is not None
        self.assertEqual(oferta["qtd"], 12)
        self.assertEqual(oferta["preco_lata"], 10.0)
        self.assertEqual(oferta["frete"], 0.0)

        produto["name"] = "Monster Energy pack com 6 473ml"
        item["title"] = "Monster Energy 473ml"
        item["price"] = 60.0
        oferta = melhor_oferta(produto, [item])
        assert oferta is not None
        self.assertEqual(oferta["qtd"], 6)
        self.assertEqual(oferta["preco_lata"], 10.0)

        produto["name"] = "Energético Monster Energy Green 473ml"
        item["title"] = ""
        item["price"] = 22.99
        item["shipping"] = {"free_shipping": False, "cost": 17.7}
        oferta = melhor_oferta(produto, [item])
        assert oferta is not None
        self.assertEqual(oferta["qtd"], 1)
        self.assertEqual(oferta["preco_lata"], 40.69)

    def test_preco_por_lata_com_frete_gratis_e_desconhecido(self) -> None:
        for caso in _json("ml_precos.json"):
            with self.subTest(price=caso["price"], frete=caso["frete"]):
                frete, obs = extrair_frete(caso["shipping"])
                if caso["frete"] is None:
                    self.assertIsNone(frete)
                else:
                    self.assertEqual(frete, caso["frete"])
                self.assertEqual(obs, caso["frete_obs"])
                self.assertEqual(
                    calcular_preco_lata(caso["price"], frete, caso["qtd"]),
                    caso["preco_lata"],
                )

    def test_teto_conta_so_oferta_publicada(self) -> None:
        produtos = [
            {
                "id": "MLB00000",
                "name": "Monster Energy 250ml",
                "domain_id": "MLB-ENERGY_DRINKS",
                "status": "active",
            }
        ]
        itens: dict = {}
        for i in range(1, 6):
            pid = f"MLB{i:05d}"
            produtos.append(
                {
                    "id": pid,
                    "name": "Monster Energy 473ml",
                    "domain_id": "MLB-ENERGY_DRINKS",
                    "status": "active",
                }
            )
            itens[pid] = []
        for i in range(6, 28):
            pid = f"MLB{i:05d}"
            produtos.append(
                {
                    "id": pid,
                    "name": "Monster Energy 473ml",
                    "domain_id": "MLB-ENERGY_DRINKS",
                    "status": "active",
                }
            )
            itens[pid] = [
                {
                    "item_id": f"ITEM{i}",
                    "price": 10.0,
                    "shipping": {"free_shipping": True},
                }
            ]
        ofertas = montar_ofertas(produtos, itens)
        self.assertEqual(len(ofertas), MAX_PRODUTOS)
        self.assertEqual(ofertas[0]["catalog_id"], "MLB00006")
        self.assertEqual(ofertas[-1]["catalog_id"], "MLB00025")
        ids = {o["catalog_id"] for o in ofertas}
        self.assertNotIn("MLB00000", ids)
        self.assertNotIn("MLB00001", ids)
        self.assertNotIn("MLB00026", ids)

    def test_so_brand_chega_ao_feed(self) -> None:
        produto = _json("ml_produto_so_brand.json")
        itens = _json("ml_itens_so_brand.json")["results"]
        self.assertNotIn("monster", produto["name"].lower())
        oferta = melhor_oferta(produto, itens)
        self.assertIsNotNone(oferta)
        assert oferta is not None
        self.assertTrue(oferta["produto"].startswith("Monster "))
        self.assertTrue(oferta["titulo"].startswith("Monster "))
        self.assertTrue(monitor._produto_elegivel(oferta["produto"]))
        obs = observacao_from_item("mercado_livre", oferta, preco_anterior=None)
        self.assertIsNotNone(obs)
        assert obs is not None
        self.assertEqual(obs["sabor"], "Ultra Peachy Keen")
        feed = build_ofertas(
            [obs],
            previous=None,
            sites_ok={"mercado_livre"},
            generated_at=QUANDO,
        )
        self.assertEqual(validar_ofertas(feed), [])
        self.assertEqual(len(feed["ofertas"]), 1)
        publicada = feed["ofertas"][0]
        self.assertEqual(publicada["loja"], "mercado_livre")
        self.assertEqual(publicada["sabor"], "Ultra Peachy Keen")
        self.assertIn("Monster", publicada["nome"])
        self.assertEqual(publicada["preco_lata"], 19.9)
        self.assertEqual(publicada["frete"], 0.0)
        self.assertEqual(feed["historico"][0]["loja"], "mercado_livre")
        self.assertEqual(feed["historico"][0]["sabor"], "Ultra Peachy Keen")
        self.assertEqual(feed["historico"][0]["id"], publicada["id"])

    def test_feed_v1_aceita_a_loja_e_o_historico_plano(self) -> None:
        green = _ofertas_da_busca()[0]
        obs = observacao_from_item("mercado_livre", green, preco_anterior=41.0)
        self.assertIsNotNone(obs)
        nome = "Energético Monster Lata 473ml"
        anterior = {
            "schema_version": 1,
            "generated_at": ANTES,
            "ofertas": [
                {
                    "id": f"tauste::{nome}",
                    "loja": "tauste",
                    "nome": nome,
                    "sabor": "Original",
                    "pack_qtd": 1,
                    "preco_pack": 9.0,
                    "preco_lata": 9.0,
                    "preco_lata_anterior": None,
                    "frete": None,
                    "cupom": None,
                    "link": None,
                    "imagem": None,
                    "visto_em": ANTES,
                }
            ],
            "historico": [
                {
                    "loja": "tauste",
                    "sabor": "Original",
                    "nome": nome,
                    "pack_qtd": 1,
                    "preco_lata": 9.0,
                    "visto_em": ANTES,
                    "id": f"tauste::{nome}",
                }
            ],
        }
        feed = build_ofertas(
            [obs],
            previous=anterior,
            sites_ok={"mercado_livre"},
            generated_at=QUANDO,
        )
        self.assertEqual(validar_ofertas(feed), [])
        self.assertEqual(feed["schema_version"], 1)
        self.assertEqual(
            {o["loja"] for o in feed["ofertas"]},
            {"mercado_livre", "tauste"},
        )
        ml = next(o for o in feed["ofertas"] if o["loja"] == "mercado_livre")
        self.assertEqual(ml["preco_pack"], 22.99)
        self.assertEqual(ml["preco_lata"], 40.69)
        self.assertEqual(ml["preco_lata_anterior"], 41.0)
        self.assertEqual(ml["frete"], 17.7)
        self.assertEqual(ml["pack_qtd"], 1)
        self.assertEqual(ml["sabor"], "Green")
        self.assertEqual(ml["link"], "https://www.mercadolivre.com.br/p/MLB100473")
        self.assertTrue(ml["imagem"].startswith("https://"))
        self.assertIn("MLB100473", ml["id"])
        ponto = next(p for p in feed["historico"] if p["loja"] == "mercado_livre")
        self.assertEqual(
            list(ponto),
            ["loja", "sabor", "nome", "pack_qtd", "preco_lata", "visto_em", "id"],
        )
        self.assertEqual(ponto["preco_lata"], 40.69)
        self.assertEqual(ponto["id"], ml["id"])
        tauste = next(p for p in feed["historico"] if p["loja"] == "tauste")
        self.assertEqual(tauste["preco_lata"], 9.0)
        self.assertEqual(tauste["visto_em"], ANTES)


class MercadoLivreCredencialTest(unittest.TestCase):
    def test_sem_credenciais_nao_consulta_a_rede(self) -> None:
        casos = (
            {"ML_CLIENT_ID": "", "ML_CLIENT_SECRET": ""},
            {"ML_CLIENT_ID": "", "ML_CLIENT_SECRET": "super-secreto"},
            {"ML_CLIENT_ID": "app-1", "ML_CLIENT_SECRET": ""},
        )
        for env in casos:
            with self.subTest(env=env):
                with patch.dict(os.environ, env, clear=False):
                    with patch(
                        "scraper_mercado_livre.requests.Session",
                        side_effect=AssertionError("rede"),
                    ):
                        with self.assertLogs(
                            "scraper_mercado_livre",
                            level="WARNING",
                        ) as logs:
                            ofertas = fetch_mercado_livre_ofertas()
                self.assertEqual(ofertas, [])
                texto = "\n".join(logs.output)
                self.assertIn("ML_CLIENT_ID/ML_CLIENT_SECRET ausentes", texto)
                self.assertIn("pulando", texto)
                self.assertNotIn("super-secreto", texto)
                self.assertNotIn("app-1", texto)

    def test_token_403_pula_sem_vazar_o_segredo(self) -> None:
        sessao = _Sessao(lambda method, url, kwargs: (403, {"message": "forbidden"}))
        with patch.dict(
            os.environ,
            {"ML_CLIENT_ID": "app-1", "ML_CLIENT_SECRET": "super-secreto"},
        ):
            with patch("scraper_mercado_livre.requests.Session", return_value=sessao):
                with patch("scraper_mercado_livre.time.sleep") as dormir:
                    with self.assertLogs("scraper_mercado_livre", level="WARNING") as logs:
                        ofertas = fetch_mercado_livre_ofertas()
        self.assertEqual(ofertas, [])
        self.assertEqual(len(sessao.chamadas), 1)
        self.assertEqual(sessao.chamadas[0]["method"], "POST")
        self.assertTrue(sessao.chamadas[0]["url"].endswith("/oauth/token"))
        dormir.assert_not_called()
        self.assertTrue(sessao.fechada)
        texto = "\n".join(logs.output)
        self.assertIn("oauth/token", texto)
        self.assertIn("403", texto)
        self.assertNotIn("super-secreto", texto)

    def test_busca_429_repete_uma_vez_e_pula_a_loja(self) -> None:
        def resolver(method, url, kwargs):
            if method == "POST":
                return 200, {"access_token": "tok-teste", "expires_in": 21600}
            return 429, {}

        sessao = _Sessao(resolver)
        with patch.dict(
            os.environ,
            {"ML_CLIENT_ID": "app-1", "ML_CLIENT_SECRET": "super-secreto"},
        ):
            with patch("scraper_mercado_livre.requests.Session", return_value=sessao):
                with patch("scraper_mercado_livre.time.sleep") as dormir:
                    ofertas = fetch_mercado_livre_ofertas()
        self.assertEqual(ofertas, [])
        buscas = [c for c in sessao.chamadas if "/products/search" in c["url"]]
        self.assertEqual(len(buscas), 2)
        dormir.assert_called_once_with(0.5)
        self.assertTrue(all("/sites/" not in c["url"] for c in sessao.chamadas))
        self.assertNotIn("super-secreto", " ".join(c["url"] for c in sessao.chamadas))

    def test_fetch_com_fixtures_nao_usa_a_rede(self) -> None:
        busca = _json("ml_busca.json")
        itens = _json("ml_itens.json")

        def resolver(method, url, kwargs):
            if method == "POST" and url.endswith("/oauth/token"):
                return 200, {"access_token": "tok-teste", "expires_in": 21600}
            if url.endswith("/products/search"):
                return 200, busca
            for pid, payload in itens.items():
                if url.endswith(f"/products/{pid}/items"):
                    return 200, payload
            raise AssertionError(f"URL inesperada: {method} {url}")

        sessao = _Sessao(resolver)
        with patch.dict(
            os.environ,
            {"ML_CLIENT_ID": "app-1", "ML_CLIENT_SECRET": "super-secreto"},
        ):
            with patch("scraper_mercado_livre.requests.Session", return_value=sessao):
                with patch("scraper_mercado_livre.time.sleep") as dormir:
                    ofertas = fetch_mercado_livre_ofertas()
        dormir.assert_not_called()
        self.assertEqual(ofertas, _ofertas_da_busca())
        self.assertTrue(sessao.fechada)
        self.assertEqual(sessao.headers["User-Agent"], "botmonster/mercado-livre")
        post = next(c for c in sessao.chamadas if c["method"] == "POST")
        self.assertEqual(post["data"]["grant_type"], "client_credentials")
        self.assertEqual(post["data"]["client_id"], "app-1")
        self.assertEqual(post["data"]["client_secret"], "super-secreto")
        self.assertEqual(
            post["headers"]["Content-Type"],
            "application/x-www-form-urlencoded",
        )
        self.assertNotIn("Authorization", post["headers"])
        self.assertNotIn("super-secreto", post["url"])
        busca_call = next(c for c in sessao.chamadas if c["url"].endswith("/products/search"))
        self.assertEqual(busca_call["timeout"], TIMEOUT_S)
        self.assertEqual(busca_call["params"]["site_id"], "MLB")
        self.assertEqual(busca_call["params"]["status"], "active")
        self.assertEqual(busca_call["params"]["limit"], SEARCH_LIMIT)
        self.assertEqual(busca_call["params"]["q"], "monster energy 473ml")
        self.assertEqual(busca_call["headers"]["Authorization"], "Bearer tok-teste")
        urls = [c["url"] for c in sessao.chamadas]
        self.assertTrue(any(u.endswith("/products/MLB100473/items") for u in urls))
        self.assertFalse(any("/sites/" in u for u in urls))
        self.assertFalse(
            any(u.rstrip("/").endswith("/products/MLB100473") for u in urls)
        )

    def test_sem_foto_na_busca_le_pictures_do_produto(self) -> None:
        ficha = _json("ml_produto_green.json")
        itens = _json("ml_itens.json")["MLB100473"]
        magro = {
            "id": "MLB100473",
            "status": "active",
            "domain_id": "MLB-ENERGY_DRINKS",
            "name": "Energético Monster Energy Green 473ml",
        }

        def resolver(method, url, kwargs):
            if method == "POST":
                return 200, {"access_token": "tok-teste", "expires_in": 21600}
            if url.endswith("/products/search"):
                return 200, {"results": [magro]}
            if url.endswith("/products/MLB100473/items"):
                return 200, itens
            if url.endswith("/products/MLB100473"):
                return 200, ficha
            raise AssertionError(url)

        sessao = _Sessao(resolver)
        with patch.dict(
            os.environ,
            {"ML_CLIENT_ID": "app-1", "ML_CLIENT_SECRET": "super-secreto"},
        ):
            with patch("scraper_mercado_livre.requests.Session", return_value=sessao):
                ofertas = fetch_mercado_livre_ofertas()
        self.assertEqual(len(ofertas), 1)
        self.assertEqual(
            ofertas[0]["imagem"],
            "https://http2.mlstatic.com/D_NQ_NP_monster-green.jpg",
        )
        self.assertEqual(ofertas[0]["preco_lata"], 40.69)

    def test_ficha_de_outro_tamanho_nao_vira_oferta(self) -> None:
        magro = {
            "id": "MLB100473",
            "status": "active",
            "domain_id": "MLB-ENERGY_DRINKS",
            "name": "Energético Monster Energy Green 473ml",
        }
        ficha = {
            "id": "MLB100473",
            "name": "Monster Energy 355ml",
            "domain_id": "MLB-ENERGY_DRINKS",
            "status": "active",
        }

        def resolver(method, url, kwargs):
            if method == "POST":
                return 200, {"access_token": "tok-teste", "expires_in": 21600}
            if url.endswith("/products/search"):
                return 200, {"results": [magro]}
            if url.endswith("/products/MLB100473"):
                return 200, ficha
            raise AssertionError(url)

        sessao = _Sessao(resolver)
        with patch.dict(
            os.environ,
            {"ML_CLIENT_ID": "app-1", "ML_CLIENT_SECRET": "super-secreto"},
        ):
            with patch("scraper_mercado_livre.requests.Session", return_value=sessao):
                ofertas = fetch_mercado_livre_ofertas()
        self.assertEqual(ofertas, [])
        self.assertFalse(any(c["url"].endswith("/items") for c in sessao.chamadas))


class MercadoLivreConfigTest(unittest.TestCase):
    def test_strategy_api_e_secrets_no_workflow(self) -> None:
        cfg = SITES_CONFIG["mercado_livre"]
        self.assertEqual(cfg["tipo"], "api")
        self.assertEqual(cfg["card_selector"], "")
        self.assertEqual(cfg["name_selector"], "")
        self.assertEqual(cfg["price_selector"], "")
        self.assertIn("/products/search", cfg["url_busca"])
        self.assertNotIn("/sites/", cfg["url_busca"])

        workflow = (REPO / ".github/workflows/price-monitor.yml").read_text(encoding="utf-8")
        self.assertIn("ML_CLIENT_ID: ${{ secrets.ML_CLIENT_ID }}", workflow)
        self.assertIn("ML_CLIENT_SECRET: ${{ secrets.ML_CLIENT_SECRET }}", workflow)

        exemplo = (REPO / ".env.example").read_text(encoding="utf-8")
        self.assertIn("ML_CLIENT_ID=", exemplo)
        self.assertIn("ML_CLIENT_SECRET=", exemplo)

        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn("ML_CLIENT_ID", readme)
        self.assertIn("secrets.ML_CLIENT_SECRET", readme)
        self.assertIn("473", readme)

        main = (REPO / "main.py").read_text(encoding="utf-8")
        self.assertIn("fetch_mercado_livre_ofertas", main)
        self.assertNotIn("fetch_search_results_api", main)
        self.assertNotIn("/sites/MLB/search", main)

        fonte = (REPO / "scraper_mercado_livre.py").read_text(encoding="utf-8")
        fim_doc = fonte.find('"""', 3)
        codigo = fonte[fim_doc + 3 :]
        self.assertNotIn("/sites/MLB/search", codigo)
        self.assertIn("/products/search", codigo)
        self.assertIn("client_credentials", codigo)


if __name__ == "__main__":
    unittest.main()
