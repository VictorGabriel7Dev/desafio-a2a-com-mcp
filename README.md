# A Ponte: um agente A2A com MCP por dentro

Central de Salas da Hill Valley Tech: um **servidor MCP** que expõe as salas como
tools e um **agente** que é host MCP por dentro (consome o servidor por HTTP) e
servidor A2A por fora (Agent Card, `SendMessage`, `GetTask`). A costura entre os
dois protocolos é a ponte do título: o `input_required` do MCP vira
`TASK_STATE_INPUT_REQUIRED` do A2A, e o `requestState` opaco atravessa a fronteira
guardado na Task.

São dois processos de verdade. O agente fala com o servidor MCP por HTTP, como um
cliente MCP qualquer; nada de importar a função da tool.

## Como rodar

A partir de um clone limpo, com Python 3.10 ou superior:

```bash
# 1. ambiente e dependências (versões travadas em pyproject.toml)
python3 -m venv .venv
.venv/bin/pip install .

# 2. a chave de integridade do requestState (>= 32 bytes, nunca versionada)
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# 3. servidor MCP na 7301, deixando o stderr visível (é onde a propagação do trace aparece)
(cd servidor-mcp && REQUEST_STATE_SECRET=$REQUEST_STATE_SECRET ../.venv/bin/python server.py)

# 4. em outro terminal, o agente A2A na 7300 (ele descobre o MCP em MCP_URL, default http://localhost:7301)
(cd agente && ../.venv/bin/python agente.py)

# 5. o validador, com os dois processos recém-iniciados
.venv/bin/python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

O servidor MCP **exige** `REQUEST_STATE_SECRET` no ambiente e aborta sem ela, de
propósito: a chave nunca mora no código. As portas 7301 (MCP) e 7300 (A2A) são o
default e podem ser trocadas por `MCP_PORT`, `A2A_PORT` e `MCP_URL`.

Rode o validador sempre com os dois processos recém-iniciados: as reservas criadas
numa execução mudam o resultado da seguinte.

## Onde a ponte acontece

A ponte está em `agente/agente.py`, na função `_aplicar_resultado_mcp`: quando o
`tools/call` do servidor MCP devolve `resultType == "input_required"`, o agente
guarda o `requestState` e a chave do `inputRequests` na Task
(`task["_request_state"]`, `task["_chave"]`) e coloca a Task em
`TASK_STATE_INPUT_REQUIRED`, com a linha `alternativas: ...` na ordem do `enum`.

O `requestState` volta para o servidor em `_continuar`: a continuação do cliente A2A
(`escolha=`) vira um novo `tools/call`, **com id de JSON-RPC novo** (porque são
requests independentes), levando `inputResponses` (a escolha) e o `requestState`
ecoado sem modificação. O agente nunca abre nem interpreta o `requestState`: ele é
opaco, guardado e devolvido, e não aparece em resposta A2A nenhuma.

Do lado do servidor (`servidor-mcp/server.py`), a pausa nasce no resolver
`escolha_de_sala`: em conflito ele retorna `Elicit(...)` com o schema das
alternativas, e o SDK do MCP transforma isso em `input_required` + `requestState`.
No retry, o mesmo resolver é satisfeito pela escolha que veio em `inputResponses`.

## Decisões técnicas

- **Proteção do `requestState`:** AES-256-GCM (AEAD) via `RequestStateSecurity` do
  SDK, com a chave derivada de `REQUEST_STATE_SECRET`. É selado e cifrado; uma
  adulteração de um caractere é detectada e rejeitada com `-32602`. A validade é de
  **10 minutos** (`ttl=600`, dentro da faixa de 5 a 30 exigida).
- **Por quanto tempo e onde vive o estado:** o servidor MCP **não guarda nada** entre
  o `input_required` e o retry: todo o pedido viaja selado no `requestState`, e por
  isso um retry funciona mesmo depois de o servidor ser reiniciado (a chave vem do
  ambiente, não da memória). Os argumentos que o cliente reenvia no retry não são
  confiáveis: o servidor usa os valores selados, então argumentos adulterados não
  tomam efeito. O estado das Tasks do agente vive em memória, por Task; duas Tasks
  pausadas ao mesmo tempo não trocam de `requestState`.
- **Host MCP por dentro:** o agente descobre as tools por `tools/list` antes do
  primeiro `tools/call`, lê a versão da política do resource `politica://uso`, declara
  a capability de elicitation em form mode e propaga o `traceparent` do cliente A2A
  em todos os requests MCP da Task (mesmo trace-id, span novo). O stderr do servidor
  MCP registra método, id e `traceparent` de cada request.
- **Determinismo:** o agente decide por regra, sem LLM. O mesmo pedido produz sempre
  o mesmo resultado.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: c77493ea7f5e5e23721c2691936fffb8
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
