# Score urbanistique pour la compensation écologique — note de méthode

**Version des règles : 0.1.0 — statut : proposition à valider par un urbaniste.**

Cette note explique ce que mesure le score, comment il est calculé, comment il traite les erreurs
des données du Géoportail de l'Urbanisme (GPU), et quelles hypothèses demandent un avis d'expert.
Le détail des ~140 règles (effets, seuils, justifications) est dans `docs/REGLES_SCORING.md`,
généré automatiquement depuis `urba_profiler/scoring/regles_compensation.yaml`. La grille de
relecture à compléter est `docs/revue_regles_scoring.xlsx`.

---

## 1. Ce que le score mesure, et ce qu'il ne mesure pas

**Question posée** : *le contexte d'urbanisme de cette parcelle est-il favorable à une mesure
compensatoire pérenne (30 ans et plus), et les règles locales permettent-elles de la réaliser ?*

Le score ne mesure **pas** :
- le gain écologique (état du milieu, potentiel de restauration) : c'est le rôle des analyses
  environnementales (zones humides probables, espèces, occupation du sol...) ;
- la difficulté de négociation avec le propriétaire : c'est la dureté foncière. Le sous-score
  *maîtrise* ne porte que sur les contraintes **réglementaires** d'acquisition (préemption,
  bénéficiaire public) ;
- la conformité au règlement écrit : le texte du règlement n'est pas lu à ce stade. Le score
  repose sur les objets cartographiques du GPU et leurs codes. C'est la limite principale (§ 8).

Le score est un outil de **priorisation et de tri** du pool de parcelles, pas un avis juridique.

## 2. Quatre sous-scores et un indice de confiance

| Sous-score | Poids | Question | Point de départ |
|---|---|---|---|
| **Pérennité** | 0,45 | Un usage concurrent (urbanisation, équipement, ENR, carrière) risque-t-il de remettre la mesure en cause ? | Base du zonage |
| **Compatibilité** | 0,30 | Les règles permettent-elles les travaux et la gestion écologique (défrichement, affouillements, plantations, servitudes techniques) ? | 80 |
| **Synergie** | 0,15 | Le document va-t-il dans le sens de la mesure (TVB, renaturation, PAEN...) ? Diminuée par les risques d'additionnalité (site déjà protégé). | 50 |
| **Maîtrise** | 0,10 | Des contraintes réglementaires pèsent-elles sur l'acquisition (DPU, ZPENS, ZAD, emplacement réservé, propriété publique) ? | 80 |

La pérennité pèse le plus : une mesure compensatoire est un engagement de très long terme,
et c'est le critère sur lequel l'urbanisme renseigne le mieux.

L'**indice de confiance** (0-100) est calculé à part et **ne modifie pas le score** : il dit si
on peut s'y fier (zonage absent, codes incohérents, couches indisponibles, document ancien ou
partiellement annulé...). Une parcelle bien notée avec une confiance faible doit être vérifiée
avant d'être retenue.

## 3. Calcul pas à pas

1. **Base de pérennité du zonage**. Chaque type de zone a une base (N 85, A 72, AUs 25, AUc 8,
   U 5, RNU 60...). On en fait la moyenne pondérée par le % de la parcelle couvert par chaque zone.
   Ex. : A 80 % + N 20 % → 0,8 × 72 + 0,2 × 85 = 74,6.
2. **Sous-secteurs** (Nzh, Npv, Nl, Ap, Nf...) : détectés par le suffixe du libellé court et/ou
   des mots-clés du libellé long. Bonus ou malus pondérés par la surface de la zone.
3. **Éléments** (prescriptions, informations, servitudes) : chaque élément est *interprété*
   (§ 4) pour obtenir une règle et une fiabilité, puis :
   `points = effet de la règle × facteur de couverture × multiplicateur de fiabilité`
   - facteur surfacique = % de la parcelle couvert (plancher 0,10, plafond 1) ;
   - facteur linéaire = longueur dans la parcelle / √surface (borné entre 0,2 et 1), réduit de
     moitié si la ligne ne fait que longer la limite ;
   - ponctuel : 0,30 à l'intérieur, 0,15 en limite ;
   - portée « présence » (DPU, ZPENS, sursis à statuer) : facteur 1 dès que l'élément est présent ;
   - fiabilité : haute × 1, moyenne × 0,8, basse × 0,6.
   Les contributions d'une **même règle** se cumulent, plafonnées à un facteur de 1 : trois
   éléments TVB ne comptent pas trois fois.
4. **Voisinage** (anneau de 200 m) : malus de pression urbaine (part U/AU, plafonné à −20) et
   bonus de continuité naturelle (part N/A, plafonné à +10).
5. **Sous-scores** = point de départ + somme des contributions, bornés à [0 ; 100].
6. **Score global** = moyenne pondérée des sous-scores.
7. **Vetos** : si une condition rédhibitoire est remplie (ex. zone U ≥ 50 %, OAP de projet ≥ 30 %,
   EBC ≥ 50 % pour une mesure de zone humide), le score est plafonné : **15** pour un veto
   bloquant, **40** pour un veto majeur. Un veto n'est jamais noyé dans une moyenne.
8. **Classe** : A ≥ 75 (très favorable), B ≥ 60, C ≥ 45 (à étudier), D ≥ 30, E < 30.

Chaque résultat est **explicable** : la sortie liste les contributions (règle, points par
sous-score, éléments concernés, fiabilité, justification), les vetos, les points à vérifier et
l'interprétation retenue pour chaque élément GPU.

## 4. Données GPU imparfaites : comment le moteur s'en protège

Les codes CNIG saisis par les collectivités sont **souvent faux ou génériques** (EBC codé en
emplacement réservé, prescriptions en code 99, zonages dont le `typezone` contredit le libellé,
objets versés dans la mauvaise couche). Le moteur ne fait donc confiance à aucune source seule.

### 4.1 Interprétation d'une prescription, d'une information ou d'une servitude

Le moteur confronte deux lectures :
- le **code** (`typepsc/stypepsc`, `typeinf/stypeinf`, catégorie SUP) → règle « codée » ;
- le **libellé saisi** (libellé, étiquette, nature ; nom de la servitude et de l'assiette) →
  règles déclenchées par des mots-clés (EBC, emplacement réservé, trame verte, zone humide,
  photovoltaïque...).

| Situation | Décision | Fiabilité |
|---|---|---|
| Code reconnu, libellé cohérent ou muet | règle du code | haute (moyenne si libellé vide) |
| Code générique (99, 05-00, 18-00, 07-00/02) + libellé explicite | le libellé **précise** le code (ex. 05-00 « bassin de rétention » → ER hydraulique) | haute ou moyenne |
| Code reconnu + libellé d'un groupe **compatible** (ex. 02-01 inconstructible + « zone inondable ») | règle du code | haute |
| Code reconnu + libellé d'un groupe **incompatible** (ex. 01-00 EBC + « ER n°4 élargissement de voie ») | **le libellé l'emporte**, conflit signalé et point à vérifier | moyenne |
| Code absent, hors nomenclature ou repli de sous-type | libellé seul | basse |
| Ni code ni libellé exploitables | non interprété (compté dans la confiance) | — |

**Choix structurant à valider** : en cas de conflit, le libellé l'emporte sur le code. Le
libellé est en général recopié de la légende du plan de zonage, alors que le code est choisi
dans une liste par l'opérateur de numérisation, d'où des erreurs plus fréquentes.

Garde-fous :
- un mot-clé « incident » ne crée pas de conflit si **l'un** des mots-clés trouvés confirme le
  code (« ER n°3 le long de l'EBC » reste un ER) ;
- les mots-clés s'appliquent aux couches prescription **et** information (objets mal rangés),
  alors que les codes restent propres à chaque famille (le code 25 vaut TVB en prescription
  mais PAEN en information) ;
- les mots-clés ont été choisis pour **ne pas** se déclencher sur les définitions réglementaires
  standard des zones (« zone naturelle et forestière à protéger... », « constructions nécessaires
  à l'activité agricole »). Des tests le vérifient.

### 4.2 Type de zone

Le `typezone` saisi est contrôlé par le préfixe du libellé (1AU, 2AU, U, A, N) :
- même grande famille → `typezone` retenu, fiabilité haute ;
- familles différentes (ex. typezone N, libellé « 1AUh ») → **libellé retenu**, conflit signalé ;
- AUc / AUs incohérents → **AUc retenu** (hypothèse la plus prudente pour la pérennité) ;
- aucun zonage → base RNU (60) et confiance −30, avec un point à vérifier (RNU ou document
  non versé au GPU).

### 4.3 Vetos et fiabilité

Un veto déclenché par une donnée de **fiabilité basse** (libellé seul, sans code) **n'est pas
appliqué** : il devient un point à vérifier. On ne bloque pas une parcelle sur une donnée douteuse.

## 5. Profils de mesure

Le même élément n'a pas le même effet selon la mesure envisagée. Quatre profils :
`generique` (lecture prudente), `zones_humides`, `boisement`, `milieux_ouverts`.

Exemples :
- **EBC** : bloquant pour une restauration de zone humide ou une réouverture (défrichement
  irrecevable, L113-2), neutre pour un boisement, avec un léger malus d'additionnalité
  (boisement déjà protégé).
- **Lignes électriques, canalisations** : forte contrainte pour un boisement (bande non
  plantable), faible pour une prairie.
- **Zone inondable / PPR** : bonus de synergie pour une zone humide.
- **Réglementation des boisements (code rural)** : quasi rédhibitoire pour un boisement.
- **Aérodromes** : malus pour les zones humides (péril animalier).

Le profil est passé en option (`scoring_profil`). On peut recalculer avec un autre profil sans
nouvel appel WFS (`POST /urba/score`).

## 6. Hypothèses structurantes à valider en priorité

1. **Poids** des sous-scores (0,45 / 0,30 / 0,15 / 0,10).
2. **Bases de pérennité** par type de zone, en particulier A (72) par rapport à N (85), et le
   traitement des AUs (25 + veto majeur au-delà de 50 %).
3. **Le libellé l'emporte sur le code** en cas de conflit (§ 4.1).
4. **Additionnalité** : réserve naturelle et cœur de parc → veto majeur ; espaces remarquables
   du littoral, EBC pour un boisement, secteurs naturels protégés → malus de synergie.
5. **Seuils de veto** : U ≥ 50 %, AUc ≥ 30 %, OAP de projet ≥ 30 %, ER voirie ≥ 50 % (bloquant)
   ou ≥ 30 % (majeur), EBC ≥ 50 % (bloquant pour zones humides et milieux ouverts).
6. **Conventions de suffixes** (Nzh, Npv, Nl, Ai, Ac...) : elles varient selon les bureaux
   d'études. Toutes les règles de suffixe ambiguës sont marquées « à confirmer ».
7. **Catégories de SUP** : les catégories A1, A2-A5, A8, A9, AC4, AR, EL10, I6, JS1, PM2, PM4+, PT
   et T sont interprétées à partir de ma connaissance de la nomenclature, à vérifier contre le
   standard CNIG SUP en vigueur.

La section 11 de `REGLES_SCORING.md` liste les **25 questions ouvertes**.

## 7. Process de relecture proposé

1. Relire d'abord les **scénarios de calibration** (section 10 de `REGLES_SCORING.md` ou onglet
   « Scénarios » de la grille). Si le **classement relatif** de ces parcelles types semble faux,
   ce sont les poids et les bases qu'il faut revoir avant les règles une à une.
2. Compléter la grille `revue_regles_scoring.xlsx` : pour chaque règle, Valider / Modifier /
   Supprimer, avec une proposition et un commentaire.
3. Reporter les modifications dans le YAML, incrémenter `version`, régénérer la documentation,
   relancer les tests.
4. Recalculer les scores déjà stockés via `POST /urba/score` : la version des règles figure
   dans chaque résultat (`version_regles`).

## 8. Limites connues

- **Le règlement écrit n'est pas lu.** Une zone A peut, selon son règlement, interdire les
  affouillements ou au contraire autoriser des bâtiments d'élevage. C'est l'objet de l'étape
  d'analyse par LLM, qui pourra s'appuyer sur ce score, ses contributions et ses points à vérifier.
- Le zonage réglementaire des PPR (rouge / bleu) n'est pas différencié : seule la servitude PM1
  est vue.
- La procédure d'évolution du document (révision en cours) n'est captée que par le sursis à
  statuer et l'âge du document.
- Le voisinage ne considère que le zonage (pas les permis Sitadel, pas l'occupation du sol).
- Un document non versé au GPU (RNU apparent) ne peut pas être distingué d'une vraie commune au RNU.

## 9. Où se trouve quoi

| Fichier | Rôle |
|---|---|
| `scoring/regles_compensation.yaml` | **Toute la logique métier** (bases, règles, effets, vetos, mots-clés, profils, poids) |
| `scoring/loader.py` | Chargement et validation stricte du YAML (erreurs explicites) |
| `scoring/engine.py` | Moteur : interprétation code × libellé, zonage, facteurs, vetos, confiance (aucune valeur métier en dur) |
| `scoring/schemas.py` | Format de sortie `ScoreUrba` |
| `scoring/doc.py` | Génération de `REGLES_SCORING.md` et de la grille Excel |
| `tests/test_scoring.py` | 40 tests (49 cas avec les paramétrages), dont l'exhaustivité vis-à-vis de la nomenclature CNIG v2024-01 |
