"""Puerto de LLM implementado sobre las CLI ya instaladas en la maquina.

Por que CLI y no API: en esta maquina hay `claude` y `codex` instalados y con
su propia sesion. Una suscripcion de chat NO implica acceso a una API de pago,
asi que no se configura ninguna clave ni se activa gasto nuevo. Estos
adaptadores reutilizan las CLI tal cual.

Banderas usadas (comprobadas con `--help` en esta maquina):
  claude 2.1.285 : claude -p --output-format text  [--model X] [--restricted]
  codex-cli 0.160.1 : codex exec --json / -o FICHERO [-m MODELO] [-s read-only]

Estado: NO EJECUTADO contra un modelo. `is_available()` solo comprueba que el
binario responde a `--version`, que es gratuito. La primera generacion real se
hara al implementar el worker de Luna, no antes.

Limites de diseno: una sola peticion por llamada, con timeout y tope de
salida. Sin conversacion abierta ni reintentos infinitos.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from gongora import redaction
from gongora.logging_setup import get_logger

DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_OUTPUT = 20_000


class LlmUnavailable(RuntimeError):
    """La CLI no esta instalada o no responde."""


@dataclass
class CliProbe:
    available: bool
    version: str = ""
    detail: str = ""


class _BaseCliLlm:
    binary = ""
    name = ""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, model: str | None = None) -> None:
        self._timeout = timeout
        self._model = model
        self._log = get_logger(f"llm.{self.name}")

    def probe(self) -> CliProbe:
        path = shutil.which(self.binary)
        if path is None:
            return CliProbe(False, detail=f"{self.binary} no esta en el PATH.")
        try:
            done = subprocess.run([self.binary, "--version"], capture_output=True,
                                  text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CliProbe(False, detail=f"{self.binary} no responde: {exc}")
        if done.returncode != 0:
            return CliProbe(False, detail=f"{self.binary} --version salio con "
                                          f"codigo {done.returncode}.")
        return CliProbe(True, version=done.stdout.strip().splitlines()[0] if done.stdout else "")

    def is_available(self) -> bool:
        return self.probe().available

    def _command(self, prompt: str, system: str) -> list[str]:
        raise NotImplementedError

    def complete(self, *, system: str, prompt: str,
                 max_output_chars: int = DEFAULT_MAX_OUTPUT,
                 timeout_seconds: float | None = None) -> str:
        """Una sola peticion acotada. El prompt nunca lleva credenciales."""
        if redaction.contains_secret({"system": system, "prompt": prompt}):
            raise ValueError("El prompt contiene algo con forma de credencial. "
                             "Los prompts no transportan secretos.")
        probe = self.probe()
        if not probe.available:
            raise LlmUnavailable(probe.detail)
        command = self._command(prompt, system)
        self._log.info("Invocando %s (prompt %s caracteres)", self.name, len(prompt))
        try:
            done = subprocess.run(
                command, input=prompt, capture_output=True, text=True,
                timeout=timeout_seconds or self._timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LlmUnavailable(f"{self.name} agoto el timeout de "
                                 f"{timeout_seconds or self._timeout}s.") from exc
        if done.returncode != 0:
            raise LlmUnavailable(
                f"{self.name} salio con codigo {done.returncode}: "
                f"{redaction.redact(done.stderr)[:500]}"
            )
        return done.stdout[:max_output_chars]


class ClaudeCliLlm(_BaseCliLlm):
    binary = "claude"
    name = "claude"

    def _command(self, prompt: str, system: str) -> list[str]:
        command = ["claude", "-p", "--output-format", "text", "--restricted"]
        if system:
            command += ["--append-system-prompt", system]
        if self._model:
            command += ["--model", self._model]
        return command


class CodexCliLlm(_BaseCliLlm):
    binary = "codex"
    name = "codex"

    def _command(self, prompt: str, system: str) -> list[str]:
        command = ["codex", "exec", "--skip-git-repo-check", "-s", "read-only"]
        if self._model:
            command += ["-m", self._model]
        if system:
            # Codex no tiene --append-system-prompt: el sistema va en el prompt,
            # delimitado, y se envia por stdin.
            command += ["-"]
        else:
            command += ["-"]
        return command


def available_backends() -> dict[str, CliProbe]:
    """Diagnostico para `gongora doctor`: que CLI hay y que version."""
    return {backend.name: backend.probe()
            for backend in (ClaudeCliLlm(), CodexCliLlm())}
