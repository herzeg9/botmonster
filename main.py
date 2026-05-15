"""Execução do scraper — vitrine de busca Pão de Açúcar e alertas no Telegram."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from scraper_supermercado import SupermarketScraper
from telegram_notifier import send_telegram_alert

_ENV_PATH = Path(__file__).resolve().parent / ".env"

SEARCH_URL = "https://www.paodeacucar.com/especial/monster_1"

# Evite classes com hash (Image-sc-XXXX): use URL estável de produto + imagem com alt.
# ``:has()`` requer Chromium recente (Playwright já usa).
CARD_SELECTOR = 'a[href*="/produto/"]:has(img[alt])'
NAME_SELECTOR = "img[alt]"
# Mantido para quando a classe ainda bater; se não, o scraper usa heurística R$ no card.
PRICE_SELECTOR = 'p[class*="TextComponent-sc-"]'

# Ajuste se um banner de cookies/modal bloquear a grade (inspecione o botão no site).
POPUP_CLOSE_SELECTOR = None  # ex.: 'button:has-text("Aceitar")'

# Threshold provisório X (BRL): nome com "Ultra" e preço <= X disparam o alerta.
PRICE_THRESHOLD_ULTRA = 8.50


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


def _build_alert_message(produto: str, preco: float) -> str:
    """Monta o texto do alerta em Markdown.

    A **primeira linha** é o nome completo do produto em texto puro: a pré-visualização
    do Telegram no celular costuma mostrar só o início da mensagem; antes o título
    genérico "Monster Ultra" fazia todas as notificações parecerem iguais.
    """
    return (
        f"{produto}\n\n"
        f"*Preço encontrado:* R$ {preco:.2f}\n"
        f"_Monster Ultra · limiar ≤ R$ {PRICE_THRESHOLD_ULTRA:.2f}_"
    )


async def main(historico: dict, arquivo_historico: str) -> None:
    """Executa a extração da listagem, imprime os itens e dispara Telegram se necessário."""
    telegram_token, telegram_chat_id = load_telegram_credentials()

    scraper = SupermarketScraper(headless=True)
    try:
        await scraper.start()
        itens = await scraper.extract_search_results(
            SEARCH_URL,
            CARD_SELECTOR,
            NAME_SELECTOR,
            PRICE_SELECTOR,
            popup_close_selector=POPUP_CLOSE_SELECTOR,
            navigation_wait="load",
            post_load_wait_ms=3000,
            card_wait_timeout_ms=90_000,
        )
        print(itens)

        if not itens:
            logging.error(
                "Lista vazia — confira no navegador (headless=False) se há captcha, "
                "cookie bar ou se o seletor do card mudou."
            )
            return

        for item in itens:
            nome = item.get("produto")
            preco = item.get("preco")
            if nome is None or preco is None:
                continue
            if "ultra" not in nome.lower() or preco > PRICE_THRESHOLD_ULTRA:
                continue

            preco_salvo = historico.get(nome)
            if nome not in historico or preco_salvo != preco:
                mensagem = _build_alert_message(nome, preco)
                if not telegram_token or not telegram_chat_id:
                    logging.warning(
                        "Gatilho acionado mas TELEGRAM_TOKEN (ou TELEGRAM_BOT_TOKEN) ou "
                        "TELEGRAM_CHAT_ID não estão definidos — configure o ambiente ou o "
                        "arquivo .env (veja .env.example)."
                    )
                    continue
                await send_telegram_alert(telegram_token, telegram_chat_id, mensagem)
                historico[nome] = preco

        save_history(arquivo_historico, historico)
    finally:
        await scraper.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ARQUIVO_HISTORICO = "historico_precos.json"
    historico = load_history(ARQUIVO_HISTORICO)
    asyncio.run(main(historico, ARQUIVO_HISTORICO))
