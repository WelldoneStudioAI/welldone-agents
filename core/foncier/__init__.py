"""
core/foncier — Moteur de prospection immobilière Québec.

Le registre foncier ne liste PAS d'immeubles à vendre : il publie des droits.
Ce moteur inverse la logique — il détecte les immeubles qui *vont* se vendre,
en croisant des sources publiques gratuites et en ne payant l'enrichissement
(registre foncier, JLR) que sur les dossiers ayant déjà passé le scoring.

Couches :
  A. socle gratuit    → rôles d'évaluation, cadastre
  B. signaux publics  → SEAO, aires TOD, PPU, ventes pour taxes
  C. enrichissement   → registre foncier / JLR / fonciq (payant, à la pièce)
  D. scoring + brief  → agents/foncier.py
"""
