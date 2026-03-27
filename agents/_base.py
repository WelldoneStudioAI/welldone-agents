"""
agents/_base.py — Interface standard pour tous les agents Welldone.

Pour créer un nouvel agent :
  1. Créer agents/mon_agent.py
  2. Créer une classe qui hérite de BaseAgent
  3. Instancier : agent = MonAgent()
  4. C'est tout — le dispatcher le découvre automatiquement.
"""
from abc import ABC, abstractmethod
from typing import Any


class BaseAgent(ABC):
    """
    Interface que chaque agent doit implémenter.

    Attributs obligatoires:
      name        → identifiant court, ex: "gmail"
      description → phrase courte pour /help, ex: "Lire et envoyer des emails"
      commands    → dict {str: callable} des sous-commandes disponibles

    Attributs optionnels:
      schedules   → liste de crons APScheduler pour les tâches automatiques
    """

    name: str = ""
    description: str = ""
    schedules: list[dict] = []  # [{"cron": "0 13 * * 1", "command": "rapport"}]

    @property
    @abstractmethod
    def commands(self) -> dict[str, Any]:
        """
        Retourne un dict {commande: callable}.
        Chaque callable reçoit (context: dict | None) et retourne str.

        Ex:
          {"read": self.read_unread, "send": self.send}
        """

    async def help(self) -> str:
        """Retourne l'aide auto-générée pour cet agent."""
        cmds = "\n".join(f"  /{self.name} {cmd}" for cmd in self.commands)
        return f"*{self.name.upper()}* — {self.description}\n{cmds}"

    async def run_command(self, command: str, context: dict | None = None) -> str:
        """
        Exécute une sous-commande par son nom.
        Retourne un message d'erreur si la commande n'existe pas.
        """
        fn = self.commands.get(command)
        if fn is None:
            available = ", ".join(self.commands.keys())
            return f"❌ Commande `{command}` inconnue pour {self.name}. Disponibles: {available}"
        return await fn(context)
