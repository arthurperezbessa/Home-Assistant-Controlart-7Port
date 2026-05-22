"""Constantes da integração ControlArt 7Port."""

from __future__ import annotations

DOMAIN = "controlart_7port"

# --- Configuração do hub (config entry) ---
CONF_HOST = "host"
CONF_PORT = "port"
CONF_NAME = "name"

DEFAULT_PORT = 4998
DEFAULT_TCP_TIMEOUT = 5.0

# Terminador de comando recomendado no manual da 7Port.
COMMAND_TERMINATOR = "\r\n"

# Porta lógica do emissor interno "Blaster" da 7Port.
BLASTER_PORT = 8
MIN_IR_PORT = 1
MAX_IR_PORT = 8

# --- Subentry (dispositivo) ---
SUBENTRY_TYPE_DEVICE = "device"

CONF_DEVICE_TYPE = "device_type"
CONF_DEVICE_ID = "device_id"
CONF_BRAND = "brand"
CONF_MODEL = "model"
CONF_IR_PORT = "ir_port"
CONF_POWER_BEHAVIOR = "power_behavior"
CONF_ON_DELAY = "on_delay"
CONF_ENABLED_HVAC_MODES = "enabled_hvac_modes"
CONF_ENABLE_SWING = "enable_swing"
CONF_ENABLE_LIGHT_OFF = "enable_light_off"  # legado; substituído por CONF_LIGHT_OFF_BEHAVIOR
CONF_LIGHT_OFF_BEHAVIOR = "light_off_behavior"
CONF_POWER_SENSOR = "power_sensor"
CONF_POWER_THRESHOLD = "power_threshold"

DEFAULT_ON_DELAY = 0.8
DEFAULT_POWER_THRESHOLD = 0.1

# Tipos de dispositivo suportados.
DEVICE_TYPE_CLIMATE = "climate"
DEVICE_TYPE_TV = "tv"
DEVICE_TYPE_COVER = "cover"
SUPPORTED_DEVICE_TYPES = [DEVICE_TYPE_CLIMATE, DEVICE_TYPE_TV, DEVICE_TYPE_COVER]

# Configuração de TV.
CONF_BACKING_ENTITY = "backing_entity"

# Configuração de cortina/persiana.
CONF_WINDOW_SENSOR = "window_sensor"

# Comportamentos de ligar.
POWER_STATEFUL = "stateful"      # o código de estado já liga o aparelho
POWER_EXPLICIT_ON = "explicit_on"  # precisa enviar "ligar" antes do estado
POWER_BEHAVIORS = [POWER_STATEFUL, POWER_EXPLICIT_ON]

# Comportamentos de controle da luz do display.
LIGHT_OFF_NEVER = "never"    # não mexe na luz
LIGHT_OFF_ONCE = "once"      # desliga uma vez ao ligar o aparelho (AC lembra o estado)
LIGHT_OFF_ALWAYS = "always"  # desliga após cada comando IR (AC reseta a luz a cada comando)
LIGHT_OFF_BEHAVIORS = [LIGHT_OFF_NEVER, LIGHT_OFF_ONCE, LIGHT_OFF_ALWAYS]

# Modos de swing suportados pelo banco de dados.
SWING_NONE = "none"
SWING_SEPARATE = "separate"  # comandos swing_on / swing_off independentes
SWING_MODES_DB = [SWING_NONE, SWING_SEPARATE]

# Chaves de comando padrão.
CMD_POWER_OFF = "power_off"
CMD_POWER_ON = "power_on"
CMD_LIGHT_OFF = "light_off"
CMD_SWING_ON = "swing_on"
CMD_SWING_OFF = "swing_off"
CMD_OPEN = "open"
CMD_CLOSE = "close"
CMD_STOP = "stop"

# Modos HVAC reconhecidos no banco de dados.
DB_HVAC_MODES = ["cool", "heat", "dry", "fan_only"]
DB_FAN_MODES = ["auto", "low", "medium", "high"]

# Armazenamento de definições de dispositivo criadas pelo usuário.
STORAGE_KEY = f"{DOMAIN}_devices"
STORAGE_VERSION = 1

# Sentinela usada no fluxo de configuração para "criar nova definição".
NEW_DEFINITION = "__new__"
