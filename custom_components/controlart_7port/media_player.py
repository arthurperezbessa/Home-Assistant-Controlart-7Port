"""Plataforma media_player (TV) da integração ControlArt 7Port.

Cada TV é um subentry da entrada do 7Port. A entidade envolve uma entidade
de media_player já integrada no Home Assistant (backing entity) e:

- Substitui ligar, desligar e seleção de source por comandos IR via 7Port.
- Espelha o estado e os atributos (volume, source atual, título, etc.) da
  entidade original.
- Quando a entidade original fica unavailable (TV desligada da rede), a
  entidade wrapper aparece como OFF — não unavailable.
- Comandos de volume, play/pause e demais funções são encaminhados
  diretamente para a entidade original via serviço do HA.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_VOLUME_DOWN,
    SERVICE_VOLUME_MUTE,
    SERVICE_VOLUME_SET,
    SERVICE_VOLUME_UP,
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from . import SevenPortConfigEntry
from .const import (
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CONF_BACKING_ENTITY,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_IR_PORT,
    CONF_MEDIA_PLAYER_CLASS,
    DEVICE_TYPE_TV,
    DOMAIN,
)
from .device_db import DeviceDefinition, async_get_database
from .tcp import SevenPortClient, SevenPortError

_LOGGER = logging.getLogger(__name__)

# Estados da backing entity interpretados como "TV desligada / indisponível".
_OFF_STATES = {STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN, "standby"}

# Mapa de estado textual → MediaPlayerState.
_STATE_MAP: dict[str, MediaPlayerState] = {
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
    "idle": MediaPlayerState.IDLE,
    "buffering": MediaPlayerState.BUFFERING,
    "on": MediaPlayerState.ON,
}

# Features de volume — encaminhadas à backing entity se ela as suportar.
_VOLUME_FEATURES = (
    MediaPlayerEntityFeature.VOLUME_SET
    | MediaPlayerEntityFeature.VOLUME_MUTE
    | MediaPlayerEntityFeature.VOLUME_STEP
)

# Features de mídia — encaminhadas à backing entity se ela as suportar.
_MEDIA_FEATURES = (
    MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.SEEK
    | MediaPlayerEntityFeature.SHUFFLE_SET
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.SELECT_SOUND_MODE
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SevenPortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Cria as entidades media_player a partir dos subentries da entrada."""
    database = await async_get_database(hass)
    client = entry.runtime_data.client

    for subentry_id, subentry in entry.subentries.items():
        if subentry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_TV:
            continue
        definition = database.get(subentry.data.get(CONF_DEVICE_ID, ""))
        if definition is None:
            _LOGGER.warning(
                "Subentry '%s' referencia uma definição de TV inexistente (%s); ignorado.",
                subentry.title,
                subentry.data.get(CONF_DEVICE_ID),
            )
            continue

        entity = SevenPortTV(
            entry_id=entry.entry_id,
            subentry_id=subentry_id,
            name=subentry.title,
            options=dict(subentry.data),
            definition=definition,
            client=client,
        )
        async_add_entities([entity], config_subentry_id=subentry_id)


class SevenPortTV(MediaPlayerEntity, RestoreEntity):
    """Entidade de TV controlada via IR pelo 7Port."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

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
        self._backing_entity_id: str = options[CONF_BACKING_ENTITY]

        # Device class: tv (padrão), receiver ou speaker.
        _class_str = options.get(CONF_MEDIA_PLAYER_CLASS, "tv")
        try:
            self._attr_device_class = MediaPlayerDeviceClass(_class_str)
        except ValueError:
            self._attr_device_class = MediaPlayerDeviceClass.TV

        self._attr_unique_id = f"{subentry_id}_tv"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry_id)},
            name=name,
            manufacturer=definition.brand,
            model=definition.model,
            via_device=(DOMAIN, entry_id),
        )

        # Sources IR definidos pelo usuário (prioridade na lista mesclada).
        self._ir_sources: list[str] = list(definition.source_list)
        self._ir_source_set: set[str] = set(self._ir_sources)
        self._attr_source_list: list[str] = list(self._ir_sources)

        # Estado inicial — off até a backing entity ser consultada.
        self._attr_state = MediaPlayerState.OFF
        self._attr_source = None
        self._attr_volume_level: float | None = None
        self._attr_is_volume_muted: bool | None = None
        self._attr_media_title: str | None = None
        self._attr_app_name: str | None = None

        # Features base: ligar, desligar e (se houver) sources IR.
        base = (
            MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
        )
        if self._ir_sources:
            base |= MediaPlayerEntityFeature.SELECT_SOURCE
        self._base_features = base
        self._attr_supported_features = base

        self._unsub_backing: Any = None

    # -- Ciclo de vida ---------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Subscreve às mudanças da backing entity e inicializa o estado."""
        await super().async_added_to_hass()
        self._unsub_backing = async_track_state_change_event(
            self.hass, [self._backing_entity_id], self._async_backing_changed
        )
        # Aplica o estado atual da backing entity imediatamente.
        self._update_from_backing(self.hass.states.get(self._backing_entity_id))

    async def async_will_remove_from_hass(self) -> None:
        """Cancela a subscrição ao remover a entidade."""
        if self._unsub_backing is not None:
            self._unsub_backing()
            self._unsub_backing = None

    @callback
    def _async_backing_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Callback chamado quando a backing entity muda de estado."""
        self._update_from_backing(event.data.get("new_state"))
        self.async_write_ha_state()

    # -- Sincronização com a backing entity ------------------------------------

    def _update_from_backing(self, state: Any) -> None:
        """Atualiza atributos locais a partir do estado da backing entity."""
        if state is None or state.state in _OFF_STATES:
            self._attr_state = MediaPlayerState.OFF
            self._attr_source = None
            self._attr_volume_level = None
            self._attr_is_volume_muted = None
            self._attr_media_title = None
            self._attr_app_name = None
            self._attr_source_list = list(self._ir_sources)
            self._attr_supported_features = self._base_features
            return

        self._attr_state = _STATE_MAP.get(state.state, MediaPlayerState.ON)

        attrs = state.attributes
        self._attr_source = attrs.get("source")
        self._attr_volume_level = attrs.get("volume_level")
        self._attr_is_volume_muted = attrs.get("is_volume_muted")
        self._attr_media_title = attrs.get("media_title")
        self._attr_app_name = attrs.get("app_name")

        # Mescla sources IR (prioridade) + sources da backing entity não cobertos por IR.
        backing_sources: list[str] = list(attrs.get("source_list") or [])
        extra = [s for s in backing_sources if s not in self._ir_source_set]
        self._attr_source_list = list(self._ir_sources) + extra

        # Espelha as features de volume e mídia da backing entity.
        backing_features = int(attrs.get("supported_features") or 0)
        features = (
            self._base_features
            | (backing_features & (_VOLUME_FEATURES | _MEDIA_FEATURES))
        )
        if self._attr_source_list:
            features |= MediaPlayerEntityFeature.SELECT_SOURCE
        self._attr_supported_features = features

    # -- Comandos IR -----------------------------------------------------------

    async def async_turn_on(self) -> None:
        """Liga via IR; se não houver código, repassa para a backing entity."""
        await self._async_send_ir_cmd(CMD_POWER_ON, fallback="turn_on")

    async def async_turn_off(self) -> None:
        """Desliga via IR; se não houver código, repassa para a backing entity."""
        await self._async_send_ir_cmd(CMD_POWER_OFF, fallback="turn_off")

    async def async_select_source(self, source: str) -> None:
        """Seleciona um source via IR — enviado mesmo se a TV ainda estiver offline."""
        code = self._definition.source_code(source)
        if code:
            await self._async_send_ir_code(code)
        else:
            # Source sem código IR: repassa para a backing entity.
            await self._async_call_backing(
                "select_source", {"source": source}
            )

    # -- Comandos encaminhados à backing entity --------------------------------

    async def async_volume_up(self) -> None:
        """Aumenta o volume via backing entity."""
        await self._async_call_backing(SERVICE_VOLUME_UP)

    async def async_volume_down(self) -> None:
        """Diminui o volume via backing entity."""
        await self._async_call_backing(SERVICE_VOLUME_DOWN)

    async def async_set_volume_level(self, volume: float) -> None:
        """Define o nível de volume via backing entity."""
        await self._async_call_backing(SERVICE_VOLUME_SET, {"volume_level": volume})

    async def async_mute_volume(self, mute: bool) -> None:
        """Silencia / desilencia via backing entity."""
        await self._async_call_backing(SERVICE_VOLUME_MUTE, {"is_volume_muted": mute})

    async def async_media_play(self) -> None:
        """Play via backing entity."""
        await self._async_call_backing("media_play")

    async def async_media_pause(self) -> None:
        """Pause via backing entity."""
        await self._async_call_backing("media_pause")

    async def async_media_stop(self) -> None:
        """Stop via backing entity."""
        await self._async_call_backing("media_stop")

    async def async_media_next_track(self) -> None:
        """Próxima faixa via backing entity."""
        await self._async_call_backing("media_next_track")

    async def async_media_previous_track(self) -> None:
        """Faixa anterior via backing entity."""
        await self._async_call_backing("media_previous_track")

    # -- Utilitários -----------------------------------------------------------

    async def _async_send_ir_cmd(
        self, cmd: str, fallback: str | None = None
    ) -> None:
        """Envia um código IR de comando; se ausente, repassa para a backing entity.

        ``fallback`` é o nome do serviço HA a chamar na backing entity quando
        não há código IR definido (ex.: ``"turn_on"``, ``"turn_off"``).
        Útil para receivers onde só o ligar é via IR.
        """
        code = self._definition.command(cmd)
        if code:
            await self._async_send_ir_code(code)
        elif fallback:
            await self._async_call_backing(fallback)
        else:
            _LOGGER.warning(
                "Aparelho '%s' não tem código IR para '%s'.",
                self._definition.label, cmd,
            )

    async def _async_send_ir_code(self, code: str) -> None:
        """Envia um payload IR pela porta configurada."""
        try:
            await self._client.async_send_ir(self._ir_port, code)
        except SevenPortError as err:
            _LOGGER.error(
                "Falha ao enviar IR para TV '%s' (porta %s): %s",
                self._definition.label, self._ir_port, err,
            )

    async def _async_call_backing(
        self, service: str, extra: dict[str, Any] | None = None
    ) -> None:
        """Chama um serviço media_player na backing entity."""
        if not self._backing_entity_id:
            return
        data: dict[str, Any] = {ATTR_ENTITY_ID: self._backing_entity_id}
        if extra:
            data.update(extra)
        await self.hass.services.async_call(
            "media_player", service, data, blocking=False
        )
