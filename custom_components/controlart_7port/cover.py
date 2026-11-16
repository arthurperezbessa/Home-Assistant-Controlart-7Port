"""Plataforma cover (cortina/persiana) da integração ControlArt 7Port.

Cada cortina é um subentry da entrada do 7Port. A entidade:

- Envia comandos IR/RF de abrir, fechar e parar via 7Port.
- Rastreia o estado de forma otimista (não há feedback de posição via IR).
- Bloqueia o fechamento quando um sensor de janela indica que a janela
  atrelada à cortina está aberta.
- Restaura o último estado após reinício do Home Assistant.
- Opcionalmente usa uma cortina já integrada (ex.: Tuya Wi-Fi) como
  entidade principal: enquanto ela está disponível, os comandos vão para ela
  e o estado, a posição e as features são espelhados dela. Os códigos da
  7Port só são enviados quando a principal fica indisponível.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_CLOSED,
    STATE_CLOSING,
    STATE_OPEN,
    STATE_OPENING,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from . import SevenPortConfigEntry
from .const import (
    CMD_CLOSE,
    CMD_DOWN,
    CMD_OPEN,
    CMD_STOP,
    CMD_UP,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_IR_PORT,
    CONF_PRIMARY_ENTITY,
    CONF_WINDOW_SENSOR,
    DEVICE_TYPE_COVER,
    DEVICE_TYPE_FLAP,
    DOMAIN,
)
from .device_db import DeviceDefinition, async_get_database
from .tcp import SevenPortClient, SevenPortError

_LOGGER = logging.getLogger(__name__)

# Features da entidade principal que a cortina espelha e encaminha.
_PRIMARY_FEATURES = (
    CoverEntityFeature.OPEN
    | CoverEntityFeature.CLOSE
    | CoverEntityFeature.STOP
    | CoverEntityFeature.SET_POSITION
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SevenPortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Cria as entidades cover a partir dos subentries da entrada."""
    database = await async_get_database(hass)
    client = entry.runtime_data.client

    for subentry_id, subentry in entry.subentries.items():
        device_type = subentry.data.get(CONF_DEVICE_TYPE)
        if device_type not in (DEVICE_TYPE_COVER, DEVICE_TYPE_FLAP):
            continue
        definition = database.get(subentry.data.get(CONF_DEVICE_ID, ""))
        if definition is None:
            _LOGGER.warning(
                "Subentry '%s' referencia uma definição inexistente (%s); ignorado.",
                subentry.title,
                subentry.data.get(CONF_DEVICE_ID),
            )
            continue

        entity_class = (
            SevenPortFlap if device_type == DEVICE_TYPE_FLAP else SevenPortCover
        )
        entity = entity_class(
            entry_id=entry.entry_id,
            subentry_id=subentry_id,
            name=subentry.title,
            options=dict(subentry.data),
            definition=definition,
            client=client,
        )
        async_add_entities([entity], config_subentry_id=subentry_id)


async def _async_send_command(
    client: SevenPortClient,
    definition: DeviceDefinition,
    ir_port: int,
    cmd: str,
    entity_name: Any,
) -> None:
    """Resolve o código de um comando na definição e envia para a 7Port."""
    code = definition.command(cmd)
    if not code:
        _LOGGER.warning(
            "'%s' não tem código para o comando '%s'.", entity_name, cmd
        )
        return
    try:
        await client.async_send_code(ir_port, code)
    except SevenPortError as err:
        _LOGGER.error(
            "Falha ao enviar '%s' para '%s': %s", cmd, entity_name, err
        )


class SevenPortCover(CoverEntity, RestoreEntity):
    """Entidade de cortina/persiana controlada via IR/RF pelo 7Port."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_device_class = CoverDeviceClass.CURTAIN

    def __init__(
        self,
        *,
        entry_id: str,
        subentry_id: str,
        name: str,
        options: dict[str, Any],
        definition: DeviceDefinition,
        client: SevenPortClient,
    ) -> None:
        """Inicializa a entidade."""
        self._entry_id = entry_id
        self._definition = definition
        self._client = client
        self._ir_port: int = int(options[CONF_IR_PORT])
        self._window_sensor_id: str | None = options.get(CONF_WINDOW_SENSOR) or None
        self._primary_entity_id: str | None = (
            options.get(CONF_PRIMARY_ENTITY) or None
        )
        self._unsub_primary: Any = None

        self._attr_unique_id = f"{subentry_id}_cover"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry_id)},
            name=name,
            manufacturer=definition.brand,
            model=definition.model,
            via_device=(DOMAIN, entry_id),
        )

        # Estado inicial desconhecido — RestoreEntity preenche no async_added_to_hass.
        self._is_closed: bool | None = None

        # Features da 7Port: open + close sempre; stop apenas se houver código.
        features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        if definition.command(CMD_STOP):
            features |= CoverEntityFeature.STOP
        self._base_features = features

    # -- Ciclo de vida --------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restaura o último estado e subscreve à entidade principal, se houver."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            if last.state == STATE_CLOSED:
                self._is_closed = True
            elif last.state in (STATE_OPEN, STATE_OPENING, STATE_CLOSING):
                self._is_closed = False
            # Qualquer outro estado (unknown, unavailable) → mantém None.

        if self._primary_entity_id:
            self._unsub_primary = async_track_state_change_event(
                self.hass, [self._primary_entity_id], self._async_primary_changed
            )
            self._sync_from_primary(self._primary_state())

    async def async_will_remove_from_hass(self) -> None:
        """Cancela a subscrição à entidade principal."""
        if self._unsub_primary is not None:
            self._unsub_primary()
            self._unsub_primary = None

    @callback
    def _async_primary_changed(self, event: Event[EventStateChangedData]) -> None:
        """Callback chamado quando a entidade principal muda de estado."""
        self._sync_from_primary(self._primary_state())
        self.async_write_ha_state()

    def _sync_from_primary(self, state: State | None) -> None:
        """Guarda o último estado da principal para continuar dele no fallback."""
        if state is None:
            return
        if state.state in (STATE_CLOSED, STATE_CLOSING):
            self._is_closed = True
        elif state.state in (STATE_OPEN, STATE_OPENING):
            self._is_closed = False

    # -- Propriedades ---------------------------------------------------------

    @property
    def is_closed(self) -> bool | None:
        """Retorna True se fechada, False se aberta, None se posição desconhecida."""
        primary = self._primary_state()
        if primary is not None:
            return primary.state == STATE_CLOSED
        return self._is_closed

    @property
    def is_opening(self) -> bool | None:
        """Espelha o movimento da principal; sem ela não há feedback."""
        primary = self._primary_state()
        return primary is not None and primary.state == STATE_OPENING

    @property
    def is_closing(self) -> bool | None:
        """Espelha o movimento da principal; sem ela não há feedback."""
        primary = self._primary_state()
        return primary is not None and primary.state == STATE_CLOSING

    @property
    def current_cover_position(self) -> int | None:
        """Posição da principal; a 7Port não informa posição."""
        primary = self._primary_state()
        if primary is None:
            return None
        return primary.attributes.get(ATTR_CURRENT_POSITION)

    @property
    def supported_features(self) -> CoverEntityFeature:
        """Features da principal enquanto disponível; senão, as da 7Port."""
        primary = self._primary_state()
        if primary is None:
            return self._base_features
        primary_features = CoverEntityFeature(
            int(primary.attributes.get("supported_features") or 0)
        )
        return (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | (primary_features & _PRIMARY_FEATURES)
        )

    @property
    def assumed_state(self) -> bool:
        """Estado real só existe enquanto a principal está disponível."""
        return self._primary_state() is None

    # -- Comandos -------------------------------------------------------------

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Abre a cortina pela principal ou, se ela estiver fora, pela 7Port."""
        if self._primary_state() is not None:
            await self._async_call_primary("open_cover")
            return
        await self._async_send_cmd(CMD_OPEN)
        self._is_closed = False
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Fecha a cortina pela principal ou, se ela estiver fora, pela 7Port.

        O comando é bloqueado se o sensor de janela configurado indicar que
        a janela está aberta (estado 'on' do binary_sensor).
        """
        if self._window_blocks_closing():
            return
        if self._primary_state() is not None:
            await self._async_call_primary("close_cover")
            return
        await self._async_send_cmd(CMD_CLOSE)
        self._is_closed = True
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Para o movimento da cortina."""
        primary = self._primary_state()
        if primary is not None and self._supports(primary, CoverEntityFeature.STOP):
            await self._async_call_primary("stop_cover")
            return
        await self._async_send_cmd(CMD_STOP)
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Leva a cortina a uma posição; só existe enquanto a principal suporta.

        Fechar parcialmente também é fechar: com a janela aberta, qualquer
        posição abaixo da atual é bloqueada.
        """
        position: int = kwargs[ATTR_POSITION]
        current = self.current_cover_position
        if (current is None or position < current) and self._window_blocks_closing():
            return

        if self._primary_state() is not None:
            await self._async_call_primary("set_cover_position", {ATTR_POSITION: position})
            return

        # A principal caiu entre a tela e o comando: a 7Port só abre ou fecha.
        if position == 0:
            await self._async_send_cmd(CMD_CLOSE)
            self._is_closed = True
        elif position == 100:
            await self._async_send_cmd(CMD_OPEN)
            self._is_closed = False
        else:
            _LOGGER.warning(
                "'%s': posição %s ignorada; sem a entidade principal a 7Port só "
                "abre ou fecha.",
                self.name,
                position,
            )
            return
        self.async_write_ha_state()

    # -- Utilitários ----------------------------------------------------------

    def _primary_state(self) -> State | None:
        """Estado da entidade principal, ou None se não houver ou estiver fora."""
        if not self._primary_entity_id:
            return None
        state = self.hass.states.get(self._primary_entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        return state

    @staticmethod
    def _supports(state: State, feature: CoverEntityFeature) -> bool:
        """Indica se a entidade anuncia suporte à feature."""
        return bool(int(state.attributes.get("supported_features") or 0) & feature)

    def _window_blocks_closing(self) -> bool:
        """True (e registra no log) se o sensor de janela indica janela aberta."""
        if not self._window_sensor_id:
            return False
        sensor = self.hass.states.get(self._window_sensor_id)
        if sensor is None or sensor.state != "on":
            return False
        _LOGGER.warning(
            "Fechamento da cortina '%s' bloqueado: sensor de janela '%s' "
            "indica que a janela está aberta.",
            self.name,
            self._window_sensor_id,
        )
        return True

    async def _async_call_primary(
        self, service: str, extra: dict[str, Any] | None = None
    ) -> None:
        """Chama um serviço cover na entidade principal."""
        data: dict[str, Any] = {ATTR_ENTITY_ID: self._primary_entity_id}
        if extra:
            data.update(extra)
        await self.hass.services.async_call("cover", service, data, blocking=False)

    async def _async_send_cmd(self, cmd: str) -> None:
        """Envia um código de comando (IR ou RF) para a 7Port."""
        await _async_send_command(
            self._client, self._definition, self._ir_port, cmd, self.name
        )


class SevenPortFlap(CoverEntity, RestoreEntity):
    """Eixo vertical de um flap motorizado de TV.

    Sobe e desce o flap via RF/IR. O giro horizontal fica em entidades
    `button` separadas, já que o Home Assistant não representa movimento
    lateral em um cover — as setas de um cover são sempre cima/baixo.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_device_class = CoverDeviceClass.SHUTTER

    def __init__(
        self,
        *,
        entry_id: str,
        subentry_id: str,
        name: str,
        options: dict[str, Any],
        definition: DeviceDefinition,
        client: SevenPortClient,
    ) -> None:
        """Inicializa a entidade."""
        self._definition = definition
        self._client = client
        self._ir_port: int = int(options[CONF_IR_PORT])

        self._attr_unique_id = f"{subentry_id}_flap"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry_id)},
            name=name,
            manufacturer=definition.brand,
            model=definition.model,
            via_device=(DOMAIN, entry_id),
        )

        self._is_closed: bool | None = None

        features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        if definition.command(CMD_STOP):
            features |= CoverEntityFeature.STOP
        self._attr_supported_features = features

    async def async_added_to_hass(self) -> None:
        """Restaura a última posição conhecida após reinício do HA."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            if last.state == STATE_CLOSED:
                self._is_closed = True
            elif last.state in (STATE_OPEN, "opening", "closing"):
                self._is_closed = False

    @property
    def is_closed(self) -> bool | None:
        """True se recolhido (embaixo), False se estendido (em cima)."""
        return self._is_closed

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Sobe o flap."""
        await self._async_send_cmd(CMD_UP)
        self._is_closed = False
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Desce o flap."""
        await self._async_send_cmd(CMD_DOWN)
        self._is_closed = True
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Para o movimento do flap."""
        await self._async_send_cmd(CMD_STOP)
        self.async_write_ha_state()

    async def _async_send_cmd(self, cmd: str) -> None:
        """Envia um código de comando (IR ou RF) para a 7Port."""
        await _async_send_command(
            self._client, self._definition, self._ir_port, cmd, self.name
        )
