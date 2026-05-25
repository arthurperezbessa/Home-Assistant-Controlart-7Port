"""Banco de dados de dispositivos da integração ControlArt 7Port.

O banco combina duas fontes:

1. Definições embutidas no repositório (`devices/**/*.yaml`) — atualizadas
   junto com a integração via HACS.
2. Definições criadas pelo usuário dentro do Home Assistant — persistidas
   em `.storage` e preservadas entre atualizações da integração.

Cada definição descreve um modelo de aparelho (marca/modelo) e os códigos
IR necessários para controlá-lo.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from .const import (
    CMD_CLOSE,
    CMD_LIGHT_OFF,
    CMD_OPEN,
    CMD_POWER_OFF,
    CMD_POWER_ON,
    CMD_STOP,
    CMD_SWING_OFF,
    CMD_SWING_ON,
    DB_FAN_MODES,
    DB_HVAC_MODES,
    DEVICE_TYPE_CLIMATE,
    DEVICE_TYPE_COVER,
    DEVICE_TYPE_TV,
    DOMAIN,
    POWER_BEHAVIORS,
    POWER_STATEFUL,
    STORAGE_KEY,
    STORAGE_VERSION,
    SWING_NONE,
    SWING_SEPARATE,
)

_LOGGER = logging.getLogger(__name__)

_DEVICES_DIR = Path(__file__).parent / "devices"


class DeviceDefinition:
    """Definição imutável de um modelo de aparelho."""

    def __init__(self, data: dict[str, Any], builtin: bool) -> None:
        """Cria a definição a partir do dicionário YAML/JSON."""
        self.raw = data
        self.builtin = builtin
        self.id: str = data["id"]
        self.brand: str = data.get("brand", "Desconhecida")
        self.model: str = data.get("model", "Genérico")
        self.device_type: str = data.get("device_type", DEVICE_TYPE_CLIMATE)
        self.power_behavior: str = data.get("power_behavior", POWER_STATEFUL)
        self.min_temp: int = int(data.get("min_temp", 16))
        self.max_temp: int = int(data.get("max_temp", 30))
        self.temp_step: int = int(data.get("temp_step", 1))
        self.hvac_modes: list[str] = list(data.get("hvac_modes", ["cool"]))
        self.fan_modes: list[str] = list(data.get("fan_modes", DB_FAN_MODES))
        self.swing_mode: str = data.get("swing_mode", SWING_NONE)
        self.commands: dict[str, Any] = data.get("commands", {})
        # sources[nome_source] -> código IR (apenas para TVs)
        self.sources: dict[str, str] = dict(data.get("sources") or {})
        # states[modo][fan][temp] -> código IR
        self.states: dict[str, dict[str, dict[int, str]]] = {}
        for mode, fans in (data.get("states") or {}).items():
            self.states[mode] = {}
            for fan, temps in (fans or {}).items():
                self.states[mode][fan] = {
                    int(t): code for t, code in (temps or {}).items()
                }

    @property
    def label(self) -> str:
        """Rótulo amigável para exibição em listas."""
        return f"{self.brand} — {self.model}"

    @property
    def has_power_on(self) -> bool:
        """Indica se há um código de 'ligar' separado."""
        return bool(self.commands.get(CMD_POWER_ON))

    @property
    def has_light_off(self) -> bool:
        """Indica se há um código de 'apagar luz' do aparelho."""
        return bool(self.commands.get(CMD_LIGHT_OFF))

    @property
    def has_swing(self) -> bool:
        """Indica se o aparelho oferece controle de swing."""
        return self.swing_mode != SWING_NONE

    def state_code(self, mode: str, fan: str, temp: int) -> str | None:
        """Retorna o código IR para um estado (modo/fan/temperatura)."""
        return self.states.get(mode, {}).get(fan, {}).get(int(temp))

    def command(self, key: str) -> str | None:
        """Retorna um código de comando especial (power_off, etc.)."""
        return self.commands.get(key)

    def source_code(self, name: str) -> str | None:
        """Retorna o código IR de um source de TV pelo nome."""
        return self.sources.get(name)

    @property
    def source_list(self) -> list[str]:
        """Lista de sources de TV disponíveis na definição."""
        return list(self.sources.keys())


class DeviceDatabase:
    """Agrega definições embutidas e definições do usuário."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Inicializa o banco de dados."""
        self._hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._builtin: dict[str, DeviceDefinition] = {}
        self._custom: dict[str, DeviceDefinition] = {}

    async def async_load(self) -> None:
        """Carrega definições embutidas (YAML) e do usuário (storage)."""
        builtin = await self._hass.async_add_executor_job(_load_builtin)
        self._builtin = {d.id: d for d in builtin}

        stored = await self._store.async_load() or {}
        self._custom = {}
        for dev in stored.get("devices", {}).values():
            try:
                definition = DeviceDefinition(dev, builtin=False)
                self._custom[definition.id] = definition
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning("Definição de dispositivo inválida ignorada: %s", err)

        _LOGGER.debug(
            "Banco de dispositivos carregado: %s embutidos, %s do usuário",
            len(self._builtin),
            len(self._custom),
        )

    @property
    def _all(self) -> dict[str, DeviceDefinition]:
        """Todas as definições (usuário sobrepõe embutidas com mesmo id)."""
        return {**self._builtin, **self._custom}

    def get(self, device_id: str) -> DeviceDefinition | None:
        """Retorna uma definição pelo id."""
        return self._all.get(device_id)

    def brands(self, device_type: str) -> list[str]:
        """Lista marcas que possuem ao menos um modelo do tipo informado."""
        brands = {
            d.brand
            for d in self._all.values()
            if d.device_type == device_type
        }
        return sorted(brands)

    def models(self, device_type: str, brand: str) -> list[DeviceDefinition]:
        """Lista definições de uma marca para um tipo de dispositivo."""
        return sorted(
            (
                d
                for d in self._all.values()
                if d.device_type == device_type and d.brand == brand
            ),
            key=lambda d: d.model,
        )

    async def async_add_custom(self, definition: dict[str, Any]) -> DeviceDefinition:
        """Persiste uma nova definição criada pelo usuário."""
        device = DeviceDefinition(definition, builtin=False)
        self._custom[device.id] = device
        await self._async_save()
        return device

    async def async_remove_custom(self, device_id: str) -> None:
        """Remove uma definição criada pelo usuário."""
        if device_id in self._custom:
            del self._custom[device_id]
            await self._async_save()

    async def _async_save(self) -> None:
        """Grava as definições do usuário em `.storage`."""
        await self._store.async_save(
            {"devices": {d.id: d.raw for d in self._custom.values()}}
        )

    def unique_id(self, base: str) -> str:
        """Gera um id único a partir de um texto base (marca+modelo)."""
        root = slugify(base) or "dispositivo"
        candidate = root
        i = 2
        while candidate in self._all:
            candidate = f"{root}_{i}"
            i += 1
        return candidate


def _load_builtin() -> list[DeviceDefinition]:
    """Carrega todos os arquivos YAML embutidos (executado em executor)."""
    devices: list[DeviceDefinition] = []
    if not _DEVICES_DIR.exists():
        return devices
    for path in sorted(_DEVICES_DIR.rglob("*.yaml")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
            if not isinstance(data, dict) or "id" not in data:
                _LOGGER.warning("Arquivo de dispositivo inválido: %s", path)
                continue
            devices.append(DeviceDefinition(data, builtin=True))
        except (yaml.YAMLError, OSError, KeyError, ValueError) as err:
            _LOGGER.error("Erro ao carregar %s: %s", path, err)
    return devices


async def async_get_database(hass: HomeAssistant) -> DeviceDatabase:
    """Retorna a instância compartilhada do banco de dados (carrega 1x)."""
    store = hass.data.setdefault(DOMAIN, {})
    db: DeviceDatabase | None = store.get("database")
    if db is None:
        db = DeviceDatabase(hass)
        await db.async_load()
        store["database"] = db
    return db


# ---------------------------------------------------------------------------
# Conversão de códigos colados pelo usuário (assistente "Criar dispositivo")
# ---------------------------------------------------------------------------

_RE_SENDIR_PREFIX = re.compile(r"^sendir,\d+:\d+(.*)$", re.IGNORECASE)

# Formato legado: Temp-auto22 ou auto22 (sempre cool)
_RE_TEMP_LEGACY = re.compile(
    r"^(?:temp[-_ ]?)?(auto|low|medium|high)[-_ ]?(\d+)$", re.IGNORECASE
)

# Formato multi-modo: cool17-auto, heat22-high, dry20-low, fan18-medium
_RE_STATE_MULTI = re.compile(
    r"^(cool|heat|dry|fan)(\d+)[_-](\w+)$", re.IGNORECASE
)

# Mapeamento nome-curto → chave do banco de dados
_MODE_SHORT_TO_DB = {
    "cool": "cool",
    "heat": "heat",
    "dry": "dry",
    "fan": "fan_only",
}

# Mapeamento chave do banco de dados → nome-curto para exibição
_DB_TO_MODE_SHORT = {v: k for k, v in _MODE_SHORT_TO_DB.items()}


_RE_RF_PREFIX = re.compile(r"^sendrf", re.IGNORECASE)


def normalize_code(raw: str) -> str | None:
    """Normaliza um código colado para o formato armazenado no banco.

    Comportamento por tipo de código:
    - **RF** (`sendrf,...` / `sendrf_rc,...`): preservado inteiro — a porta
      e todos os parâmetros já fazem parte da string capturada no 7Config.
    - **IR** (`sendir,1:X,...`): o prefixo `sendir,1:<porta>` é removido;
      armazena somente o payload (`,1,38000,...`), que é reconstituído com
      a porta configurada pelo usuário na hora do envio.
    """
    if raw is None:
        return None
    code = str(raw).strip().strip('"').strip("'").strip()
    if not code:
        return None
    # Códigos RF são armazenados e enviados exatamente como capturados.
    if _RE_RF_PREFIX.match(code):
        return code
    # Códigos IR: remove o prefixo sendir,1:X para reutilizar com qualquer porta.
    match = _RE_SENDIR_PREFIX.match(code)
    if match:
        code = match.group(1)
    if not code.startswith(","):
        code = "," + code
    return code


class CodeParseResult:
    """Resultado da análise de um bloco de códigos colados."""

    def __init__(self) -> None:
        """Inicializa o resultado vazio."""
        self.commands: dict[str, str] = {}
        # states[mode][fan][temp] → código IR  (climate)
        self.states: dict[str, dict[str, dict[int, str]]] = {}
        # sources[nome] → código IR  (TV)
        self.sources: dict[str, str] = {}
        self.errors: list[str] = []
        self.unknown: list[str] = []

    @property
    def state_count(self) -> int:
        """Quantidade de códigos de estado reconhecidos (climate)."""
        return sum(
            len(temps)
            for fans in self.states.values()
            for temps in fans.values()
        )

    @property
    def source_count(self) -> int:
        """Quantidade de sources reconhecidos (TV)."""
        return len(self.sources)


def parse_code_block(text: str) -> CodeParseResult:
    """Analisa um bloco de texto com linhas `nome: código`.

    Espera linhas no formato ``nome: código``.
    """
    result = CodeParseResult()
    alias = {
        "ligar_ar": CMD_POWER_ON,
        "ligar": CMD_POWER_ON,
        "power_on": CMD_POWER_ON,
        "on": CMD_POWER_ON,
        "desligar_ar": CMD_POWER_OFF,
        "desligar": CMD_POWER_OFF,
        "power_off": CMD_POWER_OFF,
        "off": CMD_POWER_OFF,
        "luz_do_ar": CMD_LIGHT_OFF,
        "luz": CMD_LIGHT_OFF,
        "light_off": CMD_LIGHT_OFF,
        "swing_on": CMD_SWING_ON,
        "oscilar_on": CMD_SWING_ON,
        "swing_off": CMD_SWING_OFF,
        "oscilar_off": CMD_SWING_OFF,
    }

    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            result.errors.append(f"Linha {lineno}: faltou ':' — '{line[:40]}'")
            continue
        name, _, value = line.partition(":")
        name = name.strip()
        code = normalize_code(value)
        if not code:
            result.errors.append(f"Linha {lineno}: código vazio para '{name}'")
            continue

        key = name.lower()
        if key in alias:
            result.commands[alias[key]] = code
            continue

        # Formato multi-modo: cool17-auto, heat22-high, fan18-medium, dry20-low
        match = _RE_STATE_MULTI.match(name)
        if match:
            mode_db = _MODE_SHORT_TO_DB[match.group(1).lower()]
            temp = int(match.group(2))
            fan = match.group(3).lower()
            result.states.setdefault(mode_db, {}).setdefault(fan, {})[temp] = code
            continue

        # Formato legado: Temp-auto22 ou auto22 (mapeado para cool)
        match = _RE_TEMP_LEGACY.match(name)
        if match:
            fan = match.group(1).lower()
            temp = int(match.group(2))
            result.states.setdefault("cool", {}).setdefault(fan, {})[temp] = code
            continue

        result.unknown.append(name)

    return result


def build_climate_definition(
    *,
    device_id: str,
    brand: str,
    model: str,
    power_behavior: str,
    min_temp: int,
    max_temp: int,
    fan_modes: list[str],
    swing_mode: str,
    parsed: CodeParseResult,
    hvac_modes: list[str] | None = None,
) -> dict[str, Any]:
    """Monta o dicionário de definição de um aparelho de climatização."""
    if power_behavior not in POWER_BEHAVIORS:
        power_behavior = POWER_STATEFUL

    requested_modes = hvac_modes or ["cool"]

    commands = {
        CMD_POWER_OFF: parsed.commands.get(CMD_POWER_OFF),
        CMD_POWER_ON: parsed.commands.get(CMD_POWER_ON),
        CMD_LIGHT_OFF: parsed.commands.get(CMD_LIGHT_OFF),
    }
    if swing_mode == SWING_SEPARATE:
        commands[CMD_SWING_ON] = parsed.commands.get(CMD_SWING_ON)
        commands[CMD_SWING_OFF] = parsed.commands.get(CMD_SWING_OFF)

    states: dict[str, Any] = {}
    actual_modes: list[str] = []

    for mode in requested_modes:
        mode_parsed = parsed.states.get(mode, {})
        mode_states: dict[str, Any] = {}
        for fan in fan_modes:
            temps = mode_parsed.get(fan, {})
            if temps:
                mode_states[fan] = {
                    t: c for t, c in sorted(temps.items())
                    if min_temp <= t <= max_temp
                }
        if mode_states:
            states[mode] = mode_states
            actual_modes.append(mode)

    # Garante ao menos o modo cool mesmo sem códigos (evita definição vazia).
    if not actual_modes:
        actual_modes = ["cool"]
        states = {"cool": {}}

    # Inclui apenas os fans que têm código em ao menos um modo.
    used_fans = {fan for mode_s in states.values() for fan in mode_s}
    output_fan_modes = [f for f in fan_modes if f in used_fans]

    return {
        "id": device_id,
        "brand": brand,
        "model": model,
        "device_type": DEVICE_TYPE_CLIMATE,
        "power_behavior": power_behavior,
        "min_temp": int(min_temp),
        "max_temp": int(max_temp),
        "temp_step": 1,
        "hvac_modes": actual_modes,
        "fan_modes": output_fan_modes,
        "swing_mode": swing_mode,
        "commands": commands,
        "states": states,
    }


def parse_tv_code_block(text: str) -> CodeParseResult:
    """Analisa um bloco de texto com códigos de TV.

    Formato esperado — uma linha por comando::

        power_on:  sendir,1:8,...
        power_off: sendir,1:8,...
        HDMI 1:    sendir,1:8,...
        Netflix:   sendir,1:8,...

    ``power_on`` e ``power_off`` (e sinônimos em português) são tratados como
    comandos especiais. Qualquer outra linha vira um *source* da TV.
    """
    _ALIASES: dict[str, str] = {
        "power_on": CMD_POWER_ON,
        "ligar": CMD_POWER_ON,
        "on": CMD_POWER_ON,
        "power_off": CMD_POWER_OFF,
        "desligar": CMD_POWER_OFF,
        "off": CMD_POWER_OFF,
    }

    result = CodeParseResult()
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            result.errors.append(f"Linha {lineno}: faltou ':' — '{line[:40]}'")
            continue
        name, _, value = line.partition(":")
        name = name.strip()
        code = normalize_code(value)
        if not code:
            result.errors.append(f"Linha {lineno}: código vazio para '{name}'")
            continue

        cmd = _ALIASES.get(name.lower())
        if cmd is not None:
            result.commands[cmd] = code
        else:
            # Qualquer outra linha é um source; preserva o nome original.
            result.sources[name] = code

    return result


def build_tv_definition(
    *,
    device_id: str,
    brand: str,
    model: str,
    parsed: CodeParseResult,
) -> dict[str, Any]:
    """Monta o dicionário de definição de uma TV."""
    commands = {
        CMD_POWER_OFF: parsed.commands.get(CMD_POWER_OFF),
        CMD_POWER_ON: parsed.commands.get(CMD_POWER_ON),
    }
    return {
        "id": device_id,
        "brand": brand,
        "model": model,
        "device_type": DEVICE_TYPE_TV,
        "commands": commands,
        "sources": dict(parsed.sources),
    }


def parse_cover_code_block(text: str) -> CodeParseResult:
    """Analisa um bloco de texto com códigos de cortina/persiana.

    Formato esperado — uma linha por comando::

        open:  sendir,1:8,...
        close: sendir,1:8,...
        stop:  sendir,1:8,...   # opcional

    Aceita sinônimos em português.
    """
    _ALIASES: dict[str, str] = {
        "open": CMD_OPEN, "abrir": CMD_OPEN, "abre": CMD_OPEN,
        "close": CMD_CLOSE, "fechar": CMD_CLOSE, "fecha": CMD_CLOSE,
        "stop": CMD_STOP, "parar": CMD_STOP, "para": CMD_STOP,
    }

    result = CodeParseResult()
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            result.errors.append(f"Linha {lineno}: faltou ':' — '{line[:40]}'")
            continue
        name, _, value = line.partition(":")
        name = name.strip()
        code = normalize_code(value)
        if not code:
            result.errors.append(f"Linha {lineno}: código vazio para '{name}'")
            continue
        cmd = _ALIASES.get(name.lower())
        if cmd is not None:
            result.commands[cmd] = code
        else:
            result.unknown.append(name)

    return result


def build_cover_definition(
    *,
    device_id: str,
    brand: str,
    model: str,
    parsed: CodeParseResult,
) -> dict[str, Any]:
    """Monta o dicionário de definição de uma cortina/persiana."""
    commands: dict[str, Any] = {
        CMD_OPEN: parsed.commands.get(CMD_OPEN),
        CMD_CLOSE: parsed.commands.get(CMD_CLOSE),
    }
    stop = parsed.commands.get(CMD_STOP)
    if stop:
        commands[CMD_STOP] = stop
    return {
        "id": device_id,
        "brand": brand,
        "model": model,
        "device_type": DEVICE_TYPE_COVER,
        "commands": commands,
    }


def definition_to_yaml(definition: dict[str, Any]) -> str:
    """Serializa uma definição em YAML (para o usuário contribuir no repo)."""

    class _Dumper(yaml.SafeDumper):
        pass

    _Dumper.add_representer(
        type(None),
        lambda d, _: d.represent_scalar("tag:yaml.org,2002:null", "~"),
    )
    return yaml.dump(
        definition,
        Dumper=_Dumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        width=10**9,
    )


def expected_state_keys(
    fan_modes: list[str],
    min_temp: int,
    max_temp: int,
    hvac_modes: list[str] | None = None,
) -> list[str]:
    """Lista os nomes de comando esperados para a matriz modo × temperatura × fan.

    Formato de saída:
    - Um único modo cool → legado ``Temp-{fan}{temp}`` (compatibilidade).
    - Múltiplos modos → novo formato ``{modo_curto}{temp}-{fan}``.
    """
    modes = hvac_modes or ["cool"]
    keys: list[str] = []

    if modes == ["cool"]:
        # Mantém o formato legado para definições só-cool.
        for fan in fan_modes:
            for temp in range(min_temp, max_temp + 1):
                keys.append(f"Temp-{fan}{temp}")
        return keys

    for mode in modes:
        prefix = _DB_TO_MODE_SHORT.get(mode, mode)
        for temp in range(min_temp, max_temp + 1):
            for fan in fan_modes:
                keys.append(f"{prefix}{temp}-{fan}")
    return keys


__all__ = [
    "DB_FAN_MODES",
    "DB_HVAC_MODES",
    "CodeParseResult",
    "DeviceDatabase",
    "DeviceDefinition",
    "async_get_database",
    "build_climate_definition",
    "build_cover_definition",
    "build_tv_definition",
    "definition_to_yaml",
    "expected_state_keys",
    "normalize_code",
    "parse_code_block",
    "parse_cover_code_block",
    "parse_tv_code_block",
]
