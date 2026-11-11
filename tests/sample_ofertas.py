"""Gera um ofertas.json de exemplo e valida o schema v1. Sem rede e sem Telegram.

Uso:
    python tests/sample_ofertas.py
    python -m unittest tests.sample_ofertas tests.test_telegram_alert
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from ofertas_feed import (  # noqa: E402
    HISTORICO_MAX_PONTOS,
    build_ofertas,
    derivar_sabor,
    load_ofertas,
    observacao_from_item,
    save_ofertas,
    validar_ofertas,
)
from scraper_amazon import parse_search_html  # noqa: E402
from scraper_shopee import parse_nodes  # noqa: E402
from scraper_supermercado import (  # noqa: E402
    SupermarketScraper,
    absolute_http_url,
    escolher_imagem_card,
)

QUANDO = "2026-11-11T18:00:00Z"
DEPOIS = "2026-11-11T20:00:00Z"

SABORES_HISTORICO = {
    "Energético Diet Monster Absolutely Zero Lata 473ml": "Absolutely Zero",
    "Energético Juice Creme de Papaya Monster Rio Punch Lata 473ml": "Rio Punch",
    "Energético Juice Monster Mango Loco Lata 473ml": "Mango Loco",
    "Energético Juice Monster Pipeline Punch Lata 473ml": "Pipeline Punch",
    "Energético Juice Tropical Orange Monster Khaotic Lata 473ml": "Khaotic",
    "Energético Lemon Monster Dragon Ice Tea Lata 473ml": "Dragon Ice Tea",
    "Energético Monster Energy Ultra Violet Sem Açúcar 473ml": "Ultra Violet",
    "Energético Monster Lata 473ml": "Original",
    "Energético Monster Ultra Lata 473ml": "Ultra",
    "Energético Ultra Fiesta Mango Zero Açúcar Monster Lata 473ml": "Ultra Fiesta Mango",
    "Energético Ultra Paradise Zero Açúcar Monster Lata 473ml": "Ultra Paradise",
    "Energético Ultra Peachy Keen Zero Açúcar Monster Lata 473ml": "Ultra Peachy Keen",
    "Energético Ultra Strawberry Dreams Zero Açúcar Monster Lata 473ml": "Ultra Strawberry Dreams",
    "Energético Ultra Watermelon Zero Açúcar Monster Lata 473ml": "Ultra Watermelon",
    "Energético Zero Açúcar Monster Lata 473ml": "Zero Açúcar",
    "Energético Monster Absolutely Zero Lata 473ml": "Absolutely Zero",
    "Energético Monster Dragon Ice Tea Limão Lata 473ml": "Dragon Ice Tea",
    "Energético Monster Energy Lata 473ml": "Original",
    "Energético Monster Energy The Doctor Lata 473ml": "The Doctor",
    "Energético Monster Energy Ultra Peachy Keen sem Açúcar Lata 473ml": "Ultra Peachy Keen",
    "Energético Monster Energy Ultra Watermelon Lata 473ml": "Ultra Watermelon",
    "Energético Monster Energy sem Açúcar Lata 473ml": "Zero Açúcar",
    "Energético Monster Juice Pacific Punch Lata 473ml": "Pacific Punch",
    "Energético Monster Khaotic Tropical Orange Lata 473ml": "Khaotic",
    "Energético Monster Mango Loco Lata 473ml": "Mango Loco",
    "Energético Monster Pipeline Punch Lata 473ml": "Pipeline Punch",
    "Energético Monster Rio Punch Lata 473ml": "Rio Punch",
    "Energético Monster Ultra Fiesta Mango sem Açúcar Lata 473ml": "Ultra Fiesta Mango",
    "Energético Monster Ultra Strawberry Dreams sem Açúcar Lata 473ml": "Ultra Strawberry Dreams",
    "Energético Monster Ultra Violeta Lata 473ml": "Ultra Violet",
    "Energético Monster Ultra Violet Zero Açúcar Lata 473ml": "Ultra Violet",
    "Energético Ultra Monster Energy Lata 473ml": "Ultra",
}

AMAZON_HTML = """
<div data-component-type="s-search-result" data-asin="B0UNDERSCORE">
  <h2>Monster_Energy Ultra Peachy Keen Pack 6 latas 473ml</h2>
  <span class="a-price"><span class="a-offscreen">R$ 48,00</span></span>
  <div data-cy="delivery-recipe">Frete GRÁTIS</div>
  <i class="a-icon a-icon-prime" aria-label="Amazon Prime"></i>
  <img class="s-image" src="https://m.media-amazon.com/images/I/peachy_keen.jpg" />
</div>
<div data-component-type="s-search-result" data-asin="B0LATAUNICA">
  <h2>Monster Energy Lata 473ml</h2>
  <span class="a-price"><span class="a-offscreen">R$ 9,90</span></span>
</div>
<div data-component-type="s-search-result" data-asin="B0DOZE12">
  <h2>Monster Energy Pack 12 latas 473ml</h2>
  <span class="a-price"><span class="a-offscreen">R$ 90,00</span></span>
</div>
"""

SHOPEE_NODES = [
    {
        "productName": "Monster_Energy Pipeline Punch pack 6 latas 473ml",
        "itemId": "9988_7766",
        "priceMin": "47.90",
        "offerLink": "https://s.shopee.com.br/an_offer_link?utm_content=a_b&x=1",
        "imageUrl": "//cf.shopee.com.br/file/monster_pipeline.jpg",
        "shopName": "Loja_Monster",
    },
    {
        "productName": "Monster Energy lata 473ml",
        "itemId": "1",
        "priceMin": "9.90",
        "offerLink": "https://s.shopee.com.br/lata",
    },
    {
        "productName": "Monster Energy kit 12 latas",
        "itemId": "2",
        "priceMin": "80.00",
        "offerLink": "https://s.shopee.com.br/doze",
    },
    {
        "productName": "Red Bull pack 6 latas",
        "itemId": "3",
        "priceMin": "40.00",
        "offerLink": "https://s.shopee.com.br/rb",
    },
]


def _obs_historico(loja: str, nome: str, preco: float, anterior: float | None):
    return observacao_from_item(
        loja,
        {
            "produto": nome,
            "preco": preco,
            "link": f"https://exemplo.test/{loja}/monster",
            "imagem": f"https://exemplo.test/{loja}/monster.jpg",
        },
        preco_anterior=anterior,
    )


def feed_de_exemplo() -> dict:
    """Ofertas a partir do histórico real + um pack Amazon e um pack Shopee."""
    historico = json.loads((REPO / "historico_precos.json").read_text(encoding="utf-8"))
    observacoes = []
    for chave, preco in historico.items():
        loja, _, nome = chave.partition("::")
        observacoes.append(_obs_historico(loja, nome, preco, preco))

    amazon = parse_search_html(AMAZON_HTML)[0]
    observacoes.append(
        observacao_from_item("amazon", amazon, preco_anterior=9.5)
    )
    shopee = parse_nodes(SHOPEE_NODES)[0]
    observacoes.append(
        observacao_from_item("shopee", shopee, preco_anterior=None)
    )
    return build_ofertas(
        [o for o in observacoes if o],
        previous=None,
        sites_ok={"pao_de_acucar", "tauste", "sams_club", "amazon", "shopee"},
        generated_at=QUANDO,
    )


class OfertasFeedTest(unittest.TestCase):
    def test_sabores_dos_nomes_reais(self) -> None:
        historico = json.loads((REPO / "historico_precos.json").read_text(encoding="utf-8"))
        nomes = {chave.split("::", 1)[1] for chave in historico}
        self.assertEqual(nomes, set(SABORES_HISTORICO))
        for nome, sabor in SABORES_HISTORICO.items():
            self.assertEqual(derivar_sabor(nome), sabor, nome)

    def test_sabor_desconhecido_usa_o_resto_do_titulo(self) -> None:
        self.assertEqual(
            derivar_sabor("Monster Energy Lewis Hamilton pack 6 latas 473ml"),
            "Lewis Hamilton",
        )

    def test_sample_bate_com_o_schema(self) -> None:
        feed = feed_de_exemplo()
        self.assertEqual(validar_ofertas(feed), [])
        lojas = {o["loja"] for o in feed["ofertas"]}
        self.assertEqual(
            lojas,
            {"pao_de_acucar", "tauste", "sams_club", "amazon", "shopee"},
        )
        amazon = next(o for o in feed["ofertas"] if o["loja"] == "amazon")
        self.assertEqual(amazon["pack_qtd"], 6)
        self.assertEqual(amazon["preco_pack"], 48.0)
        self.assertEqual(amazon["preco_lata"], 8.0)
        self.assertEqual(amazon["preco_lata_anterior"], 9.5)
        self.assertEqual(amazon["frete"], 0.0)
        self.assertIsNone(amazon["cupom"])
        self.assertEqual(amazon["sabor"], "Ultra Peachy Keen")
        self.assertIn("_", amazon["nome"])
        self.assertTrue(amazon["imagem"].startswith("https://"))
        self.assertIn("B0UNDERSCORE", amazon["id"])

        shopee = next(o for o in feed["ofertas"] if o["loja"] == "shopee")
        self.assertEqual(shopee["preco_pack"], 47.9)
        self.assertEqual(shopee["preco_lata"], 7.98)
        self.assertIsNone(shopee["preco_lata_anterior"])
        self.assertIsNone(shopee["frete"])
        self.assertIn("offer_link", shopee["link"])
        self.assertIn("_", shopee["link"])
        self.assertTrue(shopee["imagem"].startswith("https://cf.shopee.com.br/"))

        lata = next(
            o
            for o in feed["ofertas"]
            if o["id"] == "pao_de_acucar::Energético Monster Lata 473ml"
        )
        self.assertEqual(lata["pack_qtd"], 1)
        self.assertEqual(lata["preco_pack"], lata["preco_lata"])
        self.assertEqual(lata["preco_lata_anterior"], lata["preco_lata"])
        self.assertIsNone(lata["frete"])
        self.assertEqual(lata["sabor"], "Original")
        self.assertIsInstance(lata["preco_lata"], float)

        self.assertIsInstance(feed["historico"], list)
        self.assertEqual(len(feed["historico"]), len(feed["ofertas"]))
        ids_oferta = {o["id"] for o in feed["ofertas"]}
        for ponto in feed["historico"]:
            self.assertEqual(
                list(ponto),
                ["loja", "sabor", "nome", "pack_qtd", "preco_lata", "visto_em", "id"],
            )
            self.assertEqual(ponto["visto_em"], QUANDO)
            self.assertIsInstance(ponto["preco_lata"], float)
            self.assertIsInstance(ponto["pack_qtd"], int)
            self.assertIn(ponto["id"], ids_oferta)

    def test_historico_compacto_so_quando_o_preco_muda(self) -> None:
        item = {
            "produto": "Energético Monster Lata 473ml",
            "preco": 8.49,
        }
        primeiro = build_ofertas(
            [observacao_from_item("tauste", item, preco_anterior=9.1)],
            previous=None,
            sites_ok={"tauste"},
            generated_at=QUANDO,
        )
        segundo = build_ofertas(
            [observacao_from_item("tauste", item, preco_anterior=8.49)],
            previous=primeiro,
            sites_ok={"tauste"},
            generated_at=DEPOIS,
        )
        nome = "Energético Monster Lata 473ml"

        def pontos_tauste(feed: dict) -> list[dict]:
            return [p for p in feed["historico"] if p["loja"] == "tauste" and p["nome"] == nome]

        self.assertIsInstance(segundo["historico"], list)
        self.assertEqual(len(pontos_tauste(segundo)), 1)
        self.assertEqual(pontos_tauste(segundo)[0]["sabor"], "Original")
        self.assertEqual(pontos_tauste(segundo)[0]["pack_qtd"], 1)
        self.assertEqual(segundo["ofertas"][0]["visto_em"], DEPOIS)
        self.assertEqual(segundo["ofertas"][0]["preco_lata_anterior"], 8.49)

        item["preco"] = 7.99
        terceiro = build_ofertas(
            [observacao_from_item("tauste", item, preco_anterior=8.49)],
            previous=segundo,
            sites_ok={"tauste"},
            generated_at="2026-11-12T00:00:00Z",
        )
        pontos = pontos_tauste(terceiro)
        self.assertEqual([p["preco_lata"] for p in pontos], [8.49, 7.99])
        self.assertEqual(validar_ofertas(terceiro), [])

    def test_historico_limita_pontos(self) -> None:
        previous = {
            "schema_version": 1,
            "generated_at": QUANDO,
            "ofertas": [],
            "historico": {
                "tauste::Energético Monster Lata 473ml": [
                    {"visto_em": f"2026-01-01T00:{i:02d}:00Z", "preco_lata": float(i)}
                    for i in range(HISTORICO_MAX_PONTOS)
                ]
            },
        }
        item = {"produto": "Energético Monster Lata 473ml", "preco": 4.2}
        feed = build_ofertas(
            [observacao_from_item("tauste", item, preco_anterior=3.0)],
            previous=previous,
            sites_ok={"tauste"},
            generated_at=DEPOIS,
        )
        self.assertIsInstance(feed["historico"], list)
        self.assertEqual(len(feed["historico"]), HISTORICO_MAX_PONTOS)
        self.assertEqual(feed["historico"][-1]["preco_lata"], 4.2)
        self.assertEqual(feed["historico"][-1]["loja"], "tauste")
        self.assertEqual(feed["historico"][-1]["nome"], "Energético Monster Lata 473ml")
        self.assertEqual(feed["historico"][-1]["sabor"], "Original")
        self.assertEqual(feed["historico"][-1]["pack_qtd"], 1)
        self.assertEqual(validar_ofertas(feed), [])

    def test_objeto_antigo_e_lista_nova_sao_aceitos(self) -> None:
        nome = "Energético Monster Lata 473ml"
        legado = {
            "schema_version": 1,
            "generated_at": QUANDO,
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
                    "visto_em": QUANDO,
                }
            ],
            "historico": {
                f"tauste::{nome}": [
                    {"visto_em": QUANDO, "preco_lata": 9.0},
                ]
            },
        }
        item = {"produto": nome, "preco": 8.0}
        feed = build_ofertas(
            [observacao_from_item("tauste", item, preco_anterior=9.0)],
            previous=legado,
            sites_ok={"tauste"},
            generated_at=DEPOIS,
        )
        self.assertEqual(
            [(p["preco_lata"], p["visto_em"]) for p in feed["historico"]],
            [(9.0, QUANDO), (8.0, DEPOIS)],
        )
        self.assertEqual(feed["historico"][0]["sabor"], "Original")
        de_novo = build_ofertas(
            [observacao_from_item("tauste", item, preco_anterior=8.0)],
            previous=feed,
            sites_ok={"tauste"},
            generated_at="2026-11-12T02:00:00Z",
        )
        self.assertEqual([p["preco_lata"] for p in de_novo["historico"]], [9.0, 8.0])
        self.assertEqual(validar_ofertas(de_novo), [])

    def test_loja_que_falhou_mantem_a_oferta_anterior(self) -> None:
        anterior = feed_de_exemplo()
        so_tauste = observacao_from_item(
            "tauste",
            {"produto": "Energético Monster Energy Lata 473ml", "preco": 8.5},
            preco_anterior=9.1,
        )
        feed = build_ofertas(
            [so_tauste],
            previous=anterior,
            sites_ok={"tauste"},
            generated_at=DEPOIS,
        )
        lojas = {o["loja"] for o in feed["ofertas"]}
        self.assertIn("pao_de_acucar", lojas)
        self.assertIn("amazon", lojas)
        self.assertIn("shopee", lojas)
        pao = next(o for o in feed["ofertas"] if o["loja"] == "pao_de_acucar")
        self.assertEqual(pao["visto_em"], QUANDO)
        pontos_pao = [p for p in feed["historico"] if p["loja"] == "pao_de_acucar"]
        self.assertTrue(pontos_pao)
        self.assertTrue(all(p["visto_em"] == QUANDO for p in pontos_pao))
        self.assertEqual(validar_ofertas(feed), [])

    def test_produto_sumiu_da_loja_que_respondeu(self) -> None:
        anterior = build_ofertas(
            [
                observacao_from_item(
                    "tauste",
                    {"produto": "Energético Monster Lata 473ml", "preco": 9.0},
                    preco_anterior=None,
                ),
                observacao_from_item(
                    "tauste",
                    {"produto": "Energético Monster Ultra Lata 473ml", "preco": 9.0},
                    preco_anterior=None,
                ),
            ],
            previous=None,
            sites_ok={"tauste"},
            generated_at=QUANDO,
        )
        feed = build_ofertas(
            [
                observacao_from_item(
                    "tauste",
                    {"produto": "Energético Monster Ultra Lata 473ml", "preco": 8.0},
                    preco_anterior=9.0,
                )
            ],
            previous=anterior,
            sites_ok={"tauste"},
            generated_at=DEPOIS,
        )
        ids = [o["id"] for o in feed["ofertas"]]
        self.assertEqual(ids, ["tauste::Energético Monster Ultra Lata 473ml"])
        nomes = {p["nome"] for p in feed["historico"]}
        self.assertIn("Energético Monster Lata 473ml", nomes)

    def test_roundtrip_em_arquivo(self) -> None:
        import tempfile

        feed = feed_de_exemplo()
        with tempfile.TemporaryDirectory() as tmp:
            caminho = str(Path(tmp) / "ofertas.json")
            save_ofertas(caminho, feed)
            recarregado = load_ofertas(caminho)
        self.assertEqual(validar_ofertas(recarregado), [])
        self.assertEqual(recarregado["schema_version"], 1)
        texto = json.dumps(recarregado)
        self.assertNotIn('"preco_lata": "', texto)

    def test_amazon_so_pack_de_6_e_imagem(self) -> None:
        itens = parse_search_html(AMAZON_HTML)
        self.assertEqual(len(itens), 1)
        self.assertEqual(itens[0]["asin"], "B0UNDERSCORE")
        self.assertEqual(itens[0]["frete"], 0.0)
        self.assertTrue(itens[0]["prime"])
        self.assertIn("peachy_keen", itens[0]["imagem"])

    def test_shopee_filtra_quantidade_e_imagem(self) -> None:
        itens = parse_nodes(SHOPEE_NODES)
        self.assertEqual(len(itens), 1)
        self.assertEqual(itens[0]["qtd"], 6)
        self.assertEqual(itens[0]["preco"], 7.98)
        self.assertIsNone(itens[0]["frete"])
        self.assertTrue(itens[0]["imagem"].startswith("https://"))

    def test_preco_de_supermercado_continua_em_reais(self) -> None:
        limpar = SupermarketScraper._clean_price
        self.assertEqual(limpar("R$ 8,49"), 8.49)
        self.assertEqual(limpar("R$ 1.062,94"), 1062.94)
        self.assertEqual(limpar("10.48"), 10.48)

    def test_url_absoluta_do_card(self) -> None:
        base = "https://www.paodeacucar.com/busca?w=monster"
        self.assertEqual(
            absolute_http_url(base, "/produto/123"),
            "https://www.paodeacucar.com/produto/123",
        )
        self.assertIsNone(absolute_http_url(base, "data:image/gif;base64,aaaa"))
        self.assertIsNone(absolute_http_url(base, "https://cdn.exemplo/placeholder.png"))

    def test_imagem_do_card_prefere_data_src_ao_placeholder_magento(self) -> None:
        base = "https://tauste.com.br/campinas1/catalogsearch/result/?q=monster"
        placeholder = (
            "https://tauste.com.br/static/version1/frontend/Tauste/default/"
            "pt_BR/Magento_Catalog/images/product/placeholder/small_image.jpg"
        )
        foto = "https://tauste.com.br/media/catalog/product/m/o/monster-ultra.jpg"
        self.assertEqual(
            escolher_imagem_card(
                base,
                {"src": placeholder, "currentSrc": placeholder, "data-src": foto},
            ),
            foto,
        )
        # data-src ganha mesmo quando src já é uma foto de verdade.
        self.assertEqual(
            escolher_imagem_card(
                base,
                {
                    "src": "https://cdn.exemplo/baixa.jpg",
                    "currentSrc": "https://cdn.exemplo/baixa.jpg",
                    "data-src": foto,
                },
            ),
            foto,
        )

    def test_imagem_do_card_pula_placeholder_e_cai_no_proximo(self) -> None:
        base = "https://tauste.com.br/campinas1/"
        self.assertEqual(
            escolher_imagem_card(
                base,
                {
                    "data-src": "data:image/gif;base64,aaaa",
                    "data-original": "/media/catalog/product/real.jpg",
                    "src": "https://tauste.com.br/static/placeholder/default.jpg",
                },
            ),
            "https://tauste.com.br/media/catalog/product/real.jpg",
        )
        self.assertEqual(
            escolher_imagem_card(
                "https://www.samsclub.com.br/monster",
                {
                    "src": "https://cdn.exemplo/placeholder.png",
                    "data-srcset": (
                        "https://cdn.exemplo/spinner.gif 320w, "
                        "https://cdn.exemplo/monster.jpg 640w"
                    ),
                },
            ),
            "https://cdn.exemplo/monster.jpg",
        )
        self.assertEqual(
            escolher_imagem_card(
                "https://tauste.com.br/",
                {
                    "src": "https://tauste.com.br/placeholder/x.jpg",
                    "sources": [
                        {"data-srcset": "https://tauste.com.br/media/via-source.jpg 1x"}
                    ],
                },
            ),
            "https://tauste.com.br/media/via-source.jpg",
        )
        self.assertIsNone(
            escolher_imagem_card(
                base,
                {
                    "data-src": "https://cdn.exemplo/placeholder.png",
                    "src": "https://cdn.exemplo/blank.gif",
                    "currentSrc": "https://cdn.exemplo/pixel.gif",
                },
            )
        )

    def test_imagem_do_card_src_real_quando_nao_ha_lazy(self) -> None:
        base = "https://www.paodeacucar.com/busca?w=monster"
        self.assertEqual(
            escolher_imagem_card(base, {"src": "/files/monster.png"}),
            "https://www.paodeacucar.com/files/monster.png",
        )

    def _obs_amazon(self, titulo: str, asin: str, preco: float):
        return observacao_from_item(
            "amazon",
            {
                "produto": f"{titulo} [{asin}]",
                "titulo": titulo,
                "qtd": 6,
                "preco_pack": round(preco * 6, 2),
                "preco_lata": preco,
                "preco": preco,
                "frete": 0.0,
            },
            preco_anterior=preco,
        )

    def test_historico_nao_oscila_entre_anuncios_com_o_mesmo_titulo(self) -> None:
        titulo = "Monster Energy Ultra Pack 6 latas 473ml"
        primeiro = build_ofertas(
            [self._obs_amazon(titulo, "B0AAA", 8.0), self._obs_amazon(titulo, "B0BBB", 10.0)],
            previous=None,
            sites_ok={"amazon"},
            generated_at=QUANDO,
        )

        def serie(feed: dict, asin: str) -> list[float]:
            oid = f"amazon::{titulo} [{asin}]"
            return [p["preco_lata"] for p in feed["historico"] if p.get("id") == oid]

        self.assertEqual(serie(primeiro, "B0AAA"), [8.0])
        self.assertEqual(serie(primeiro, "B0BBB"), [10.0])
        self.assertEqual(len(primeiro["historico"]), 2)
        for ponto in primeiro["historico"]:
            for chave in ("loja", "sabor", "nome", "pack_qtd", "preco_lata", "visto_em"):
                self.assertIn(chave, ponto)

        segundo = build_ofertas(
            [self._obs_amazon(titulo, "B0AAA", 8.0), self._obs_amazon(titulo, "B0BBB", 10.0)],
            previous=primeiro,
            sites_ok={"amazon"},
            generated_at=DEPOIS,
        )
        self.assertEqual(serie(segundo, "B0AAA"), [8.0])
        self.assertEqual(serie(segundo, "B0BBB"), [10.0])
        self.assertEqual(len(segundo["historico"]), 2)

        terceiro = build_ofertas(
            [self._obs_amazon(titulo, "B0AAA", 7.5), self._obs_amazon(titulo, "B0BBB", 10.0)],
            previous=segundo,
            sites_ok={"amazon"},
            generated_at="2026-11-12T00:00:00Z",
        )
        self.assertEqual(serie(terceiro, "B0AAA"), [8.0, 7.5])
        self.assertEqual(serie(terceiro, "B0BBB"), [10.0])
        self.assertEqual(validar_ofertas(terceiro), [])

    def test_historico_lista_sem_id_continua_na_mesma_oferta(self) -> None:
        nome = "Energético Monster Lata 473ml"
        oid = f"tauste::{nome}"
        anterior = {
            "schema_version": 1,
            "generated_at": QUANDO,
            "ofertas": [
                {
                    "id": oid,
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
                    "visto_em": QUANDO,
                }
            ],
            "historico": [
                {
                    "loja": "tauste",
                    "sabor": "Original",
                    "nome": nome,
                    "pack_qtd": 1,
                    "preco_lata": 9.0,
                    "visto_em": QUANDO,
                }
            ],
        }
        feed = build_ofertas(
            [observacao_from_item("tauste", {"produto": nome, "preco": 8.0}, preco_anterior=9.0)],
            previous=anterior,
            sites_ok={"tauste"},
            generated_at=DEPOIS,
        )
        self.assertEqual([p["preco_lata"] for p in feed["historico"]], [9.0, 8.0])
        self.assertTrue(all(p["id"] == oid for p in feed["historico"]))
        self.assertEqual(validar_ofertas(feed), [])

    def test_historico_antigo_ambiguo_nao_volta_a_oscilar(self) -> None:
        titulo = "Monster Energy Ultra Pack 6 latas 473ml"

        def oferta(asin: str, preco: float) -> dict:
            return {
                "id": f"amazon::{titulo} [{asin}]",
                "loja": "amazon",
                "nome": titulo,
                "sabor": "Ultra",
                "pack_qtd": 6,
                "preco_pack": preco * 6,
                "preco_lata": preco,
                "preco_lata_anterior": None,
                "frete": 0.0,
                "cupom": None,
                "link": None,
                "imagem": None,
                "visto_em": QUANDO,
            }

        anterior = {
            "schema_version": 1,
            "generated_at": QUANDO,
            "ofertas": [oferta("B0AAA", 8.0), oferta("B0BBB", 10.0)],
            "historico": [
                {
                    "loja": "amazon",
                    "sabor": "Ultra",
                    "nome": titulo,
                    "pack_qtd": 6,
                    "preco_lata": 8.0,
                    "visto_em": "2026-11-11T16:00:00Z",
                },
                {
                    "loja": "amazon",
                    "sabor": "Ultra",
                    "nome": titulo,
                    "pack_qtd": 6,
                    "preco_lata": 10.0,
                    "visto_em": QUANDO,
                },
            ],
        }
        feed = build_ofertas(
            [self._obs_amazon(titulo, "B0AAA", 8.0), self._obs_amazon(titulo, "B0BBB", 10.0)],
            previous=anterior,
            sites_ok={"amazon"},
            generated_at=DEPOIS,
        )
        por_id: dict[str, list[float]] = {}
        for ponto in feed["historico"]:
            por_id.setdefault(ponto["id"], []).append(ponto["preco_lata"])
        self.assertEqual(por_id[f"amazon::{titulo} [B0AAA]"], [8.0])
        self.assertEqual(por_id[f"amazon::{titulo} [B0BBB]"], [10.0])
        legadas = [
            chave
            for chave in por_id
            if chave not in (f"amazon::{titulo} [B0AAA]", f"amazon::{titulo} [B0BBB]")
        ]
        self.assertEqual(legadas, [f"amazon::{titulo}||Ultra||6"])
        self.assertEqual(por_id[legadas[0]], [8.0, 10.0])

        de_novo = build_ofertas(
            [self._obs_amazon(titulo, "B0AAA", 8.0), self._obs_amazon(titulo, "B0BBB", 10.0)],
            previous=feed,
            sites_ok={"amazon"},
            generated_at="2026-11-12T00:00:00Z",
        )
        self.assertEqual(len(de_novo["historico"]), len(feed["historico"]))
        # O anúncio que continua não herda a série fundida.
        so_um = build_ofertas(
            [self._obs_amazon(titulo, "B0AAA", 8.0)],
            previous=de_novo,
            sites_ok={"amazon"},
            generated_at="2026-11-12T04:00:00Z",
        )
        por_id = {}
        for ponto in so_um["historico"]:
            por_id.setdefault(ponto["id"], []).append(ponto["preco_lata"])
        self.assertEqual(por_id[f"amazon::{titulo} [B0AAA]"], [8.0])
        self.assertEqual(por_id[legadas[0]], [8.0, 10.0])
        self.assertEqual(validar_ofertas(so_um), [])

    def test_lista_sem_id_reencaixa_quando_a_oferta_volta(self) -> None:
        nome = "Energético Monster Lata 473ml"
        anterior = {
            "schema_version": 1,
            "generated_at": QUANDO,
            "ofertas": [],
            "historico": [
                {
                    "loja": "tauste",
                    "sabor": "Original",
                    "nome": nome,
                    "pack_qtd": 1,
                    "preco_lata": 9.0,
                    "visto_em": QUANDO,
                }
            ],
        }
        feed = build_ofertas(
            [observacao_from_item("tauste", {"produto": nome, "preco": 9.0}, preco_anterior=9.0)],
            previous=anterior,
            sites_ok={"tauste"},
            generated_at=DEPOIS,
        )
        self.assertEqual([p["preco_lata"] for p in feed["historico"]], [9.0])
        self.assertEqual(feed["historico"][0]["id"], f"tauste::{nome}")
        self.assertEqual(feed["historico"][0]["visto_em"], QUANDO)

    def test_historico_amazon_sem_id_reencaixa_no_asin_desta_rodada(self) -> None:
        titulo = "Monster Energy Ultra Pack 6 latas 473ml"
        oid = f"amazon::{titulo} [B0AAA]"
        anterior = {
            "schema_version": 1,
            "generated_at": QUANDO,
            "ofertas": [],
            "historico": [
                {
                    "loja": "amazon",
                    "sabor": "Ultra",
                    "nome": titulo,
                    "pack_qtd": 6,
                    "preco_lata": 8.0,
                    "visto_em": QUANDO,
                }
            ],
        }
        feed = build_ofertas(
            [self._obs_amazon(titulo, "B0AAA", 8.0)],
            previous=anterior,
            sites_ok={"amazon"},
            generated_at=DEPOIS,
        )
        self.assertEqual([p["id"] for p in feed["historico"]], [oid])
        self.assertEqual([p["preco_lata"] for p in feed["historico"]], [8.0])
        self.assertNotIn(f"amazon::{titulo}", {p["id"] for p in feed["historico"]})

        de_novo = build_ofertas(
            [self._obs_amazon(titulo, "B0AAA", 8.0)],
            previous=feed,
            sites_ok={"amazon"},
            generated_at="2026-11-12T00:00:00Z",
        )
        self.assertEqual([p["id"] for p in de_novo["historico"]], [oid])
        self.assertEqual(len(de_novo["historico"]), 1)

    def test_historico_sem_oferta_nao_grava_id_do_titulo(self) -> None:
        titulo = "Monster Energy Pipeline Punch pack 6 latas 473ml"
        anterior = {
            "schema_version": 1,
            "generated_at": QUANDO,
            "ofertas": [],
            "historico": [
                {
                    "loja": "shopee",
                    "sabor": "Pipeline Punch",
                    "nome": titulo,
                    "pack_qtd": 6,
                    "preco_lata": 7.98,
                    "visto_em": QUANDO,
                }
            ],
        }
        feed = build_ofertas(
            [],
            previous=anterior,
            sites_ok=set(),
            generated_at=DEPOIS,
        )
        self.assertEqual(len(feed["historico"]), 1)
        self.assertNotIn("id", feed["historico"][0])
        self.assertEqual(feed["historico"][0]["preco_lata"], 7.98)
        self.assertEqual(feed["historico"][0]["visto_em"], QUANDO)

        oid = f"shopee::{titulo} [9988]"
        voltou = build_ofertas(
            [
                observacao_from_item(
                    "shopee",
                    {
                        "produto": f"{titulo} [9988]",
                        "titulo": titulo,
                        "qtd": 6,
                        "preco_pack": 47.88,
                        "preco_lata": 7.98,
                        "preco": 7.98,
                        "frete": None,
                    },
                    preco_anterior=7.98,
                )
            ],
            previous=feed,
            sites_ok={"shopee"},
            generated_at="2026-11-12T00:00:00Z",
        )
        self.assertEqual([p["id"] for p in voltou["historico"]], [oid])
        self.assertEqual([p["preco_lata"] for p in voltou["historico"]], [7.98])
        self.assertEqual(voltou["historico"][0]["visto_em"], QUANDO)

    def test_historico_limita_60_por_id_e_nao_por_titulo(self) -> None:
        titulo = "Monster Energy Ultra Pack 6 latas 473ml"

        def pontos(asin: str) -> list[dict]:
            oid = f"amazon::{titulo} [{asin}]"
            return [
                {
                    "id": oid,
                    "loja": "amazon",
                    "sabor": "Ultra",
                    "nome": titulo,
                    "pack_qtd": 6,
                    "preco_lata": float(i),
                    "visto_em": f"2026-03-{(i // 24) + 1:02d}T{i % 24:02d}:00:00Z",
                }
                for i in range(HISTORICO_MAX_PONTOS)
            ]

        previous = {
            "schema_version": 1,
            "generated_at": QUANDO,
            "ofertas": [],
            "historico": pontos("B0AAA") + pontos("B0BBB"),
        }
        feed = build_ofertas(
            [self._obs_amazon(titulo, "B0AAA", 4.2), self._obs_amazon(titulo, "B0BBB", 4.2)],
            previous=previous,
            sites_ok={"amazon"},
            generated_at=DEPOIS,
        )
        self.assertEqual(len(feed["historico"]), HISTORICO_MAX_PONTOS * 2)
        self.assertEqual(validar_ofertas(feed), [])
        for asin in ("B0AAA", "B0BBB"):
            serie = [p for p in feed["historico"] if p["id"].endswith(f"[{asin}]")]
            self.assertEqual(len(serie), HISTORICO_MAX_PONTOS)
            self.assertEqual(serie[-1]["preco_lata"], 4.2)

        estourado = {
            "schema_version": 1,
            "generated_at": QUANDO,
            "ofertas": [],
            "historico": pontos("B0AAA")
            + [
                {
                    "id": f"amazon::{titulo} [B0AAA]",
                    "loja": "amazon",
                    "sabor": "Ultra",
                    "nome": titulo,
                    "pack_qtd": 6,
                    "preco_lata": 99.0,
                    "visto_em": "2026-04-01T00:00:00Z",
                }
            ],
        }
        erros = validar_ofertas(estourado)
        self.assertTrue(any("excede" in erro and "B0AAA" in erro for erro in erros))

    def test_workflow_versiona_os_dois_arquivos(self) -> None:
        texto = (REPO / ".github/workflows/price-monitor.yml").read_text(encoding="utf-8")
        self.assertIn("git add historico_precos.json", texto)
        self.assertIn("git add ofertas.json", texto)
        self.assertIn("ofertas.json [skip ci]", texto)
        self.assertIn("SHOPEE_AFFILIATE_APP_ID", texto)
        self.assertNotIn("parse_mode", texto)


def main() -> int:
    """Imprime um feed válido (histórico real + packs Amazon/Shopee). Não grava no repo."""
    feed = feed_de_exemplo()
    erros = validar_ofertas(feed)
    if erros:
        print("ofertas.json inválido:")
        for erro in erros:
            print(" -", erro)
        return 1
    vistos: set[str] = set()
    amostra = []
    for oferta in feed["ofertas"]:
        if oferta["loja"] in vistos:
            continue
        vistos.add(oferta["loja"])
        amostra.append(oferta)
    print(json.dumps(
        {
            "schema_version": feed["schema_version"],
            "generated_at": feed["generated_at"],
            "ofertas": len(feed["ofertas"]),
            "historico": len(feed["historico"]),
            "amostra": amostra,
        },
        ensure_ascii=False,
        indent=2,
    ))
    print(f"válido: {len(feed['ofertas'])} ofertas, {len(feed['historico'])} pontos")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        raise SystemExit(main())
    # `python tests/sample_ofertas.py` mostra a amostra e roda os testes.
    codigo_amostra = main()
    resultado = unittest.main(argv=[sys.argv[0]], exit=False, verbosity=2)
    raise SystemExit(codigo_amostra or (0 if resultado.result.wasSuccessful() else 1))
