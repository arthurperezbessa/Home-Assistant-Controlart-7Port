"""Cliente TCP para envio de comandos à 7Port da ControlArt.

A 7Port mantém um servidor TCP (porta padrão 4998) que aceita comandos
de texto no padrão Global Cache: `sendir`, `sendrf`, `sendrf_rc`.

O hardware não devolve um feedback confiável; portanto consideramos o
comando entregue quando a conexão TCP é aberta e os bytes são escritos
com sucesso. A ausência de resposta NÃO é tratada como erro.
"""

from __future__ import annotations

import asyncio
import logging

from .const import (
    BLASTER_PORT,
    COMMAND_TERMINATOR,
    DEFAULT_TCP_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class SevenPortError(Exception):
    """Erro de comunicação com a 7Port."""


class SevenPortClient:
    """Envia comandos para uma 7Port via TCP.

    Cada comando abre uma conexão nova (abre/escreve/fecha). O integrador
    informou que esse modelo funciona bem; uma conexão persistente não é
    necessária e evita problemas com o limite de clientes do equipamento.
    """

    def __init__(self, host: str, port: int) -> None:
        """Inicializa o cliente."""
        self._host = host
        self._port = port
        # Serializa os envios: a 7Port processa um comando por vez.
        self._lock = asyncio.Lock()

    @property
    def host(self) -> str:
        """Endereço IP/host da 7Port."""
        return self._host

    @property
    def port(self) -> int:
        """Porta TCP do servidor da 7Port."""
        return self._port

    async def async_send_raw(self, command: str) -> None:
        """Envia uma string de comando crua para a 7Port.

        Acrescenta o terminador de fim de linha recomendado pelo manual.
        Levanta `SevenPortError` se a conexão TCP falhar.
        """
        payload = (command.strip() + COMMAND_TERMINATOR).encode("ascii", "ignore")

        async with self._lock:
            writer = None
            try:
                async with asyncio.timeout(DEFAULT_TCP_TIMEOUT):
                    reader, writer = await asyncio.open_connection(
                        self._host, self._port
                    )
                    writer.write(payload)
                    await writer.drain()
                    # A 7Port pode responder "CMD OK"; lemos e ignoramos,
                    # sem depender disso (timeout curto e não-fatal).
                    try:
                        async with asyncio.timeout(0.5):
                            await reader.read(256)
                    except (asyncio.TimeoutError, OSError):
                        pass
            except (OSError, asyncio.TimeoutError) as err:
                raise SevenPortError(
                    f"Falha ao enviar comando para {self._host}:{self._port}: {err}"
                ) from err
            finally:
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except (OSError, asyncio.TimeoutError):
                        pass

        _LOGGER.debug("Comando enviado para %s:%s -> %s",
                      self._host, self._port, command)

    async def async_send_ir(self, ir_port: int, payload: str) -> None:
        """Envia um comando IR (`sendir`) para uma porta da 7Port.

        `payload` é o trecho do comando IR após `sendir,1:<porta>`, ou seja,
        algo como `,1,38000,1,1,168,...` (exatamente como armazenado no
        banco de dados de dispositivos).
        """
        payload = (payload or "").strip()
        if not payload:
            raise SevenPortError("Código IR vazio.")
        if not payload.startswith(","):
            payload = "," + payload
        command = f"sendir,1:{ir_port}{payload}"
        await self.async_send_raw(command)

    async def async_send_code(self, ir_port: int, code: str) -> None:
        """Envia um código do banco de dispositivos, IR ou RF.

        Códigos RF (`sendrf,...` / `sendrf_rc,...`) carregam a porta e todos
        os parâmetros na própria string capturada no 7Config, então seguem
        inteiros. Códigos IR são reconstituídos com a porta configurada.
        """
        if code.lower().startswith("sendrf"):
            await self.async_send_raw(code)
        else:
            await self.async_send_ir(ir_port, code)

    async def async_test_connection(self) -> bool:
        """Testa se é possível abrir uma conexão TCP com a 7Port.

        Não envia nenhum comando real; apenas valida host/porta.
        """
        try:
            async with asyncio.timeout(DEFAULT_TCP_TIMEOUT):
                reader, writer = await asyncio.open_connection(
                    self._host, self._port
                )
                writer.close()
                await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            return False
        return True


def is_blaster(ir_port: int) -> bool:
    """Retorna True se a porta indicada for o emissor interno (Blaster)."""
    return ir_port == BLASTER_PORT
