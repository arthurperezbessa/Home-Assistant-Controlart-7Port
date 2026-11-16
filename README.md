# ControlArt 7Port — Integração para Home Assistant

Integração customizada (HACS) para o **7Port da ControlArt** — o blaster
IR/RF que expõe um servidor TCP (porta padrão `4998`) e aceita comandos de
texto no padrão Global Cache (`sendir`, `sendrf`, `sendrf_rc`).

Esta primeira versão entrega o controle de **ar-condicionado** como entidade
`climate` nativa do Home Assistant, substituindo o uso de `shell_command` com
scripts `.sh` + `netcat`.

> **Status:** versão 0.1.0 — somente ar-condicionado. TV, Receiver e Cortinas
> estão planejados para versões seguintes.

---

## O que ela faz

- Cada equipamento **7Port** é uma entrada de configuração (IP + porta TCP).
- Cada aparelho controlado é um **dispositivo** dentro dessa 7Port, com suas
  próprias entidades. Você escolhe a **porta de saída** (1 a 7, ou 8 para o
  Blaster interno) ao adicionar o aparelho.
- O ar-condicionado vira uma entidade `climate` completa: modos, temperatura,
  velocidade de ventilação e (opcionalmente) swing.
- Traz um **banco de dados de aparelhos** embutido (Carrier, LG e Philco já
  inclusos) que é atualizado junto com a integração via HACS.
- Inclui um **assistente para criar novos aparelhos** colando os códigos IR
  capturados no aplicativo 7Config.

## Conceitos importantes

### Comportamento de ligar (`power_behavior`)

Alguns aparelhos ligam só com o código de estado; outros precisam de um
comando de "ligar" antes:

- **`stateful`** — o código de temperatura/modo já liga o aparelho.
  Ex.: Carrier. É o padrão.
- **`explicit_on`** — é preciso enviar o código de "ligar" e aguardar um
  instante antes de mandar o estado. Ex.: LG. O atraso padrão é `0,8 s` e é
  configurável por aparelho.

O código de "ligar" só é reenviado na transição **desligado → ligado**; trocar
temperatura com o aparelho já ligado envia apenas o novo estado.

### Desligar é sempre enviado

O comando de desligar é discreto e é **sempre** transmitido quando você
seleciona o modo "Desligado", mesmo que o Home Assistant já considere o
aparelho desligado. Assim, se alguém ligou o ar pelo controle físico, o
desligar pelo HA continua surtindo efeito — sem risco de deixar o ar ligado.

### Feedback de estado (opcional)

O 7Port não devolve confirmação. Por padrão a entidade é **otimista**
(`assumed_state`): o estado mostrado é o último comando enviado.

Você pode, opcionalmente, indicar um **sensor** (um `binary_sensor`, ou um
`sensor` numérico de corrente/potência) para detectar se o aparelho está
ligado de verdade. Para sensores numéricos há um limiar configurável: acima
dele, considera-se ligado. Com um sensor configurado, ligar/desligar pelo
controle físico passa a refletir no Home Assistant.

### Luz do display (opcional)

O comando `luz_do_ar` é enviado **após** os demais comandos, para apagar a luz
do display do aparelho. É opcional e ativado por aparelho.

## Instalação via HACS

1. Em **HACS → Integrações → menu (⋮) → Repositórios personalizados**,
   adicione a URL deste repositório com a categoria **Integration**.
2. Procure por **ControlArt 7Port** na lista do HACS e instale.
3. Reinicie o Home Assistant.
4. Vá em **Configurações → Dispositivos e Serviços → Adicionar integração** e
   procure por **ControlArt 7Port**.

## Como usar

### 1. Adicionar a 7Port

Informe um nome, o **IP** do equipamento e a **porta TCP** (padrão `4998`).
A integração testa a conexão antes de concluir.

### 2. Adicionar um ar-condicionado

Na 7Port recém-criada, use **Adicionar dispositivo**:

1. **Tipo** — Ar-condicionado.
2. **Marca** — escolha entre as do banco (Carrier, LG, Philco) ou
   **Criar nova definição**.
3. **Modelo** — escolha o modelo da marca, ou crie um novo.
4. **Configuração** — nome do aparelho, **porta da 7Port** onde o emissor
   está ligado, comportamento de ligar, atraso do `explicit_on`, modos HVAC
   habilitados, luz do display, swing e sensor de feedback (opcionais).

Uma porta da 7Port pode ser usada por vários aparelhos; cada aparelho usa
uma única porta.

### 3. Criar uma nova definição de aparelho

Ao escolher **Criar nova definição** no passo de marca/modelo:

1. Preencha marca, modelo, faixa de temperatura, velocidades de ventilação,
   swing e comportamento de ligar.
2. Cole o bloco de códigos, **um por linha**, no formato `nome: código` — o
   mesmo formato da lista de comandos do 7Config. Exemplo:

   ```
   desligar_ar: ",1,38000,1,1,168,169,..."
   ligar_ar: ",1,38000,1,1,127,379,..."
   luz_do_ar: ",1,39000,1,1,132,384,..."
   Temp-auto22: ",1,39000,3,1,170,170,..."
   Temp-low18: ",1,38000,1,1,168,169,..."
   ```

   Os códigos podem estar completos (`sendir,1:8,...`) ou já "limpos"
   (`,1,38000,...`) — a integração normaliza automaticamente, removendo o
   trecho `sendir,1:<porta>` para que o mesmo código sirva em qualquer porta.

3. A definição é salva no Home Assistant (em `.storage`, preservada entre
   atualizações da integração) e fica disponível para novos aparelhos.

> **Dica:** ao criar uma definição, a integração grava o YAML correspondente
> no log do Home Assistant. Você pode copiar esse YAML para a pasta
> `devices/` deste repositório e contribuir com o banco de dados embutido.

### 4. Cortina com entidade principal (fallback)

Ao configurar uma **Cortina / Persiana**, o campo opcional **Cortina
principal** recebe uma cortina já integrada ao Home Assistant (por exemplo, o
motor Tuya Wi-Fi da mesma cortina). Com ele preenchido:

- Enquanto a principal está disponível, abrir, fechar, parar e posição são
  enviados **para ela**, e a cortina da 7Port espelha o estado, a posição e as
  funções dela.
- Quando a principal fica `unavailable` ou `unknown`, os mesmos comandos saem
  pelos códigos IR/RF da 7Port, com estado otimista a partir do último estado
  conhecido da principal. Nesse modo só existem abrir, fechar e parar.
- O sensor de janela vale nos dois caminhos: com a janela aberta, fechar (ou
  levar a uma posição abaixo da atual) é bloqueado.

Use a cortina da 7Port no dashboard, nas cenas e nos grupos; a principal fica
só como caminho de comando.

## Banco de dados de aparelhos

| Arquivo | Marca | Modelo | Observações |
|---|---|---|---|
| `devices/ac/carrier_generico.yaml` | Carrier | Genérico | `stateful`, 16–30 °C |
| `devices/ac/lg_generico.yaml` | LG | Genérico | `explicit_on`, 18–30 °C, com luz |
| `devices/ac/philco_2.yaml` | Philco | IR AR Philco 2 | `stateful`, 16–30 °C |

> **Atenção (Philco):** o `power_behavior` foi definido como `stateful`. Se o
> aparelho não ligar apenas com o comando de temperatura, edite o dispositivo
> e mude para `explicit_on` (o código de ligar já está no banco).

### Estrutura de uma definição

```yaml
id: carrier_generico
brand: Carrier
model: Genérico
device_type: climate
power_behavior: stateful      # stateful | explicit_on
min_temp: 16
max_temp: 30
temp_step: 1
hvac_modes: [cool]            # cool | heat | dry | fan_only
fan_modes: [auto, low, medium, high]
swing_mode: none              # none | separate
commands:
  power_off: ",1,38000,..."   # sempre enviado ao desligar
  power_on: ~                 # usado quando power_behavior = explicit_on
  light_off: ~                # opcional, enviado após os demais comandos
states:
  cool:
    auto: { 16: ",1,...", 17: ",1,...", ... }
    low: { ... }
    medium: { ... }
    high: { ... }
```

Os códigos são armazenados **sem a porta** (o trecho após `sendir,1:<porta>`).
A integração monta `sendir,1:<porta>` + código + `\r\n` na hora do envio.

## Limitações

- O 7Port não fornece feedback confiável: sem um sensor configurado, o estado
  é assumido a partir do último comando.
- A captura de novos códigos IR é feita pelo aplicativo **7Config** (USB, no
  Windows); não há comando TCP de aprendizado. Esta integração apenas
  **reproduz** códigos já capturados.
- O modo de swing embutido na matriz de estados (`matrix`) ainda não está
  implementado — apenas `none` e `separate` (comandos de swing independentes).

## Roadmap

- TV (`media_player`): ligar/desligar e seleção de entradas.
- Receiver (`media_player`): zonas, entradas e modos de som.
- Cortinas (`cover`): sobe/desce/para via RF (`sendrf` / `sendrf_rc`).

## Licença

MIT — veja o arquivo [LICENSE](LICENSE).

Não é um projeto oficial da ControlArt. "7Port" e "ControlArt" pertencem aos
seus respectivos donos.
