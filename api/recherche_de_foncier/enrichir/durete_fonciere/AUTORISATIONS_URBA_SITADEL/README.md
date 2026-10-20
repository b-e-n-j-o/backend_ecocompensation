# Autorisations d'urbanisme (Sitadel × cadastre actuel)

Recherche en lecture seule sur les tables déjà croisées
(`sitadel_parcelle`, `sitadel_autorisation`, jeux `logements` / `locaux` / `pa` / `pd`).
Couverture : Nouvelle-Aquitaine, cadastre Etalab actuel. Pas les millésimes.

Pour chaque IDU :

- dossiers **sur** la parcelle (`sitadel_parcelle.idu`) ;
- dossiers sur une parcelle **voisine** : même département, `ST_Touches`
  (y compris un contact par un coin). La définition « attenante » pourra changer.

Le score de dureté n'est pas modifié. `signal_score.applique_au_score` reste `false`.
Si `sitadel_autorisation` est verrouillée (calcul des points en cours), le lien
parcelle, les dates et l'état sont quand même renvoyés ; `geoloc_statut` reste vide
et un avertissement l'indique.
Hypothèse de surcharge (voir `signal_score.py`) : +15 pour un PC/PA actif de moins
de 10 ans sur la parcelle, +5 s'il est plus ancien, +0 pour une DP, une démolition
seule, un permis seulement chez le voisin, un lien ambigu ou un permis annulé.
Le branchement prévu, s'il est retenu, est le même que le bonus vigne : une
surcharge par IDU après le score SIREN, plafonnée à 100, hors prompt LLM.

## Pool

`POST /api/projects/{project_id}/pool/runs/{run_id}/recompute-autorisations-urba`

Écrit la métrique `autorisations_urba` dans `parcelles_pool_metrics`.
Elle ressort dans le snapshot du run (`by_idu`), à côté de la dureté, sans la recalculer.
Les parcelles indésirables du projet sont exclues, comme pour la dureté.

## Seul

`POST /api/autorisations-urba/parcelles` avec `{ "idus": ["..."], "avec_voisins": true }`

`GET /api/autorisations-urba/parcelles/{idu}`

```bash
python -m durete_fonciere.AUTORISATIONS_URBA_SITADEL.service --idus 33063000AB0123
```
