"""Fundação de web scraper para supermercado com Playwright assíncrono."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from bs4 import BeautifulSoup

from playwright.async_api import Browser
from playwright.async_api import ElementHandle
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import Playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

_POPUP_CHECK_TIMEOUT_MS = 3000

# Linhas que não são o preço venda do SKU (vitrine / mídia / tachado genérico).
_LISTING_LINE_SKIP = re.compile(
    r"a\s+partir\s+de|"
    r"desde\s+r\$|"
    r"\bpor\s+apenas\b|"
    r"frete|entrega|taxa\b|"
    r"clube\s|assinatura|cashback|cupom|"
    r"leve\s+\d+|"
    r"(^|\s)de\s+r\$",
    re.IGNORECASE,
)

# User-Agent e viewport reduzem sinais de automação e aproximam um desktop comum.
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled",)


class SupermarketScraper:
    """Scraper assíncrono com Chromium headless e carregamento via Playwright.

    Attributes:
        headless: Se True, executa o Chromium sem interface gráfica.
    """

    def __init__(self, headless: bool = True) -> None:
        """Inicializa a configuração do scraper para uso com Chromium headless.

        Args:
            headless: Quando True, o navegador é lançado em modo headless.
        """
        self.headless = headless
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None

    def _context_options(self) -> dict[str, Any]:
        """Opções de :meth:`Browser.new_context` (idioma, UA, viewport)."""
        return {
            "locale": "pt-BR",
            "user_agent": _DEFAULT_USER_AGENT,
            "viewport": {"width": 1365, "height": 900},
        }

    async def start(self) -> None:
        """Sobe o Playwright e lança o Chromium conforme `headless`.

        Raises:
            PlaywrightError: Se o lançamento do navegador falhar.
        """
        if self._browser is not None:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=list(_LAUNCH_ARGS),
        )

    async def close(self) -> None:
        """Encerra o navegador e o driver Playwright, liberando recursos."""
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _try_dismiss_popup(
        self,
        page: Page,
        popup_close_selector: Optional[str],
    ) -> None:
        """Clica no seletor de fechamento do pop-up, se visível (timeout curto)."""
        if not popup_close_selector:
            return
        try:
            popup = page.locator(popup_close_selector).first
            await popup.wait_for(state="visible", timeout=_POPUP_CHECK_TIMEOUT_MS)
            await popup.click(timeout=_POPUP_CHECK_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass
        except PlaywrightError:
            pass

    async def fetch_page(
        self,
        url: str,
        popup_close_selector: Optional[str] = None,
    ) -> str:
        """Navega até a URL e aguarda `networkidle` para conteúdo dinâmico.

        Args:
            url: Endereço absoluto da página a ser carregada.
            popup_close_selector: Seletor CSS opcional (ex.: botão de cookies ou
                fechar modal). Se o elemento ficar visível em até alguns segundos,
                recebe um clique antes de capturar o HTML; caso contrário a etapa
                é ignorada sem bloquear a execução.

        Returns:
            HTML completo da página após o carregamento.

        Raises:
            RuntimeError: Se `start` não tiver sido chamado antes.
            PlaywrightTimeoutError: Propagada após log, em caso de timeout.
            PlaywrightError: Propagada após log, em falhas de rede/navegação.
        """
        if self._browser is None:
            raise RuntimeError("Chame await start() antes de fetch_page().")

        context = await self._browser.new_context(**self._context_options())
        page = await context.new_page()

        try:
            try:
                await page.goto(url, wait_until="networkidle")
            except PlaywrightTimeoutError as exc:
                print(f"[SupermarketScraper] Timeout ao carregar a URL: {url!r}: {exc}")
                raise
            except PlaywrightError as exc:
                print(f"[SupermarketScraper] Falha de conexão ou navegação em {url!r}: {exc}")
                raise

            await self._try_dismiss_popup(page, popup_close_selector)

            return await page.content()
        finally:
            await page.close()
            await context.close()

    @staticmethod
    def _clean_price(price_str: Optional[str]) -> float:
        """Normaliza texto de preço (pt-BR) para ``float`` usando expressões regulares.

        Aceita formatos como ``R$ 8,90``, ``R$8.90`` ou ``1.234,56``. Strings vazias ou
        nulas resultam em ``0.0``.

        Args:
            price_str: Texto bruto do preço ou ``None``.

        Returns:
            Valor numérico; ``0.0`` se não houver dígitos válidos após a limpeza.
        """
        if price_str is None:
            return 0.0
        raw = str(price_str).strip()
        if not raw:
            return 0.0

        raw = re.sub(r"(?i)r\$\s*", "", raw).strip()
        digits = re.sub(r"[^\d,.]", "", raw)
        if not digits:
            return 0.0

        if "," in digits and "." in digits:
            if digits.rfind(",") > digits.rfind("."):
                digits = digits.replace(".", "").replace(",", ".")
            else:
                digits = digits.replace(",", "")
        elif "," in digits:
            digits = digits.replace(",", ".")

        try:
            return float(digits)
        except ValueError:
            return 0.0

    async def extract_product_data(
        self,
        url: str,
        price_selector: str,
        name_selector: str,
    ) -> dict[str, Any]:
        """Carrega a página e extrai nome e preço via seletores CSS.

        Args:
            url: URL do produto.
            price_selector: Seletor CSS do elemento de preço.
            name_selector: Seletor CSS do nome do produto.

        Returns:
            Dicionário ``{'url', 'produto', 'preco'}``. ``preco`` é ``None`` se o
            seletor de preço não existir na página; caso contrário é o ``float``
            obtido com :meth:`_clean_price`.
        """
        html = await self.fetch_page(url)
        soup = BeautifulSoup(html, "html.parser")

        nome: Optional[str] = None
        preco_float: Optional[float] = None

        name_el = soup.select_one(name_selector)
        if name_el is None:
            logger.error(
                "Seletor de nome não encontrado: url=%r selector=%r",
                url,
                name_selector,
            )
        else:
            nome = name_el.get_text(strip=True) or None

        price_el = soup.select_one(price_selector)
        if price_el is None:
            logger.error(
                "Seletor de preço não encontrado: url=%r selector=%r",
                url,
                price_selector,
            )
            preco_float = None
        else:
            preco_float = self._clean_price(price_el.get_text(strip=True) or None)

        return {"url": url, "produto": nome, "preco": preco_float}

    async def _inner_text_or_alt(self, handle: ElementHandle) -> Optional[str]:
        """Retorna texto visível do nó ou, se vazio, o atributo ``alt`` (ex.: imagem de vitrine)."""
        try:
            text = (await handle.inner_text() or "").strip()
        except PlaywrightError:
            text = ""
        if text:
            return text
        alt = await handle.get_attribute("alt")
        return (alt or "").strip() or None

    async def _listing_sale_price_from_dom(self, card: ElementHandle) -> Optional[float]:
        """Lê preços no container do produto e ignora nós com tachado (preço antigo).

        Em promoções o site mostra valor anterior riscado e o atual; o valor atual costuma
        ser o **menor** entre os dois, mas a ordem no ``innerText`` varia. Aqui usamos
        ``getComputedStyle`` para excluir tachado e, entre candidatos válidos, o **mínimo**.
        """
        raw = await card.evaluate(
            r"""anchor => {
                const hasPrice = t => /R\$\s*\d/.test(t || '');
                const hrefPath = a => {
                    try {
                        const u = new URL(a.getAttribute('href'), location.origin);
                        return u.pathname.replace(/\/+$/, '');
                    } catch (e) {
                        const h = (a.getAttribute('href') || '').split('?')[0];
                        return h.replace(/\/+$/, '');
                    }
                };
                const struck = el => {
                    let n = el;
                    for (let i = 0; i < 8 && n && n !== document.body; i++) {
                        const st = window.getComputedStyle(n);
                        const line = st.textDecorationLine || '';
                        const dec = (st.textDecoration || '').toLowerCase();
                        if (line.includes('line-through') || dec.includes('line-through')) {
                            return true;
                        }
                        const tag = (n.tagName || '').toLowerCase();
                        if (tag === 'del' || tag === 's' || tag === 'strike') return true;
                        n = n.parentElement;
                    }
                    return false;
                };
                const parseBrlFromText = t => {
                    const m = (t || '').match(
                        /R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})/
                    );
                    if (!m) return null;
                    const x = m[1].replace(/\./g, '').replace(',', '.');
                    const v = parseFloat(x);
                    return (isNaN(v) || v <= 0) ? null : v;
                };

                let root = null;
                let n = anchor.parentElement;
                for (let i = 0; i < 16 && n; i++) {
                    const links = Array.from(n.querySelectorAll('a[href*="/produto/"]'));
                    const paths = new Set(links.map(hrefPath));
                    const t = (n.innerText || '').trim();
                    if (paths.size === 1 && hasPrice(t)) {
                        root = n;
                        break;
                    }
                    n = n.parentElement;
                }
                if (!root) {
                    n = anchor;
                    for (let j = 0; j < 14 && n; j++) {
                        if (hasPrice((n.innerText || '').trim())) {
                            root = n;
                            break;
                        }
                        n = n.parentElement;
                    }
                }
                if (!root) return null;

                const candidates = [];
                for (const node of root.querySelectorAll(
                    'span, p, div, strong, b, em, label, h2, h3, h4, a'
                )) {
                    if (struck(node)) continue;
                    const t = ((node.textContent || '') + '').trim().replace(/\s+/g, ' ');
                    if (t.length < 4 || t.length > 48) continue;
                    if (!/^R\$\s*[\d.,]+$/.test(t)) continue;
                    const v = parseBrlFromText(t);
                    if (v !== null && v < 100000) candidates.push(v);
                }
                if (candidates.length === 0) return null;
                return Math.min.apply(null, candidates);
            }""",
        )
        if raw is None:
            return None
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    async def _heuristic_price_text_from_card(self, card: ElementHandle) -> Optional[str]:
        """Legado: busca ``R$`` só em ``p/span/div`` dentro do próprio nó do card."""
        snippet = await card.evaluate(
            r"""el => {
                const re = /R\$\s*\d/;
                const nodes = el.querySelectorAll('p, span, div, strong, b, small, h2, h3, h4, label');
                for (const n of nodes) {
                    const t = (n.innerText || '').trim();
                    if (re.test(t)) {
                        const line = t.split('\n')[0].trim();
                        return line || null;
                    }
                }
                return null;
            }""",
        )
        if snippet is None:
            return None
        s = str(snippet).strip()
        return s or None

    async def _listing_block_text_with_price(self, card: ElementHandle) -> str:
        """Sobe na árvore e devolve o texto do bloco que deve conter o preço do item.

        Evita o falso positivo em que o primeiro ancestral com ``R$`` é uma vitrine
        inteira (“A partir de R$ 8,49”) repetida para todos os produtos.
        Prioriza ancestrais onde há apenas **um** caminho de produto (um SKU).
        """
        raw = await card.evaluate(
            r"""el => {
                const hasPrice = t => /R\$\s*\d/.test(t || '');
                const hrefPath = a => {
                    try {
                        const u = new URL(a.getAttribute('href'), location.origin);
                        return u.pathname.replace(/\/+$/, '');
                    } catch (e) {
                        const h = (a.getAttribute('href') || '').split('?')[0];
                        return h.replace(/\/+$/, '');
                    }
                };
                let n = el.parentElement;
                for (let i = 0; i < 16 && n; i++) {
                    const links = Array.from(n.querySelectorAll('a[href*="/produto/"]'));
                    const paths = new Set(links.map(hrefPath));
                    const t = (n.innerText || '').trim();
                    if (paths.size === 1 && hasPrice(t)) return t;
                    n = n.parentElement;
                }
                n = el;
                for (let i = 0; i < 14 && n; i++) {
                    const t = (n.innerText || '').trim();
                    if (hasPrice(t)) return t;
                    n = n.parentElement;
                }
                return (el.innerText || '').trim();
            }""",
        )
        return str(raw or "").strip()

    def _first_price_float_from_listing_text(self, block: str) -> Optional[float]:
        """Extrai o preço de venda a partir do texto do item na listagem."""
        if not block:
            return None
        candidates: list[float] = []
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            if _LISTING_LINE_SKIP.search(line):
                continue
            if re.search(r'\d+\s*x\s+de\s+r\$', line, re.IGNORECASE):
                continue
            if re.search(r'r\$\s*\d', line, re.IGNORECASE):
                val = self._clean_price(line)
                if val > 0:
                    candidates.append(val)
        if candidates:
            # Com desconto há dois valores; o promocional costuma ser o menor (e linhas "De R$" já foram filtradas).
            return min(candidates)
        snippets = [
            m.group(0)
            for m in re.finditer(r'R\$\s*[\d.,]+', block, flags=re.IGNORECASE)
        ]
        values = [self._clean_price(s) for s in snippets]
        values = [v for v in values if v > 0]
        return min(values) if values else None

    async def _extract_listing_price_float(
        self, card: ElementHandle, price_selector: str
    ) -> Optional[float]:
        """Preço na grade: DOM (ignora tachado), texto agregado, seletor CSS e heurística."""
        try:
            await card.scroll_into_view_if_needed()
        except PlaywrightError:
            pass
        await asyncio.sleep(0.15)

        dom_price = await self._listing_sale_price_from_dom(card)
        if dom_price is not None:
            return dom_price

        block = await self._listing_block_text_with_price(card)
        found = self._first_price_float_from_listing_text(block)
        if found is not None:
            return found

        price_el = await card.query_selector(price_selector)
        if price_el is not None:
            raw = (await price_el.inner_text() or "").strip()
            if raw:
                cleaned = self._clean_price(raw)
                if cleaned > 0:
                    return cleaned

        legacy = await self._heuristic_price_text_from_card(card)
        if legacy:
            cleaned = self._clean_price(legacy)
            return cleaned if cleaned > 0 else None
        return None

    async def extract_search_results(
        self,
        url: str,
        card_selector: str,
        name_selector: str,
        price_selector: str,
        *,
        popup_close_selector: Optional[str] = None,
        navigation_wait: str = "load",
        post_load_wait_ms: int = 2500,
        card_wait_timeout_ms: int = 60_000,
    ) -> list[dict[str, Any]]:
        """Extrai nome e preço de cada card na página de busca/listagem.

        Usa ``navigation_wait`` (por padrão ``load`` em vez de ``networkidle``) para
        SPAs onde a rede “fica ociosa” antes do React pintar a grade. Em seguida
        aguarda o ``card_selector`` aparecer, espera um curto período extra e só
        então executa ``query_selector_all``.

        Args:
            url: URL da busca ou vitrine.
            card_selector: CSS que identifica cada item (elemento repetido na grade).
            name_selector: CSS relativo ao card para o nome (ou ``img[alt]``, etc.).
            price_selector: CSS relativo ao card para o texto do preço.
            popup_close_selector: Opcional; mesmo comportamento de :meth:`fetch_page`.
            navigation_wait: Valor de ``wait_until`` em :meth:`Page.goto`
                (ex.: ``load``, ``domcontentloaded``, ``networkidle``).
            post_load_wait_ms: Pausa após o card existir no DOM para hidratação/pintura.
            card_wait_timeout_ms: Tempo máximo para o primeiro card aparecer.

        Returns:
            Lista de ``{'produto': str | None, 'preco': float | None}``.
        """
        if self._browser is None:
            raise RuntimeError("Chame await start() antes de extract_search_results().")

        context = await self._browser.new_context(**self._context_options())
        page = await context.new_page()
        results: list[dict[str, Any]] = []
        seen_hrefs: set[str] = set()

        try:
            try:
                await page.goto(url, wait_until=navigation_wait, timeout=120_000)
            except PlaywrightTimeoutError as exc:
                print(f"[SupermarketScraper] Timeout ao carregar a URL: {url!r}: {exc}")
                raise
            except PlaywrightError as exc:
                print(f"[SupermarketScraper] Falha de conexão ou navegação em {url!r}: {exc}")
                raise

            await self._try_dismiss_popup(page, popup_close_selector)

            try:
                await page.wait_for_selector(
                    card_selector,
                    state="attached",
                    timeout=card_wait_timeout_ms,
                )
            except PlaywrightTimeoutError:
                logger.error(
                    "Nenhum card encontrado com o seletor após %sms — possíveis causas: "
                    "classes CSS mudaram (hash), página ainda não renderizou (SPA), "
                    "modal/captcha ou seletor incorreto. url=%r selector=%r",
                    card_wait_timeout_ms,
                    url,
                    card_selector,
                )
                return results

            await page.wait_for_timeout(post_load_wait_ms)

            cards = await page.query_selector_all(card_selector)
            if not cards:
                logger.error(
                    "wait_for_selector passou mas query_selector_all voltou 0 nós — url=%r",
                    url,
                )
                return results

            for card in cards:
                href = await card.get_attribute("href")
                if href:
                    norm = href.split("?", 1)[0].rstrip("/")
                    if norm in seen_hrefs:
                        continue
                    seen_hrefs.add(norm)

                nome: Optional[str] = None
                preco_float: Optional[float] = None

                name_el = await card.query_selector(name_selector)
                if name_el is not None:
                    nome = await self._inner_text_or_alt(name_el)
                else:
                    tag = await card.evaluate("el => el.tagName")
                    if tag and str(tag).upper() == "IMG":
                        nome = (await card.get_attribute("alt") or "").strip() or None
                    if nome is None:
                        logger.warning(
                            "Nome não encontrado no card (seletor %r)",
                            name_selector,
                        )

                preco_float = await self._extract_listing_price_float(card, price_selector)

                if preco_float is None:
                    logger.warning(
                        "Preço não encontrado no card (seletor %r; ancestrais e heurística falharam)",
                        price_selector,
                    )

                results.append({"produto": nome, "preco": preco_float})

        finally:
            await page.close()
            await context.close()

        return results

    async def __aenter__(self) -> "SupermarketScraper":
        """Entrada do context manager assíncrono: inicia o navegador."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Saída do context manager: sempre encerra o navegador (equivalente a ``finally``)."""
        await self.close()


async def _demo() -> None:
    """Exemplo com ``try``/``finally`` garantindo ``close()`` mesmo após erro."""
    scraper = SupermarketScraper(headless=True)
    try:
        await scraper.start()
        html = await scraper.fetch_page("https://example.com")
        print(f"Bytes de HTML (aprox.): {len(html)}")
    finally:
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(_demo())
