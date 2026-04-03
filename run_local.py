#!/usr/bin/env python3
"""
run_local.py — Test local d'un agent sans lancer le bot Telegram.

Usage :
  .venv/bin/python run_local.py <agent> <commande> [clé=valeur ...]

Exemples :
  .venv/bin/python run_local.py watchdog check
  .venv/bin/python run_local.py analytics rapport
  .venv/bin/python run_local.py veille run
  .venv/bin/python run_local.py email auto_trier
  .venv/bin/python run_local.py blog rédiger sujet="Photo commerciale pour PME"
  .venv/bin/python run_local.py qbo list
  .venv/bin/python run_local.py gmail read

Ce script :
  - Charge le .env local automatiquement
  - Importe les agents directement (pas de Telegram, pas de conflit Railway)
  - Affiche le résultat brut dans le terminal
  - Mesure le temps d'exécution réel
"""
import asyncio
import sys
import time
import os

# ── Charger le .env AVANT tout import config ──────────────────────────────────
# override=True : le .env local prend priorité sur les vars vides du shell
# (le shell peut avoir des vars "" qui sinon bloqueraient le chargement)
from dotenv import load_dotenv
load_dotenv(override=True)

# ── Ajouter le répertoire courant au path ─────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def run(agent_name: str, command: str, context: dict) -> None:
    from core.dispatcher import discover_agents, REGISTRY

    print(f"\n🔧 LOCAL — {agent_name}.{command}")
    print(f"   contexte : {context or '{}'}")
    print("─" * 60)

    discover_agents()

    if agent_name not in REGISTRY:
        available = list(REGISTRY.keys())
        print(f"❌ Agent '{agent_name}' inconnu.")
        print(f"   Agents disponibles : {available}")
        return

    agent = REGISTRY[agent_name]
    cmds = list(agent.commands.keys())
    if command not in agent.commands:
        print(f"❌ Commande '{command}' inconnue pour {agent_name}.")
        print(f"   Commandes disponibles : {cmds}")
        return

    start = time.time()
    try:
        result = await agent.run_command(command, context or None)
        elapsed = round(time.time() - start, 1)
        print(result)
        print("─" * 60)
        print(f"✅ Terminé en {elapsed}s")
    except Exception as e:
        elapsed = round(time.time() - start, 1)
        print(f"❌ Exception après {elapsed}s : {e}")
        import traceback
        traceback.print_exc()


def parse_args() -> tuple[str, str, dict]:
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        print("\nAgents disponibles :")
        print("  watchdog, analytics, veille, email, gmail, blog,")
        print("  framer, notion, calendar, qbo, voyage, ceo, qualite")
        sys.exit(1)

    agent_name = args[0]
    command = args[1]

    # Clés=valeur supplémentaires → contexte dict
    context = {}
    for arg in args[2:]:
        if "=" in arg:
            k, v = arg.split("=", 1)
            context[k] = v
        else:
            # Argument positionnel → "sujet"
            context["sujet"] = arg

    return agent_name, command, context


if __name__ == "__main__":
    agent_name, command, context = parse_args()
    asyncio.run(run(agent_name, command, context))
