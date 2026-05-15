"""Notificação assíncrona via API Bot do Telegram."""

from __future__ import annotations

from typing import Any, Optional

import aiohttp


async def send_telegram_alert(token: str, chat_id: str, message: str) -> None:
    """Envia uma mensagem ao chat configurado usando a API ``sendMessage`` do Telegram.

    A requisição é não bloqueante (aiohttp + async/await). O corpo usa
    ``parse_mode='Markdown'`` para permitir formatação futura (negrito/itálico).

    Args:
        token: Token do bot (``@BotFather``).
        chat_id: Identificador do chat ou canal de destino.
        message: Texto da mensagem (Markdown quando aplicável).

    Note:
        Em falha HTTP ou quando a API retorna ``ok: false`` no JSON, o erro é
        impresso no terminal (telemetria da transmissão).
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
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
                    return

                try:
                    data: Optional[dict[str, Any]] = await response.json()
                except aiohttp.ContentTypeError:
                    data = None

                if not isinstance(data, dict) or not data.get("ok", False):
                    print(
                        "[telegram_notifier] Falha na transmissão da telemetria: "
                        f"API Telegram retornou ok=false ou corpo inesperado — {data!r}"
                    )
    except aiohttp.ClientError as exc:
        print(
            "[telegram_notifier] Falha na transmissão da telemetria (rede/HTTP client): "
            f"{exc!r}"
        )
