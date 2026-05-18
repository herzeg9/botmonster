"""Execução do scraper — monitor multi-sites (Strategy via ``SITES_CONFIG``)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from config_sites import SITES_CONFIG
from scraper_supermercado import SupermarketScraper
from telegram_notifier import send_telegram_alert

_ENV_PATH = Path(__file__).resolve().parent / ".env"

# Termo passado ao placeholder ``{query}`` em ``url_busca``.
TERMO_BUSCA = "energetico monster"

# Ajuste se um banner de cookies/modal bloquear a grade (inspecione o botão no site).
POPUP_CLOSE_SELECTOR = None  # ex.: 'button:has-text("Aceitar")'

# Threshold provisório X (BRL): nome com "Ultra" e preço <= X disparam o alerta.
PRICE_THRESHOLD_ULTRA = 8.50

# User-Agent de navegador real — o WAF do ML bloqueia o UA padrão do requests/aiohttp.
_API_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# A API pública do Mercado Livre costuma responder 403 sem cabeçalhos de cliente real (Akamai).
_ML_API_HEADERS = {
    "User-Agent": _API_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.mercadolivre.com.br/",
    "Origin": "https://www.mercadolivre.com.br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}


def _read_telegram_from_environ() -> tuple[str, str]:
    """Lê token e chat só de ``os.environ`` (sem tocar no ``.env``)."""
    token = (
        (os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "")
        .strip()
    )
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat_id


def load_telegram_credentials() -> tuple[str, str]:
    """Obtém credenciais do Telegram.

    Ordem: variáveis já presentes no ambiente (ex.: GitHub Actions); se faltar
    token ou chat, carrega ``.env`` no diretório do projeto e lê novamente.
    ``TELEGRAM_BOT_TOKEN`` continua aceito como alias de ``TELEGRAM_TOKEN`` para
    compatibilidade com arquivos ``.env`` antigos.
    """
    token, chat_id = _read_telegram_from_environ()
    if token and chat_id:
        return token, chat_id
    load_dotenv(_ENV_PATH)
    return _read_telegram_from_environ()


def load_history(filepath: str) -> dict:
    """Carrega o histórico de preços de um JSON ou retorna dicionário vazio."""
    try:
        with open(filepath, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_history(filepath: str, data: dict) -> None:
    """Persiste o histórico atualizado em disco com indentação legível."""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def _historico_chave(site_id: str, nome_produto: str) -> str:
    """Chave única por site + nome de produto (evita colisão multi-sites)."""
    return f"{site_id}::{nome_produto}"


def _html_url_query_placeholder(site_id: str, termo: str) -> str:
    """Valor de ``{query}`` em ``url_busca`` para rotas HTML por site.

    * ``tauste`` — parâmetro ``q`` típico de Magento: ``+`` entre termos (:func:`quote_plus`).
    * ``sams_club``, ``pao_de_acucar`` e demais — espaços como ``%20``.
    """
    if site_id == "tauste":
        return urllib.parse.quote_plus(termo)
    return termo.replace(" ", "%20")


def _build_alert_message(produto: str, preco: float, site_id: str) -> str:
    """Monta o texto do alerta em Markdown, incluindo o identificador do site."""
    return (
        "⚠️ ALERTA DE PREÇO BAÍXO ⚠️\n\n"
        f"Site: {site_id}\n"
        f"Produto: {produto}\n"
        f"*Preço encontrado:* R$ {preco:.2f}\n"
        f"_Monster Ultra · limiar ≤ R$ {PRICE_THRESHOLD_ULTRA:.2f}_"
    )


def _parse_mercado_livre_search_payload(data: Any) -> list[dict[str, Any]]:
    """Normaliza o JSON de ``/sites/MLB/search`` na lista ``produto`` / ``preco``."""
    itens: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return itens
    raw_results = data.get("results")
    if not isinstance(raw_results, list):
        return itens
    for row in raw_results:
        if not isinstance(row, dict):
            continue
        title = row.get("title")
        price = row.get("price")
        if title is None or price is None:
            continue
        nome = str(title).strip()
        if not nome:
            continue
        try:
            preco_float = float(price)
        except (TypeError, ValueError):
            continue
        itens.append({"produto": nome, "preco": preco_float})
    return itens


def _ml_search_http_get_json(url: str) -> Any:
    """GET síncrono para a Search API (executado em thread pelo asyncio)."""
    r = requests.get(url, headers=_ML_API_HEADERS, timeout=60)
    r.raise_for_status()
    return r.json()


async def fetch_search_results_api(url: str) -> list[dict[str, Any]]:
    """Busca listagem via API REST (ex.: Mercado Livre Search).

    Usa ``requests`` em ``asyncio.to_thread`` porque a CDN do ML muitas vezes devolve
    **403** para clientes ``aiohttp`` com os mesmos cabeçalhos.

    Espera JSON com ``results`` (itens com ``title`` e ``price``), retornando
    ``[{'produto': str, 'preco': float}, ...]``.
    """
    data = await asyncio.to_thread(_ml_search_http_get_json, url)
    return _parse_mercado_livre_search_payload(data)


async def main(historico: dict, arquivo_historico: str) -> None:
    """Varre cada site em ``SITES_CONFIG``, aplica filtros e persiste o histórico."""
    telegram_token, telegram_chat_id = load_telegram_credentials()

    precisa_browser = any(cfg["tipo"] == "html" for cfg in SITES_CONFIG.values())
    scraper: Optional[SupermarketScraper] = (
        SupermarketScraper(headless=True) if precisa_browser else None
    )

    try:
        if scraper is not None:
            await scraper.start()

        for site_id, config in SITES_CONFIG.items():
            if config["tipo"] == "html":
                url_alvo = config["url_busca"].format(
                    query=_html_url_query_placeholder(site_id, TERMO_BUSCA),
                )
                try:
                    assert scraper is not None
                    itens = await scraper.extract_search_results(
                        url_alvo,
                        config["card_selector"],
                        config["name_selector"],
                        config["price_selector"],
                        popup_close_selector=POPUP_CLOSE_SELECTOR,
                        navigation_wait="load",
                        post_load_wait_ms=3000,
                        card_wait_timeout_ms=90_000,
                    )
                except Exception:
                    logging.exception(
                        "Falha ao extrair listagem HTML — site=%r url=%r",
                        site_id,
                        url_alvo,
                    )
                    continue

            elif config["tipo"] == "api":
                url_alvo = config["url_busca"].format(
                    query=urllib.parse.quote(TERMO_BUSCA, safe=""),
                )
                try:
                    itens = await fetch_search_results_api(url_alvo)
                except Exception:
                    logging.exception(
                        "Falha ao extrair listagem API — site=%r url=%r",
                        site_id,
                        url_alvo,
                    )
                    continue

            else:
                logging.warning("Tipo de estratégia desconhecido: %r", config.get("tipo"))
                continue

            print({"site": site_id, "url": url_alvo, "itens": itens})

            if not itens:
                logging.warning(
                    "Lista vazia — site=%r url=%r (sem resultados ou formato inesperado).",
                    site_id,
                    url_alvo,
                )
                continue

            for item in itens:
                nome = item.get("produto")
                preco = item.get("preco")
                if nome is None or preco is None:
                    continue
                if "ultra" not in nome.lower() or preco > PRICE_THRESHOLD_ULTRA:
                    continue

                chave = _historico_chave(site_id, nome)
                preco_salvo = historico.get(chave)
                if chave not in historico or preco_salvo != preco:
                    mensagem = _build_alert_message(nome, preco, site_id)
                    if not telegram_token or not telegram_chat_id:
                        logging.warning(
                            "Gatilho acionado mas TELEGRAM_TOKEN (ou TELEGRAM_BOT_TOKEN) ou "
                            "TELEGRAM_CHAT_ID não estão definidos — configure o ambiente ou o "
                            "arquivo .env (veja .env.example)."
                        )
                        continue
                    await send_telegram_alert(telegram_token, telegram_chat_id, mensagem)
                    historico[chave] = preco

        save_history(arquivo_historico, historico)
    finally:
        if scraper is not None:
            await scraper.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ARQUIVO_HISTORICO = "historico_precos.json"
    historico = load_history(ARQUIVO_HISTORICO)
    asyncio.run(main(historico, ARQUIVO_HISTORICO))
