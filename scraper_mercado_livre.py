"""Estratégia Mercado Livre — Monster 473 ml via API de catálogo.

A busca ``/sites/MLB/search`` e o HTML público respondem 403 ou caem em
verificação anti-bot. Esta loja usa só a API oficial:

1. ``POST /oauth/token`` com ``grant_type=client_credentials`` (token novo
   a cada execução; ``expires_in`` típico de 21600 s).
2. ``GET /products/search`` (``site_id=MLB``).
3. ``GET /products/{id}/items`` para o preço e o frete de cada produto.
4. ``GET /products/{id}`` quando a busca não traz ``pictures`` ou atributos.

Sem ``ML_CLIENT_ID`` / ``ML_CLIENT_SECRET``, ou se a API recusar, loga e
devolve ``[]`` (o monitor segue com as outras lojas). No máximo
:data:`MAX_PRODUTOS` ofertas publicadas por execução. Produto sem anúncio
não ocupa essa vaga. A busca lê uma página de até :data:`SEARCH_LIMIT`.
"""

from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.mercadolibre.com"
TOKEN_URL = f"{API_BASE}/oauth/token"
CATALOGO_BASE = "https://www.mercadolivre.com.br/p/"

TERMO_BUSCA_MERCADO_LIVRE = "monster energy 473ml"
DOMINIO_ENERGETICO = "MLB-ENERGY_DRINKS"

MAX_PRODUTOS = 20
SEARCH_LIMIT = 50
TIMEOUT_S = 20
MAX_TENTATIVAS = 2

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,40}$")
_MONSTER_RE = re.compile(r"\bmonster\b", re.I)
_VOLUME_RE = re.compile(r"(?<!\d)(\d{2,4})\s*(?:ml|mililitros?)\b", re.I)

# Quantidade no título. O primeiro padrão que casar ganha.
_QTD_PATTERNS = [
    re.compile(r"(\d{1,2})\s*[xX×]\s*\d{2,4}\s*ml\b", re.I),  # 6x473ml
    re.compile(r"(\d{1,2})\s*[xX×]\s*\d{2,4}\b", re.I),  # 6x473
    re.compile(
        r"\b(?:pack|kit|caixa|fardo|cx|c/)\s*(?:com|de|c/)?\s*(\d{1,2})\b",
        re.I,
    ),
    re.compile(r"\bcom\s*(\d{1,2})\b", re.I),  # com 6
    re.compile(r"\b(\d{1,2})\s*(?:unidades|unid\.?|un\.?|latas)\b", re.I),
    re.compile(r"\b(\d{1,2})\s*[xX×]\b", re.I),  # 12x
]


class _FalhaAPI(Exception):
    def __init__(self, status: int, onde: str) -> None:
        super().__init__(f"HTTP {status} em {onde}")
        self.status = status
        self.onde = onde


class _Limite(Exception):
    """HTTP 429 depois do retry leve — a loja inteira fica para a próxima rodada."""

    def __init__(self, onde: str) -> None:
        super().__init__(onde)
        self.onde = onde


def load_credentials() -> tuple[str, str]:
    return (
        (os.environ.get("ML_CLIENT_ID") or "").strip(),
        (os.environ.get("ML_CLIENT_SECRET") or "").strip(),
    )


def _indicio_pack(titulo: str) -> Optional[int]:
    for pat in _QTD_PATTERNS:
        achado = pat.search(titulo or "")
        if not achado:
            continue
        qtd = int(achado.group(1))
        if 1 <= qtd <= 48:
            return qtd
    return None


def parse_pack_qtd(titulo: str) -> int:
    """Quantidade de latas no título. Sem indício de pack, 1."""
    return _indicio_pack(titulo) or 1


def _qtd_anuncio(titulo_item: str, nome_produto: str) -> int:
    """Pack no título do item; se não houver indício, no nome do produto."""
    for texto in (titulo_item, nome_produto):
        qtd = _indicio_pack(texto)
        if qtd:
            return qtd
    return 1


def _volumes_texto(texto: str) -> set[int]:
    return {int(achado.group(1)) for achado in _VOLUME_RE.finditer(texto or "")}


def _volumes_atributos(produto: dict[str, Any]) -> set[int]:
    achados: set[int] = set()
    for attr in produto.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        estrutura = attr.get("value_struct")
        if isinstance(estrutura, dict):
            unidade = str(estrutura.get("unit") or "").lower()
            if unidade in {"ml", "milliliter", "milliliters", "millilitre", "millilitres"}:
                try:
                    achados.add(int(round(float(estrutura["number"]))))
                except (KeyError, TypeError, ValueError):
                    pass
        achados |= _volumes_texto(str(attr.get("value_name") or ""))
    return achados


def _com_monster(texto: str) -> str:
    """Garante a palavra Monster no rótulo que o monitor filtra.

    A marca às vezes só vem no atributo ``BRAND``. Sem isso, ``main`` descarta
    a oferta (e ela já ocupou uma vaga do teto de 20) porque ``produto`` não
    contém ``monster``.
    """
    texto = (texto or "").strip()
    if _MONSTER_RE.search(texto):
        return texto
    if not texto:
        return "Monster"
    return f"Monster {texto}"


def _marca_monster(produto: dict[str, Any]) -> bool:
    if _MONSTER_RE.search(str(produto.get("name") or "")):
        return True
    for attr in produto.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        ident = str(attr.get("id") or "").upper()
        nome_attr = str(attr.get("name") or "").strip().lower()
        if ident not in {"BRAND", "MARCA"} and nome_attr not in {"marca", "brand"}:
            continue
        valor = str(attr.get("value_name") or attr.get("value") or "")
        if _MONSTER_RE.search(valor):
            return True
    return False


def _dominio_ok(domain_id: Any) -> bool:
    if domain_id is None:
        return True
    texto = str(domain_id).strip()
    if not texto:
        return True
    return texto.upper() == DOMINIO_ENERGETICO


def _status_ok(produto: dict[str, Any]) -> bool:
    status = produto.get("status")
    if not isinstance(status, str) or not status.strip():
        return True
    return status.strip().lower() == "active"


def produto_elegivel(produto: dict[str, Any]) -> bool:
    """Monster 473 ml: domínio de energético (quando vier), marca e volume.

    Outro volume no nome ou nos atributos (250 ml, 355 ml, kit misto) fica
    de fora. Sem nenhum volume, também: não dá para assumir que é 473 ml.
    """
    if not isinstance(produto, dict) or not _status_ok(produto):
        return False
    if not _dominio_ok(produto.get("domain_id")):
        return False
    if not _marca_monster(produto):
        return False
    volumes = _volumes_texto(str(produto.get("name") or ""))
    volumes |= _volumes_atributos(produto)
    return volumes == {473}


def extrair_frete(shipping: Any) -> tuple[Optional[float], str]:
    """``(valor, observação)``.

    ``free_shipping`` true → 0. Sem ``cost`` (e sem frete grátis) →
    ``None`` ("frete desconhecido"): o preço por lata usa só o ``price``.
    """
    if not isinstance(shipping, dict):
        return None, "frete desconhecido"
    if shipping.get("free_shipping") is True:
        return 0.0, "frete grátis"
    if "cost" not in shipping or shipping.get("cost") is None:
        return None, "frete desconhecido"
    try:
        custo = round(float(shipping["cost"]), 2)
    except (TypeError, ValueError):
        return None, "frete desconhecido"
    if custo < 0:
        return None, "frete desconhecido"
    return custo, "frete informado pela API"


def calcular_preco_lata(price: float, frete: Optional[float], qtd: int) -> float:
    """``(price + frete) / qtd``. Frete desconhecido entra como 0 na conta."""
    quantidade = qtd if isinstance(qtd, int) and not isinstance(qtd, bool) and qtd >= 1 else 1
    extra = 0.0 if frete is None else float(frete)
    return round((float(price) + extra) / quantidade, 2)


def _to_float(valor: Any) -> Optional[float]:
    if isinstance(valor, bool) or valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _http_url(valor: Any) -> Optional[str]:
    if not isinstance(valor, str):
        return None
    texto = valor.strip()
    if texto.startswith("//"):
        texto = "https:" + texto
    if texto.startswith(("http://", "https://")):
        return texto
    return None


def _primeira_imagem(produto: dict[str, Any]) -> Optional[str]:
    fotos = produto.get("pictures")
    if isinstance(fotos, dict):
        fotos = [fotos]
    if not isinstance(fotos, list):
        return None
    for foto in fotos:
        if isinstance(foto, str):
            url = _http_url(foto)
        elif isinstance(foto, dict):
            url = _http_url(foto.get("url") or foto.get("secure_url"))
        else:
            url = None
        if url:
            return url
    return None


def _link_oferta(item: dict[str, Any], catalog_id: str) -> str:
    permalink = _http_url(item.get("permalink"))
    if permalink:
        return permalink
    return f"{CATALOGO_BASE}{catalog_id}"


def melhor_oferta(
    produto: dict[str, Any],
    itens: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """A oferta de menor preço por lata de um produto de catálogo."""
    if not produto_elegivel(produto):
        return None
    catalog_id = str(produto.get("id") or "").strip()
    if not catalog_id:
        return None
    nome = str(produto.get("name") or "").strip()
    melhor: Optional[tuple[Any, ...]] = None
    for item in itens or []:
        if not isinstance(item, dict):
            continue
        price = _to_float(item.get("price"))
        if price is None or price <= 0:
            continue
        titulo_item = str(item.get("title") or item.get("name") or "").strip()
        qtd = _qtd_anuncio(titulo_item, nome)
        frete, frete_obs = extrair_frete(item.get("shipping"))
        por_lata = calcular_preco_lata(price, frete, qtd)
        ordenacao = (por_lata, round(price, 2), str(item.get("item_id") or ""))
        if melhor is not None and ordenacao >= melhor[0]:
            continue
        melhor = (ordenacao, item, qtd, frete, frete_obs, por_lata, price, titulo_item)
    if melhor is None:
        return None
    _, item, qtd, frete, frete_obs, por_lata, price, titulo_item = melhor
    if not nome:
        nome = titulo_item
    if not nome:
        return None
    nome_monitor = _com_monster(nome)
    return {
        "produto": f"{nome_monitor} [{catalog_id}]",
        "titulo": _com_monster(titulo_item or nome),
        "qtd": qtd,
        "preco_pack": round(price, 2),
        "frete": frete,
        "frete_obs": frete_obs,
        "preco_lata": por_lata,
        "preco": por_lata,
        "link": _link_oferta(item, catalog_id),
        "imagem": _primeira_imagem(produto),
        "catalog_id": catalog_id,
        "item_id": item.get("item_id"),
    }


def listar_produtos_elegiveis(
    produtos: list[Any],
    *,
    limite: int = MAX_PRODUTOS,
) -> list[dict[str, Any]]:
    """Até ``limite`` produtos Monster 473 ml, na ordem da busca.

    Isto limita a varredura da página. A vaga de :data:`MAX_PRODUTOS` só
    fecha em :func:`montar_ofertas` e na coleta, quando a oferta sai.
    """
    saida: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for produto in produtos or []:
        if len(saida) >= limite:
            break
        if not isinstance(produto, dict) or not produto_elegivel(produto):
            continue
        pid = str(produto.get("id") or "").strip()
        if not pid or pid in vistos:
            continue
        vistos.add(pid)
        saida.append(produto)
    return saida


def montar_ofertas(
    produtos: list[Any],
    itens_por_produto: dict[str, list[dict[str, Any]]],
    *,
    limite: int = MAX_PRODUTOS,
) -> list[dict[str, Any]]:
    """Até ``limite`` ofertas. Produto sem anúncio não ocupa vaga. Sem rede."""
    ofertas: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for produto in produtos or []:
        if len(ofertas) >= limite:
            break
        if not isinstance(produto, dict) or not produto_elegivel(produto):
            continue
        pid = str(produto.get("id") or "").strip()
        if not pid or pid in vistos:
            continue
        vistos.add(pid)
        oferta = melhor_oferta(produto, itens_por_produto.get(pid) or [])
        if oferta is None:
            continue
        ofertas.append(oferta)
    return ofertas


def _resultados(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        bruto = payload
    elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
        bruto = payload["results"]
    else:
        return []
    return [item for item in bruto if isinstance(item, dict)]


def _id_seguro(pid: str) -> bool:
    return bool(_ID_RE.fullmatch(pid))


def _precisa_ficha(produto: dict[str, Any]) -> bool:
    return _primeira_imagem(produto) is None or not produto.get("attributes")


def _mesclar(busca: dict[str, Any], detalhe: dict[str, Any]) -> dict[str, Any]:
    junto = dict(busca)
    junto["id"] = busca.get("id") or detalhe.get("id")
    for chave in ("name", "domain_id", "status"):
        valor = detalhe.get(chave)
        if isinstance(valor, str) and valor.strip():
            junto[chave] = valor.strip()
    if detalhe.get("attributes"):
        junto["attributes"] = detalhe["attributes"]
    if _primeira_imagem(detalhe):
        junto["pictures"] = detalhe["pictures"]
    return junto


def _enviar(session: requests.Session, method: str, url: str, *, onde: str, **kwargs: Any) -> Any:
    ultimo: Optional[BaseException] = None
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resposta = session.request(method, url, timeout=TIMEOUT_S, **kwargs)
        except (requests.Timeout, requests.ConnectionError) as exc:
            ultimo = exc
            if tentativa >= MAX_TENTATIVAS:
                raise
            time.sleep(0.5 * tentativa)
            continue
        status = resposta.status_code
        if status in _RETRY_STATUS and tentativa < MAX_TENTATIVAS:
            time.sleep(0.5 * tentativa)
            continue
        if status == 429:
            raise _Limite(onde)
        if status == 404:
            return None
        if status >= 400:
            raise _FalhaAPI(status, onde)
        try:
            return resposta.json()
        except ValueError as exc:
            raise _FalhaAPI(status, onde) from exc
    if ultimo is not None:
        raise ultimo
    raise _FalhaAPI(0, onde)


def _get_json(
    session: requests.Session,
    url: str,
    *,
    onde: str,
    token: str,
    params: Optional[dict[str, Any]] = None,
    ausente_ok: bool = False,
) -> Any:
    try:
        return _enviar(
            session,
            "GET",
            url,
            onde=onde,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
    except _FalhaAPI as exc:
        if ausente_ok and exc.status not in (401, 403):
            logger.warning(
                "Mercado Livre: HTTP %s em %s — seguindo sem esse dado.",
                exc.status,
                onde,
            )
            return None
        raise


def _obter_token(session: requests.Session, client_id: str, client_secret: str) -> str:
    payload = _enviar(
        session,
        "POST",
        TOKEN_URL,
        onde="oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise _FalhaAPI(0, "oauth/token")
    return token.strip()


def _oferta_do_produto(
    session: requests.Session,
    token: str,
    produto: dict[str, Any],
) -> Optional[dict[str, Any]]:
    pid = str(produto.get("id") or "").strip()
    if not _id_seguro(pid):
        logger.warning("Mercado Livre: id de catálogo ignorado: %r", pid)
        return None
    completo = produto
    if _precisa_ficha(produto):
        detalhe = _get_json(
            session,
            f"{API_BASE}/products/{urllib.parse.quote(pid, safe='')}",
            onde=f"products/{pid}",
            token=token,
            ausente_ok=True,
        )
        if isinstance(detalhe, dict):
            completo = _mesclar(produto, detalhe)
    if not produto_elegivel(completo):
        return None
    payload = _get_json(
        session,
        f"{API_BASE}/products/{urllib.parse.quote(pid, safe='')}/items",
        onde=f"products/{pid}/items",
        token=token,
        ausente_ok=True,
    )
    return melhor_oferta(completo, _resultados(payload))


def _coletar(client_id: str, client_secret: str, keyword: str) -> list[dict[str, Any]]:
    session = requests.Session()
    session.headers["User-Agent"] = "botmonster/mercado-livre"
    session.headers["Accept"] = "application/json"
    try:
        token = _obter_token(session, client_id, client_secret)
        busca = _get_json(
            session,
            f"{API_BASE}/products/search",
            onde="products/search",
            token=token,
            params={
                "site_id": "MLB",
                "q": keyword,
                "status": "active",
                "limit": SEARCH_LIMIT,
            },
        )
        # A página pode ter mais elegíveis do que o teto. A vaga só fecha
        # quando a oferta entra; sem anúncio, o próximo produto segue.
        candidatos = listar_produtos_elegiveis(_resultados(busca), limite=SEARCH_LIMIT)
        ofertas: list[dict[str, Any]] = []
        for produto in candidatos:
            if len(ofertas) >= MAX_PRODUTOS:
                break
            try:
                oferta = _oferta_do_produto(session, token, produto)
            except requests.RequestException:
                logger.warning(
                    "Mercado Livre: falha de rede no produto %s — ignorado.",
                    produto.get("id"),
                )
                continue
            if oferta:
                ofertas.append(oferta)
        logger.info(
            "Mercado Livre: %d ofertas publicadas (teto %d).",
            len(ofertas),
            MAX_PRODUTOS,
        )
        return ofertas
    finally:
        session.close()


def fetch_mercado_livre_ofertas(
    keyword: str = TERMO_BUSCA_MERCADO_LIVRE,
) -> list[dict[str, Any]]:
    """Consulta o catálogo (síncrono). Nunca levanta por credencial ou HTTP.

    Token novo a cada chamada. Lista vazia mantém a oferta anterior no feed.
    """
    client_id, client_secret = load_credentials()
    if not client_id or not client_secret:
        logger.warning(
            "Mercado Livre: ML_CLIENT_ID/ML_CLIENT_SECRET ausentes — pulando."
        )
        return []
    try:
        return _coletar(client_id, client_secret, keyword)
    except _Limite:
        logger.warning(
            "Mercado Livre: limite de requisições (HTTP 429) — pulando."
        )
        return []
    except _FalhaAPI as exc:
        logger.warning(
            "Mercado Livre: API recusou %s (HTTP %s) — pulando.",
            exc.onde,
            exc.status,
        )
        return []
    except requests.RequestException as exc:
        logger.warning(
            "Mercado Livre: falha de rede (%s) — pulando.",
            type(exc).__name__,
        )
        return []
