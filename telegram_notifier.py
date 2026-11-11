"""Notificação assíncrona via API Bot do Telegram."""

from __future__ import annotations

import html
from typing import Any, Optional

import aiohttp


def escape_html(text: object) -> str:
    """Escapa texto para o ``parse_mode`` HTML do Telegram.

    ``<``, ``>`` e ``&`` quebram o parser; ``_`` e ``*`` (comuns em título e
    link de afiliado) são literais e não precisam de escape.
    """
    return html.escape(str(text), quote=False)


async def send_telegram_alert(token: str, chat_id: str, message: str) -> bool:
    """Envia uma mensagem ao chat configurado usando ``sendMessage``.

    O corpo usa ``parse_mode='HTML'``. O texto já deve vir escapado
    (:func:`escape_html`); tags ``<b>`` e ``<i>`` são as únicas marcações.

    Returns:
        ``True`` só quando a API responde HTTP 200 e ``ok: true``.
        Qualquer falha de rede, HTTP ou ``ok: false`` devolve ``False``
        (o chamador decide se tenta de novo).
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status != 200:
                    body_text = await response.text()
                    print(
                        "[telegram_notifier] Falha na transmissão da telemetria: "
                        f"HTTP {response.status} — resposta: {body_text[:500]!r}"
                    )
                    return False

                try:
                    data: Optional[dict[str, Any]] = await response.json()
                except aiohttp.ContentTypeError:
                    data = None

                if not isinstance(data, dict) or not data.get("ok", False):
                    print(
                        "[telegram_notifier] Falha na transmissão da telemetria: "
                        f"API Telegram retornou ok=false ou corpo inesperado — {data!r}"
                    )
                    return False
                return True
    except aiohttp.ClientError as exc:
        print(
            "[telegram_notifier] Falha na transmissão da telemetria (rede/HTTP client): "
            f"{exc!r}"
        )
        return False
    except Exception as exc:  # noqa: BLE001 — falha de envio não pode parecer sucesso
        print(
            "[telegram_notifier] Falha na transmissão da telemetria (inesperada): "
            f"{exc!r}"
        )
        return False
