"""Comandos avulsos da 7Port expostos como actions.

Cada subentry do tipo ``command`` registra a action
``controlart_7port.<service>``, que envia a string configurada, inteira, para
a 7Port — o equivalente a um ``shell_command`` com ``echo ... | nc``, mas
usando a conexão TCP que a integração já mantém.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service import async_set_service_schema

from .const import CONF_COMMAND, CONF_DEVICE_TYPE, CONF_SERVICE, DEVICE_TYPE_COMMAND, DOMAIN
from .tcp import SevenPortClient, SevenPortError

_LOGGER = logging.getLogger(__name__)


def async_register_commands(
    hass: HomeAssistant, entry: ConfigEntry, client: SevenPortClient
) -> list[str]:
    """Registra uma action por comando avulso da entrada; retorna os nomes."""
    registered: list[str] = []
    for subentry in entry.subentries.values():
        if subentry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_COMMAND:
            continue
        service = subentry.data[CONF_SERVICE]
        # O fluxo de configuração já impede nomes repetidos; isto cobre uma
        # edição manual do storage.
        if hass.services.has_service(DOMAIN, service):
            _LOGGER.warning(
                "Action '%s.%s' já existe; comando '%s' ignorado.",
                DOMAIN, service, subentry.title,
            )
            continue
        hass.services.async_register(
            DOMAIN,
            service,
            _make_handler(client, subentry.data[CONF_COMMAND], subentry.title),
            schema=vol.Schema({}),
        )
        async_set_service_schema(
            hass,
            DOMAIN,
            service,
            {
                "name": subentry.title,
                "description": f"Envia um comando avulso para a 7Port {entry.title}.",
                "fields": {},
            },
        )
        registered.append(service)
    return registered


def async_unregister_commands(hass: HomeAssistant, services: list[str]) -> None:
    """Remove as actions registradas por uma entrada."""
    for service in services:
        hass.services.async_remove(DOMAIN, service)


def _make_handler(
    client: SevenPortClient, command: str, title: str
) -> Callable[[ServiceCall], Awaitable[None]]:
    async def _handle(call: ServiceCall) -> None:
        try:
            await client.async_send_raw(command)
        except SevenPortError as err:
            raise HomeAssistantError(
                f"Falha ao enviar o comando '{title}': {err}"
            ) from err

    return _handle
