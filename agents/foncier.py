"""
agents/foncier.py — Agent de prospection immobilière (Québec).

Cherche des immeubles commerciaux et multirésidentiels de 1 à 5 M$ avec
potentiel de développement, avant qu'ils arrivent sur Centris.

Le principe : le registre foncier ne publie aucune annonce, il publie des
droits. On ne lui demande donc pas « qu'est-ce qui est à vendre » — on croise
des sources publiques gratuites pour repérer ce qui *va* se vendre, on classe,
et on ne paie l'enrichissement que sur les dossiers qui ont passé le filtre.

Commandes :
  scan       → ventes pour taxes des MRC (gratuit, hebdomadaire)
  seao       → avis d'appel d'offres annonçant une revalorisation de secteur
  marche     → alertes Centris reçues par courriel, croisées avec la base
  calibrer   → mise au point du parseur d'alertes Centris
  roles      → charge un rôle d'évaluation et score le parc
  top        → meilleurs dossiers en base
  brief      → dossier rédigé sur les meilleurs candidats → Notion + Telegram
  enrichir   → registre foncier sur les dossiers au-dessus du seuil (PAYANT)
  sources    → état de configuration de chaque source
  cubf       → distribution des codes d'utilisation d'un rôle
  marquer    → change le statut d'un dossier

Exemples :
  python dispatch.py foncier sources
  python dispatch.py foncier scan --telegram
  python dispatch.py foncier roles --fichier data/role-montreal.geojson --millesime 2026
  python dispatch.py foncier calibrer --fichier alerte-centris.eml
  python dispatch.py foncier marche --jours 7
  python dispatch.py foncier top --limite 15 --municipalite Longueuil
  python dispatch.py foncier brief --limite 10
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from agents._base import BaseAgent

log = logging.getLogger(__name__)


class FoncierAgent(BaseAgent):
    name = "foncier"
    description = "Prospection immobilière QC : signaux publics → immeubles scorés"
    schedules = [
        # Jours ouvrables 7h00 Montréal — les alertes Centris arrivent le matin.
        # C'est le seul scan quotidien : une mise en marché se joue en heures.
        {"cron": "0 11 * * 1-5", "command": "marche"},
        # Mardi 6h00 Montréal — les MRC publient leurs avis en semaine.
        {"cron": "0 10 * * 2", "command": "scan"},
        # Jeudi 6h00 Montréal — nouveaux avis SEAO de la semaine.
        {"cron": "0 10 * * 4", "command": "seao"},
        # Lundi 7h30 Montréal — brief des meilleurs dossiers.
        {"cron": "30 11 * * 1", "command": "brief"},
    ]

    @property
    def commands(self) -> dict[str, Any]:
        return {
            "scan": self.scan_ventes_taxes,
            "seao": self.scan_seao,
            "marche": self.scan_marche,
            "calibrer": self.calibrer,
            "roles": self.charger_role,
            "top": self.top,
            "brief": self.brief,
            "enrichir": self.enrichir,
            "sources": self.diagnostic_sources,
            "cubf": self.distribution_cubf,
            "marquer": self.marquer,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Diagnostic
    # ─────────────────────────────────────────────────────────────────────────

    async def diagnostic_sources(self, context: dict | None = None) -> str:
        """État de chaque source. À lancer en premier sur une installation neuve."""
        from core.foncier.criteres import DEFAUT, POIDS, SCORE_MAX
        from core.foncier.sources import registre, ventes_taxes
        from core.foncier.sources.zonage import Zonage
        from core.foncier import store

        lignes = ["🏗️  FONCIER — état des sources", ""]

        lignes.append("THÈSE ACTIVE")
        lignes.append(
            f"  Commercial + multirésidentiel · "
            f"{DEFAUT.prix_min/1e6:.0f} à {DEFAUT.prix_max/1e6:.0f} M$ · "
            f"NOI {DEFAUT.noi_cible_min:.0%}-{DEFAUT.noi_cible_max:.0%} · "
            f"détention {DEFAUT.detention_seuil} ans+"
        )
        lignes.append(
            f"  Capture élargie à {DEFAUT.capture_min/1e6:.2f}-{DEFAUT.capture_max/1e6:.2f} M$ "
            f"(écart rôle/marché)"
        )
        lignes.append("")

        lignes.append("SOURCES GRATUITES")
        lignes.append(f"  ✅ Ventes pour taxes — {len(ventes_taxes.REGISTRE_MRC)} MRC au registre "
                      f"(~85 au Québec)")
        lignes.append("  ✅ SEAO — avis d'appel d'offres via Données Québec")
        lignes.append("  ✅ Rôles d'évaluation — chargement manuel par --fichier ou --url")
        lignes.append("")

        zonage = await asyncio.to_thread(Zonage.charger)
        lignes.append("ZONAGE")
        for ligne in zonage.diagnostic().splitlines():
            lignes.append(f"  {ligne}")
        lignes.append("")

        lignes.append("ENRICHISSEMENT PAYANT")
        for ligne in registre.diagnostic().splitlines():
            lignes.append(f"  {ligne}")
        lignes.append("")

        try:
            stats = await asyncio.to_thread(store.statistiques)
            lignes.append("BASE")
            lignes.append(f"  {stats['total']} immeuble(s) suivis · "
                          f"{stats['score_60_et_plus']} à 60+ points")
            lignes.append(f"  Dépensé ce mois-ci : {stats['depenses_mois']:.2f} $")
            if stats["par_statut"]:
                detail = ", ".join(f"{k}: {v}" for k, v in stats["par_statut"].items())
                lignes.append(f"  Statuts — {detail}")
        except Exception as e:
            lignes.append(f"  ⚠️  Base inaccessible : {e}")

        lignes.append("")
        lignes.append(f"SCORE — {SCORE_MAX} points répartis sur {len(POIDS)} signaux")
        for poids in POIDS:
            lignes.append(f"  {poids.points:>3} pts — {poids.libelle}")

        return "\n".join(lignes)

    # ─────────────────────────────────────────────────────────────────────────
    # Sources gratuites
    # ─────────────────────────────────────────────────────────────────────────

    async def scan_ventes_taxes(self, context: dict | None = None) -> str:
        """
        Scanne les avis de vente pour défaut de paiement de taxes des MRC.

        La source la plus rentable du système : publique par obligation légale,
        gratuite, et le propriétaire en défaut est par définition motivé.
        """
        from core.foncier.sources import ventes_taxes
        from core.foncier import scoring, store

        context = context or {}
        forcer = bool(context.get("forcer"))

        immeubles, resultats = await asyncio.to_thread(
            ventes_taxes.scanner, ventes_taxes.REGISTRE_MRC, forcer
        )

        if not immeubles:
            return ventes_taxes.rapport(resultats)

        # Les avis de vente pour taxes ne portent ni usage ni valeur : on ne peut
        # pas appliquer les filtres de la thèse. Ils entrent tous en base et
        # seront qualifiés au prochain croisement avec un rôle d'évaluation.
        for immeuble in immeubles:
            scoring.scorer(immeuble)

        resume_store = await asyncio.to_thread(store.enregistrer, immeubles)

        lignes = [ventes_taxes.rapport(resultats), ""]
        lignes.append(
            f"→ {resume_store['nouveaux']} nouveau(x), "
            f"{resume_store['mis_a_jour']} déjà connu(s)"
        )
        lignes.append("")
        lignes.append(
            "⚠️  Un avis de vente pour taxes ne porte ni usage ni valeur au rôle. "
            "Ces immeubles sont en base mais NON qualifiés contre la thèse tant "
            "qu'un rôle d'évaluation n'a pas été croisé (`foncier roles`)."
        )

        top = sorted(immeubles, key=lambda i: -i.score)[:10]
        if top:
            lignes.append("")
            lignes.append("Dix premiers :")
            for immeuble in top:
                lignes.append(f"  · lot {immeuble.lot} — {immeuble.municipalite}"
                              + (f" — {immeuble.adresse}" if immeuble.adresse else ""))

        return "\n".join(lignes)

    async def scan_seao(self, context: dict | None = None) -> str:
        """
        Récupère les avis SEAO annonçant une revalorisation de secteur et les
        rattache aux immeubles déjà en base.
        """
        from core.foncier.sources import seao
        from core.foncier.sources._http import SourceIndisponible
        from core.foncier import scoring, store

        context = context or {}
        semaines = int(context.get("semaines", 8))

        try:
            signaux = await asyncio.to_thread(seao.signaux, semaines)
        except SourceIndisponible as e:
            return f"❌ SEAO indisponible : {e}"

        if not signaux:
            return f"Aucun avis SEAO pertinent sur les {semaines} dernières semaines."

        # Rattachement aux immeubles suivis.
        suivis = await asyncio.to_thread(store.meilleurs, 5000, 0)
        rattachements = seao.rattacher(suivis, signaux)

        if rattachements:
            for immeuble in suivis:
                scoring.scorer(immeuble)
            await asyncio.to_thread(store.enregistrer, suivis)

        lignes = [
            f"SEAO — {len(signaux)} avis pertinent(s) sur {semaines} semaines, "
            f"{rattachements} rattachement(s) à des immeubles suivis",
            "",
        ]
        for territoire, signal in signaux[:20]:
            lignes.append(f"  · [{territoire or '?'}] {signal.libelle}")

        if len(signaux) > 20:
            lignes.append(f"  … et {len(signaux) - 20} autre(s)")

        return "\n".join(lignes)

    async def scan_marche(self, context: dict | None = None) -> str:
        """
        Lit les alertes Centris reçues par courriel et les croise avec la base.

        Le moment qui vaut le plus cher du système : un immeuble surveillé
        depuis des mois arrive sur le marché, et tu sais déjà qui le détient,
        depuis quand, et ce qu'on peut y construire — pendant que les autres
        découvrent une fiche.

        Options : --jours (défaut 7) --fichier (courriel local, hors Gmail)
        """
        from core.foncier.sources import centris
        from core.foncier import scoring, store

        context = context or {}
        jours = int(context.get("jours", 7))
        fichier = context.get("fichier")

        try:
            if fichier:
                fiches, diagnostic = await asyncio.to_thread(centris.lire_fichier, fichier)
            else:
                fiches, diagnostic = await asyncio.to_thread(centris.lire_alertes, jours)
        except FileNotFoundError:
            return f"❌ Fichier introuvable : {fichier}"
        except Exception as e:
            log.error("foncier.marche: lecture impossible (%s)", e, exc_info=True)
            return (
                f"❌ Lecture des alertes impossible : {e}\n\n"
                "Vérifier que les identifiants Google sont configurés "
                "(GOOGLE_OAUTH_JSON), ou passer --fichier pour tester sur un "
                "courriel sauvegardé."
            )

        if not fiches:
            return (
                "Aucune fiche Centris extraite.\n\n"
                + diagnostic.rapport()
                + "\n\n→ `foncier calibrer` affiche le texte réellement reçu."
            )

        # Croisement avec les dossiers déjà suivis.
        suivis = await asyncio.to_thread(store.meilleurs, 5000, 0)
        rapprochements, orphelines = centris.rapprocher(fiches, suivis)

        # Les rapprochements enrichissent les dossiers existants ; les fiches
        # sans correspondance entrent comme nouveaux dossiers à surveiller.
        a_enregistrer = [r.immeuble for r in rapprochements]
        for fiche in orphelines:
            immeuble = centris.vers_immeuble(fiche)
            if immeuble is not None:
                a_enregistrer.append(immeuble)

        for immeuble in a_enregistrer:
            scoring.scorer(immeuble)

        resume_store = await asyncio.to_thread(store.enregistrer, a_enregistrer)

        lignes = [
            f"🏘️  MARCHÉ — {len(fiches)} fiche(s) sur {jours} jours",
            "",
        ]

        if rapprochements:
            lignes.append(
                f"🎯 {len(rapprochements)} DOSSIER(S) SUIVI(S) MAINTENANT SUR LE MARCHÉ"
            )
            lignes.append("")
            for rapprochement in sorted(
                rapprochements, key=lambda r: -r.immeuble.score
            ):
                lignes.append(rapprochement.resume())
                lignes.append("")
        else:
            lignes.append("Aucune fiche ne correspond à un dossier déjà suivi.")
            lignes.append("")

        nouvelles = [f for f in orphelines if f.adresse]
        if nouvelles:
            lignes.append(f"Nouvelles fiches ({len(nouvelles)}) :")
            for fiche in nouvelles[:15]:
                lignes.append(f"  · {fiche.resume()}")
            if len(nouvelles) > 15:
                lignes.append(f"  … et {len(nouvelles) - 15} autre(s)")
            lignes.append("")

        sans_adresse = [f for f in orphelines if not f.adresse]
        if sans_adresse:
            lignes.append(
                f"⚠️  {len(sans_adresse)} fiche(s) sans adresse extraite — "
                "non rapprochables, non enregistrées :"
            )
            for fiche in sans_adresse[:5]:
                lignes.append(f"     {fiche.url}")
            lignes.append("")

        lignes.append(
            f"Base — {resume_store['nouveaux']} nouveau(x), "
            f"{resume_store['mis_a_jour']} mis à jour"
        )
        lignes.append("")
        lignes.append(centris.comparables(fiches, context.get("municipalite", "")))

        if diagnostic.avertissements:
            lignes.append("")
            for avertissement in diagnostic.avertissements:
                lignes.append(f"⚠️  {avertissement}")

        return "\n".join(lignes)

    async def calibrer(self, context: dict | None = None) -> str:
        """
        Rapport de mise au point du parseur d'alertes Centris.

        Affiche ce qui a été extrait, ce qui a été manqué, et un extrait du
        texte réellement analysé. À lancer sur un vrai courriel d'alerte avant
        de se fier à `foncier marche`.

        Options : --fichier (.eml/.html/.txt) OU --jours pour lire Gmail
        """
        from core.foncier.sources import centris

        context = context or {}
        fichier = context.get("fichier")
        jours = int(context.get("jours", 7))

        try:
            if fichier:
                fiches, diagnostic = await asyncio.to_thread(centris.lire_fichier, fichier)
                origine = f"fichier {fichier}"
            else:
                fiches, diagnostic = await asyncio.to_thread(centris.lire_alertes, jours, 5)
                origine = f"Gmail, {jours} derniers jours"
        except FileNotFoundError:
            return f"❌ Fichier introuvable : {fichier}"
        except Exception as e:
            return (
                f"❌ {e}\n\n"
                "Sauvegarder une alerte Centris en .eml et relancer avec "
                "--fichier permet de calibrer sans accès Gmail."
            )

        lignes = [
            "🔧 CALIBRATION — parseur d'alertes Centris",
            f"   Source : {origine}",
            f"   Expéditeurs surveillés : {', '.join(centris.EXPEDITEURS)}",
            "",
            diagnostic.rapport(),
            "",
        ]

        if fiches:
            lignes.append("FICHES EXTRAITES")
            for fiche in fiches[:20]:
                lignes.append(f"  #{fiche.no_centris}")
                lignes.append(f"     url        : {fiche.url}")
                lignes.append(f"     adresse    : {fiche.adresse or '❌ NON EXTRAITE'}")
                lignes.append(f"     ville      : {fiche.municipalite or '❌ NON EXTRAITE'}")
                lignes.append(
                    "     prix       : "
                    + (f"{fiche.prix_demande:,.0f} $".replace(",", " ")
                       if fiche.prix_demande else "❌ NON EXTRAIT")
                )
                lignes.append(f"     type       : {fiche.type_declare or '—'}")
                lignes.append(f"     logements  : {fiche.nombre_logements or '—'}")
                if fiche.adresse:
                    from core.foncier.adresse import cle as cle_adresse

                    lignes.append(
                        f"     clé rappro : "
                        f"{cle_adresse(fiche.adresse, fiche.municipalite)}"
                    )
                lignes.append("")
        else:
            lignes.append("AUCUNE FICHE EXTRAITE")
            lignes.append("")

        lignes.append("TEXTE ANALYSÉ (1500 premiers caractères)")
        lignes.append("─" * 55)
        lignes.append(diagnostic.echantillon_texte or "(vide)")
        lignes.append("─" * 55)
        lignes.append("")
        lignes.append(
            "Si des champs sont ❌ NON EXTRAITS, envoie-moi ce rapport : les "
            "motifs d'extraction sont dans core/foncier/sources/centris.py et "
            "s'ajustent en quelques lignes."
        )

        return "\n".join(lignes)

    async def charger_role(self, context: dict | None = None) -> str:
        """
        Charge un rôle d'évaluation, applique la thèse et score le parc.

        Options :
          --fichier    chemin local (CSV, GeoJSON, XML)
          --url        URL du rôle (mise en cache un mois)
          --millesime  année du rôle
          --limite     n'analyser que les N premières lignes (mise au point)
        """
        from core.foncier.sources import roles
        from core.foncier.sources.zonage import Zonage
        from core.foncier import finance, scoring, store

        context = context or {}
        fichier = context.get("fichier")
        url = context.get("url")

        if not fichier and not url:
            return (
                "❌ Préciser --fichier ou --url.\n\n"
                "Les rôles d'évaluation du Québec (1 140 fichiers XML, millésimes "
                "2008 à 2026) sont sur Données Québec :\n"
                "  https://www.donneesquebec.ca/recherche/dataset/roles-d-evaluation-fonciere-du-quebec\n\n"
                "Exemple :\n"
                "  python dispatch.py foncier roles --fichier data/role-2026.geojson --millesime 2026"
            )

        millesime = int(context["millesime"]) if context.get("millesime") else None
        limite = int(context["limite"]) if context.get("limite") else None

        try:
            if fichier:
                immeubles = await asyncio.to_thread(
                    roles.charger_fichier, fichier, millesime, limite
                )
            else:
                immeubles = await asyncio.to_thread(
                    roles.charger_url, url, millesime, limite, False
                )
        except roles.SchemaInconnu as e:
            return f"❌ Schéma non reconnu.\n\n{e}"
        except (OSError, ValueError) as e:
            return f"❌ Chargement impossible : {e}"

        if not immeubles:
            return "Aucun immeuble lisible dans la source."

        # Zonage — aires TOD et zones PPU.
        zonage = await asyncio.to_thread(Zonage.charger)
        compteur_zonage = zonage.enrichir(immeubles) if zonage.actif else {}

        # Filtres de la thèse + scoring.
        retenus, rejets = await asyncio.to_thread(scoring.classer, immeubles)

        # Estimation financière sur les retenus seulement.
        for immeuble in retenus:
            finance.appliquer(immeuble)

        resume_store = await asyncio.to_thread(store.enregistrer, retenus)

        lignes = [
            f"📋 Rôle chargé — {len(immeubles)} unité(s) d'évaluation lue(s)",
            f"   {len(retenus)} retenue(s) par la thèse, {len(rejets)} écartée(s)",
            "",
            scoring.resume_rejets(rejets),
            "",
        ]

        if compteur_zonage:
            lignes.append(
                f"Zonage — {compteur_zonage.get('tod', 0)} en aire TOD, "
                f"{compteur_zonage.get('ppu', 0)} en zone PPU"
            )
            if compteur_zonage.get("sans_coordonnees"):
                lignes.append(
                    f"  ⚠️  {compteur_zonage['sans_coordonnees']} sans coordonnées — "
                    "signaux TOD/PPU non évaluables (utiliser une source géoréférencée)"
                )
        elif not zonage.actif:
            lignes.append("⚠️  Zonage non configuré — 35 des 100 points inaccessibles.")

        lignes.append("")
        lignes.append(
            f"Base — {resume_store['nouveaux']} nouveau(x), "
            f"{resume_store['mis_a_jour']} mis à jour"
        )

        dans_cible = [
            i for i in retenus
            if i.cap_rate_estime and i.cap_rate_estime >= 0.07
        ]
        if dans_cible:
            lignes.append(f"   {len(dans_cible)} avec cap rate estimé ≥ 7 %")

        lignes.append("")
        lignes.append("Dix meilleurs :")
        for immeuble in retenus[:10]:
            lignes.append(f"  {immeuble.resume()}")

        return "\n".join(lignes)

    # ─────────────────────────────────────────────────────────────────────────
    # Consultation
    # ─────────────────────────────────────────────────────────────────────────

    async def top(self, context: dict | None = None) -> str:
        """Meilleurs dossiers en base. Options : --limite --municipalite --score"""
        from core.foncier import scoring, store

        context = context or {}
        limite = int(context.get("limite", 15))
        municipalite = context.get("municipalite")
        score_minimum = int(context.get("score", 0))
        detail = bool(context.get("detail"))

        immeubles = await asyncio.to_thread(
            store.meilleurs, limite, score_minimum, None, municipalite
        )

        if not immeubles:
            return "Aucun dossier en base. Lancer `foncier scan` ou `foncier roles`."

        lignes = [f"🏢 {len(immeubles)} meilleur(s) dossier(s)", ""]
        for immeuble in immeubles:
            lignes.append(immeuble.resume())
            if detail:
                for ligne in scoring.expliquer(immeuble).splitlines()[1:]:
                    lignes.append(f"    {ligne.strip()}")
                lignes.append("")

        progres = await asyncio.to_thread(store.progressions, 60, 10)
        if progres:
            lignes.append("")
            lignes.append("En progression sur 60 jours :")
            for p in progres[:5]:
                lignes.append(
                    f"  ↗ +{p['progression']} pts → {p['score']} — "
                    f"{p['adresse']}, {p['municipalite']}"
                )

        return "\n".join(lignes)

    async def distribution_cubf(self, context: dict | None = None) -> str:
        """
        Distribution réelle des codes d'utilisation d'un rôle.

        À lancer avant la première campagne sur une nouvelle municipalité, pour
        valider que les groupes CUBF de `criteres.py` correspondent bien à ce
        que la municipalité publie.
        """
        from core.foncier.sources import roles

        context = context or {}
        fichier = context.get("fichier")
        if not fichier:
            return "❌ Préciser --fichier (le rôle à analyser)."

        try:
            immeubles = await asyncio.to_thread(
                roles.charger_fichier, fichier, None,
                int(context["limite"]) if context.get("limite") else None,
            )
        except (roles.SchemaInconnu, OSError, ValueError) as e:
            return f"❌ {e}"

        return roles.distribution_cubf(immeubles, int(context.get("codes", 30)))

    async def marquer(self, context: dict | None = None) -> str:
        """Change le statut d'un dossier. --id <identifiant> --statut <statut> --note <texte>"""
        from core.foncier import store

        context = context or {}
        identifiant = context.get("id")
        if not identifiant:
            return "❌ Préciser --id (identifiant du dossier, visible dans `foncier top`)."

        statuts_valides = (
            "nouveau", "surveille", "enrichi", "en_vente", "contacte", "rejete"
        )
        statut = context.get("statut")
        if statut and statut not in statuts_valides:
            return f"❌ Statut invalide. Valides : {', '.join(statuts_valides)}"

        ok = await asyncio.to_thread(
            store.marquer, identifiant, statut, context.get("note")
        )
        return f"✅ {identifiant} mis à jour." if ok else f"❌ {identifiant} introuvable."

    # ─────────────────────────────────────────────────────────────────────────
    # Enrichissement payant
    # ─────────────────────────────────────────────────────────────────────────

    async def enrichir(self, context: dict | None = None) -> str:
        """
        Enrichit les meilleurs dossiers via le registre foncier. PAYANT.

        Ne touche que les dossiers au-dessus du seuil et jamais deux fois le même.
        Options : --limite (défaut 10) --budget (plafond en $) --seuil
        """
        from core.foncier.criteres import DEFAUT
        from core.foncier.sources import registre
        from core.foncier import finance, scoring, store

        context = context or {}
        limite = int(context.get("limite", 10))
        seuil = int(context.get("seuil", DEFAUT.seuil_enrichissement))
        plafond = float(context.get("budget", registre.BUDGET_PAR_PASSE))

        if not registre.fournisseurs_actifs():
            return registre.diagnostic()

        # Garde-fou mensuel avant toute dépense.
        depense_mois = await asyncio.to_thread(store.depenses_du_mois)
        if depense_mois >= registre.BUDGET_MENSUEL:
            return (
                f"🛑 Plafond mensuel atteint : {depense_mois:.2f} $ / "
                f"{registre.BUDGET_MENSUEL:.2f} $. Aucun enrichissement effectué."
            )

        restant = registre.BUDGET_MENSUEL - depense_mois
        plafond = min(plafond, restant)

        candidats = await asyncio.to_thread(store.meilleurs, limite * 3, seuil)
        if not candidats:
            return f"Aucun dossier au-dessus de {seuil} points. Rien à enrichir."

        # On ne paie jamais deux fois pour le même immeuble.
        neufs = []
        for immeuble in candidats:
            if not await asyncio.to_thread(store.deja_enrichi, immeuble.identifiant):
                neufs.append(immeuble)
            if len(neufs) >= limite:
                break

        if not neufs:
            return (
                f"{len(candidats)} dossier(s) au-dessus de {seuil} points, "
                "tous déjà enrichis. Aucune dépense."
            )

        budget = registre.Budget(plafond=plafond)
        enrichis, budget, avertissements = await asyncio.to_thread(
            registre.enrichir, neufs, budget, limite
        )

        for immeuble in neufs:
            scoring.scorer(immeuble)
            finance.appliquer(immeuble)

        await asyncio.to_thread(store.enregistrer, neufs)

        lignes = [
            f"💳 Enrichissement — {enrichis} dossier(s) sur {len(neufs)} candidat(s)",
            f"   {budget.resume()}",
            f"   Cumul du mois : {depense_mois + budget.depense:.2f} $ / "
            f"{registre.BUDGET_MENSUEL:.2f} $",
            "",
        ]

        for avertissement in avertissements:
            lignes.append(f"⚠️  {avertissement}")

        enrichis_tries = sorted(
            [i for i in neufs if i.proprietaire or i.date_acquisition],
            key=lambda i: -i.score,
        )
        if enrichis_tries:
            lignes.append("")
            lignes.append("Résultats :")
            for immeuble in enrichis_tries:
                detention = (
                    f"{immeuble.annees_detention} ans"
                    if immeuble.annees_detention is not None
                    else "détention inconnue"
                )
                lignes.append(
                    f"  [{immeuble.score}] {immeuble.adresse or immeuble.lot} — "
                    f"{immeuble.proprietaire or 'propriétaire non retourné'} — {detention}"
                )

        return "\n".join(lignes)

    # ─────────────────────────────────────────────────────────────────────────
    # Brief
    # ─────────────────────────────────────────────────────────────────────────

    async def brief(self, context: dict | None = None) -> str:
        """
        Dossier d'analyse sur les meilleurs candidats, rédigé par Claude,
        livré dans Notion et résumé sur Telegram.

        Claude n'intervient qu'ici, sur une poignée de dossiers déjà classés par
        le scoring déterministe — jamais pour trier des centaines de milliers
        d'unités d'évaluation.
        """
        from core.foncier.criteres import DEFAUT
        from core.foncier import finance, store

        context = context or {}
        limite = int(context.get("limite", 8))
        seuil = int(context.get("seuil", DEFAUT.seuil_alerte))

        immeubles = await asyncio.to_thread(store.meilleurs, limite, seuil)

        if not immeubles:
            stats = await asyncio.to_thread(store.statistiques)
            return (
                f"Aucun dossier au-dessus de {seuil} points "
                f"({stats['total']} immeuble(s) en base).\n"
                "Baisser le seuil avec --seuil, ou alimenter la base "
                "(`foncier scan`, `foncier roles`)."
            )

        for immeuble in immeubles:
            if immeuble.noi_estime is None:
                finance.appliquer(immeuble)

        contexte_dossiers = self._formater_dossiers(immeubles)
        texte = await self._rediger(contexte_dossiers, limite)

        # Livraison Notion.
        url_notion = None
        try:
            from core.notion_delivery import pipeline_create

            url_notion = await pipeline_create(
                title=f"Prospection foncière — {datetime.now():%d %B %Y}",
                agent="foncier",
                type_="prospection",
                content=texte,
                status="Prêt révision",
            )
        except Exception as e:
            log.warning("foncier.brief: Notion indisponible (%s)", e)

        entete = [
            f"🏗️  PROSPECTION FONCIÈRE — {len(immeubles)} dossier(s) à {seuil}+ points",
        ]
        if url_notion:
            entete.append(f"📄 {url_notion}")
        entete.append("")

        return "\n".join(entete) + texte

    def _formater_dossiers(self, immeubles: list) -> str:
        """Prépare le contexte factuel envoyé à Claude. Aucune interprétation ici."""
        from core.foncier import scoring

        blocs = []
        for i, immeuble in enumerate(immeubles, 1):
            valeur = (
                f"{immeuble.valeur_totale:,.0f} $".replace(",", " ")
                if immeuble.valeur_totale else "inconnue"
            )
            details = [
                f"DOSSIER {i} — {immeuble.adresse or 'adresse inconnue'}, {immeuble.municipalite}",
                f"  identifiant     : {immeuble.identifiant}",
                f"  lot             : {immeuble.lot or 'n/d'}",
                f"  usage (CUBF)    : {immeuble.cubf or 'n/d'} — {immeuble.libelle_utilisation or 'n/d'}",
                f"  type            : {immeuble.type_immeuble}",
                f"  logements       : {immeuble.nombre_logements or 'n/d'}",
                f"  année           : {immeuble.annee_construction or 'n/d'}",
                f"  terrain / bâti  : {immeuble.superficie_terrain or 'n/d'} m² / "
                f"{immeuble.superficie_batiment or 'n/d'} m²",
                f"  valeur au rôle  : {valeur} (millésime {immeuble.millesime or 'n/d'})",
                f"  propriétaire    : {immeuble.proprietaire or 'NON ENRICHI'}",
                "  détention       : "
                + (f"{immeuble.annees_detention} ans" if immeuble.annees_detention is not None
                   else "INCONNUE — dossier non enrichi"),
                "  NOI estimé      : "
                + (f"{immeuble.noi_estime:,.0f} $".replace(",", " ") if immeuble.noi_estime
                   else "non estimable"),
                "  cap rate estimé : "
                + (f"{immeuble.cap_rate_estime:.2%}" if immeuble.cap_rate_estime else "n/d")
                + f" (confiance : {immeuble.confiance_financiere})",
                "  " + scoring.expliquer(immeuble).replace("\n", "\n  "),
            ]
            blocs.append("\n".join(details))

        return "\n\n".join(blocs)

    async def _rediger(self, dossiers: str, limite: int) -> str:
        """Fait rédiger le brief par Claude. Retombe sur le brut si l'API échoue."""
        from core.foncier.criteres import DEFAUT

        consigne = f"""Tu es analyste en acquisition immobilière pour un investisseur québécois.

THÈSE D'ACQUISITION
· Commercial et multirésidentiel
· 1 M$ à 5 M$
· NOI visé de 7 à 8 %
· Potentiel de développement : densification, modification de zonage
· Secteurs TOD et zones PPU
· Propriétaires détenant depuis plus de {DEFAUT.detention_seuil} ans
· Préavis d'exercice comme signal de motivation

Voici {limite} dossiers déjà filtrés et scorés par un moteur déterministe.

{dossiers}

Rédige un dossier d'analyse en français québécois, en markdown, structuré ainsi :

1. **Lecture d'ensemble** — 3 à 4 phrases : ce que ce lot de dossiers dit du
   marché en ce moment, et où se concentre l'occasion.

2. **Par dossier** (les 5 meilleurs seulement) :
   - Pourquoi ce dossier, en une phrase
   - L'angle d'approche du propriétaire
   - Ce qu'il faut vérifier avant de dépenser du temps dessus
   - Le risque principal, nommé franchement

3. **Prochaines actions** — 3 à 5 actions concrètes et ordonnées.

RÈGLES ABSOLUES
· N'invente aucun chiffre. Si une donnée est marquée « n/d », « INCONNUE » ou
  « NON ENRICHI », dis-le explicitement plutôt que de l'estimer.
· Les NOI et cap rates fournis sont des ESTIMATIONS reconstruites à partir du
  rôle d'évaluation, pas des états financiers. Traite-les comme des indices de
  classement, jamais comme des chiffres d'offre. Rappelle-le quand tu t'appuies
  dessus.
· Un dossier non enrichi a une détention inconnue : ne suppose jamais qu'un
  propriétaire détient depuis longtemps sans la donnée.
· Pas de langage de vente. Un ton d'analyste qui présente aussi ce qui cloche."""

        try:
            from config import CLAUDE_MODEL
            from core.brain import get_client

            client = get_client()
            reponse = await asyncio.to_thread(
                client.messages.create,
                model=CLAUDE_MODEL,
                max_tokens=4000,
                messages=[{"role": "user", "content": consigne}],
            )
            return "".join(
                bloc.text for bloc in reponse.content if getattr(bloc, "type", "") == "text"
            )

        except Exception as e:
            log.error("foncier.brief: rédaction impossible (%s)", e, exc_info=True)
            return (
                f"⚠️  Rédaction Claude indisponible ({e}).\n"
                "Données brutes des dossiers :\n\n" + dossiers
            )


agent = FoncierAgent()
