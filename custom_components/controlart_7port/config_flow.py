"""Fluxos de configuração da integração ControlArt 7Port.

- Fluxo principal: cadastra um equipamento 7Port (IP + porta TCP).
- Fluxo de subentry "device": adiciona um aparelho dentro de uma 7Port.
- Dentro do fluxo de subentry, um assistente permite criar uma nova
  definição de dispositivo colando os códigos IR capturados no 7Config.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BRAND,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_ENABLE_LIGHT_OFF,
    CONF_ENABLE_SWING,
    CONF_ENABLED_HVAC_MODES,
    CONF_HOST,
    CONF_IR_PORT,
    CONF_MODEL,
    CONF_NAME,
    CONF_ON_DELAY,
    CONF_PORT,
    CONF_POWER_BEHAVIOR,
    CONF_POWER_SENSOR,
    CONF_POWER_THRESHOLD,
    DEFAULT_ON_DELAY,
    DEFAULT_PORT,
    DEFAULT_POWER_THRESHOLD,
    DEVICE_TYPE_CLIMATE,
    DOMAIN,
    MAX_IR_PORT,
    MIN_IR_PORT,
    NEW_DEFINITION,
    POWER_BEHAVIORS,
    POWER_EXPLICIT_ON,
    POWER_STATEFUL,
    SUBENTRY_TYPE_DEVICE,
    SWING_MODES_DB,
    SWING_NONE,
    SWING_SEPARATE,
)
from .device_db import (
    DB_FAN_MODES,
    async_get_database,
    build_climate_definition,
    definition_to_yaml,
    expected_state_keys,
    parse_code_block,
)
from .tcp import SevenPortClient

_LOGGER = logging.getLogger(__name__)


class SevenPortConfigFlow(ConfigFlow, domain=DOMAIN):
    """Fluxo de configuração de um equipamento 7Port."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Cadastra a 7Port (nome, IP, porta)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            client = SevenPortClient(host, port)
            if not await client.async_test_connection():
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME].strip(),
                    data={CONF_HOST: host, CONF_PORT: port},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="7Port"): str,
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
                    int, vol.Range(min=1, max=65535)
                ),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Declara os tipos de subentry suportados (aparelhos)."""
        return {SUBENTRY_TYPE_DEVICE: DeviceSubentryFlow}


class DeviceSubentryFlow(ConfigSubentryFlow):
    """Fluxo para adicionar/editar um aparelho dentro de uma 7Port."""

    def __init__(self) -> None:
        """Inicializa o estado temporário do fluxo."""
        self._device_type: str = DEVICE_TYPE_CLIMATE
        self._brand: str | None = None
        self._device_id: str | None = None
        # Estado do assistente de criação de definição.
        self._new_meta: dict[str, Any] = {}

    # -- Adicionar aparelho ------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Passo inicial: escolha do tipo de dispositivo."""
        if user_input is not None:
            self._device_type = user_input[CONF_DEVICE_TYPE]
            return await self.async_step_brand()

        # Apenas climate está disponível nesta versão.
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE_TYPE, default=DEVICE_TYPE_CLIMATE
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[DEVICE_TYPE_CLIMATE],
                        translation_key="device_type",
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_brand(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Escolha da marca (ou criação de uma nova definição)."""
        database = await async_get_database(self.hass)
        brands = database.brands(self._device_type)

        if user_input is not None:
            choice = user_input[CONF_BRAND]
            if choice == NEW_DEFINITION:
                return await self.async_step_new_meta()
            self._brand = choice
            return await self.async_step_model()

        options = [
            selector.SelectOptionDict(value=b, label=b) for b in brands
        ]
        options.append(
            selector.SelectOptionDict(
                value=NEW_DEFINITION, label="➕ Criar nova definição…"
            )
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_BRAND): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options)
                )
            }
        )
        return self.async_show_form(step_id="brand", data_schema=schema)

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Escolha do modelo (ou criação de um novo)."""
        database = await async_get_database(self.hass)
        models = database.models(self._device_type, self._brand or "")

        if user_input is not None:
            choice = user_input[CONF_MODEL]
            if choice == NEW_DEFINITION:
                return await self.async_step_new_meta()
            self._device_id = choice
            return await self.async_step_configure()

        options = [
            selector.SelectOptionDict(value=m.id, label=m.model)
            for m in models
        ]
        options.append(
            selector.SelectOptionDict(
                value=NEW_DEFINITION, label="➕ Criar novo modelo…"
            )
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_MODEL): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options)
                )
            }
        )
        return self.async_show_form(
            step_id="model",
            data_schema=schema,
            description_placeholders={"brand": self._brand or ""},
        )

    # -- Assistente: criar definição de dispositivo ------------------------

    async def async_step_new_meta(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Coleta os metadados da nova definição de aparelho."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input["min_temp"] > user_input["max_temp"]:
                errors["base"] = "temp_range"
            elif not user_input["fan_modes"]:
                errors["base"] = "no_fan_modes"
            else:
                self._new_meta = {
                    **user_input,
                    "min_temp": int(user_input["min_temp"]),
                    "max_temp": int(user_input["max_temp"]),
                }
                return await self.async_step_new_codes()

        schema = vol.Schema(
            {
                vol.Required(CONF_BRAND, default=self._brand or ""): str,
                vol.Required(CONF_MODEL, default="Genérico"): str,
                vol.Required(
                    CONF_POWER_BEHAVIOR, default=POWER_STATEFUL
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(POWER_BEHAVIORS),
                        translation_key="power_behavior",
                    )
                ),
                vol.Required("min_temp", default=16): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10,
                        max=32,
                        step=1,
                        unit_of_measurement="°C",
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Required("max_temp", default=30): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10,
                        max=32,
                        step=1,
                        unit_of_measurement="°C",
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Required(
                    "fan_modes", default=list(DB_FAN_MODES)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(DB_FAN_MODES),
                        multiple=True,
                        translation_key="fan_modes",
                    )
                ),
                vol.Required(
                    "swing_mode", default=SWING_NONE
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(SWING_MODES_DB),
                        translation_key="swing_mode",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="new_meta", data_schema=schema, errors=errors
        )

    async def async_step_new_codes(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Recebe o bloco de códigos colados e monta a definição."""
        errors: dict[str, str] = {}
        meta = self._new_meta
        fan_modes: list[str] = meta["fan_modes"]
        min_temp: int = meta["min_temp"]
        max_temp: int = meta["max_temp"]

        if user_input is not None:
            parsed = parse_code_block(user_input["codes"])
            if parsed.errors:
                errors["base"] = "parse_errors"
            elif parsed.state_count == 0:
                errors["base"] = "no_states"
            else:
                database = await async_get_database(self.hass)
                device_id = database.unique_id(
                    f"{meta[CONF_BRAND]}_{meta[CONF_MODEL]}"
                )
                definition = build_climate_definition(
                    device_id=device_id,
                    brand=meta[CONF_BRAND].strip(),
                    model=meta[CONF_MODEL].strip(),
                    power_behavior=meta[CONF_POWER_BEHAVIOR],
                    min_temp=min_temp,
                    max_temp=max_temp,
                    fan_modes=fan_modes,
                    swing_mode=meta["swing_mode"],
                    parsed=parsed,
                )
                await database.async_add_custom(definition)
                self._brand = definition[CONF_BRAND]
                self._device_id = device_id
                # Loga o YAML para o usuário poder contribuir no repositório.
                _LOGGER.info(
                    "Definição de dispositivo criada (%s). YAML para o "
                    "repositório:\n%s",
                    device_id,
                    definition_to_yaml(definition),
                )
                return await self.async_step_configure()

        # Lista os comandos esperados para orientar o usuário.
        expected = (
            ["desligar_ar", "ligar_ar", "luz_do_ar"]
            + (["swing_on", "swing_off"] if meta["swing_mode"] == SWING_SEPARATE else [])
            + expected_state_keys(fan_modes, min_temp, max_temp)
        )
        placeholder = (
            "\n".join(f"{name}: sendir,1:8,1,38000,..." for name in expected[:6])
            + "\n..."
        )

        schema = vol.Schema(
            {
                vol.Required("codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                )
            }
        )
        return self.async_show_form(
            step_id="new_codes",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "expected": ", ".join(expected),
                "count": str(len(expected)),
                "example": placeholder,
            },
        )

    # -- Configuração final do aparelho ------------------------------------

    async def async_step_configure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Configura porta, recursos e sensor opcional do aparelho."""
        database = await async_get_database(self.hass)
        definition = database.get(self._device_id or "")
        if definition is None:
            return self.async_abort(reason="unknown_device")

        errors: dict[str, str] = {}

        # Quando o aparelho vem do wizard de nova definição, power_behavior já
        # foi escolhido na etapa new_meta — não precisa ser perguntado de novo.
        from_wizard = bool(self._new_meta)

        if user_input is not None:
            data: dict[str, Any] = {
                CONF_DEVICE_TYPE: self._device_type,
                CONF_DEVICE_ID: definition.id,
                CONF_BRAND: definition.brand,
                CONF_MODEL: definition.model,
                CONF_IR_PORT: user_input[CONF_IR_PORT],
                CONF_POWER_BEHAVIOR: user_input.get(
                    CONF_POWER_BEHAVIOR, definition.power_behavior
                ),
                CONF_ON_DELAY: user_input.get(CONF_ON_DELAY, DEFAULT_ON_DELAY),
                CONF_ENABLED_HVAC_MODES: user_input.get(
                    CONF_ENABLED_HVAC_MODES, definition.hvac_modes
                ),
                CONF_ENABLE_LIGHT_OFF: user_input.get(
                    CONF_ENABLE_LIGHT_OFF, False
                ),
                CONF_ENABLE_SWING: user_input.get(CONF_ENABLE_SWING, False),
                CONF_POWER_SENSOR: user_input.get(CONF_POWER_SENSOR) or None,
                CONF_POWER_THRESHOLD: user_input.get(
                    CONF_POWER_THRESHOLD, DEFAULT_POWER_THRESHOLD
                ),
            }
            return self.async_create_entry(
                title=user_input[CONF_NAME].strip(), data=data
            )

        skip = {CONF_POWER_BEHAVIOR} if from_wizard else set()
        schema = _build_configure_schema(definition, defaults=None, skip_fields=skip)
        return self.async_show_form(
            step_id="configure",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "device": definition.label,
                "note": definition.raw.get("_note", ""),
            },
        )

    # -- Reconfiguração de um aparelho existente ---------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edita um aparelho já cadastrado."""
        subentry = self._get_reconfigure_subentry()
        database = await async_get_database(self.hass)
        definition = database.get(subentry.data.get(CONF_DEVICE_ID, ""))
        if definition is None:
            return self.async_abort(reason="unknown_device")

        if user_input is not None:
            data = dict(subentry.data)
            data.update(
                {
                    CONF_IR_PORT: user_input[CONF_IR_PORT],
                    CONF_POWER_BEHAVIOR: user_input[CONF_POWER_BEHAVIOR],
                    CONF_ON_DELAY: user_input.get(
                        CONF_ON_DELAY, DEFAULT_ON_DELAY
                    ),
                    CONF_ENABLED_HVAC_MODES: user_input.get(
                        CONF_ENABLED_HVAC_MODES, definition.hvac_modes
                    ),
                    CONF_ENABLE_LIGHT_OFF: user_input.get(
                        CONF_ENABLE_LIGHT_OFF, False
                    ),
                    CONF_ENABLE_SWING: user_input.get(
                        CONF_ENABLE_SWING, False
                    ),
                    CONF_POWER_SENSOR: user_input.get(CONF_POWER_SENSOR)
                    or None,
                    CONF_POWER_THRESHOLD: user_input.get(
                        CONF_POWER_THRESHOLD, DEFAULT_POWER_THRESHOLD
                    ),
                }
            )
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=user_input[CONF_NAME].strip(),
                data=data,
            )

        schema = _build_configure_schema(
            definition, defaults={CONF_NAME: subentry.title, **subentry.data}
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            description_placeholders={"device": definition.label},
        )


def _build_configure_schema(
    definition: Any,
    defaults: dict[str, Any] | None,
    skip_fields: set[str] | None = None,
) -> vol.Schema:
    """Monta o schema do passo de configuração do aparelho."""
    defaults = defaults or {}
    skip = skip_fields or set()

    fields: dict[Any, Any] = {
        vol.Required(
            CONF_NAME,
            default=defaults.get(CONF_NAME, definition.label),
        ): str,
        vol.Required(
            CONF_IR_PORT,
            default=defaults.get(CONF_IR_PORT, MIN_IR_PORT),
        ): vol.All(int, vol.Range(min=MIN_IR_PORT, max=MAX_IR_PORT)),
    }

    if CONF_POWER_BEHAVIOR not in skip:
        fields[
            vol.Required(
                CONF_POWER_BEHAVIOR,
                default=defaults.get(
                    CONF_POWER_BEHAVIOR, definition.power_behavior
                ),
            )
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(POWER_BEHAVIORS),
                translation_key="power_behavior",
            )
        )

    fields[
        vol.Optional(
            CONF_ON_DELAY,
            default=defaults.get(CONF_ON_DELAY, DEFAULT_ON_DELAY),
        )
    ] = vol.All(
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.0, max=5.0, step=0.1,
                unit_of_measurement="s", mode=selector.NumberSelectorMode.BOX,
            )
        ),
    )

    # Modos HVAC: só aparece se a definição tiver mais de um modo.
    if len(definition.hvac_modes) > 1:
        fields[
            vol.Required(
                CONF_ENABLED_HVAC_MODES,
                default=defaults.get(
                    CONF_ENABLED_HVAC_MODES, definition.hvac_modes
                ),
            )
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(definition.hvac_modes),
                multiple=True,
                translation_key="hvac_modes",
            )
        )

    if definition.has_light_off:
        fields[
            vol.Optional(
                CONF_ENABLE_LIGHT_OFF,
                default=defaults.get(CONF_ENABLE_LIGHT_OFF, False),
            )
        ] = bool

    if definition.has_swing:
        fields[
            vol.Optional(
                CONF_ENABLE_SWING,
                default=defaults.get(CONF_ENABLE_SWING, True),
            )
        ] = bool

    fields[
        vol.Optional(
            CONF_POWER_SENSOR,
            description={
                "suggested_value": defaults.get(CONF_POWER_SENSOR)
            },
        )
    ] = selector.EntitySelector(
        selector.EntitySelectorConfig(domain=["binary_sensor", "sensor"])
    )
    fields[
        vol.Optional(
            CONF_POWER_THRESHOLD,
            default=defaults.get(
                CONF_POWER_THRESHOLD, DEFAULT_POWER_THRESHOLD
            ),
        )
    ] = selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0.0, max=100.0, step=0.1, mode=selector.NumberSelectorMode.BOX
        )
    )

    return vol.Schema(fields)
