"""Cliente MCP do agente: fala HTTP JSON-RPC com o servidor MCP, como um host
de verdade (dois processos). Enxerga o `input_required` CRU, sem callback de
elicitation, porque e o agente A2A, e nao o cliente MCP, quem decide a pausa.
"""

from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request

PROTOCOLO = "2026-07-28"
CAP_FORM = {"elicitation": {"form": {}}}
CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}


class ClienteMCP:
    def __init__(self, base_url: str) -> None:
        self.url = base_url.rstrip("/")
        self._ids = itertools.count(1)

    def _meta(self, traceparent: str | None) -> dict:
        meta = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": CAP_FORM,
        }
        if traceparent:
            meta["traceparent"] = traceparent
        return meta

    def _post(self, metodo: str, params: dict, nome: str | None, traceparent: str | None) -> dict:
        cabecalhos = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if nome:
            cabecalhos["Mcp-Name"] = nome
        corpo = {
            "jsonrpc": "2.0",
            "id": next(self._ids),  # id novo a cada chamada; o retry nunca reusa o id inicial
            "method": metodo,
            "params": {**params, "_meta": self._meta(traceparent)},
        }
        req = urllib.request.Request(
            f"{self.url}/mcp", data=json.dumps(corpo).encode(), headers=cabecalhos, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode())

    # --- primitivas do protocolo ---

    def tools_list(self, traceparent: str | None = None) -> list[dict]:
        resp = self._post("tools/list", {}, None, traceparent)
        return (resp.get("result") or {}).get("tools", [])

    def resources_read(self, uri: str, traceparent: str | None = None) -> dict:
        resp = self._post("resources/read", {"uri": uri}, uri, traceparent)
        return resp.get("result") or {}

    def reservar(
        self,
        argumentos: dict,
        traceparent: str | None = None,
        input_responses: dict | None = None,
        request_state: str | None = None,
    ) -> dict:
        """Chama reservar_sala. No retry, leva inputResponses + requestState."""
        params: dict = {"name": "reservar_sala", "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state
        return self._post("tools/call", params, "reservar_sala", traceparent)
