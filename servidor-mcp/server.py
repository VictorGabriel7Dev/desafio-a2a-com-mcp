"""Servidor MCP da Central de Salas (Streamable HTTP, porta 7301).

Tres tools (listar_salas, consultar_disponibilidade, reservar_sala), um resource
(politica://uso) e o ciclo de MRTR na reserva: em conflito, o servidor termina a
resposta pedindo a escolha de uma alternativa (input_required), com o requestState
selado por AES-256-GCM sob REQUEST_STATE_SECRET. Nao ha canal de volta: quem
retoma e o cliente, com um request novo.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Annotated, Literal

from mcp.server.mcpserver import (
    CancelledElicitation,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from pydantic import BaseModel, Field, create_model

import dominio
from dominio import Agenda, RegraViolada

log = logging.getLogger("central-de-salas")

AGENDA = Agenda()
POLITICA = dominio.versao_politica()


# ------------------------------- modelos de saida -------------------------------

class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class Escolha(BaseModel):
    """Tipo estatico para a anotacao do corpo. O schema real (com o enum das
    alternativas) e criado em runtime no resolver."""
    sala: str


# --------------------------------- servidor ---------------------------------

server = MCPServer(
    "central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(
        keys=[os.environ["REQUEST_STATE_SECRET"]],  # >= 32 bytes, nunca hardcoded
        ttl=600.0,  # 10 min, dentro da faixa 5-30 exigida
    ),
    log_level="WARNING",
)


@server.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**vars(s)) for s in AGENDA.salas.values()])


@server.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    try:
        ini, f = AGENDA.validar(sala, inicio, fim)
    except RegraViolada as e:
        raise ToolError(str(e)) from e
    conflitos = AGENDA.conflitos(sala, ini, f)
    return Disponibilidade(
        sala=sala,
        livre=not conflitos,
        conflitos=[ConflitoOut(id=r.id, inicio=r.inicio, fim=r.fim, responsavel=r.responsavel) for r in conflitos],
    )


def _schema_das_alternativas(alternativas: list[str]) -> type[BaseModel]:
    """Schema plano com `sala` restrita as alternativas. Com 2+ vira enum; com 1,
    o gerador pode emitir const - ambos sao aceitos pelo enunciado."""
    tipo = Literal[tuple(alternativas)]  # type: ignore[valid-type]
    return create_model(
        "EscolhaDeSala",
        sala=(tipo, Field(description="Sala alternativa escolhida")),
    )


def escolha_de_sala(sala: str, inicio: str, fim: str):
    """Resolver da reserva. Valida; se livre, usa a propria sala; se ocupada,
    pede a escolha (Elicit) entre as alternativas; se ocupada sem alternativa,
    erro de execucao."""
    try:
        ini, f = AGENDA.validar(sala, inicio, fim)
    except RegraViolada as e:
        raise ToolError(str(e)) from e
    if not AGENDA.conflitos(sala, ini, f):
        return Escolha(sala=sala)
    alternativas = AGENDA.alternativas(sala, ini, f)
    if not alternativas:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    schema = _schema_das_alternativas(alternativas)
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", schema)


@server.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[Escolha], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if isinstance(escolha, (DeclinedElicitation, CancelledElicitation)):
        return ReservaOut(reservado=False, motivo="recusado")
    sala_final = escolha.data.sala
    reserva = AGENDA.criar(sala_final, inicio, fim, responsavel)
    return ReservaOut(
        reserva=reserva.id,
        reservado=True,
        sala=sala_final,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=POLITICA,
    )


@server.resource("politica://uso", mime_type="text/markdown")
def politica_de_uso() -> str:
    return dominio.texto_politica()


# ------------------------- logging de cada request no stderr -------------------------

class LogRequests:
    """Middleware ASGI: registra metodo, id e traceparent de cada request MCP no
    stderr, lendo o corpo JSON-RPC e repassando-o intacto."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        corpo = b""
        mais = True
        while mais:
            evento = await receive()
            corpo += evento.get("body", b"")
            mais = evento.get("more_body", False)
        try:
            msg = json.loads(corpo)
            meta = (msg.get("params") or {}).get("_meta") or {}
            print(
                f"[mcp] method={msg.get('method')} id={msg.get('id')} "
                f"traceparent={meta.get('traceparent')}",
                file=sys.stderr,
                flush=True,
            )
        except (ValueError, AttributeError):
            pass
        entregue = False

        async def receive_bufferizado():
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        await self.app(scope, receive_bufferizado, send)


def build_app():
    app = server.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True)
    return LogRequests(app)


app = build_app()


if __name__ == "__main__":
    import uvicorn

    porta = int(os.environ.get("MCP_PORT", "7301"))
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    uvicorn.run(app, host="0.0.0.0", port=porta, log_level="warning")
