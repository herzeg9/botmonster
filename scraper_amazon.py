"""Estratégia Amazon Brasil — packs de 6 latas de Monster.

Busca pública (sem login) em ``amazon.com.br/s?k=...`` via Playwright e devolve,
para cada pack de **6 unidades**: preço do pack, frete (quando visível), preço
por lata, selo Prime e link. Captcha/bloqueio é detectado, logado e a
estratégia retorna lista vazia (não derruba o monitor).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

AMAZON_BASE = "https://www.amazon.com.br"
PACK_ALVO = 6

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HEADERS = {
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

_CAPTCHA_RE = re.compile(
    r"validateCaptcha|captchacharacters|Digite os caracteres|"
    r"Type the characters you see|api-services-support@amazon|Robot Check",
    re.IGNORECASE,
)

# Padrões de quantidade no título (ordem: mais específico primeiro).
_QTD_PATTERNS = [
    re.compile(r"(\d{1,2})\s*[xX×]\s*\d{2,4}\s*ml", re.I),            # 6x473ml
    re.compile(r"\b(?:pack|kit|caixa|fardo|c/|com)\s*(?:com|de|c/)?\s*(\d{1,2})\b", re.I),  # pack com 6
    re.compile(r"\b(\d{1,2})\s*(?:unidades|unid\.?|un\.?|latas)\b", re.I),  # 6 unidades / 6 latas
]


def parse_pack_qtd(titulo: str) -> Optional[int]:
    """Extrai a quantidade de latas do título (``None`` se não identificada)."""
    for pat in _QTD_PATTERNS:
        m = pat.search(titulo)
        if m:
            n = int(m.group(1))
            if 1 <= n <= 48:
                return n
    return None


def parse_brl(texto: str) -> Optional[float]:
    """``"R$ 1.062,94"`` -> ``1062.94``."""
    m = re.search(r"R\$\s*([\d.]+,\d{2})", texto or "")
    if not m:
        return None
    return float(m.group(1).replace(".", "").replace(",", "."))


def parse_frete(texto: str) -> tuple[Optional[float], str]:
    """Interpreta o bloco de entrega do card.

    Retorna ``(frete, observação)``. ``frete`` é ``0.0`` para entrega grátis
    incondicional, valor numérico se houver ``R$ X ... frete/entrega`` e ``None``
    quando desconhecido (inclui "grátis no seu primeiro pedido", que não vale
    para quem já comprou).
    """
    t = " ".join((texto or "").split())
    if not t:
        return None, "frete não informado na busca"
    if re.search(r"gr[aá]tis", t, re.I):
        if re.search(r"primeiro pedido", t, re.I):
            return None, "frete grátis só no 1º pedido"
        if re.search(r"acima de|pedidos? (?:de|a partir)", t, re.I):
            return None, "frete grátis condicionado a valor mínimo"
        return 0.0, "frete grátis"
    m = re.search(r"R\$\s*[\d.]+,\d{2}[^.]{0,25}?(?:frete|entrega)", t, re.I) or re.search(
        r"(?:frete|entrega)[^R]{0,25}R\$\s*[\d.]+,\d{2}", t, re.I
    )
    if m:
        return parse_brl(m.group(0)), "frete visível na busca"
    return None, "frete não informado na busca"


def is_captcha(html: str) -> bool:
    return bool(_CAPTCHA_RE.search(html or ""))


def parse_search_html(html: str, *, pack_alvo: int = PACK_ALVO) -> list[dict[str, Any]]:
    """Converte o HTML da busca em itens de packs Monster com ``pack_alvo`` latas."""
    soup = BeautifulSoup(html, "html.parser")
    itens: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for card in soup.select('div[data-component-type="s-search-result"]'):
        asin = (card.get("data-asin") or "").strip()
        h2 = card.select_one("h2")
        titulo = h2.get_text(" ", strip=True) if h2 else ""
        if not asin or asin in vistos or "monster" not in titulo.lower():
            continue
        qtd = parse_pack_qtd(titulo)
        if qtd != pack_alvo:
            continue
        preco_el = card.select_one(".a-price:not(.a-text-price) .a-offscreen")
        preco = parse_brl(preco_el.get_text(strip=True)) if preco_el else None
        if preco is None:
            continue  # indisponível / sem oferta na listagem
        entrega = card.select_one('[data-cy="delivery-recipe"]')
        frete, frete_obs = parse_frete(entrega.get_text(" ", strip=True) if entrega else "")
        prime = bool(
            card.select_one("i.a-icon-prime, [aria-label*='Prime' i]")
        )
        if prime and frete is None and "1º pedido" not in frete_obs:
            frete, frete_obs = 0.0, "Prime (frete grátis)"
        patrocinado = "Patrocinado" in card.get_text(" ", strip=True)[:200]
        total = preco + (frete or 0.0)
        vistos.add(asin)
        itens.append(
            {
                "asin": asin,
                "produto": f"{titulo} [{asin}]",
                "titulo": titulo,
                "qtd": qtd,
                "preco_pack": round(preco, 2),
                "frete": frete,
                "frete_obs": frete_obs,
                "preco_lata": round(total / qtd, 2),
                "preco": round(total / qtd, 2),  # valor usado no histórico/alerta
                "prime": prime,
                "patrocinado": patrocinado,
                "link": f"{AMAZON_BASE}/dp/{asin}",
            }
        )
    return itens


async def fetch_amazon_packs(browser: Any, url: str) -> list[dict[str, Any]]:
    """Abre a busca com o ``Browser`` Playwright já iniciado e devolve os packs.

    Nunca levanta exceção por captcha: loga e retorna ``[]``.
    """
    context = await browser.new_context(
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        user_agent=_UA,
        viewport={"width": 1365, "height": 900},
        extra_http_headers=_HEADERS,
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    page = await context.new_page()
    page.set_default_timeout(20_000)
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        status = resp.status if resp else None
        try:
            await page.wait_for_selector(
                'div[data-component-type="s-search-result"]', timeout=15_000
            )
        except Exception:
            pass
        html = await page.content()
        if is_captcha(html):
            logger.warning("Amazon: captcha/robot check detectado — pulando. url=%r", url)
            return []
        if status and status >= 400:
            logger.warning("Amazon: HTTP %s — pulando. url=%r", status, url)
            return []
        return parse_search_html(html)
    finally:
        try:
            await asyncio.wait_for(context.close(), 15)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Amazon: falha/timeout ao fechar contexto: %r", exc)
