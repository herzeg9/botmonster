"""Configuração de extração por supermercado (padrão Strategy).

Cada entrada em :data:`SITES_CONFIG` descreve **onde** buscar (URL com ``{query}``)
e **com** extrair os dados:

* ``tipo == "html"`` — listagem web; preenche os seletores CSS para o Playwright/DOM.
* ``tipo == "api"`` — endpoint REST; seletores ficam vazios; a orquestração deve
  usar um cliente HTTP e mapear o JSON no ``main``.

Para um novo canal, adicione uma chave com o mesmo contrato de
:class:`SiteListingStrategy`.
"""

from __future__ import annotations

from typing import Literal, TypedDict


SiteTipo = Literal["html", "api"]


class SiteListingStrategy(TypedDict):
    """Contrato de estratégia por site (HTML ou API)."""

    tipo: SiteTipo
    """``html`` = raspagem de página; ``api`` = consumo REST (sem seletores DOM)."""

    url_busca: str
    """URL com ``{query}`` (termo já codificado na montagem da URL, se necessário)."""

    card_selector: str
    """Seletor CSS do card; vazio quando ``tipo == "api"``."""

    name_selector: str
    """Seletor CSS do nome relativo ao card; vazio quando ``tipo == "api"``."""

    price_selector: str
    """Seletor CSS do preço; vazio quando ``tipo == "api"``."""


SITES_CONFIG: dict[str, SiteListingStrategy] = {
    "pao_de_acucar": {
        "tipo": "html",
        "url_busca": "https://www.paodeacucar.com/busca?w={query}",
        "card_selector": 'a[href*="/produto/"]:has(img[alt])',
        "name_selector": "img[alt]",
        "price_selector": 'p[class*="TextComponent-sc-"]',
    },
    "tauste": {
        "tipo": "html",
        "url_busca": "https://tauste.com.br/campinas1/catalogsearch/result/?q={query}",
        # Magento: grade em .search.results; nome completo em .product-item-name (não no link curto).
        "card_selector": ".search.results li.product-item",
        "name_selector": "strong.product-item-name",
        "price_selector": "span.price",
    },
    "sams_club": {
        "tipo": "html",
        "url_busca": "https://www.samsclub.com.br/{query}",
        # VTEX IO: grade em galleryItem; nome em productBrand (não h2/h3).
        "card_selector": ".vtex-search-result-3-x-galleryItem",
        "name_selector": "span.vtex-product-summary-2-x-productBrand",
        "price_selector": ".vtex-productShowCasePrice",
    },
}
