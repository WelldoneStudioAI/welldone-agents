"""
core/foncier/sources — Connecteurs vers les sources de données.

Règle de conception : chaque connecteur retourne des objets normalisés
(`Immeuble` ou `Signal`) et ne connaît rien du scoring. Ajouter une source
(une nouvelle MRC, un nouveau fournisseur) ne touche jamais au moteur.

Gratuites et en lot :
  roles          — rôles d'évaluation foncière (Données Québec, portails municipaux)
  seao           — avis d'appel d'offres publics
  zonage         — aires TOD de la CMM, zones PPU municipales
  ventes_taxes   — avis de vente pour défaut de paiement de taxes des MRC

Payante, à la pièce :
  registre       — registre foncier via fonciq / JLR
"""
