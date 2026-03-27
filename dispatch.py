#!/usr/bin/env python3
"""
dispatch.py — CLI pour lancer les agents depuis Claude Code ou le terminal.

Usage :
  python dispatch.py <agent> [command] [--key value ...]

Exemples :
  python dispatch.py health
  python dispatch.py gmail read
  python dispatch.py gmail search --query "Jean Martin"
  python dispatch.py analytics rapport --days 7
  python dispatch.py analytics keywords --site archi
  python dispatch.py calendar add --title "Réunion client" --date 2026-04-01 --time 14:00
  python dispatch.py notion task --title "Appeler Martin" --priority Haute
  python dispatch.py zoho list --search "Dupont"
  python dispatch.py veille run
  python dispatch.py agents          → liste tous les agents et commandes

Options globales :
  --telegram     → envoyer le résultat sur Telegram en plus du terminal
  --json         → sortie JSON brute
"""
import sys, asyncio, json, argparse

# Charger .env si présent (dev local)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from core.log import setup_logging
setup_logging()


def parse_args() -> tuple[str, str, dict, list[str]]:
    """Parse les arguments CLI et retourne (agent, command, context, flags)."""
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    agent = args[0]

    # Commandes spéciales
    if agent == "agents":
        return "agents", "list", {}, []
    if agent == "health":
        flags = [a for a in args[1:] if a.startswith("--")]
        return "health", "check", {}, flags

    command = args[1] if len(args) > 1 and not args[1].startswith("--") else "help"
    flags   = [a for a in args if a.startswith("--")]
    params  = [a for a in args[2:] if not a.startswith("--")]

    # Extraire --key value pairs
    context = {}
    i = 2
    while i < len(args):
        if args[i].startswith("--") and not args[i] in ("--telegram", "--json"):
            key = args[i][2:]
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                context[key] = args[i + 1]
                i += 2
            else:
                context[key] = True
                i += 1
        else:
            i += 1

    return agent, command, context, flags


async def run():
    agent_name, command, context, flags = parse_args()
    as_json    = "--json"     in flags
    to_telegram = "--telegram" in flags

    # ── Commande spéciale: health ──────────────────────────────────────────────
    if agent_name == "health":
        import health as h
        print("\n" + "═" * 50)
        print("  WELLDONE — Health Check")
        print("═" * 50 + "\n")
        results = h.run_checks()
        report  = h.format_report(results)
        print(f"\n{report}\n")

        if to_telegram:
            from telegram import Bot
            from config import TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.send_message(chat_id=TELEGRAM_ALLOWED_USER_ID, text=report)
            print("📱 Rapport envoyé sur Telegram")

        errors = [r for r in results if r["status"] == "error"]
        sys.exit(1 if errors else 0)

    # ── Commande spéciale: agents list ────────────────────────────────────────
    if agent_name == "agents":
        from core.dispatcher import discover_agents
        registry = discover_agents()
        print(f"\n🤖 {len(registry)} agents disponibles:\n")
        for name, ag in registry.items():
            cmds = ", ".join(ag.commands.keys())
            scheds = f" ⏰ {ag.schedules[0]['cron']}" if ag.schedules else ""
            print(f"  /{name:<12} {ag.description[:45]}")
            print(f"  {'':12} Commandes: {cmds}{scheds}\n")
        return

    # ── Dispatch vers un agent ────────────────────────────────────────────────
    from core.dispatcher import dispatch, discover_agents

    if command == "help":
        registry = discover_agents()
        ag = registry.get(agent_name)
        if ag:
            print(await ag.help())
        else:
            available = ", ".join(discover_agents().keys())
            print(f"❌ Agent '{agent_name}' inconnu. Disponibles: {available}")
            sys.exit(1)
        return

    print(f"⏳ {agent_name}.{command} {json.dumps(context) if context else ''}")
    result = await dispatch(agent_name, command, context or None)

    if as_json:
        print(json.dumps({"agent": agent_name, "command": command, "result": result}))
    else:
        print(f"\n{result}\n")

    if to_telegram:
        from telegram import Bot
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        await bot.send_message(
            chat_id=TELEGRAM_ALLOWED_USER_ID,
            text=f"💻 *dispatch: /{agent_name} {command}*\n\n{result}",
            parse_mode="Markdown",
        )
        print("📱 Résultat envoyé sur Telegram")


if __name__ == "__main__":
    asyncio.run(run())
