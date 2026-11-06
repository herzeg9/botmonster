"""Estratégia Shopee Brasil — packs de 6 latas de Monster via Affiliate Open API.

Usa a API GraphQL oficial de afiliados (``productOfferV2``), autenticada com
``SHOPEE_AFFILIATE_APP_ID`` / ``SHOPEE_AFFILIATE_SECRET``. Sem credenciais, ou
se a API recusar o acesso, loga e devolve ``[]`` (o monitor segue).

A API de afiliados **não informa frete**: o preço por lata considera só o
produto e o alerta marca o frete como desconhecido.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

SHOPEE_AFFILIATE_URL = "https://open-api.affiliate.shopee.com.br/graphql"
PACK_ALVO = 6

_QUERY = (
    "query($kw:String,$page:Int,$limit:Int){productOfferV2(keyword:$kw,page:$page,"
    "limit:$limit,sortType:2){nodes{itemId shopId productName price priceMin priceMax "
    "shopName productLink offerLink sales ratingStar}pageInfo{page limit hasNextPage}}}"
)

_QTD_PATTERNS = [
    re.compile(r"(\d{1,2})\s*[xX×]\s*\d{2,4}\s*ml", re.I),
    re.compile(r"\b(?:pack|kit|caixa|fardo|c/|com)\s*(?:com|de|c/)?\s*(\d{1,2})\b", re.I),
    re.compile(r"\b(\d{1,2})\s*(?:unidades|unid\.?|un\.?|latas)\b", re.I),
]


def parse_pack_qtd(titulo: str) -> Optional[int]:
    """Quantidade de latas no título (``None`` se não identificada)."""
    for pat in _QTD_PATTERNS:
        m = pat.search(titulo or "")
        if m:
            n = int(m.group(1))
            if 1 <= n <= 48:
                return n
    return None


def load_credentials() -> tuple[str, str]:
    return (
        (os.environ.get("SHOPEE_AFFILIATE_APP_ID") or "").strip(),
        (os.environ.get("SHOPEE_AFFILIATE_SECRET") or "").strip(),
    )


def _auth_header(app_id: str, secret: str, payload: str, ts: Optional[int] = None) -> str:
    ts_s = str(ts if ts is not None else int(time.time()))
    sig = hashlib.sha256(f"{app_id}{ts_s}{payload}{secret}".encode()).hexdigest()
    return f"SHA256 Credential={app_id}, Timestamp={ts_s}, Signature={sig}"


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_nodes(nodes: list[dict[str, Any]], *, pack_alvo: int = PACK_ALVO) -> list[dict[str, Any]]:
    """Filtra packs Monster de ``pack_alvo`` latas e calcula o preço por lata."""
    itens: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for n in nodes or []:
        titulo = str(n.get("productName") or "").strip()
        item_id = str(n.get("itemId") or "")
        if not titulo or "monster" not in titulo.lower() or item_id in vistos:
            continue
        if parse_pack_qtd(titulo) != pack_alvo:
            continue
        # priceMin = menor variação; com variações (ex.: sabores) é o "a partir de".
        preco = _to_float(n.get("priceMin")) or _to_float(n.get("price"))
        if not preco or preco <= 0:
            continue
        vistos.add(item_id)
        por_lata = round(preco / pack_alvo, 2)
        itens.append(
            {
                "produto": f"{titulo} [{item_id}]",
                "titulo": titulo,
                "qtd": pack_alvo,
                "preco_pack": round(preco, 2),
                "frete": None,
                "frete_obs": "Shopee API não informa frete",
                "preco_lata": por_lata,
                "preco": por_lata,
                "prime": False,
                "loja": n.get("shopName"),
                "link": n.get("offerLink") or n.get("productLink") or "",
            }
        )
    return itens


def _post(payload: str, app_id: str, secret: str) -> dict[str, Any]:
    r = requests.post(
        SHOPEE_AFFILIATE_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": _auth_header(app_id, secret, payload),
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def fetch_shopee_packs(keyword: str, *, pages: int = 2, limit: int = 50) -> list[dict[str, Any]]:
    """Consulta ``productOfferV2`` (síncrono). Nunca levanta por erro de acesso."""
    app_id, secret = load_credentials()
    if not app_id or not secret:
        logger.warning("Shopee: SHOPEE_AFFILIATE_APP_ID/SECRET ausentes — pulando.")
        return []
    nodes: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        payload = json.dumps(
            {"query": _QUERY, "variables": {"kw": keyword, "page": page, "limit": limit}},
            separators=(",", ":"),
        )
        try:
            data = _post(payload, app_id, secret)
        except Exception as exc:  # rede/HTTP
            logger.warning("Shopee: falha HTTP na API de afiliados (%s) — pulando.", exc)
            break
        if data.get("errors"):
            err = data["errors"][0]
            logger.warning(
                "Shopee: API recusou (code=%s): %s — pulando.",
                (err.get("extensions") or {}).get("code"),
                err.get("message"),
            )
            break
        offer = (data.get("data") or {}).get("productOfferV2") or {}
        nodes.extend(offer.get("nodes") or [])
        if not (offer.get("pageInfo") or {}).get("hasNextPage"):
            break
    return parse_nodes(nodes)
