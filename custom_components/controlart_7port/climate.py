"""Plataforma climate (ar-condicionado) da integração ControlArt 7Port.

Cada aparelho de climatização é um subentry da entrada do 7Port. A entidade
mantém o estado "comandado" (modo, temperatura, ventilação, swing) e, a cada
mudança, envia o código IR completo correspondente — pois cada combinação
modo+temperatura+ventilação é um código único.

Comportamento de ligar:
- `stateful`: o próprio código de estado liga o aparelho (ex.: Carrier).
- `explicit_on`: é preciso enviar o código "ligar" antes do estado (ex.: LG),
  respeitando um pequeno atraso configurável.

O código "desligar" é sempre enviado quando o modo OFF é selecionado, mesmo
que a entidade já se considere desligada — assim, se o aparelho foi ligado
pelo controle físico, o desligar do HA ainda surte efeito.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from . import SevenPortConfigEntry
from .const import (
    CMD_LIGHT_OFF,
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_SWING_OFF,
    CMD_SWING_ON,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENABLE_LIGHT_OFF,
    CONF_ENABLE_SWING,
    CONF_ENABLED_HVAC_MODES,
    CONF_IR_PORT,
    CONF_LIGHT_OFF_BEHAVIOR,
    CONF_ON_DELAY,
    CONF_POWER_BEHAVIOR,
    CONF_POWER_SENSOR,
    CONF_POWER_THRESHOLD,
    DEFAULT_ON_DELAY,
    DEFAULT_POWER_THRESHOLD,
    DEVICE_TYPE_CLIMATE,
    DOMAIN,
    LIGHT_OFF_ALWAYS,
    LIGHT_OFF_NEVER,
    LIGHT_OFF_ONCE,
    POWER_EXPLICIT_ON,
    SWING_SEPARATE,
)
from .device_db import DeviceDefinition, async_get_database
from .tcp import SevenPortClient, SevenPortError

_LOGGER = logging.getLogger(__name__)

# Mapa: modo do banco de dados -> HVACMode do Home Assistant.
_DB_TO_HVAC: dict[str, HVACMode] = {
    "cool": HVACMode.COOL,
    "heat": HVACMode.HEAT,
    "dry": HVACMode.DRY,
    "fan_only": HVACMode.FAN_ONLY,
}
_HVAC_TO_DB: dict[HVACMode, str] = {v: k for k, v in _DB_TO_HVAC.items()}

_HVAC_ACTION: dict[HVACMode, HVACAction] = {
    HVACMode.COOL: HVACAction.COOLING,
    HVACMode.HEAT: HVACAction.HEATING,
    HVACMode.DRY: HVACAction.DRYING,
    HVACMode.FAN_ONLY: HVACAction.FAN,
}

SWING_ON = "on"
SWING_OFF = "off"

# Estados textuais comuns interpretados como "ligado"/"desligado".
_TRUE_STATES = {"on", "open", "true", "home", "detected", "heat", "cool", "1"}
_FALSE_STATES = {"off", "closed", "false", "not_home", "clear", "idle", "0"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SevenPortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Cria as entidades climate a partir dos subentries da entrada."""
    database = await async_get_database(hass)
    client = entry.runtime_data.client

    for subentry_id, subentry in entry.subentries.items():
        if subentry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_CLIMATE:
            continue
        definition = database.get(subentry.data.get(CONF_DEVICE_ID, ""))
        if definition is None:
            _LOGGER.warning(
                "Subentry '%s' referencia uma definição inexistente (%s); ignorado.",
                subentry.title,
                subentry.data.get(CONF_DEVICE_ID),
            )
            continue

        entity = SevenPortClimate(
            entry_id=entry.entry_id,
            subentry_id=subentry_id,
            name=subentry.title,
            options=dict(subentry.data),
            definition=definition,
            client=client,
        )
        async_add_entities([entity], config_subentry_id=subentry_id)


class SevenPortClimate(ClimateEntity, RestoreEntity):
    """Entidade de ar-condicionado controlada via 7Port."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

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
        """Inicializa a entidade a partir do subentry e da definição."""
        self._entry_id = entry_id
        self._definition = definition
        self._client = client
        self._options = options

        self._ir_port: int = int(options[CONF_IR_PORT])
        self._power_behavior: str = options.get(
            CONF_POWER_BEHAVIOR, definition.power_behavior
        )
        self._on_delay: float = float(options.get(CONF_ON_DELAY, DEFAULT_ON_DELAY))
        # Migração: configs antigas usavam CONF_ENABLE_LIGHT_OFF (bool).
        _raw_behavior = options.get(CONF_LIGHT_OFF_BEHAVIOR)
        if _raw_behavior is None:
            _raw_behavior = (
                LIGHT_OFF_ALWAYS
                if options.get(CONF_ENABLE_LIGHT_OFF)
                else LIGHT_OFF_NEVER
            )
        self._light_off_behavior: str = (
            _raw_behavior if definition.has_light_off else LIGHT_OFF_NEVER
        )
        self._light_off_sent: bool = False
        self._swing_enabled: bool = bool(
            options.get(CONF_ENABLE_SWING, False)
        ) and definition.has_swing
        self._power_sensor: str | None = options.get(CONF_POWER_SENSOR) or None
        self._power_threshold: float = float(
            options.get(CONF_POWER_THRESHOLD, DEFAULT_POWER_THRESHOLD)
        )

        # Identidade.
        self._attr_unique_id = f"{subentry_id}_climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry_id)},
            name=name,
            manufacturer=definition.brand,
            model=definition.model,
            via_device=(DOMAIN, entry_id),
        )

        # Modos HVAC expostos = OFF + os habilitados pelo usuário (na ordem
        # do banco de dados, limitados aos que possuem códigos).
        enabled = options.get(CONF_ENABLED_HVAC_MODES) or definition.hvac_modes
        self._db_modes = [m for m in definition.hvac_modes if m in enabled]
        if not self._db_modes:
            self._db_modes = list(definition.hvac_modes)
        self._attr_hvac_modes = [HVACMode.OFF] + [
            _DB_TO_HVAC[m] for m in self._db_modes if m in _DB_TO_HVAC
        ]

        self._attr_fan_modes = list(definition.fan_modes)
        self._attr_min_temp = definition.min_temp
        self._attr_max_temp = definition.max_temp
        self._attr_target_temperature_step = definition.temp_step

        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self._swing_enabled:
            features |= ClimateEntityFeature.SWING_MODE
            self._attr_swing_modes = [SWING_OFF, SWING_ON]
        self._attr_supported_features = features

        # Sem sensor de feedback a entidade é "otimista".
        self._attr_assumed_state = self._power_sensor is None

        # Estado comandado (interno).
        first_mode = self._attr_hvac_modes[1] if len(self._attr_hvac_modes) > 1 else HVACMode.OFF
        self._commanded_mode: HVACMode = HVACMode.OFF
        self._last_active_mode: HVACMode = first_mode
        # Crença sobre o aparelho estar ligado (para o modo explicit_on).
        self._powered: bool = False
        self._target_temp: float = float(
            min(max(24, definition.min_temp), definition.max_temp)
        )
        self._fan_mode: str = (
            "auto" if "auto" in definition.fan_modes else definition.fan_modes[0]
        )
        self._swing_mode: str = SWING_OFF
        self._unsub_sensor = None

    # -- Restauração de estado e listeners ---------------------------------

    async def async_added_to_hass(self) -> None:
        """Restaura o último estado e ativa o listener do sensor."""
        await super().async_added_to_hass()

        if (last := await self.async_get_last_state()) is not None:
            try:
                if last.state in self._db_state_values():
                    restored = HVACMode(last.state)
                    self._commanded_mode = restored
                    if restored != HVACMode.OFF:
                        self._last_active_mode = restored
                    self._powered = restored != HVACMode.OFF
            except ValueError:
                pass
            if (temp := last.attributes.get("temperature")) is not None:
                self._target_temp = float(temp)
            if (fan := last.attributes.get("fan_mode")) in self._attr_fan_modes:
                self._fan_mode = fan
            if (swing := last.attributes.get("swing_mode")) in (SWING_ON, SWING_OFF):
                self._swing_mode = swing

        if self._power_sensor:
            self._unsub_sensor = async_track_state_change_event(
                self.hass, [self._power_sensor], self._async_sensor_changed
            )

    async def async_will_remove_from_hass(self) -> None:
        """Remove o listener do sensor ao descarregar a entidade."""
        if self._unsub_sensor is not None:
            self._unsub_sensor()
            self._unsub_sensor = None

    def _db_state_values(self) -> set[str]:
        """Valores de estado válidos para restauração."""
        return {m.value for m in self._attr_hvac_modes}

    @callback
    def _async_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        """Atualiza a entidade quando o sensor de energia muda."""
        self.async_write_ha_state()

    # -- Leitura de estado --------------------------------------------------

    def _sensor_power_state(self) -> bool | None:
        """Interpreta o sensor de energia: True=ligado, False=desligado."""
        if not self._power_sensor:
            return None
        state = self.hass.states.get(self._power_sensor)
        if state is None or state.state in (None, "unknown", "unavailable"):
            return None
        value = str(state.state).strip().lower()
        if value in _TRUE_STATES:
            return True
        if value in _FALSE_STATES:
            return False
        try:
            return float(value) > self._power_threshold
        except ValueError:
            return None

    @property
    def hvac_mode(self) -> HVACMode:
        """Modo HVAC exibido (considera o sensor de energia, se houver)."""
        sensor = self._sensor_power_state()
        if sensor is False:
            return HVACMode.OFF
        if sensor is True and self._commanded_mode == HVACMode.OFF:
            # Ligado fisicamente: mostra o último modo ativo conhecido.
            return self._last_active_mode
        return self._commanded_mode

    @property
    def hvac_action(self) -> HVACAction:
        """Ação atual do aparelho (derivada do modo exibido)."""
        mode = self.hvac_mode
        if mode == HVACMode.OFF:
            return HVACAction.OFF
        return _HVAC_ACTION.get(mode, HVACAction.IDLE)

    @property
    def target_temperature(self) -> float:
        """Temperatura alvo comandada."""
        return self._target_temp

    @property
    def fan_mode(self) -> str:
        """Velocidade de ventilação comandada."""
        return self._fan_mode

    @property
    def swing_mode(self) -> str | None:
        """Estado de swing comandado."""
        return self._swing_mode if self._swing_enabled else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Atributos auxiliares úteis para diagnóstico."""
        return {
            "ir_port": self._ir_port,
            "power_behavior": self._power_behavior,
            "device_definition": self._definition.id,
        }

    # -- Comandos -----------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Define o modo HVAC (inclui ligar/desligar)."""
        if hvac_mode == HVACMode.OFF:
            await self._async_turn_off()
            return
        if hvac_mode not in self._attr_hvac_modes:
            raise ValueError(f"Modo HVAC não suportado: {hvac_mode}")
        self._commanded_mode = hvac_mode
        self._last_active_mode = hvac_mode
        await self._async_apply_state()

    async def async_turn_on(self) -> None:
        """Liga o aparelho restaurando o último modo ativo."""
        self._commanded_mode = self._last_active_mode
        await self._async_apply_state()

    async def async_turn_off(self) -> None:
        """Desliga o aparelho."""
        await self._async_turn_off()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Define a temperatura alvo (e, opcionalmente, o modo)."""
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            self._target_temp = float(
                min(max(temp, self._attr_min_temp), self._attr_max_temp)
            )
        mode = kwargs.get("hvac_mode")
        if mode is not None and mode != HVACMode.OFF:
            self._commanded_mode = mode
            self._last_active_mode = mode

        if self._commanded_mode == HVACMode.OFF:
            # Ajuste de temperatura com o aparelho desligado: apenas guarda
            # o valor; será usado quando o aparelho for ligado.
            self.async_write_ha_state()
            return
        await self._async_apply_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Define a velocidade de ventilação."""
        if fan_mode not in self._attr_fan_modes:
            raise ValueError(f"Velocidade não suportada: {fan_mode}")
        self._fan_mode = fan_mode
        if self._commanded_mode == HVACMode.OFF:
            self.async_write_ha_state()
            return
        await self._async_apply_state()

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Define o estado de swing (oscilação das pás)."""
        if not self._swing_enabled:
            return
        self._swing_mode = swing_mode
        definition = self._definition
        if definition.swing_mode == SWING_SEPARATE:
            key = CMD_SWING_ON if swing_mode == SWING_ON else CMD_SWING_OFF
            await self._async_send_command(definition.command(key))
        elif self._commanded_mode != HVACMode.OFF:
            # Swing embutido na matriz de estados: reaplica o estado.
            await self._async_apply_state()
        self.async_write_ha_state()

    # -- Execução -----------------------------------------------------------

    async def _async_turn_off(self) -> None:
        """Envia o código de desligar (sempre, por segurança)."""
        was_on = self._commanded_mode != HVACMode.OFF
        self._commanded_mode = HVACMode.OFF
        self._powered = False
        definition = self._definition
        sent = await self._async_send_command(definition.command(CMD_POWER_OFF))
        if not sent:
            _LOGGER.warning(
                "Aparelho '%s' sem código de desligar configurado.",
                self._definition.label,
            )
        # Apaga a luz do display após desligar, se habilitado.
        if was_on or sent:
            await self._async_send_light_off()
        self._light_off_sent = False  # reseta o ciclo para o próximo ligar
        self.async_write_ha_state()

    async def _async_apply_state(self) -> None:
        """Liga (se necessário) e envia o código do estado atual."""
        definition = self._definition
        mode_key = _HVAC_TO_DB.get(self._commanded_mode)
        if mode_key is None:
            _LOGGER.error("Modo HVAC sem mapeamento: %s", self._commanded_mode)
            return

        # Garante que o aparelho esteja ligado antes do estado, se preciso.
        # O código de ligar só é enviado na transição desligado -> ligado.
        # Se houver um sensor de energia, ele tem prioridade sobre a crença
        # interna (cobre o caso de ligar/desligar pelo controle físico).
        if self._power_behavior == POWER_EXPLICIT_ON:
            needs_power_on = not self._powered
            sensor = self._sensor_power_state()
            if sensor is True:
                needs_power_on = False
            elif sensor is False:
                needs_power_on = True
            if needs_power_on:
                power_on = definition.command(CMD_POWER_ON)
                if power_on:
                    await self._async_send_command(power_on)
                    await asyncio.sleep(self._on_delay)
                else:
                    _LOGGER.warning(
                        "Aparelho '%s' marcado como 'explicit_on' mas sem "
                        "código de ligar; enviando apenas o estado.",
                        definition.label,
                    )

        just_turned_on = not self._powered

        temp = int(round(self._target_temp))
        code = definition.state_code(mode_key, self._fan_mode, temp)
        if code is None:
            _LOGGER.warning(
                "Sem código IR para %s/%s/%s°C no aparelho '%s'.",
                mode_key, self._fan_mode, temp, definition.label,
            )
        else:
            await self._async_send_command(code)
        self._powered = True

        await self._async_send_light_off(just_turned_on=just_turned_on)
        self.async_write_ha_state()

    async def _async_send_light_off(self, *, just_turned_on: bool = False) -> None:
        """Envia o comando de apagar a luz conforme o comportamento configurado.

        - never:  não faz nada.
        - once:   envia apenas na primeira vez que o aparelho liga no ciclo.
        - always: envia após cada comando IR.
        """
        if self._light_off_behavior == LIGHT_OFF_NEVER:
            return
        if self._light_off_behavior == LIGHT_OFF_ONCE:
            if self._light_off_sent and not just_turned_on:
                return
        await self._async_send_command(self._definition.command(CMD_LIGHT_OFF))
        self._light_off_sent = True

    async def _async_send_command(self, code: str | None) -> bool:
        """Envia um código IR pela porta configurada. Retorna sucesso."""
        if not code:
            return False
        try:
            await self._client.async_send_ir(self._ir_port, code)
        except SevenPortError as err:
            _LOGGER.error(
                "Falha ao enviar comando para '%s' (porta %s): %s",
                self._definition.label, self._ir_port, err,
            )
            return False
        return True
