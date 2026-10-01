"""Agente da Central de Salas: host MCP por dentro, servidor A2A por fora.

A ponte vive em `_continuar` e `_reservar`: o `input_required` que o servidor MCP
devolve vira `TASK_STATE_INPUT_REQUIRED`, o `requestState` opaco fica guardado na
Task (nunca exposto ao cliente A2A), e a continuacao repete o `tools/call` com um
id novo levando a escolha de volta. O trace-id do cliente A2A e propagado a todos
os requests MCP da Task.
"""

from __future__ import annotations

import json
import os
import secrets
import sys

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cliente_mcp import ClienteMCP

MCP_URL = os.environ.get("MCP_URL", "http://localhost:7301")
CLIENTE = ClienteMCP(MCP_URL)

# Estado descoberto do servidor MCP (host MCP: descobre em runtime, nao hardcoda).
_descoberto: dict = {"ok": False, "politica": None}


def _descobrir(traceparent: str | None) -> None:
    """tools/list + leitura do resource, uma vez, antes do primeiro tools/call."""
    if _descoberto["ok"]:
        return
    CLIENTE.tools_list(traceparent)  # o log do MCP registra isto antes do 1o tools/call
    recurso = CLIENTE.resources_read("politica://uso", traceparent)
    texto = (recurso.get("contents") or [{}])[0].get("text", "")
    primeira = texto.splitlines()[0] if texto else ""
    _descoberto["politica"] = primeira.split(":", 1)[1].strip() if ":" in primeira else None
    _descoberto["ok"] = True


# ------------------------------ Tasks em memoria ------------------------------

TASKS: dict[str, dict] = {}
TERMINAIS = {"TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED"}


def _nova_task(texto_user: str) -> dict:
    tid = f"task-{secrets.token_hex(6)}"
    task = {
        "id": tid,
        "contextId": f"ctx-{secrets.token_hex(6)}",
        "status": {"state": "TASK_STATE_SUBMITTED"},
        "history": [_msg("ROLE_USER", texto_user)],
        "artifacts": [],
        # internos (prefixo _, nunca serializados ao cliente A2A)
        "_pedido": None,
        "_request_state": None,
        "_chave": None,
        "_alternativas": [],
        "_traceparent": None,
    }
    TASKS[tid] = task
    return task


def _msg(role: str, texto: str, task: dict | None = None) -> dict:
    m = {"messageId": f"msg-{secrets.token_hex(6)}", "role": role, "parts": [{"text": texto}]}
    if task is not None:
        m["taskId"] = task["id"]
        m["contextId"] = task["contextId"]
    return m


def _publicar(task: dict) -> dict:
    """A Task como vai para o cliente A2A: sem os campos internos."""
    return {k: v for k, v in task.items() if not k.startswith("_")}


def _virar(task: dict, estado: str, texto: str) -> None:
    msg = _msg("ROLE_AGENT", texto, task)
    task["status"] = {"state": estado, "message": msg}
    task["history"].append(msg)


def _artifact_reserva(structured: dict, politica: str | None) -> dict:
    conteudo = {
        "reserva": structured.get("reserva"),
        "sala": structured.get("sala"),
        "inicio": structured.get("inicio"),
        "fim": structured.get("fim"),
        "responsavel": structured.get("responsavel"),
        "politica": politica if politica is not None else structured.get("politica"),
    }
    return {"artifactId": f"art-{secrets.token_hex(6)}", "name": "reserva", "parts": [{"text": json.dumps(conteudo)}]}


# --------------------------------- parsing ---------------------------------

def _campos(texto: str) -> dict:
    pares = {}
    for token in texto.split():
        if "=" in token:
            chave, valor = token.split("=", 1)
            pares[chave] = valor
    return pares


def _texto_do(message: dict) -> str:
    return " ".join(p.get("text", "") for p in message.get("parts", []))


def _alternativas_do(input_requests: dict) -> tuple[str, list[str]]:
    chave = next(iter(input_requests), "")
    schema = (((input_requests.get(chave) or {}).get("params") or {}).get("requestedSchema") or {})
    campo = (schema.get("properties") or {}).get("sala", {})
    enum = campo.get("enum") or ([campo["const"]] if "const" in campo else [])
    return chave, enum


# ------------------------------ ponte: reservar ------------------------------

def _reservar(task: dict, campos: dict) -> None:
    tp = task["_traceparent"]
    _descobrir(tp)
    argumentos = {
        "sala": campos.get("sala", ""),
        "inicio": campos.get("inicio", ""),
        "fim": campos.get("fim", ""),
        "responsavel": campos.get("responsavel", ""),
    }
    task["_pedido"] = argumentos
    resp = CLIENTE.reservar(argumentos, traceparent=tp)
    _aplicar_resultado_mcp(task, resp)


def _continuar(task: dict, campos: dict) -> None:
    escolha = campos.get("escolha", "")
    tp = task["_traceparent"]
    chave = task["_chave"]
    if escolha == "recusar":
        resp = CLIENTE.reservar(
            task["_pedido"], traceparent=tp,
            input_responses={chave: {"action": "decline"}}, request_state=task["_request_state"],
        )
        resultado = resp.get("result") or {}
        if resultado.get("resultType") == "complete" and not resultado.get("isError"):
            _virar(task, "TASK_STATE_CANCELED", "Reserva recusada.")
        else:
            _virar(task, "TASK_STATE_FAILED", _texto_result(resultado))
        return
    if escolha not in task["_alternativas"]:
        # escolha fora do enum: mantem pausada e repete as alternativas
        _virar(task, "TASK_STATE_INPUT_REQUIRED", "alternativas: " + ", ".join(task["_alternativas"]))
        return
    resp = CLIENTE.reservar(
        task["_pedido"], traceparent=tp,
        input_responses={chave: {"action": "accept", "content": {"sala": escolha}}},
        request_state=task["_request_state"],
    )
    _aplicar_resultado_mcp(task, resp)


def _texto_result(resultado: dict) -> str:
    return " ".join(p.get("text", "") for p in resultado.get("content", []))


def _aplicar_resultado_mcp(task: dict, resp: dict) -> None:
    """Traduz a resposta do tools/call para o estado da Task. A ponte."""
    if resp.get("error"):
        _virar(task, "TASK_STATE_FAILED", resp["error"].get("message", "erro de protocolo"))
        return
    resultado = resp.get("result") or {}
    tipo = resultado.get("resultType")
    if resultado.get("isError"):
        _virar(task, "TASK_STATE_FAILED", _texto_result(resultado))
        return
    if tipo == "input_required":
        chave, alternativas = _alternativas_do(resultado.get("inputRequests") or {})
        task["_request_state"] = resultado.get("requestState")
        task["_chave"] = chave
        task["_alternativas"] = alternativas
        _virar(task, "TASK_STATE_INPUT_REQUIRED", "alternativas: " + ", ".join(alternativas))
        return
    # complete
    structured = resultado.get("structuredContent") or {}
    artifact = _artifact_reserva(structured, _descoberto["politica"])
    task["artifacts"] = [artifact]
    _virar(task, "TASK_STATE_COMPLETED", f"Reserva {structured.get('reserva')} confirmada na {structured.get('sala')}.")


# ------------------------------- A2A JSON-RPC -------------------------------

def _send_message(params: dict, traceparent: str | None) -> dict:
    message = params.get("message") or {}
    texto = _texto_do(message)
    task_id = message.get("taskId")

    if task_id:
        task = TASKS.get(task_id)
        if task is None:
            raise _RpcError(-32001, f"Task {task_id} nao encontrada")
        if task["status"]["state"] in TERMINAIS:
            raise _RpcError(-32002, "Task em estado terminal nao aceita novas mensagens")
        task["history"].append(_msg("ROLE_USER", texto, task))
        if traceparent:
            task["_traceparent"] = traceparent
        _continuar(task, _campos(texto))
        return {"task": _publicar(task)}

    task = _nova_task(texto)
    task["_traceparent"] = traceparent
    task["status"] = {"state": "TASK_STATE_WORKING"}
    _reservar(task, _campos(texto))
    return {"task": _publicar(task)}


def _get_task(params: dict) -> dict:
    task = TASKS.get(params.get("id"))
    if task is None:
        raise _RpcError(-32001, "Task nao encontrada")
    return {"task": _publicar(task)}


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message


async def a2a(request: Request) -> JSONResponse:
    corpo = await request.json()
    rid = corpo.get("id")
    metodo = corpo.get("method")
    params = corpo.get("params") or {}
    traceparent = request.headers.get("traceparent")
    print(f"[a2a] method={metodo} id={rid} traceparent={traceparent}", file=sys.stderr, flush=True)
    try:
        if metodo == "SendMessage":
            resultado = _send_message(params, traceparent)
        elif metodo == "GetTask":
            resultado = _get_task(params)
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": resultado})
    except _RpcError as e:
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": e.message}})


def _porta() -> int:
    return int(os.environ.get("A2A_PORT", "7300"))


async def agent_card(request: Request) -> JSONResponse:
    base = f"http://127.0.0.1:{_porta()}"
    return JSONResponse({
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [
            {"url": f"{base}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    })


app = Starlette(routes=[
    Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
    Route("/a2a", a2a, methods=["POST"]),
])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=_porta(), log_level="warning")
