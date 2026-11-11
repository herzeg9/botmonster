"""Feed ``ofertas.json`` (schema v1) para o app Android.

Contrato publicado na raiz do repositório e lido em
``https://raw.githubusercontent.com/herzeg9/botmonster/main/ofertas.json``.

Raiz
    ``schema_version`` (int ``1``), ``generated_at`` (ISO-8601),
    ``ofertas`` (lista) e ``historico`` (opcional, séries do gráfico).

Oferta
    ``id``, ``loja``, ``nome``, ``sabor``, ``pack_qtd``, ``preco_pack``,
    ``preco_lata``, ``preco_lata_anterior``, ``frete`` (``0`` = grátis,
    ``null`` = desconhecido), ``cupom``, ``link``, ``imagem``, ``visto_em``.
    Preços são números em reais.

``historico`` é uma lista plana de pontos
``{loja, sabor, nome, pack_qtd, preco_lata, visto_em}`` — o mesmo formato
que o app decodifica como ``List``. Cada ponto pode trazer ``id`` (a chave
da oferta); o app ignora chaves que não conhece. A série é interna por
``id``: ponto novo só quando o preço por lata **daquela** oferta muda,
limitado aos ``HISTORICO_MAX_PONTOS`` mais recentes. Na leitura, a lista
antiga sem ``id`` e o objeto legado (id → pontos) também são aceitos.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

from scraper_amazon import parse_pack_qtd

SCHEMA_VERSION = 1
HISTORICO_MAX_PONTOS = 60

LOJAS = ("pao_de_acucar", "tauste", "sams_club", "amazon", "shopee")

_OFERTA_CHAVES = (
    "id",
    "loja",
    "nome",
    "sabor",
    "pack_qtd",
    "preco_pack",
    "preco_lata",
    "preco_lata_anterior",
    "frete",
    "cupom",
    "link",
    "imagem",
    "visto_em",
)

# Primeiro match ganha. Texto comparado sem acento e em minúsculas.
_SABORES: tuple[tuple[str, str], ...] = (
    ("ultra strawberry dreams", "Ultra Strawberry Dreams"),
    ("strawberry dreams", "Ultra Strawberry Dreams"),
    ("ultra peachy keen", "Ultra Peachy Keen"),
    ("peachy keen", "Ultra Peachy Keen"),
    ("ultra watermelon", "Ultra Watermelon"),
    ("watermelon", "Ultra Watermelon"),
    ("ultra fiesta mango", "Ultra Fiesta Mango"),
    ("fiesta mango", "Ultra Fiesta Mango"),
    ("ultra paradise", "Ultra Paradise"),
    ("paradise", "Ultra Paradise"),
    ("ultra violeta", "Ultra Violet"),
    ("ultra violet", "Ultra Violet"),
    ("violeta", "Ultra Violet"),
    ("absolutely zero", "Absolutely Zero"),
    ("mango loco", "Mango Loco"),
    ("pipeline punch", "Pipeline Punch"),
    ("pacific punch", "Pacific Punch"),
    ("rio punch", "Rio Punch"),
    ("creme de papaya", "Rio Punch"),
    ("papaya", "Rio Punch"),
    ("khaotic", "Khaotic"),
    ("tropical orange", "Khaotic"),
    ("dragon ice", "Dragon Ice Tea"),
    ("the doctor", "The Doctor"),
    ("zero acucar", "Zero Açúcar"),
    ("sem acucar", "Zero Açúcar"),
    ("ultra", "Ultra"),
)

_STOPWORDS = {
    "energetico",
    "monster",
    "energy",
    "lata",
    "latas",
    "pack",
    "kit",
    "caixa",
    "fardo",
    "ml",
    "unidades",
    "unidade",
    "unid",
    "un",
    "com",
    "de",
    "da",
    "do",
    "das",
    "dos",
    "sem",
    "para",
    "em",
    "juice",
    "bebida",
    "gelada",
    "sabor",
    "sabores",
    "original",
    "zero",
    "acucar",
}


def offer_id(loja: str, nome_historico: str) -> str:
    """Mesma chave de ``historico_precos.json`` (``loja::produto``)."""
    return f"{loja}::{nome_historico.strip()}"


def iso_now() -> str:
    """ISO-8601 em UTC com sufixo ``Z`` e precisão de segundos."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _fold(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto or "")
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return sem_acento.lower()


def derivar_sabor(nome: str) -> str:
    """Sabor a partir do nome do produto (linha Monster conhecida ou o resto do título)."""
    folded = _fold(nome)
    for needle, rotulo in _SABORES:
        if needle in folded:
            return rotulo
    palavras = []
    for palavra in re.findall(r"[a-z0-9]+", folded):
        if palavra in _STOPWORDS or len(palavra) <= 1 or any(ch.isdigit() for ch in palavra):
            continue
        palavras.append(palavra.capitalize())
    if not palavras:
        return "Original"
    return " ".join(palavras)


def _numero(valor: Any) -> Optional[float]:
    if isinstance(valor, bool) or valor is None:
        return None
    try:
        return round(float(valor), 2)
    except (TypeError, ValueError):
        return None


def _texto_ou_none(valor: Any) -> Optional[str]:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _iso_ok(valor: Any) -> bool:
    if not isinstance(valor, str) or not valor.strip():
        return False
    try:
        datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def observacao_from_item(
    loja: str,
    item: dict[str, Any],
    *,
    preco_anterior: Optional[float],
) -> Optional[dict[str, Any]]:
    """Normaliza um item de scraper para a montagem do feed.

    ``preco`` do item é o valor que o monitor grava (por lata nos packs
    Amazon/Shopee; preço de prateleira no HTML). ``preco_lata_anterior``
    vem do histórico **antes** desta execução.
    """
    nome_historico = _texto_ou_none(item.get("produto"))
    if not nome_historico:
        return None
    preco_monitor = _numero(item.get("preco"))
    if preco_monitor is None:
        return None

    nome = _texto_ou_none(item.get("titulo")) or nome_historico
    if "preco_pack" in item:
        pack_qtd = item.get("qtd")
        try:
            pack_qtd_int = int(pack_qtd)
        except (TypeError, ValueError):
            pack_qtd_int = 1
        if pack_qtd_int < 1:
            pack_qtd_int = 1
        preco_pack = _numero(item.get("preco_pack"))
        if preco_pack is None:
            preco_pack = preco_monitor
        preco_lata = _numero(item.get("preco_lata"))
        if preco_lata is None:
            preco_lata = preco_monitor
        frete = item.get("frete")
        frete_num = None if frete is None else _numero(frete)
    else:
        pack_qtd_int = parse_pack_qtd(nome_historico) or parse_pack_qtd(nome) or 1
        if pack_qtd_int > 1:
            preco_pack = preco_monitor
            preco_lata = round(preco_monitor / pack_qtd_int, 2)
            if preco_anterior is not None:
                preco_anterior = round(float(preco_anterior) / pack_qtd_int, 2)
        else:
            preco_pack = preco_monitor
            preco_lata = preco_monitor
        frete_num = None

    anterior = None if preco_anterior is None else round(float(preco_anterior), 2)
    return {
        "id": offer_id(loja, nome_historico),
        "loja": loja,
        "nome": nome,
        "sabor": derivar_sabor(nome),
        "pack_qtd": pack_qtd_int,
        "preco_pack": preco_pack,
        "preco_lata": preco_lata,
        "preco_lata_anterior": anterior,
        "frete": frete_num,
        "cupom": _texto_ou_none(item.get("cupom")),
        "link": _texto_ou_none(item.get("link") or item.get("url")),
        "imagem": _texto_ou_none(item.get("imagem")),
    }


def _pack_inteiro(valor: Any, nome: str) -> Optional[int]:
    if isinstance(valor, bool):
        return None
    try:
        qtd = int(valor)
    except (TypeError, ValueError):
        qtd = parse_pack_qtd(nome) or 0
    if qtd < 1:
        return None
    return qtd


def _ponto_historico(
    loja: str,
    sabor: str,
    nome: str,
    pack_qtd: int,
    preco_lata: float,
    visto_em: str,
    serie_id: str,
) -> dict[str, Any]:
    return {
        "loja": loja,
        "sabor": sabor,
        "nome": nome,
        "pack_qtd": pack_qtd,
        "preco_lata": preco_lata,
        "visto_em": visto_em,
        "id": serie_id,
    }


def _id_no_ponto(ponto: dict[str, Any]) -> Optional[str]:
    oid = ponto.get("id")
    if isinstance(oid, str) and oid.strip():
        return oid.strip()
    return None


def _chave_legada(loja: str, nome: str, sabor: str, pack: int) -> str:
    """Série de um ponto antigo sem ``id`` que não cabe numa única oferta.

    O separador não aparece num ``id`` de oferta (``loja::produto``), então
    dois anúncios com o mesmo título não herdam esta série.
    """
    return f"{loja}::{nome}||{sabor}||{pack}"


def _ids_da_mesma_oferta(
    loja: str,
    nome: str,
    sabor: str,
    pack: int,
    ofertas_por_id: dict[str, dict[str, Any]],
) -> list[str]:
    """Ofertas anteriores com o mesmo rótulo (loja, nome, sabor, pack)."""
    achados: list[str] = []
    for oid, oferta in ofertas_por_id.items():
        if not isinstance(oferta, dict) or oferta.get("loja") != loja:
            continue
        onome = oferta.get("nome")
        if not isinstance(onome, str) or onome.strip() != nome:
            continue
        osabor = oferta.get("sabor")
        if not isinstance(osabor, str) or not osabor.strip():
            osabor = derivar_sabor(onome)
        if osabor != sabor:
            continue
        opack = _pack_inteiro(oferta.get("pack_qtd"), onome)
        if opack != pack:
            continue
        achados.append(oid)
    return achados


def _resolver_serie_id(
    ponto: dict[str, Any],
    loja: str,
    nome: str,
    sabor: str,
    pack: int,
    ofertas_por_id: dict[str, dict[str, Any]],
) -> str:
    """Chave interna da série.

    ``id`` gravado no ponto ganha. Sem ele, um único anúncio anterior com o
    mesmo rótulo empresta o seu ``id``. Vários anúncios (Amazon/Shopee com
    o mesmo título e ASIN/itemId diferentes) não voltam a ser fundidos.
    """
    explicito = _id_no_ponto(ponto)
    if explicito is not None:
        return explicito
    achados = _ids_da_mesma_oferta(loja, nome, sabor, pack, ofertas_por_id)
    if len(achados) == 1:
        return achados[0]
    # Sem anúncio único: supermercado reencontra ``loja::nome`` quando o
    # rótulo é o que o nome sozinho produziria. Vários anúncios com o mesmo
    # título ficam numa série à parte e não são absorvidos por um ASIN ou
    # itemId que tenha sobrado.
    if (
        not achados
        and pack == (parse_pack_qtd(nome) or 1)
        and sabor == derivar_sabor(nome)
    ):
        return offer_id(loja, nome)
    return _chave_legada(loja, nome, sabor, pack)


def _meta_legado(oid: str, ofertas_por_id: dict[str, dict[str, Any]]) -> Optional[tuple[str, str, str, int]]:
    """Loja, nome, sabor e pack de uma chave antiga ``loja::produto``."""
    oferta = ofertas_por_id.get(oid)
    if isinstance(oferta, dict):
        loja = oferta.get("loja")
        nome = oferta.get("nome")
        if loja in LOJAS and isinstance(nome, str) and nome.strip():
            sabor = oferta.get("sabor")
            if not isinstance(sabor, str) or not sabor.strip():
                sabor = derivar_sabor(nome)
            pack = _pack_inteiro(oferta.get("pack_qtd"), nome)
            if pack is not None:
                return (str(loja), nome.strip(), sabor, pack)
    loja, sep, nome = str(oid).partition("::")
    nome = nome.strip()
    if not sep or loja not in LOJAS or not nome:
        return None
    return (loja, nome, derivar_sabor(nome), parse_pack_qtd(nome) or 1)


def _series_de_historico(
    bruto: Any,
    ofertas_por_id: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Agrupa o histórico anterior pelo ``id`` da oferta.

    Aceita a lista do contrato (com ou sem ``id`` em cada ponto) e o objeto
    legado ``{id: [{visto_em, preco_lata}]}``.
    """
    soltos: list[tuple[str, dict[str, Any]]] = []
    if isinstance(bruto, dict):
        for oid, pontos in bruto.items():
            if not isinstance(oid, str) or not oid.strip() or not isinstance(pontos, list):
                continue
            chave = oid.strip()
            meta = _meta_legado(chave, ofertas_por_id)
            if meta is None:
                continue
            loja, nome, sabor, pack = meta
            for ponto in pontos:
                if not isinstance(ponto, dict) or not _iso_ok(ponto.get("visto_em")):
                    continue
                preco = _numero(ponto.get("preco_lata"))
                if preco is None:
                    continue
                soltos.append(
                    (
                        chave,
                        _ponto_historico(
                            loja, sabor, nome, pack, preco, str(ponto["visto_em"]), chave
                        ),
                    )
                )
    elif isinstance(bruto, list):
        for ponto in bruto:
            if not isinstance(ponto, dict) or not _iso_ok(ponto.get("visto_em")):
                continue
            loja = ponto.get("loja")
            nome = ponto.get("nome")
            sabor = ponto.get("sabor")
            if loja not in LOJAS or not isinstance(nome, str) or not nome.strip():
                continue
            if not isinstance(sabor, str) or not sabor.strip():
                sabor = derivar_sabor(nome)
            nome_limpo = nome.strip()
            pack = _pack_inteiro(ponto.get("pack_qtd"), nome_limpo)
            preco = _numero(ponto.get("preco_lata"))
            if pack is None or preco is None:
                continue
            chave = _resolver_serie_id(
                ponto, str(loja), nome_limpo, sabor, pack, ofertas_por_id
            )
            soltos.append(
                (
                    chave,
                    _ponto_historico(
                        str(loja), sabor, nome_limpo, pack, preco, str(ponto["visto_em"]), chave
                    ),
                )
            )

    series: dict[str, list[dict[str, Any]]] = {}
    soltos.sort(key=lambda item: (item[0], item[1]["visto_em"]))
    for chave, ponto in soltos:
        serie = series.setdefault(chave, [])
        if serie and serie[-1]["preco_lata"] == ponto["preco_lata"]:
            continue
        serie.append(ponto)
    for chave, serie in series.items():
        series[chave] = serie[-HISTORICO_MAX_PONTOS:]
    return series


def _lista_historico(
    series: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    pontos: list[dict[str, Any]] = []
    for chave in sorted(series):
        pontos.extend(series[chave])
    return pontos


def _oferta_observada(obs: dict[str, Any], visto_em: str) -> dict[str, Any]:
    oferta = {
        "id": obs["id"],
        "loja": obs["loja"],
        "nome": obs["nome"],
        "sabor": obs["sabor"],
        "pack_qtd": obs["pack_qtd"],
        "preco_pack": obs["preco_pack"],
        "preco_lata": obs["preco_lata"],
        "preco_lata_anterior": obs["preco_lata_anterior"],
        "frete": obs["frete"],
        "cupom": obs["cupom"],
        "link": obs["link"],
        "imagem": obs["imagem"],
        "visto_em": visto_em,
    }
    return oferta


def load_ofertas(path: str) -> Optional[dict[str, Any]]:
    """Lê o feed anterior (séries do gráfico). Arquivo ausente ou inválido → ``None``."""
    try:
        with open(path, encoding="utf-8") as arquivo:
            data = json.load(arquivo)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        logging.warning("ofertas.json inválido; séries do gráfico recomeçam.")
        return None
    if not isinstance(data, dict):
        logging.warning("ofertas.json não é objeto; séries do gráfico recomeçam.")
        return None
    return data


def build_ofertas(
    observacoes: list[dict[str, Any]],
    *,
    previous: Optional[dict[str, Any]],
    sites_ok: set[str],
    generated_at: str,
) -> dict[str, Any]:
    """Monta o feed desta execução.

    Lojas em ``sites_ok`` substituem as ofertas anteriores. Loja que falhou
    (timeout, captcha, API) **mantém** a última oferta, com o ``visto_em``
    antigo, para um site fora do ar não esvaziar o app.
    """
    atuais: dict[str, dict[str, Any]] = {}
    for obs in observacoes:
        if not obs or not obs.get("id"):
            continue
        atuais[obs["id"]] = _oferta_observada(obs, generated_at)

    anteriores: dict[str, dict[str, Any]] = {}
    if isinstance(previous, dict):
        brutas = previous.get("ofertas")
        if isinstance(brutas, list):
            for oferta in brutas:
                if isinstance(oferta, dict) and isinstance(oferta.get("id"), str):
                    anteriores[oferta["id"]] = oferta

    por_id: dict[str, dict[str, Any]] = dict(atuais)
    for oid, antiga in anteriores.items():
        if oid in por_id:
            continue
        loja = antiga.get("loja")
        if loja not in sites_ok:
            por_id[oid] = antiga

    ofertas = sorted(
        por_id.values(),
        key=lambda o: (str(o.get("loja")), str(o.get("nome")), str(o.get("id"))),
    )

    series = _series_de_historico(
        previous.get("historico") if isinstance(previous, dict) else None,
        anteriores,
    )
    for oferta in atuais.values():
        pack = _pack_inteiro(oferta.get("pack_qtd"), str(oferta.get("nome") or ""))
        nome = oferta.get("nome")
        sabor = oferta.get("sabor")
        loja = oferta.get("loja")
        preco = _numero(oferta.get("preco_lata"))
        oid = oferta.get("id")
        if (
            pack is None
            or preco is None
            or loja not in LOJAS
            or not isinstance(nome, str)
            or not nome.strip()
            or not isinstance(sabor, str)
            or not sabor.strip()
            or not isinstance(oid, str)
            or not oid.strip()
        ):
            continue
        chave = oid.strip()
        serie = list(series.get(chave, []))
        if not serie or serie[-1]["preco_lata"] != preco:
            serie.append(
                _ponto_historico(str(loja), sabor, nome.strip(), pack, preco, generated_at, chave)
            )
        series[chave] = serie[-HISTORICO_MAX_PONTOS:]

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "ofertas": ofertas,
        "historico": _lista_historico(series),
    }


def save_ofertas(path: str, feed: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as arquivo:
        json.dump(feed, arquivo, indent=2, ensure_ascii=False)
        arquivo.write("\n")


def _eh_numero(valor: Any) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def validar_ofertas(feed: Any) -> list[str]:
    """Devolve erros do schema v1 (lista vazia = válido)."""
    erros: list[str] = []
    if not isinstance(feed, dict):
        return ["raiz não é objeto"]
    if feed.get("schema_version") != SCHEMA_VERSION:
        erros.append("schema_version deve ser 1")
    if not _iso_ok(feed.get("generated_at")):
        erros.append("generated_at não é ISO-8601")
    ofertas = feed.get("ofertas")
    if not isinstance(ofertas, list):
        erros.append("ofertas não é lista")
        ofertas = []
    vistos: set[str] = set()
    for i, oferta in enumerate(ofertas):
        prefixo = f"ofertas[{i}]"
        if not isinstance(oferta, dict):
            erros.append(f"{prefixo} não é objeto")
            continue
        for chave in _OFERTA_CHAVES:
            if chave not in oferta:
                erros.append(f"{prefixo} sem {chave}")
        oid = oferta.get("id")
        if not isinstance(oid, str) or not oid.strip():
            erros.append(f"{prefixo}.id inválido")
        elif oid in vistos:
            erros.append(f"{prefixo}.id duplicado: {oid}")
        else:
            vistos.add(oid)
        if oferta.get("loja") not in LOJAS:
            erros.append(f"{prefixo}.loja inválida: {oferta.get('loja')!r}")
        if not isinstance(oferta.get("nome"), str) or not oferta.get("nome").strip():
            erros.append(f"{prefixo}.nome inválido")
        if not isinstance(oferta.get("sabor"), str) or not oferta.get("sabor").strip():
            erros.append(f"{prefixo}.sabor inválido")
        qtd = oferta.get("pack_qtd")
        if isinstance(qtd, bool) or not isinstance(qtd, int) or qtd < 1:
            erros.append(f"{prefixo}.pack_qtd inválido")
        for campo in ("preco_pack", "preco_lata"):
            if not _eh_numero(oferta.get(campo)):
                erros.append(f"{prefixo}.{campo} não é número")
        anterior = oferta.get("preco_lata_anterior")
        if anterior is not None and not _eh_numero(anterior):
            erros.append(f"{prefixo}.preco_lata_anterior inválido")
        frete = oferta.get("frete")
        if frete is not None and (not _eh_numero(frete) or frete < 0):
            erros.append(f"{prefixo}.frete inválido")
        if oferta.get("cupom") is not None and not isinstance(oferta.get("cupom"), str):
            erros.append(f"{prefixo}.cupom inválido")
        for campo in ("link", "imagem"):
            valor = oferta.get(campo)
            if valor is None:
                continue
            if not isinstance(valor, str) or not valor.startswith(("http://", "https://")):
                erros.append(f"{prefixo}.{campo} inválido")
        if not _iso_ok(oferta.get("visto_em")):
            erros.append(f"{prefixo}.visto_em inválido")

    historico = feed.get("historico")
    if not isinstance(historico, list):
        erros.append("historico não é lista")
        return erros
    por_oferta: dict[tuple[Any, ...], int] = {}
    for i, ponto in enumerate(historico):
        prefixo = f"historico[{i}]"
        if not isinstance(ponto, dict):
            erros.append(f"{prefixo} não é objeto")
            continue
        for chave in ("loja", "sabor", "nome", "pack_qtd", "preco_lata", "visto_em"):
            if chave not in ponto:
                erros.append(f"{prefixo} sem {chave}")
        if ponto.get("loja") not in LOJAS:
            erros.append(f"{prefixo}.loja inválida")
        if not isinstance(ponto.get("nome"), str) or not str(ponto.get("nome")).strip():
            erros.append(f"{prefixo}.nome inválido")
        if not isinstance(ponto.get("sabor"), str) or not str(ponto.get("sabor")).strip():
            erros.append(f"{prefixo}.sabor inválido")
        qtd = ponto.get("pack_qtd")
        if isinstance(qtd, bool) or not isinstance(qtd, int) or qtd < 1:
            erros.append(f"{prefixo}.pack_qtd inválido")
        else:
            oid = ponto.get("id")
            if isinstance(oid, str) and oid.strip():
                chave_oferta: tuple[Any, ...] = ("id", oid.strip())
            else:
                chave_oferta = (
                    str(ponto.get("loja")),
                    str(ponto.get("nome")),
                    str(ponto.get("sabor")),
                    qtd,
                )
            por_oferta[chave_oferta] = por_oferta.get(chave_oferta, 0) + 1
        if not _eh_numero(ponto.get("preco_lata")):
            erros.append(f"{prefixo}.preco_lata não é número")
        if not _iso_ok(ponto.get("visto_em")):
            erros.append(f"{prefixo}.visto_em inválido")
    for chave_oferta, quantidade in por_oferta.items():
        if quantidade <= HISTORICO_MAX_PONTOS:
            continue
        if chave_oferta[0] == "id":
            erros.append(
                f"historico de {chave_oferta[1]!r} excede {HISTORICO_MAX_PONTOS} pontos"
            )
        else:
            erros.append(
                f"historico de {chave_oferta[0]} / {chave_oferta[1]!r} "
                f"excede {HISTORICO_MAX_PONTOS} pontos"
            )
    return erros
