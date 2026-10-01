"""Regras de negocio das salas. Puro: sem MCP, sem HTTP, deterministico.

As mensagens de erro sao a fonte unica de verdade do enunciado e o validador
exige o texto exato. Nao reescrever.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DADOS = Path(__file__).resolve().parent.parent / "dados"

# Mensagens exatas (o validador compara com `in`). Nao mexer.
ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"

JANELA_INICIO = 8  # 08:00
JANELA_FIM = 20  # 20:00
DURACAO_MAXIMA_H = 2
MAX_ALTERNATIVAS = 3


class RegraViolada(Exception):
    """Erro de execucao da tool: vira isError com a mensagem exata."""


@dataclass(frozen=True)
class Sala:
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


@dataclass
class Reserva:
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str


def _ler_json(nome: str):
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


def versao_politica() -> str:
    """A primeira linha de politica-de-uso.md declara `versao: AAAA-MM-DD`."""
    primeira = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8").splitlines()[0]
    return primeira.split(":", 1)[1].strip()


def texto_politica() -> str:
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")


class Agenda:
    """Salas fixas + reservas em memoria. As reservas iniciais vem do disco;
    as criadas em runtime vivem so no processo (nao sobrevivem a restart, por
    desenho do enunciado)."""

    def __init__(self) -> None:
        self.salas: dict[str, Sala] = {
            s["id"]: Sala(s["id"], s["nome"], s["capacidade"], s["recursos"])
            for s in _ler_json("salas.json")
        }
        self.reservas: list[Reserva] = [
            Reserva(r["id"], r["sala"], r["inicio"], r["fim"], r["responsavel"])
            for r in _ler_json("reservas.json")
        ]
        maiores = [int(r.id.split("-")[1]) for r in self.reservas if r.id.startswith("res-")]
        self._proximo = max(maiores, default=0) + 1

    # --- validacao (compartilhada por consultar_disponibilidade e reservar_sala) ---

    def validar(self, sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
        if sala not in self.salas:
            raise RegraViolada(ERRO_SALA.format(sala=sala))
        ini = datetime.fromisoformat(inicio)
        f = datetime.fromisoformat(fim)
        if f <= ini:
            raise RegraViolada(ERRO_INTERVALO)
        if ini.hour < JANELA_INICIO or _passa_do_fim(f):
            raise RegraViolada(ERRO_JANELA)
        if (f - ini).total_seconds() > DURACAO_MAXIMA_H * 3600:
            raise RegraViolada(ERRO_DURACAO)
        return ini, f

    # --- consultas ---

    def conflitos(self, sala: str, ini: datetime, f: datetime) -> list[Reserva]:
        achados = [
            r for r in self.reservas
            if r.sala == sala and _sobrepoe(ini, f, datetime.fromisoformat(r.inicio), datetime.fromisoformat(r.fim))
        ]
        achados.sort(key=lambda r: r.inicio)
        return achados

    def alternativas(self, sala_pedida: str, ini: datetime, f: datetime) -> list[str]:
        """Salas livres no intervalo com capacidade >= a da pedida, exceto a
        pedida; no maximo 3, por capacidade crescente e, no empate, id alfabetico."""
        minima = self.salas[sala_pedida].capacidade
        livres = [
            s for s in self.salas.values()
            if s.id != sala_pedida and s.capacidade >= minima and not self.conflitos(s.id, ini, f)
        ]
        livres.sort(key=lambda s: (s.capacidade, s.id))
        return [s.id for s in livres[:MAX_ALTERNATIVAS]]

    # --- escrita ---

    def criar(self, sala: str, inicio: str, fim: str, responsavel: str) -> Reserva:
        r = Reserva(f"res-{self._proximo:04d}", sala, inicio, fim, responsavel)
        self._proximo += 1
        self.reservas.append(r)
        return r


def _passa_do_fim(f: datetime) -> bool:
    """Fim fora da janela: depois das 20:00. 20:00 em ponto fecha a janela e e' valido."""
    return (f.hour, f.minute, f.second) > (JANELA_FIM, 0, 0)


def _sobrepoe(ini_a: datetime, fim_a: datetime, ini_b: datetime, fim_b: datetime) -> bool:
    return ini_a < fim_b and ini_b < fim_a
