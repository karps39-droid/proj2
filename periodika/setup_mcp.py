"""``periodika mcp-install`` — pieslēdz rīku Claude uz šī datora.

Izveido MCP servera konfigurāciju ar *šī datora* absolūtajiem ceļiem (tā Python,
tā repozitorija mape), lai nebūtu jāmin, kur kas atrodas. Pēc noklusējuma tikai
parāda, ko rakstīt; failu maina tikai ar ``--write``, un pirms tam saglabā kopiju.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

__all__ = ["server_entry", "claude_cli_command", "config_targets", "install_into"]

SERVER_NAME = "periodika"


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _running_from_source() -> bool:
    """Vai pakotne ir repozitorijā (tad vajag ``cwd``), vai uzstādīta vidē."""
    root = _package_root()
    return (root / "pyproject.toml").exists()


def server_entry(*, data_dir: str | Path | None = None,
                 wordlist: str | Path | None = None) -> dict[str, Any]:
    """MCP servera ieraksts šim datoram."""
    entry: dict[str, Any] = {
        "command": sys.executable,
        "args": ["-m", "periodika.mcp_server"],
    }
    if _running_from_source():
        entry["cwd"] = str(_package_root())
    env: dict[str, str] = {}
    if data_dir:
        env["PERIODIKA_HOME"] = str(Path(data_dir).expanduser().resolve())
    if wordlist:
        env["PERIODIKA_LV_WORDLIST"] = str(Path(wordlist).expanduser().resolve())
    if env:
        entry["env"] = env
    return entry


def claude_cli_command(**kwargs: Any) -> str:
    """Gatava ``claude mcp add`` komanda Claude Code lietotājam."""
    entry = server_entry(**kwargs)
    parts = ["claude", "mcp", "add", SERVER_NAME]
    for key, value in (entry.get("env") or {}).items():
        parts += ["--env", f"{key}={value}"]
    parts += ["--", entry["command"], *entry["args"]]
    return " ".join(_quote(p) for p in parts)


def _quote(value: str) -> str:
    return f'"{value}"' if " " in value else value


def config_targets() -> dict[str, Path]:
    """Kur šajā operētājsistēmā dzīvo konfigurācijas faili."""
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        desktop = home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        desktop = Path(appdata) / "Claude" / "claude_desktop_config.json"
    else:
        desktop = home / ".config" / "Claude" / "claude_desktop_config.json"
    return {
        "claude-code-lietotāja": home / ".claude.json",
        "claude-code-projekta": Path.cwd() / ".mcp.json",
        "claude-desktop": desktop,
    }


def install_into(
    path: str | Path, *, write: bool = False, **kwargs: Any
) -> dict[str, Any]:
    """Ieraksta (vai parāda) servera ierakstu norādītajā JSON konfigurācijā."""
    target = Path(path).expanduser()
    entry = server_entry(**kwargs)
    existing: dict[str, Any] = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError as exc:
            return {
                "fails": str(target),
                "kļūda": f"nav derīgs JSON ({exc}); izlabo to pats vai norādi citu failu",
            }
    servers = existing.setdefault("mcpServers", {})
    previous = servers.get(SERVER_NAME)
    servers[SERVER_NAME] = entry

    result: dict[str, Any] = {
        "fails": str(target),
        "ieraksts": {SERVER_NAME: entry},
        "jau_bija": bool(previous),
        "rakstīts": False,
    }
    if not write:
        result["norāde"] = (
            "Nekas netika mainīts. Pievieno --write, lai ierakstītu, "
            "vai iekopē 'ieraksts' saturu savā mcpServers sadaļā."
        )
        return result

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_suffix(target.suffix + ".periodika-bak")
        shutil.copy2(target, backup)
        result["dublējums"] = str(backup)
    target.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    result["rakstīts"] = True
    result["norāde"] = "Pārstartē Claude, lai jaunais serveris tiktu ielasīts."
    return result
