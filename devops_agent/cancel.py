"""Arrêt à la demande d'une tâche de fond (analyse, usine, déploiement).

Chaque tâche tourne dans son propre thread. Le serveur y attache un drapeau :
  - le code de l'agent appelle `check()` à chaque étape (le journal de la tâche le fait à chaque ligne) ;
  - les sous-processus longs (opencode, helm) s'enregistrent pour être tués sur-le-champ.
"""

from __future__ import annotations

import subprocess
import threading


class Cancelled(BaseException):
    """BaseException, comme Ctrl+C : traverse les `except Exception` du code de l'agent."""


_flags: dict[int, threading.Event] = {}
_procs: dict[int, set] = {}
_lock = threading.Lock()


def bind(event: threading.Event) -> None:
    with _lock:
        _flags[threading.get_ident()] = event


def unbind() -> None:
    tid = threading.get_ident()
    with _lock:
        _flags.pop(tid, None)
        _procs.pop(tid, None)


def requested() -> bool:
    ev = _flags.get(threading.get_ident())
    return bool(ev and ev.is_set())


def check() -> None:
    if requested():
        raise Cancelled()


def register(proc: subprocess.Popen) -> None:
    with _lock:
        _procs.setdefault(threading.get_ident(), set()).add(proc)


def unregister(proc: subprocess.Popen) -> None:
    with _lock:
        _procs.get(threading.get_ident(), set()).discard(proc)


def cancel_thread(tid: int) -> None:
    """Lève le drapeau de la tâche et tue ses sous-processus en cours."""
    with _lock:
        ev = _flags.get(tid)
        procs = list(_procs.get(tid, ()))
    if ev:
        ev.set()
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass
