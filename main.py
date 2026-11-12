"""Execução do scraper — monitor multi-sites (Strategy via ``SITES_CONFIG``)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from config_sites import SITES_CONFIG
from ofertas_feed import (
    build_ofertas,
    iso_now,
    load_ofertas,
    observacao_from_item,
    offer_id,
    save_ofertas,
)
from scraper_amazon import fetch_amazon_packs
from scraper_mercado_livre import (
    TERMO_BUSCA_MERCADO_LIVRE,
    fetch_mercado_livre_ofertas,
)
from scraper_shopee import fetch_shopee_packs
from scraper_supermercado import SupermarketScraper
from telegram_notifier import escape_html, send_telegram_alert

_ENV_PATH = Path(__file__).resolve().parent / ".env"

# Termo passado ao placeholder ``{query}`` em ``url_busca``.
TERMO_BUSCA = "energetico monster"

# Amazon: foco em packs de 6 latas (preço comparado por lata).
TERMO_BUSCA_AMAZON = "monster energy pack 6 latas 473ml"
# Shopee: foco em packs de 6 latas (preço comparado por lata).
TERMO_BUSCA_SHOPEE = "monster energy pack 6 latas"
# Mercado Livre: catálogo MLB, só lata de 473 ml (o pack sai do título).

# Ajuste se um banner de cookies/modal bloquear a grade (inspecione o botão no site).
POPUP_CLOSE_SELECTOR = None  # ex.: 'button:has-text("Aceitar")'

# Tempo máximo (s) por site: um site lento/travado nunca segura a execução toda.
SITE_TIMEOUT_S = 180

# Limiar de alerta (BRL): Telegram só quando o preço **mudou** e é <= este valor.
MONSTER_ALERT_MAX_PRICE = 8.50

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


def _normalize_preco(val: Any) -> float:
    """Arredonda para 2 casas — evita falso positivo em comparação float."""
    return round(float(val), 2)


def _historico_chave(site_id: str, nome_produto: str) -> str:
    """Chave única por site + nome de produto (evita colisão multi-sites)."""
    return offer_id(site_id, nome_produto)


def normalize_history(historico: Any) -> dict[str, float]:
    """Valida chaves ``site_id::produto``, migra formato legado e normaliza preços."""
    if not isinstance(historico, dict):
        logging.warning("historico_precos.json inválido; iniciando vazio.")
        return {}

    valid_sites = set(SITES_CONFIG)
    out: dict[str, float] = {}

    for chave, valor in historico.items():
        if not isinstance(chave, str):
            continue

        if "::" not in chave:
            chave = _historico_chave("pao_de_acucar", chave)
            logging.info("Migrando chave legada do histórico para %r", chave)

        site_id, sep, nome = chave.partition("::")
        if not sep or not site_id or not nome.strip():
            logging.warning("Chave de histórico ignorada: %r", chave)
            continue

        if site_id not in valid_sites:
            logging.warning(
                "Site %r não está em SITES_CONFIG; entrada ignorada: %r",
                site_id,
                chave,
            )
            continue

        try:
            out[_historico_chave(site_id, nome)] = _normalize_preco(valor)
        except (TypeError, ValueError):
            logging.warning("Preço inválido no histórico para %r: %r", chave, valor)

    return out


def load_history(filepath: str) -> dict[str, float]:
    """Carrega o histórico de preços de um JSON ou retorna dicionário vazio."""
    try:
        with open(filepath, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    return normalize_history(raw)


def save_history(filepath: str, data: dict) -> None:
    """Persiste o histórico normalizado, ordenado por chave."""
    ordenado = dict(sorted(normalize_history(data).items(), key=lambda item: item[0]))
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(ordenado, f, indent=4, ensure_ascii=False)
        f.write("\n")


def _html_url_query_placeholder(site_id: str, termo: str) -> str:
    """Valor de ``{query}`` em ``url_busca`` para rotas HTML por site.

    * ``tauste`` — parâmetro ``q`` típico de Magento: ``+`` entre termos (:func:`quote_plus`).
    * ``sams_club``, ``pao_de_acucar`` e demais — espaços como ``%20``.
    """
    if site_id == "tauste":
        return urllib.parse.quote_plus(termo)
    return termo.replace(" ", "%20")


def _produto_elegivel(nome: str) -> bool:
    """True se o nome do produto contém ``monster`` (sem diferenciar maiúsculas)."""
    return "monster" in nome.lower()


def _build_alert_message(
    produto: str, preco: float, site_id: str, extra: Optional[dict[str, Any]] = None
) -> str:
    """Monta o alerta em HTML do Telegram.

    Título e link entram escapados. Um ``_`` em nome de produto ou em
    ``offerLink`` da Shopee não pode derrubar o ``sendMessage`` (o modo
    Markdown antigo tratava ``_`` como itálico e a API recusava a mensagem).
    """
    if extra and "preco_pack" in extra:
        frete = extra.get("frete")
        frete_txt = (
            "grátis" if frete == 0 else
            (f"R$ {frete:.2f}" if frete is not None else "desconhecido")
        )
        prime_txt = (
            f"Prime: {'sim' if extra.get('prime') else 'não'}\n"
            if site_id == "amazon"
            else ""
        )
        return (
            "⚠️ ALERTA DE PREÇO BAÍXO ⚠️\n\n"
            f"Site: {escape_html(site_id)}\n"
            f"Produto: {escape_html(extra.get('titulo', produto))}\n"
            f"Pack {escape_html(extra.get('qtd'))} latas: R$ {extra['preco_pack']:.2f}\n"
            f"Frete: {escape_html(frete_txt)} ({escape_html(extra.get('frete_obs', ''))})\n"
            f"{prime_txt}"
            f"<b>Preço por lata:</b> R$ {preco:.2f}\n"
            f"{escape_html(extra.get('link', ''))}\n"
            f"<i>Monster · limiar ≤ R$ {MONSTER_ALERT_MAX_PRICE:.2f}/lata</i>"
        )
    return (
        "⚠️ ALERTA DE PREÇO BAÍXO ⚠️\n\n"
        f"Site: {escape_html(site_id)}\n"
        f"Produto: {escape_html(produto)}\n"
        f"<b>Preço encontrado:</b> R$ {preco:.2f}\n"
        f"<i>Monster · limiar ≤ R$ {MONSTER_ALERT_MAX_PRICE:.2f}</i>"
    )


async def _registrar_preco_monster(
    historico: dict[str, float],
    *,
    site_id: str,
    nome: str,
    preco: float,
    telegram_token: str,
    telegram_chat_id: str,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Espelha o preço do site no histórico; alerta só se mudou e está no limiar.

    * **Histórico** — item com ``monster`` no nome e preço válido é gravado
      quando não há alerta a enviar, ou **depois** que o Telegram confirma
      o envio.
    * **Telegram** — só quando o preço **mudou** e
      ``preço <= MONSTER_ALERT_MAX_PRICE``.
    * **Falha de envio** — o preço novo **não** entra no histórico. A chave
      continua diferente na próxima execução e o alerta é tentado de novo.
      Sem isso, um ``sendMessage`` recusado (Markdown quebrado, rede, HTTP)
      gravava o preço e o desconto nunca mais era avisado.
    """
    chave = _historico_chave(site_id, nome)
    preco_atual = _normalize_preco(preco)
    preco_anterior = historico.get(chave)
    preco_mudou = (
        preco_anterior is None
        or _normalize_preco(preco_anterior) != preco_atual
    )

    if not preco_mudou:
        return

    if preco_atual > MONSTER_ALERT_MAX_PRICE:
        historico[chave] = preco_atual
        logging.info(
            "Preço alterado no histórico (acima do limiar, sem alerta) — "
            "site=%r produto=%r preço=%.2f",
            site_id,
            nome,
            preco_atual,
        )
        return

    if not telegram_token or not telegram_chat_id:
        logging.warning(
            "Preço no limiar sem alerta enviado (Telegram não configurado) — "
            "site=%r produto=%r preço=%.2f",
            site_id,
            nome,
            preco_atual,
        )
        historico[chave] = preco_atual
        return

    try:
        mensagem = _build_alert_message(nome, preco_atual, site_id, extra)
    except Exception:
        logging.exception(
            "Falha ao montar o alerta; preço não gravado para repetir "
            "na próxima execução — site=%r produto=%r",
            site_id,
            nome,
        )
        return

    enviado = await send_telegram_alert(telegram_token, telegram_chat_id, mensagem)
    if not enviado:
        logging.error(
            "Alerta Telegram não enviado; preço não gravado para repetir "
            "na próxima execução — site=%r produto=%r preço=%.2f",
            site_id,
            nome,
            preco_atual,
        )
        return

    historico[chave] = preco_atual


async def main(
    historico: dict,
    arquivo_historico: str,
    arquivo_ofertas: str = "ofertas.json",
) -> None:
    """Varre cada site em ``SITES_CONFIG`` e persiste preços e o feed do app."""
    # Não sobrescreve variáveis já exportadas (CI). Preenche o que estiver no .env.
    load_dotenv(_ENV_PATH, override=False)
    telegram_token, telegram_chat_id = load_telegram_credentials()
    # Snapshot antes dos alertas: ``preco_lata_anterior`` é o preço da rodada
    # anterior, mesmo quando o envio falha e o histórico não é atualizado.
    historico_antes = dict(historico)
    observacoes: list[dict[str, Any]] = []
    sites_ok: set[str] = set()

    precisa_browser = any(
        cfg["tipo"] in ("html", "amazon") for cfg in SITES_CONFIG.values()
    )
    scraper: Optional[SupermarketScraper] = (
        SupermarketScraper(headless=True) if precisa_browser else None
    )

    try:
        if scraper is not None:
            await scraper.start()

        for site_id, config in SITES_CONFIG.items():
            t0 = time.monotonic()
            if config["tipo"] == "html":
                url_alvo = config["url_busca"].format(
                    query=_html_url_query_placeholder(site_id, TERMO_BUSCA),
                )
                try:
                    assert scraper is not None
                    itens = await asyncio.wait_for(scraper.extract_search_results(
                        url_alvo,
                        config["card_selector"],
                        config["name_selector"],
                        config["price_selector"],
                        popup_close_selector=POPUP_CLOSE_SELECTOR,
                        navigation_wait="load",
                        post_load_wait_ms=3000,
                        card_wait_timeout_ms=60_000,
                    ), SITE_TIMEOUT_S)
                except asyncio.TimeoutError:
                    logging.error(
                        "Timeout rígido de %ss — site=%r pulado (url=%r)",
                        SITE_TIMEOUT_S,
                        site_id,
                        url_alvo,
                    )
                    continue
                except Exception:
                    logging.exception(
                        "Falha ao extrair listagem HTML — site=%r url=%r",
                        site_id,
                        url_alvo,
                    )
                    continue

            elif config["tipo"] == "api":
                if site_id != "mercado_livre":
                    logging.warning("Tipo api sem cliente para site=%r — pulando.", site_id)
                    continue
                url_alvo = config["url_busca"].format(
                    query=urllib.parse.quote(TERMO_BUSCA_MERCADO_LIVRE, safe=""),
                )
                try:
                    itens = await asyncio.wait_for(
                        asyncio.to_thread(
                            fetch_mercado_livre_ofertas,
                            TERMO_BUSCA_MERCADO_LIVRE,
                        ),
                        SITE_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    logging.error(
                        "Timeout rígido de %ss — site=%r pulado",
                        SITE_TIMEOUT_S,
                        site_id,
                    )
                    continue
                except Exception:
                    logging.exception(
                        "Falha ao consultar Mercado Livre — site=%r",
                        site_id,
                    )
                    continue

            elif config["tipo"] == "amazon":
                url_alvo = config["url_busca"].format(
                    query=urllib.parse.quote_plus(TERMO_BUSCA_AMAZON),
                )
                try:
                    assert scraper is not None and scraper._browser is not None
                    itens = await asyncio.wait_for(
                        fetch_amazon_packs(scraper._browser, url_alvo), SITE_TIMEOUT_S
                    )
                except asyncio.TimeoutError:
                    logging.error("Timeout rígido de %ss — site=%r pulado", SITE_TIMEOUT_S, site_id)
                    continue
                except Exception:
                    logging.exception(
                        "Falha ao extrair Amazon — site=%r url=%r", site_id, url_alvo
                    )
                    continue

            elif config["tipo"] == "shopee":
                url_alvo = config["url_busca"].format(
                    query=urllib.parse.quote(TERMO_BUSCA_SHOPEE, safe=""),
                )
                try:
                    itens = await asyncio.wait_for(
                        asyncio.to_thread(fetch_shopee_packs, TERMO_BUSCA_SHOPEE),
                        SITE_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    logging.error("Timeout rígido de %ss — site=%r pulado", SITE_TIMEOUT_S, site_id)
                    continue
                except Exception:
                    logging.exception("Falha ao consultar Shopee — site=%r", site_id)
                    continue

            else:
                logging.warning("Tipo de estratégia desconhecido: %r", config.get("tipo"))
                continue

            logging.info(
                "site=%r: %d itens em %.1fs", site_id, len(itens), time.monotonic() - t0
            )
            print({"site": site_id, "url": url_alvo, "itens": itens})

            if not itens:
                # Lista vazia (captcha, API 10035, seletor quebrado ou busca sem
                # Monster) não conta como sucesso: o feed mantém a última oferta
                # dessa loja em vez de apagá-la.
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
                if not _produto_elegivel(nome):
                    continue

                chave = _historico_chave(site_id, nome)
                obs = observacao_from_item(
                    site_id,
                    item,
                    preco_anterior=historico_antes.get(chave),
                )
                if obs is not None:
                    observacoes.append(obs)
                    sites_ok.add(site_id)

                await _registrar_preco_monster(
                    historico,
                    site_id=site_id,
                    nome=nome,
                    preco=preco,
                    telegram_token=telegram_token,
                    telegram_chat_id=telegram_chat_id,
                    extra=item if "preco_pack" in item else None,
                )

        try:
            feed = build_ofertas(
                observacoes,
                previous=load_ofertas(arquivo_ofertas),
                sites_ok=sites_ok,
                generated_at=iso_now(),
            )
            save_ofertas(arquivo_ofertas, feed)
            logging.info(
                "ofertas.json: %d ofertas, %d pontos de histórico",
                len(feed["ofertas"]),
                len(feed["historico"]),
            )
        except Exception:
            logging.exception("Falha ao gravar ofertas.json")

        save_history(arquivo_historico, historico)
    finally:
        if scraper is not None:
            await scraper.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ARQUIVO_HISTORICO = "historico_precos.json"
    ARQUIVO_OFERTAS = "ofertas.json"
    historico = load_history(ARQUIVO_HISTORICO)
    asyncio.run(main(historico, ARQUIVO_HISTORICO, ARQUIVO_OFERTAS))
