"""
core/dispatcher.py — Auto-découverte et routing des agents.

Scan automatique du dossier agents/ au démarrage.
Chaque fichier agents/xxx.py qui expose un objet `agent` est enregistré.
"""
import importlib, pkgutil, logging
import agents as agents_pkg
from agents._base import BaseAgent

log = logging.getLogger(__name__)

# Registre global: {agent_name: BaseAgent}
REGISTRY: dict[str, BaseAgent] = {}


def discover_agents() -> dict[str, BaseAgent]:
    """
    Découvre et enregistre tous les agents du package agents/.
    Ignore les modules commençant par '_'.
    """
    REGISTRY.clear()
    for finder, module_name, ispkg in pkgutil.iter_modules(agents_pkg.__path__):
        if module_name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"agents.{module_name}")
            if hasattr(mod, "agent") and isinstance(mod.agent, BaseAgent):
                REGISTRY[mod.agent.name] = mod.agent
                log.info(f"✅ Agent enregistré: {mod.agent.name}")
        except Exception as e:
            log.error(f"❌ Erreur chargement agent {module_name}: {e}")

    log.info(f"Dispatcher: {len(REGISTRY)} agents chargés → {list(REGISTRY.keys())}")
    return REGISTRY


async def dispatch(agent_name: str, command: str, context: dict | None = None) -> str:
    """
    Route une commande vers l'agent approprié.

    Args:
        agent_name: Nom de l'agent, ex: "gmail"
        command:    Sous-commande, ex: "read"
        context:    Paramètres pour la commande

    Returns:
        Résultat textuel (toujours une str, jamais d'exception)
    """
    if not REGISTRY:
        discover_agents()

    agent = REGISTRY.get(agent_name)
    if agent is None:
        available = ", ".join(REGISTRY.keys())
        return f"❌ Agent `{agent_name}` inconnu. Agents disponibles: {available}"

    log.info(f"dispatch → {agent_name}.{command} context={context}")
    return await agent.run_command(command, context)


async def help_text() -> str:
    """Retourne l'aide complète de tous les agents enregistrés."""
    if not REGISTRY:
        discover_agents()

    lines = ["🤖 *Welldone AI Agent Team*\n"]
    for agent in REGISTRY.values():
        cmds = " · ".join(f"`{c}`" for c in agent.commands.keys())
        lines.append(f"*/{agent.name}* — {agent.description}\n  Commandes: {cmds}")
        if agent.schedules:
            for s in agent.schedules:
                lines.append(f"  ⏰ Auto: {s['command']} ({s['cron']})")
    lines.append("\n💬 Ou envoie un message naturel — Claude comprend et route automatiquement.")
    return "\n\n".join(lines)


def get_all_schedules() -> list[tuple[str, str, str]]:
    """
    Retourne tous les schedules déclarés par les agents.
    Returns: [(cron_expr, agent_name, command), ...]
    """
    if not REGISTRY:
        discover_agents()

    schedules = []
    for agent in REGISTRY.values():
        for sched in agent.schedules:
            schedules.append((sched["cron"], agent.name, sched["command"]))
    return schedules
