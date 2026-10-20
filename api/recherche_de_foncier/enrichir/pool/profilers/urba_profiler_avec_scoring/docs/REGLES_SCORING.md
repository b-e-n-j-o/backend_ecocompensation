# Référentiel de scoring urbanistique — compensation écologique

Version des règles **0.1.0** — statut : *proposition — à valider par un urbaniste*. Document généré le 13/10/2026 depuis `regles_compensation.yaml` : ne pas éditer à la main.

Méthode et principes : voir `SCORING.md`. Abréviations : pér. = pérennité, compat. = compatibilité, syn. = synergie, maît. = maîtrise foncière, conf. = confiance. Les effets sont les points appliqués pour une couverture totale de la parcelle et une donnée fiable.

## 1. Paramètres généraux

| Sous-score | Poids | Base | Définition |
|---|---|---|---|
| perennite | 0.45 | zonage | Risque qu'un usage concurrent (urbanisation, équipement, énergie, extraction) remette en cause la mesure sur 30 ans et plus |
| compatibilite | 0.30 | 80 | Compatibilité réglementaire des travaux et de la gestion écologique (défrichement, affouillements, plantations, servitudes techniques) |
| synergie | 0.15 | 50 | Alignement avec les politiques locales de biodiversité (TVB, renaturation, PAEN...) ; diminuée par les risques d'additionnalité (site déjà protégé) |
| maitrise | 0.10 | 80 | Contraintes réglementaires sur l'acquisition ou la maîtrise d'usage (préemption, bénéficiaire public, propriété publique) |

**Profils** : `generique` — Mesure non précisée (lecture prudente) ; `zones_humides` — Restauration / création de zones humides, mares, reméandrage ; `boisement` — Boisement, reboisement, plantation de haies, îlots de sénescence ; `milieux_ouverts` — Pelouses, prairies, landes : réouverture, gestion extensive

**Classes** : A ≥ 75 (Très favorable) ; B ≥ 60 (Favorable) ; C ≥ 45 (Mitigé — à étudier) ; D ≥ 30 (Défavorable) ; E ≥ 0 (Très défavorable / bloquant)

**Vetos** : bloquant → score plafonné à 15 ; majeur → 40. Un veto déclenché par une donnée de fiabilité basse n'est pas appliqué (rétrogradé en point à vérifier).

**Fiabilité** (multiplicateur des points) : haute ×1.0, moyenne ×0.8, basse ×0.6

## 2. Zonage — base de pérennité

| Type | Base | Justification |
|---|---|---|
| N | 85 | Zone naturelle et forestière : inconstructible par principe (R151-24/25), exceptions encadrées (L151-11 à 13). Vocation la plus cohérente avec une mesure pérenne ; le risque résiduel est un reclassement lors d'une révision. |
| A | 72 | Zone agricole : inconstructible sauf constructions et installations agricoles, extensions/annexes, STECAL, équipements collectifs, et désormais agrivoltaïsme (loi APER 2023). Pérennité bonne mais concurrence d'usages agricoles et énergétiques. |
| Nh | 50 | Ancien secteur naturel de constructibilité limitée (habitat diffus) : constructions déjà admises. |
| Ah | 45 | Ancien secteur agricole de constructibilité limitée (habitat diffus). |
| AUs | 25 | Urbanisation future, ouverture par modification/révision. Incompatible avec un engagement de 30 ans, sauf zone 2AU ancienne : depuis la loi Climat et résilience, une zone AU non ouverte dans les 6 ans ne peut l'être que par révision (L153-31), ce qui la rend moins menaçante. |
| AUc | 8 | Zone à urbaniser ouverte : urbanisation à court terme. |
| U | 5 | Zone urbaine constructible. Seule exception : désartificialisation / renaturation de friches (non couverte par ce profil). |
| CC_01 | 10 | Carte communale — secteur ouvert à la construction. |
| CC_02 | 10 | Carte communale — secteur réservé aux activités. |
| CC_03 | 75 | Carte communale — secteur non ouvert à la construction (sauf exceptions de L161-4) : équivalent A/N. |
| CC_99 | 50 | Carte communale — secteur 'autre' non qualifié. |
| RNU | 60 | Aucun zonage au GPU : commune au RNU (constructibilité limitée hors parties actuellement urbanisées, L111-3) ou document non versé. Pérennité moyenne, fiabilité faible. |
| inconnu | 50 | Type de zone non déterminable. |

**Vetos de zonage**

| Types | Seuil | Niveau | Motif |
|---|---|---|---|
| U | ≥ 50 % | bloquant | Parcelle majoritairement en zone urbaine constructible. |
| AUc | ≥ 30 % | bloquant | Zone à urbaniser ouverte sur une part significative de la parcelle. |
| AUs | ≥ 50 % | majeur | Parcelle majoritairement en zone d'urbanisation future. |
| CC_01, CC_02 | ≥ 50 % | bloquant | Secteur constructible de carte communale. |

## 3. Zonage — sous-secteurs

| ID | Secteur | Types | Suffixe (regex) | Mots-clés du libellé long | Effets | Profils | Veto | Justification | À confirmer |
|---|---|---|---|---|---|---|---|---|---|
| `sect_zone_humide` | Sous-secteur zone humide | A, N | `^[AN][a-z0-9]*zh` | `zones? humides?` / `milieux? humides?` / `marais` / `tourbiere` | pér. +8, syn. +10 | *zones_humides* : pér. +8, syn. +15 |  | Zonage dédié aux zones humides : protection renforcée (pérennité) et cohérence directe avec une compensation ZH. |  |
| `sect_naturel_protege` | Sous-secteur naturel protégé / remarquable | N | `^N(p\|pr\|r\|rem\|s\|sp)\d*$` | `protection (stricte\|renforcee)` / `natura ?2000` / `\bznieff\b` / `reservoirs? de biodiversite` / `espaces? naturels? sensibles?` | pér. +8, syn. +3 |  |  | Protection renforcée du milieu naturel. Bonne pérennité, synergie limitée par le risque d'additionnalité. | ❓ Les suffixes Np/Nr/Ns sont-ils suffisamment homogènes pour être interprétés sans le libellé long ? |
| `sect_corridor` | Sous-secteur corridor / continuité écologique | A, N | `^[AN](co\|tvb\|ce\|eco)\d*$` | `corridor` / `continuite` / `trame (verte\|bleue)` / `\btvb\b` | pér. +5, syn. +15 |  |  | Le document identifie le secteur comme continuité écologique : forte cohérence avec une mesure compensatoire (argument pour l'instructeur). |  |
| `sect_forestier` | Sous-secteur forestier | N | `^Nf\d*$` | `secteur forestier` / `exploitation forestiere` / `sylvicole` / `vocation forestiere` / `massifs? boises?` | pér. +5 | *boisement* : pér. +5, syn. +5<br>*zones_humides* : pér. +5, compat. -5<br>*milieux_ouverts* : pér. +5, compat. -8 |  | Vocation forestière confirmée. Favorable au boisement, défavorable à la réouverture. |  |
| `sect_loisirs` | Sous-secteur loisirs / tourisme / sport | A, N | `^N(l\|t\|tl\|lt\|s?l\|g\|golf\|camp\|c?amp)\d*$` | `loisirs?` / `touristiques?` / `camping` / `\bgolf\b` / `sporti` / `hebergements? touristiques?` / `parc de loisirs` / `equestre` / `aire de jeux` | pér. -20 |  |  | Aménagements de loisirs et d'hébergement admis : concurrence d'usage et fréquentation. |  |
| `sect_equipements` | Sous-secteur équipements / services publics | A, N | `^[AN](e\|eq\|sp\|step\|ep\|gv)\d*$` | `equipements? (publics?\|collectifs?\|d.interet)` / `station d.epuration` / `services? publics?` / `gens du voyage` / `aire d.accueil` / `cimetiere` | pér. -20 |  |  | Secteur destiné à recevoir des équipements : usage concurrent probable. | ❓ Le suffixe 'e' est aussi parfois 'espace' ou 'écologique' selon les communes : garder la regex ou ne se fier qu'au libellé long ? |
| `sect_activites` | Sous-secteur d'activités économiques | A, N | `^[AN](x\|y\|act\|ae\|ia)\d*$` | `activites? (economiques?\|artisanales?\|industrielles?\|commerciales?)` / `artisanat` / `industriel` / `zone d.activites?` | pér. -25 |  |  | Activités économiques admises en zone A/N (STECAL de fait). |  |
| `sect_carriere` | Sous-secteur carrière / extraction | A, N | `^N(ca\|car\|carr\|k)\d*$` | `carriere` / `extraction` / `gravier` / `granulat` / `exploitation du sous-sol` / `sabliere` | pér. -30 |  |  | Exploitation du sous-sol en cours ou autorisable : incompatibilité à court terme, opportunité à terme. |  |
| `sect_energie` | Sous-secteur énergies renouvelables (photovoltaïque, éolien) | A, N | `(pv\|enr\|phv\|solaire\|eol)` | `photovolta` / `solaire` / `energies? renouvelables?` / `eolien` / `agrivolta` / `methanis` | pér. -35 |  | **majeur** ≥ 50 % | Concurrence directe des développeurs ENR sur le foncier, à loyers sans commune mesure avec l'agricole. |  |
| `sect_habitat_diffus` | Sous-secteur habitat diffus / STECAL | A, N | `^[AN](h\|d\|hd\|ha)\d*[a-z]?$` | `habitat (diffus\|existant)` / `hameaux?` / `\bstecal\b` / `taille et (de )?capacite` / `bati existant` | pér. -20 |  |  | Constructions admises (habitat diffus, extensions). |  |
| `sect_jardins` | Sous-secteur jardins | N, U | `^Nj\d*$` | `jardins?` | pér. -5 |  |  | Usage jardiné, fragmentation foncière, abris de jardin admis. |  |
| `sect_agricole_protege` | Sous-secteur agricole protégé / inconstructible | A | `^A(p\|pr\|inc\|0\|s)\d*$` | `agricoles? protegee?s?` / `agricole inconstructible` / `interet paysager` / `protection paysagere` / `agricole stricte?` | pér. +10 |  |  | Même les bâtiments agricoles y sont interdits ou très limités : pérennité renforcée. |  |
| `sect_risque` | Sous-secteur soumis à un risque (inondation...) | A, N | `^[AN][a-z]*(i\|ri\|inond)\d*$` | `inondable` / `inondation` / `submersion` / `\bppri\b` / `\bcrues?\b` | pér. +8 | *zones_humides* : pér. +8, syn. +8 |  | Inconstructibilité de fait (pérennité). Une zone inondable est cohérente avec une restauration de zone humide ou de champ d'expansion de crue. | ❓ Suffixe 'i' (Ai, Ni) : assez fiable pour 'inondable' ? |
| `sect_infrastructures` | Sous-secteur infrastructures (aéroport, militaire, ferroviaire, autoroute) | A, N | `^[AN](aer\|aero\|mil\|fer\|ferro)\d*$` | `aeroport` / `aerodrome` / `militaire` / `defense nationale` / `ferroviaire` / `autoroute` | pér. -15 | *zones_humides* : pér. -15, syn. -8 |  | Emprise ou abords d'infrastructure : usage concurrent, nuisances. |  |
| `sect_littoral_remarquable` | Sous-secteur espace remarquable (littoral / montagne) | N, A | `^N(rl\|lr)\d*$` | `espaces? remarquables?` / `l\.? ?121-23` / `l\.? ?146-6` | pér. +12, syn. -5, compat. -5 |  |  | Protection maximale au titre de la loi Littoral : très pérenne, mais travaux limités et additionnalité discutable. | ❓ Le suffixe 'Nl' désigne tantôt le littoral, tantôt les loisirs : faut-il exiger le libellé long ? |
| `sect_agricole_constructible` | Sous-secteur agricole constructible / élevage | A | `^A(c\|b\|e?l?v\|elev)\d*$` | `agricole constructible` / `secteur constructible` / `batiments? d.elevage` | pér. -8 |  |  | Secteur où les bâtiments agricoles sont spécifiquement admis. | ❓ 'Ac' est parfois 'agricole constructible', parfois 'agricole carrière' : à confirmer. |
| `sect_dechets` | Sous-secteur déchets / stockage | A, N | `^N(isdi\|dec\|dech)\d*$` | `dechets?` / `\bisdi\b` / `decharge` / `enfouissement` | pér. -25 |  |  | Activité de stockage ou traitement de déchets. |  |
| `sect_renaturation` | Secteur de renaturation / friche | U, A, N, AUc, AUs |  | `renaturation` / `desartificialis` / `friche` / `desimpermeabilis` | syn. +15 |  |  | La collectivité identifie un besoin de renaturation : synergie forte. |  |

## 4. Prescriptions

| ID | Règle | Déclencheurs | Effets | Profils | Veto | Justification | Réf. | À confirmer |
|---|---|---|---|---|---|---|---|---|
| `psc_ebc_creer` | EBC à créer | codes 01-02<br>mots-clés : `(ebc\|espaces? boises? classes?) a creer` / `boisements? a creer` | pér. +12, compat. -5 | *boisement* : pér. +12, syn. +15, compat. +5<br>*zones_humides* : pér. +12, compat. -40<br>*milieux_ouverts* : pér. +12, compat. -40 | **bloquant** ≥ 50 % [zones_humides, milieux_ouverts] | L113-1 : obligation de créer un boisement. Synergie directe pour un boisement compensatoire, contradiction pour les autres mesures. | L113-1, R151-31 1° |  |
| `psc_ebc_littoral` | EBC significatif loi Littoral | codes 01-03<br>mots-clés : `boises? significatifs?` / `l\.? ?121-27` | pér. +20, compat. -10, syn. -5 | *zones_humides* : pér. +20, compat. -45, syn. -5<br>*milieux_ouverts* : pér. +20, compat. -45, syn. -5<br>*boisement* : pér. +20, syn. -10 | **bloquant** ≥ 50 % [zones_humides, milieux_ouverts] | Régime EBC renforcé par la loi Littoral. Boisement existant déjà protégé : additionnalité faible pour un boisement. | L121-27, L113-1 |  |
| `psc_ebc` | Espace boisé classé | codes 01-00, 01-01<br>mots-clés : `espaces? boises? classes?` / `\bebc\b` / `l\.? ?113-1\b` / `l\.? ?130-1` | pér. +15, compat. -15 | *zones_humides* : pér. +15, compat. -45<br>*milieux_ouverts* : pér. +15, compat. -45<br>*boisement* : pér. +15, compat. +0, syn. -8 | **bloquant** ≥ 50 % [zones_humides, milieux_ouverts]<br>**majeur** ≥ 15 % [zones_humides, milieux_ouverts] | Très protecteur (pérennité), mais bloque toute mesure impliquant un défrichement. Pour un boisement, l'EBC protège déjà l'existant : peu d'additionnalité. | L113-1, L113-2, R151-31 1° |  |
| `psc_inconstructible` | Secteur inconstructible (environnement, risques, intérêt général) | codes 02-01<br>mots-clés : `inconstructible` / `non aedificandi` / `interdiction de (toute )?construction` / `interdite? a la construction` | pér. +18 |  |  | Inconstructibilité explicite : pérennité forte. | R151-31 2° |  |
| `psc_constructibilite_limitee` | Constructibilité limitée ou sous conditions (environnement, risques) | codes 02-00, 02-02<br>mots-clés : `constructibilite limitee` / `conditions speciales` | pér. +8 |  |  | Constructibilité restreinte pour motif environnemental ou de risque. | R151-31 2°, R151-34 1° |  |
| `psc_reconstruction` | Dispositions de reconstruction / démolition | codes 03-* | neutre |  |  | Règle sur le bâti existant, sans effet sur une mesure compensatoire. |  |  |
| `psc_er_hydraulique` | Emplacement réservé — ouvrage hydraulique / eaux pluviales | affine psc_er_generique, psc_er_ouvrage, psc_er_interet_general si : `bassin` / `retention` / `d.orage` / `eaux pluviales` / `expansion de crue` / `noue` / `fosse` / `recalibrage` / `zone tampon` / `hydraul` | pér. -10, maît. -15, syn. +3 | *zones_humides* : pér. -5, maît. -15, syn. +10 |  | Projet public d'ouvrage hydraulique : concurrence d'usage modérée, synergie possible avec une mesure ZH. |  | ❓ Une mesure ZH sur l'emprise d'un ER hydraulique est-elle recevable si la collectivité bénéficiaire y consent ? |
| `psc_er_espaces_verts` | Emplacement réservé — espaces verts / continuités écologiques | codes 05-04<br>affine psc_er_generique si : `espaces? verts?` / `continuite` / `corridor` / `trame` / `zones? humides?` / `coulee verte` / `renaturation` / `plantation` / `boisement` / `ecolog` / `biodiversite` | pér. +10, syn. +20, maît. -10 |  |  | Seul ER aligné avec une compensation : la destination du terrain est déjà écologique. | L151-41 3°, R151-43 3° |  |
| `psc_er_logement` | Emplacement réservé — logement social / mixité | codes 05-05<br>affine psc_er_generique si : `logement` / `habitat` / `mixite sociale` | pér. -35, maît. -20 |  | **bloquant** ≥ 30 % | Terrain voué à l'urbanisation. | L151-41 4° |  |
| `psc_er_voirie` | Emplacement réservé — voirie | codes 05-01<br>affine psc_er_generique si : `voie` / `voirie` / `elargissement` / `chemin` / `piste cyclable` / `carrefour` / `giratoire` / `rond-point` / `liaison` / `desserte` / `stationnement` / `parking` / `acces` / `amenagement routier` / `route` / `rue` | pér. -30, maît. -20 |  | **bloquant** ≥ 50 %<br>**majeur** ≥ 30 % | Projet public d'infrastructure : incompatible avec une mesure sur l'emprise, et facteur de fragmentation. | L151-41 1°, R151-48 2° |  |
| `psc_er_ouvrage` | Emplacement réservé — ouvrage public | codes 05-02<br>affine psc_er_generique si : `station d.epuration` / `\bstep\b` / `reservoir` / `chateau d.eau` / `ouvrage` / `ecole` / `groupe scolaire` / `salle` / `cimetiere` / `stade` / `gymnase` / `dechet` / `equipement` | pér. -30, maît. -20 |  | **bloquant** ≥ 50 % | Projet d'équipement public sur l'emprise. | L151-41 1°, R151-50 1° |  |
| `psc_er_interet_general` | Emplacement réservé — installation d'intérêt général | codes 05-03 | pér. -25, maît. -15 |  | **bloquant** ≥ 50 % | Projet d'intérêt général sur l'emprise. | L151-41 2° |  |
| `psc_er_generique` | Emplacement réservé (objet non précisé par le code) | codes 05-00<br>mots-clés : `emplacements? reserves?` / `^er ?n?[°o]? ?\d` / `\ber ?n[°o] ?\d` / `^e\.?r\.? ?\d` | pér. -22, maît. -15 |  | **majeur** ≥ 50 % | Réserve foncière au profit d'une collectivité pour un projet public : en général incompatible avec une mesure, sauf ER espaces verts. | L151-41 |  |
| `psc_servitude_localisation` | Servitude de localisation (voies, ouvrages, espaces verts en U/AU) | codes 05-06 | pér. -25, maît. -10 |  | **majeur** ≥ 50 % | Projet public localisé (en zone U/AU). | L151-41 dernier alinéa |  |
| `psc_perimetre_attente` | Périmètre d'attente de projet d'aménagement global | codes 05-07<br>mots-clés : `perimetre d.attente` / `projet d.amenagement global` / `\bpapag\b` | pér. -35 |  | **bloquant** ≥ 30 % | Gel en attente d'un projet urbain : incompatible. | L151-41 5° |  |
| `psc_er_trait_cote` | ER relocalisation (recul du trait de côte) | codes 05-08 | pér. -20, maît. -15 |  |  | Terrain destiné à accueillir des équipements relocalisés. | L151-41 6° |  |
| `psc_paysage_ecologique_boise` | Élément de paysage boisé à préserver (motif écologique, L151-23) | codes 07-05 | pér. +10, syn. +12, compat. -5 | *zones_humides* : pér. +10, syn. +8, compat. -15<br>*milieux_ouverts* : pér. +10, syn. +5, compat. -15<br>*boisement* : pér. +10, syn. +8 |  | Protection écologique d'un boisement, moins stricte qu'un EBC (pas d'interdiction de défrichement de plein droit). | L151-23, R151-43 5° |  |
| `psc_paysage_ecologique` | Élément de paysage à préserver pour motif écologique (L151-23) | codes 07-04<br>mots-clés : `l\.? ?151-23` / `motifs? d.ordre ecologique` / `interet ecologique` | pér. +10, syn. +15, compat. -3 |  |  | Le PLU reconnaît une valeur écologique au site : cohérence et argument d'instruction. | L151-23, R151-43 5° |  |
| `psc_patrimoine_paysager_boise` | Patrimoine paysager boisé (motif culturel / paysager, L151-19) | codes 07-03 | pér. +8, compat. -5 | *zones_humides* : pér. +8, compat. -15<br>*milieux_ouverts* : pér. +8, compat. -15 |  | Boisement protégé pour motif paysager : réouverture contrainte. | L151-19 |  |
| `psc_patrimoine_paysager` | Patrimoine paysager / éléments de paysage (L151-19, L151-23) | codes 07-00, 07-02<br>mots-clés : `elements? de paysage` / `patrimoine paysager` / `paysage a proteger` / `cones? de vue` / `l\.? ?123-1-5` / `l\.? ?123-1-7` / `l\.? ?151-19` | pér. +8, compat. -5, syn. +3 |  |  | Protection paysagère : pérennité accrue, modifications soumises à déclaration. | L151-19, L151-23, R151-41 3° |  |
| `psc_patrimoine_bati` | Patrimoine bâti à protéger | codes 07-01<br>mots-clés : `bati remarquable` / `patrimoine bati` / `batiments? remarquables?` / `petit patrimoine` | pér. +2 |  |  | Protection d'un bâti : sans effet notable sur la mesure, hors gestion des abords. | L151-19 |  |
| `psc_terrain_cultive_urbain` | Terrain cultivé ou non bâti protégé en zone urbaine | codes 08-00<br>mots-clés : `terrains? cultives?` / `jardins? (familiaux\|partages\|ouvriers)` / `maraich` | pér. +8, syn. +3 |  |  | Terrain non bâti protégé en milieu urbain : pérennité en contexte défavorable. | L151-23 al. 2 |  |
| `psc_ski` | Zone d'aménagement pour le ski | codes 13-00 | pér. -20 |  |  | Aménagements de domaine skiable. | L151-38 al. 2 |  |
| `psc_regles_construction` | Règles de construction (implantation, hauteur, aspect, réseaux...) | codes 04-00, 15-*, 26-*, 27-00, 28-*, 37-00, 37-01, 38-*, 39-*, 40-*, 41-*, 42-00, 43-*, 47-*, 97-* | neutre |  |  | Règles relatives aux constructions : sans effet sur une mesure compensatoire. |  |  |
| `psc_signal_urbain` | Règles propres aux secteurs urbains (densité, mixité, stationnement...) | codes 14-00, 17-00, 22-*, 23-00, 29-*, 30-*, 36-00, 37-02, 44-*, 45-00 | pér. -8 |  |  | Leur présence confirme une vocation constructible ou urbaine du secteur, au-delà du seul zonage. |  |  |
| `psc_equipements_AN` | Équipements collectifs admis en zone A/N | codes 16-00 | pér. -10 |  |  | Possibilité d'implanter des équipements collectifs. | L151-11 I 1° |  |
| `psc_changement_destination` | Bâtiment pouvant changer de destination (A/N) | codes 16-01<br>mots-clés : `changement de destination` | pér. -5 |  |  | Signal de bâti existant et d'un usage résidentiel futur. | L151-11 I 2° |  |
| `psc_extension_habitat` | Extensions / annexes des habitations existantes (A/N) | codes 16-02 | pér. -5 |  |  | Constructibilité résiduelle autour de l'habitat existant. | L151-12 |  |
| `psc_stecal` | STECAL | codes 16-03<br>mots-clés : `\bstecal\b` / `taille et (de )?capacite d.accueil limitees?` | pér. -25 |  | **majeur** ≥ 50 % | Secteur de taille et capacité d'accueil limitées : constructions admises. | L151-13 |  |
| `psc_constructions_agricoles` | Constructions agricoles admises (A/N) | codes 16-04 | pér. -8 |  |  | Bâtiments agricoles admis sur le secteur. |  |  |
| `psc_diversification_agricole` | Diversification de l'activité agricole | codes 16-05 | pér. -8 |  |  | Constructions de transformation et de vente admises. | L151-11 II |  |
| `psc_oap_renaturation` | OAP secteur de renaturation | codes 18-12<br>mots-clés : `renaturation` / `desartificialis` / `desimpermeabilis` | pér. +10, syn. +25 |  |  | La collectivité programme la renaturation du secteur : alignement maximal. | L151-7 I 4° |  |
| `psc_oap_tvb` | OAP trame verte et bleue | codes 18-15<br>mots-clés : `oap (thematique )?(trame\|tvb\|continuites?)` | pér. +10, syn. +25 |  |  | OAP opposable en compatibilité dédiée aux continuités écologiques. | L151-6-2 |  |
| `psc_oap_zaer` | OAP zone d'accélération des énergies renouvelables | codes 18-16<br>mots-clés : `\bzaer\b` / `zones? d.acceleration` | pér. -35 |  | **majeur** ≥ 30 % | Foncier ciblé pour les projets ENR. | L151-7 I 8° |  |
| `psc_oap_franges` | OAP franges urbaines et rurales | codes 18-13<br>mots-clés : `franges? (urbaines?\|rurales?)` / `lisieres? urbaines?` | pér. +3, syn. +8 |  |  | Traitement des transitions ville / campagne : cohérent avec haies et lisières. | L151-7 I 7° |  |
| `psc_oap_patrimoniale_eco` | OAP patrimoniale, architecturale et écologique | codes 18-05<br>affine psc_oap_generique si : `ecolog` / `biodiversite` / `nature` / `paysag` / `patrimoin` / `environnement` | pér. +5, syn. +10 |  |  | OAP thématique incluant des objectifs écologiques. |  |  |
| `psc_oap_projet` | OAP de projet urbain (habitat, économie, ZAC...) | codes 18-01, 18-06, 18-07, 18-09, 18-10, 18-11<br>affine psc_oap_generique si : `habitat` / `logement` / `activite` / `economique` / `\bzac\b` / `commerc` / `quartier` / `urbanisation` / `extension` / `lotissement` / `zone d.activite` | pér. -35 |  | **bloquant** ≥ 30 % | Projet d'aménagement programmé : incompatible. | L151-6, L151-7 |  |
| `psc_oap_amenagement` | OAP d'aménagement (entrée de ville, réhabilitation, transports) | codes 18-02, 18-03, 18-04, 18-08 | pér. -20 |  | **majeur** ≥ 50 % | Aménagements programmés. |  |  |
| `psc_oap_trait_cote` | OAP recul du trait de côte | codes 18-14 | pér. -15 |  |  | Terrain menacé par le recul du trait de côte. | L151-7 III |  |
| `psc_oap_generique` | OAP (type non précisé) | codes 18-00<br>mots-clés : `\boap\b` / `orientations? d.amenagement` | pér. -20 |  | **majeur** ≥ 50 % | Une OAP sectorielle signale le plus souvent un projet d'aménagement. |  |  |
| `psc_richesse_sol` | Secteur protégé pour la richesse du sol ou du sous-sol (carrières) | codes 19-00 | pér. -25 |  |  | Les carrières y sont autorisées. | R151-34 2° |  |
| `psc_transfert_constructibilite` | Transfert de constructibilité en zone N | codes 20-00 | pér. +5 |  |  | Mécanisme qui, côté émetteur, rend le terrain inconstructible. | L151-25 | ❓ Peut-on considérer par défaut le secteur comme émetteur ? |
| `psc_voies_creer` | Voies à créer ou à modifier | codes 24-00, 24-01, 24-03<br>mots-clés : `voies? a creer` / `projet de voie` / `barreau` / `contournement` / `deviation` / `voie nouvelle` | pér. -20 |  | **majeur** ≥ 40 % | Infrastructure future : fragmentation et emprise. | L151-38 |  |
| `psc_voies_modifier` | Voies à modifier | codes 24-02 | pér. -8 |  |  | Modification de voirie existante en bordure. |  |  |
| `psc_chemins_conserver` | Voies et chemins à conserver | codes 24-04<br>mots-clés : `chemins? (ruraux\|de randonnee)? ?a conserver` / `sentiers? a conserver` | syn. +2 |  |  | Maintien d'un chemin : sans effet notable (fréquentation éventuelle). |  |  |
| `psc_tvb` | Éléments de continuité écologique / trame verte et bleue | codes 25-00<br>mots-clés : `trame verte` / `trame bleue` / `\btvb\b` / `continuites? ecologiques?` / `corridors? (ecologiques?\|biologiques?)` / `reservoirs? de biodiversite` | pér. +10, syn. +25 |  |  | Le document identifie la parcelle comme élément de TVB : forte cohérence et argument d'instruction. | L151-23 al. 2, R151-43 4° |  |
| `psc_littoral_zh` | Espace remarquable du littoral — zones humides, marais | codes 31-05 | pér. +25, compat. -5, syn. -5 | *zones_humides* : pér. +25, compat. -5, syn. +5 |  | Protection maximale. Une restauration de ZH dégradée reste possible si elle relève des aménagements légers. | L121-23, R121-4 5°, R121-5 |  |
| `psc_littoral_remarquable` | Espaces remarquables du littoral | codes 31-*<br>mots-clés : `espaces? remarquables?` / `l\.? ?121-23` / `l\.? ?146-6` | pér. +25, compat. -10, syn. -10 |  |  | Protection maximale : très pérenne, mais travaux limités et additionnalité faible. | L121-23, R121-4, R121-5 |  |
| `psc_plans_eau_exclusion` | Exclusion de la protection de plans d'eau de faible importance | codes 32-00 | neutre |  |  | Règle montagne sans effet direct. |  |  |
| `psc_rives_derogation` | Dérogation à la protection des rives (montagne) | codes 33-00 | pér. -10 |  |  | Ouvre des possibilités de construction près des rives. |  |  |
| `psc_patrimoine_montagnard` | Espaces et milieux caractéristiques du patrimoine montagnard | codes 34-00 | pér. +15, syn. +5 |  |  | Protection loi Montagne. | L122-9 |  |
| `psc_terres_agricoles_montagne` | Terres agricoles, pastorales et forestières à préserver (montagne) | codes 35-00 | pér. +15 | *boisement* : pér. +15, compat. -15<br>*zones_humides* : pér. +15, compat. -8 |  | Protection de la vocation agricole et pastorale. | L122-10 |  |
| `psc_boise_ancien` | Constructibilité des espaces boisés antérieurs au XXe siècle | codes 46-00 | pér. +3 | *boisement* : pér. +3, syn. -3 |  | Boisement ancien identifié : forte valeur écologique, faible additionnalité pour un boisement. | L151-20 | ❓ Portée exacte de ce code (référence L151-20 citée par le standard) ? |
| `psc_impermeabilisation` | Limitation de l'imperméabilisation | codes 48-00 | syn. +2 |  |  | Règle de gestion des sols, favorable mais sans effet notable. |  |  |
| `psc_eaux_pluviales` | Installations de gestion des eaux pluviales | codes 48-01 | pér. -5, syn. +2 | *zones_humides* : pér. -3, syn. +6 |  | Ouvrages de gestion des eaux pluviales prévus : usage hydraulique concurrent ou mutualisable. |  |  |
| `psc_operation_ensemble_AU` | Opération d'ensemble imposée en zone AU | codes 49-* | pér. -30 |  | **bloquant** ≥ 30 % | Signal d'une zone AU destinée à l'urbanisation. | R151-20 |  |
| `psc_interdiction_destinations` | Interdiction de certaines destinations / activités | codes 50-00 | pér. +3 |  |  | Restreint les usages, effet dépendant du contenu. |  |  |
| `psc_autorisation_conditions` | Destinations autorisées sous conditions | codes 51-00 | neutre |  |  | Effet dépendant du contenu, non interprétable sans le règlement. |  |  |
| `psc_logistique` | Infrastructures et équipements logistiques | codes 52-00<br>mots-clés : `logistique` / `entrepots?` | pér. -35 |  | **bloquant** ≥ 30 % | Vocation logistique. | L151-16-2 |  |
| `psc_derogation_axes` | Dérogation à l'inconstructibilité le long des grands axes (L111-6) | codes 53-00 | pér. -20 |  |  | Lève l'inconstructibilité de la bande de recul : projet de construction probable. | L111-6, L111-8 |  |
| `psc_autre` | Prescription 'Autre' (99) | codes 99-* | neutre |  |  | Code générique : seul le libellé renseigne (voir règles par mots-clés). |  |  |

## 5. Informations

| ID | Règle | Déclencheurs | Effets | Profils | Veto | Justification | Réf. | À confirmer |
|---|---|---|---|---|---|---|---|---|
| `inf_zac` | Zone d'aménagement concerté | codes 02-00<br>mots-clés : `zone d.amenagement concerte` / `\bzac\b` | pér. -35 |  | **bloquant** ≥ 30 % | Opération d'aménagement publique. | L311-1 |  |
| `inf_zpens` | Zone de préemption des espaces naturels sensibles (ZPENS) | codes 03-00<br>mots-clés : `espaces? naturels? sensibles?` / `\bzpens\b` / `\bens\b` / `preemption (au titre )?des espaces naturels` | syn. +10, maît. -20, pér. +5 |  |  | Droit de préemption du Département (L215-1) : contrainte d'acquisition, mais politique publique alignée. | L215-1 et s. |  |
| `inf_dpu_renforce` | Droit de préemption urbain renforcé | codes 04-01<br>mots-clés : `preemption urbain renforce` / `\bdpur\b` | maît. -15 |  |  | Préemption communale élargie. | L211-4 |  |
| `inf_dpu` | Droit de préemption urbain | codes 04-00<br>mots-clés : `droit de preemption urbain` / `\bdpu\b` | maît. -10 |  |  | Risque d'éviction lors de l'acquisition. | L211-1 |  |
| `inf_zad` | Zone d'aménagement différé | codes 05-00<br>mots-clés : `zone d.amenagement differe` / `\bzad\b` | pér. -25, maît. -25 |  | **majeur** ≥ 50 % | Projet d'aménagement futur et préemption. | L212-1 |  |
| `inf_reglementation_boisements` | Périmètre de réglementation des boisements (code rural) | codes 08-00<br>mots-clés : `reglementation des (plantations\|boisements)` / `boisements? (interdits?\|reglementes?)` / `perimetre (d.interdiction\|de reglementation) des boisements` | pér. +3, compat. -5 | *boisement* : pér. +3, compat. -35 | **majeur** ≥ 50 % [boisement] | Peut interdire purement et simplement un boisement compensatoire. | L126-1 CRPM |  |
| `inf_minier` | Périmètre minier | codes 09-00 | pér. -25 |  |  | Concession minière ou stockage souterrain. |  |  |
| `inf_carriere` | Zone de recherche et d'exploitation de carrière | codes 10-00<br>mots-clés : `carrieres?` / `gravieres?` / `extraction` / `granulats?` / `sablieres?` | pér. -25 |  |  | Exploitation actuelle ou future. |  |  |
| `inf_divisions_foncieres` | Divisions foncières soumises à déclaration préalable | codes 11-00 | pér. +3 |  |  | Outil anti-mitage dans les espaces naturels : légèrement favorable. | L115-3 |  |
| `inf_sursis` | Périmètre de sursis à statuer | codes 12-00<br>mots-clés : `sursis a statuer` | pér. -15, conf. -10 |  |  | Signal d'un projet public ou d'une révision en cours. | L424-1 |  |
| `inf_pae` | Programme d'aménagement d'ensemble | codes 13-00 | pér. -25 |  |  | Secteur d'aménagement programmé. |  |  |
| `inf_bruit` | Secteur affecté par le bruit des infrastructures | codes 14-00<br>mots-clés : `classement sonore` / `nuisances? sonores?` / `affecte par le bruit` | syn. -2 |  |  | Nuisance pour la faune sensible, effet faible. |  |  |
| `inf_archeo` | Site archéologique | codes 16-00<br>mots-clés : `archeolog` / `vestiges` | compat. -5 | *zones_humides* : compat. -10 |  | Contrainte sur les travaux de terrassement. |  |  |
| `inf_neutre` | Informations sans effet (plomb, assainissement, publicité, taxes, massif...) | codes 07-00, 17-00, 19-01, 20-00, 23-00, 32-00, 33-00, 42-*, 97-00 | neutre |  |  | Sans effet sur une mesure compensatoire. |  |  |
| `inf_traitement_eaux_dechets` | Emplacements de traitement des eaux et des déchets | codes 19-02 | pér. -20 |  |  | Équipement de traitement existant ou prévu. |  |  |
| `inf_projet_ppr` | Projet de plan de prévention des risques | codes 21-00 | pér. +5 |  |  | Futur encadrement de la constructibilité. |  |  |
| `inf_rives_montagne` | Protection des rives des plans d'eau (montagne) | codes 22-00 | pér. +10 |  |  | Inconstructibilité des rives. | L122-12 |  |
| `inf_paen` | Périmètre de protection des espaces agricoles et naturels périurbains (PAEN) | codes 25-00<br>mots-clés : `\bpaen\b` / `protection des espaces agricoles et naturels` | pér. +25, syn. +5 | *boisement* : pér. +25, syn. +5, compat. -5 |  | Protection foncière parmi les plus robustes (réduction par décret). | L113-16 |  |
| `inf_peb` | Plan d'exposition au bruit des aérodromes | codes 27-00<br>mots-clés : `plan d.exposition au bruit` / `\bpeb\b` | pér. +5 | *zones_humides* : pér. +5, syn. -10 |  | Constructibilité limitée (favorable) ; contrainte avifaune pour les ZH. |  |  |
| `inf_pup` | Périmètre de projet urbain partenarial | codes 30-00 | pér. -30 |  | **bloquant** ≥ 30 % | Opération d'aménagement financée par convention. |  |  |
| `inf_exclusion_enr` | Périmètre d'exclusion des matériaux / ENR (patrimoine) | codes 31-00 | pér. +3 |  |  | Limite la concurrence des projets ENR. |  |  |
| `inf_oin` | Périmètre d'opération d'intérêt national | codes 34-00<br>mots-clés : `operation d.interet national` / `\boin\b` | pér. -35 |  | **bloquant** ≥ 30 % | Grande opération d'aménagement de l'État. |  |  |
| `inf_densite` | Seuil minimal de densité | codes 35-00 | pér. -10 |  |  | Secteur à densifier. |  |  |
| `inf_plage` | Schéma d'aménagement de plage | codes 36-00 | pér. -5 |  |  | Aménagements de plage. |  |  |
| `inf_regime_forestier` | Bois et forêts relevant du régime forestier | codes 37-00<br>mots-clés : `regime forestier` / `forets? (domaniales?\|communales?)` / `\bonf\b` | pér. +10, maît. -25 | *zones_humides* : pér. +10, maît. -25, compat. -10<br>*milieux_ouverts* : pér. +10, maît. -25, compat. -10 |  | Forêt publique : pérenne mais non acquérable. | L211-1 C. forestier |  |
| `inf_sis` | Secteur d'information sur les sols (pollution) | codes 38-00<br>mots-clés : `secteurs? d.information sur les sols` / `\bsis\b` / `sols? pollues?` | compat. -10 |  |  | Contraintes sanitaires et de travaux. | L125-6 C. env. |  |
| `inf_afup` | Périmètre de projet AFUP | codes 39-00 | pér. -10 |  |  | Projet de remembrement urbain. |  |  |
| `inf_patrimoine_mondial` | Bien inscrit au patrimoine mondial / zone tampon | codes 40-* | pér. +5, compat. -3 |  |  | Protection patrimoniale et paysagère. |  |  |
| `inf_recul_axes` | Bande de recul le long des axes à grande circulation | codes 41-00<br>mots-clés : `marge de recul` / `bande inconstructible` / `l\.? ?111-6` / `l\.? ?111-1-4` / `amendement dupont` / `loi barnier` | pér. +3, syn. -2 |  |  | Inconstructibilité (favorable), nuisances et collisions faune (défavorable). |  |  |
| `inf_annulation_partielle` | Périmètre d'annulation partielle du document | codes 98-00<br>mots-clés : `annulation partielle` / `annule par` | conf. -25 |  |  | Fiabilité de la donnée remise en cause. |  |  |
| `inf_autre` | Information 'Autre' (99) | codes 99-* | neutre |  |  | Code générique : seul le libellé renseigne. |  |  |

## 6. Servitudes d'utilité publique

| ID | Règle | Déclencheurs | Effets | Profils | Veto | Justification | Réf. | À confirmer |
|---|---|---|---|---|---|---|---|---|
| `sup_a7_foret_protection` | A7 — Forêts de protection | SUP A7<br>mots-clés : `forets? de protection` | pér. +25, compat. -10 | *zones_humides* : pér. +25, compat. -40<br>*milieux_ouverts* : pér. +25, compat. -40<br>*boisement* : pér. +25, syn. -10 | **bloquant** ≥ 50 % [zones_humides, milieux_ouverts] | Protection forestière la plus forte du code forestier. | L141-1 C. forestier |  |
| `sup_a9_zap` | A9 — Zone agricole protégée | SUP A9<br>mots-clés : `zones? agricoles? protegees?` / `\bzap\b` | pér. +20 | *boisement* : pér. +20, compat. -10<br>*zones_humides* : pér. +20, compat. -5 |  | Protection pérenne de la vocation agricole : favorable à la pérennité, frein à une conversion en boisement. | L112-2 CRPM | ❓  |
| `sup_a1_regime_forestier` | A1 — Bois et forêts soumis au régime forestier | SUP A1 | pér. +10, maît. -20 |  |  | Forêt publique. |  | ❓  |
| `sup_a4_cours_eau` | A4 — Terrains riverains des cours d'eau non domaniaux | SUP A4<br>mots-clés : `riverains? des cours d.eau` / `cours d.eau non domaniaux` | compat. -3, syn. +5 | *zones_humides* : compat. -3, syn. +10<br>*boisement* : compat. -8, syn. +3 |  | Signal de proximité d'un cours d'eau ; servitude de passage. |  | ❓  |
| `sup_canalisations_eau` | A2, A3, A5 — Canalisations d'irrigation, d'eau, d'assainissement | SUP A2, A3, A5 | compat. -8 | *boisement* : compat. -15 |  | Servitude technique contraignante pour les plantations et les terrassements. |  | ❓  |
| `sup_a8_montagne` | A8 — Protection / restauration des sols en montagne | SUP A8 | pér. +10 |  |  | Périmètre de restauration des terrains en montagne. |  | ❓  |
| `sup_ac3_reserve` | AC3 — Réserves naturelles | SUP AC3<br>mots-clés : `reserves? naturelles?` | pér. +30, syn. -15, compat. -10 |  | **majeur** ≥ 50 % | Site déjà protégé et géré : additionnalité faible. | L332-1 C. env. |  |
| `sup_ac2_sites` | AC2 — Sites inscrits et classés | SUP AC2<br>mots-clés : `sites? (inscrits?\|classes?)` | pér. +10, compat. -8 |  |  | Protection paysagère forte, procédure supplémentaire pour les travaux. | L341-1 et s. C. env. |  |
| `sup_ac1_mh` | AC1 — Monuments historiques (abords) | SUP AC1<br>mots-clés : `monuments? historiques?` / `abords? de monument` | pér. +3, compat. -5 |  |  | Contrainte procédurale légère. |  |  |
| `sup_ac4_spr` | AC4 — Site patrimonial remarquable (ex-ZPPAUP/AVAP) | SUP AC4 | pér. +3, compat. -5 |  |  | Règles patrimoniales, souvent en contexte urbain. |  | ❓  |
| `sup_as1_immediat` | AS1 — Périmètre de protection immédiate de captage | affine sup_as1_captage si : `immediat` | maît. -40, compat. -20 |  | **bloquant** ≥ 20 % | Terrain acquis et clôturé par la collectivité. |  |  |
| `sup_as1_captage` | AS1 — Protection des captages d'eau potable | SUP AS1<br>mots-clés : `captages?` / `eau potable` / `eaux destinees a la consommation` | compat. -5, syn. +5 |  |  | Restrictions, mais intérêt convergent pour la ressource. | L1321-2 CSP |  |
| `sup_ar_defense` | AR — Servitudes de défense nationale | SUP préfixe AR* | pér. -5, maît. -10 |  |  | Usage militaire à proximité. |  | ❓  |
| `sup_el7_alignement` | EL7 — Plan d'alignement des voies | SUP EL7<br>mots-clés : `plans? d.alignement` | pér. -10, maît. -5 |  |  | Élargissement futur de voie en bordure. |  |  |
| `sup_el10_parc_national` | EL10 — Cœur de parc national | SUP EL10<br>mots-clés : `parc national` | pér. +30, syn. -10, compat. -10 |  | **majeur** ≥ 50 % | Protection maximale. |  | ❓  |
| `sup_el_littoral_halage` | EL3, EL9 — Halage / marchepied, passage piétons littoral | SUP EL3, EL9<br>mots-clés : `halage` / `marchepied` / `sentier du littoral` / `passage des pietons` | compat. -3 |  |  | Passage public à maintenir le long des berges ou du rivage. |  |  |
| `sup_el_autres` | EL — Autres servitudes de communication / équipement | SUP préfixe EL* | neutre |  |  | Sans effet notable identifié. |  | ❓  |
| `sup_canalisations_energie` | I1, I3, I5, I7 — Canalisations hydrocarbures, gaz, produits chimiques, stockages | SUP I1, I1BIS, I3, I5, I7<br>mots-clés : `gaz` / `hydrocarbures?` / `pipe.?line` / `produits chimiques` / `canalisations? de transport` | compat. -10 | *boisement* : compat. -25<br>*zones_humides* : compat. -15 |  | Servitude technique incompatible avec des plantations ligneuses et contraignante pour les terrassements. |  |  |
| `sup_i4_electricite` | I4 — Lignes électriques | SUP I4<br>mots-clés : `electri` / `lignes? (haute\|tres haute) tension` / `\bht[ab]?\b` / `rte` | compat. -5 | *boisement* : compat. -15 |  | Contrainte forte sur le boisement, faible pour les milieux ouverts et humides. |  |  |
| `sup_i6_mines` | I6 — Mines et carrières | SUP I6 | pér. -20 |  |  | Exploitation du sous-sol. |  | ❓  |
| `sup_i_autres` | I — Autres servitudes énergie | SUP préfixe I* | neutre |  |  | Sans effet notable identifié. |  | ❓  |
| `sup_int1_cimetiere` | INT1 — Voisinage des cimetières | SUP INT1<br>mots-clés : `cimetieres?` | pér. -5 |  |  | Voisinage d'équipement, extension possible. |  |  |
| `sup_js1` | JS1 — Installations sportives | SUP JS1 | pér. -5 |  |  | Équipement sportif protégé. |  | ❓  |
| `sup_pm1_ppr` | PM1 — Plan de prévention des risques naturels | SUP PM1<br>mots-clés : `prevention des risques naturels` / `\bppr[nim]?\b` / `inondation` | pér. +12, compat. -3 | *zones_humides* : pér. +12, syn. +5, compat. -3 |  | Inconstructibilité en zone d'aléa fort : pérennité. Cohérence ZH en zone inondable. | L562-1 C. env. |  |
| `sup_pm3_pprt` | PM3 — Plan de prévention des risques technologiques | SUP PM3<br>mots-clés : `risques technologiques` / `\bpprt\b` | pér. +8, compat. -5, syn. -3 |  |  | Urbanisation contrainte, contexte industriel peu favorable. |  |  |
| `sup_pm2_icpe` | PM2 — Servitudes autour des installations classées | SUP PM2 | pér. -5, compat. -5 |  |  | Proximité d'une installation classée. |  | ❓  |
| `sup_pm8_dfci` | PM8 — Défense des forêts contre l'incendie | SUP PM8<br>mots-clés : `\bdfci\b` / `defense des forets contre` | compat. -3 | *boisement* : compat. -8 |  | Contrainte de gestion de la végétation. |  |  |
| `sup_pm_autres` | PM — Autres servitudes de sécurité publique | SUP préfixe PM* | pér. +5 |  |  | Les servitudes PM restreignent généralement l'urbanisation. |  | ❓ PM4 (zones de rétention de crues, mobilité des cours d'eau, ZSGE ?) mériterait un bonus spécifique ZH : à confirmer. |
| `sup_pt` | PT — Télécommunications | SUP préfixe PT* | neutre | *boisement* : compat. -3 |  | PT2 (obstacles) peut limiter la hauteur de la végétation, effet marginal. |  | ❓  |
| `sup_t1_ferroviaire` | T1 — Voies ferrées | SUP T1<br>mots-clés : `chemins? de fer` / `ferroviaire` / `sncf` | pér. -3, compat. -3 |  |  | Contraintes de riveraineté. |  |  |
| `sup_t_aero` | T4, T5, T7 — Servitudes aéronautiques | SUP T4, T5, T7<br>mots-clés : `aeronauti` / `aerodrome` / `degagement` | neutre | *boisement* : compat. -5<br>*zones_humides* : syn. -8 |  | Contrainte pour le boisement (hauteur) et les ZH (avifaune). |  |  |
| `sup_t_autres` | T — Autres servitudes transport | SUP préfixe T* | neutre |  |  | Sans effet notable identifié. |  | ❓  |
| `sup_neutres` | Autres servitudes sans effet identifié | SUP préfixe A*, AC*, AS*, JS*, INT* | neutre |  |  | Catégorie non spécifiquement interprétée. |  | ❓  |

## 7. Règles par mots-clés (libellés, codes 99 / absents / erronés)

| ID | Règle | Mots-clés | Effets | Profils | Veto | Justification |
|---|---|---|---|---|---|---|
| `kw_energie` | Projet ou secteur énergies renouvelables (libellé) | `photovolta` / `centrale solaire` / `parc solaire` / `agrivolta` / `energies? renouvelables?` / `\benr\b` / `eolien` / `methanis` | pér. -35 |  | **majeur** ≥ 30 % | Concurrence foncière directe. |
| `kw_zone_humide` | Zone humide identifiée (libellé) | `zones? humides?` / `\bzh\b` / `milieux? humides?` / `tourbieres?` / `roselieres?` / `prairies? humides?` / `marais` | pér. +10, syn. +10 | *zones_humides* : pér. +10, syn. +20 |  | Reconnaissance d'un enjeu ZH par le document. |
| `kw_mare` | Mares, plans d'eau (libellé) | `\bmares?\b` / `plans? d.eau` / `etangs?` | syn. +8 | *zones_humides* : syn. +12 |  | Milieux aquatiques protégés ou identifiés. |
| `kw_haie` | Haies, ripisylves, alignements, arbres (libellé) | `\bhaies?\b` / `alignements? d.arbres` / `ripisylves?` / `arbres? remarquables?` / `bosquets?` / `bocag` | pér. +3, syn. +10, compat. -3 | *boisement* : pér. +3, syn. +15<br>*milieux_ouverts* : pér. +3, syn. +5, compat. -5 |  | Trame arborée protégée : cohérente avec une mesure de haies, à préserver dans les autres cas. |
| `kw_cours_eau` | Cours d'eau, berges, bandes tampon (libellé) | `cours d.eau` / `berges?` / `bandes? (enherbees?\|tampon)` / `abords? des ruisseaux` | syn. +5, pér. +3 | *zones_humides* : syn. +10, pér. +3 |  | Proximité ou protection d'un cours d'eau. |
| `kw_protection_nature` | Espace naturel protégé ou inventorié (libellé) | `natura ?2000` / `\bznieff\b` / `arrete (prefectoral )?de (protection de )?biotope` / `\bappb\b` / `reserve naturelle` / `conservatoire` / `site classe` | pér. +10, syn. -5 |  |  | Pérennité élevée, additionnalité discutable. |
| `kw_loisirs` | Loisirs, camping, golf (libellé) | `camping` / `\bgolf\b` / `parc de loisirs` / `parc d.attraction` / `equipements? sportifs?` / `aire de jeux` / `base de loisirs` | pér. -15 |  |  | Usage récréatif concurrent. |
| `kw_risque_inondation` | Risque inondation (libellé) | `inondable` / `inondation` / `\bppri\b` / `submersion` / `expansion des crues` / `aleas? (fort\|moyen\|faible)` / `crues?` | pér. +10, compat. -3 | *zones_humides* : pér. +10, syn. +8, compat. -3 |  | Inconstructibilité de fait, cohérence avec les ZH. |
| `kw_risque_feu` | Risque feu de forêt / débroussaillement (libellé) | `feux? de foret` / `incendie` / `\bpprif\b` / `debroussaillement` / `obligations? legales? de debroussaillement` / `\bdfci\b` | pér. +3, compat. -3 | *boisement* : pér. +3, compat. -10 |  | Contrainte de gestion de la végétation. |
| `kw_risque_mouvement` | Mouvements de terrain, cavités (libellé) | `mouvements? de terrain` / `glissements?` / `eboulements?` / `cavites?` / `effondrements?` / `retrait.gonflement` | pér. +5, compat. -3 | *zones_humides* : pér. +5, compat. -8 |  | Inconstructibilité partielle, prudence sur les travaux. |
| `kw_captage` | Captage d'eau potable (libellé) | `captages?` / `eau potable` / `\baep\b` / `forages?` | compat. -5, syn. +5 |  |  | Restrictions d'usage, mais intérêt convergent pour la qualité de l'eau. |
| `kw_dechets` | Déchets, décharge (libellé) | `dechets?` / `decharge` / `\bisdi\b` / `\bisdnd\b` / `decheterie` / `dechetterie` | pér. -20 |  |  | Usage concurrent, sols potentiellement dégradés. |
| `kw_lisiere` | Lisière, transition paysagère (libellé) | `lisieres?` / `transitions? paysageres?` / `espaces? tampons?` | syn. +5 |  |  | Traitement des transitions : cohérent avec des haies ou lisières. |

## 8. Voisinage

- **pression_urbaine** (coef_AUs=0.6, points_par_pct=-0.25, plafond=-20) : Un front d'urbanisation proche augmente le risque de pression foncière, de fragmentation et de conflits d'usage sur 30 ans.
- **continuite_naturelle** (coef_A=0.5, points_par_pct=0.15, plafond=10) : Une parcelle entourée de zones naturelles s'insère dans une continuité : meilleure fonctionnalité écologique de la mesure.

## 9. Groupes sémantiques compatibles

Un code et un libellé de groupes compatibles ne sont pas en conflit : boisement_protege ↔ ecologique ; boisement_protege ↔ patrimoine ; boisement_protege ↔ protection_forte ; constructibilite_AN ↔ urbain ; ecologique ↔ patrimoine ; ecologique ↔ protection_forte ; ecologique ↔ protection_reglementaire ; ecologique ↔ ressource_eau ; nuisance ↔ risque ; preemption ↔ preemption_naturelle ; projet_amenagement ↔ projet_public ; projet_amenagement ↔ projet_urbain ; projet_hydraulique ↔ projet_public ; projet_public ↔ projet_urbain ; projet_urbain ↔ urbain ; protection_agricole ↔ protection_reglementaire ; protection_forte ↔ protection_reglementaire ; protection_reglementaire ↔ risque


## 10. Scénarios de calibration

Scores obtenus avec les règles actuelles sur des parcelles types (10 000 m², PLU de 2020). À relire en premier : si le classement relatif semble faux, ce sont les poids qu'il faut revoir.

| Scénario | generique | zones_humides | boisement | milieux_ouverts | Confiance |
|---|---|---|---|---|---|
| N 100 % + trame verte et bleue sur 40 % | 81 A | 81 A | 81 A | 81 A | 100 (haute) |
| N 100 %, rien d'autre | 78 A | 78 A | 78 A | 78 A | 100 (haute) |
| N 100 % entourée d'urbanisation (U 60 % dans 200 m) | 72 B | 72 B | 72 B | 72 B | 100 (haute) |
| A 100 %, rien d'autre | 72 B | 72 B | 72 B | 72 B | 100 (haute) |
| A 100 % en PAEN | 84 A | 84 A | 82 A | 84 A | 100 (haute) |
| Nzh 100 % (secteur zone humide) + PPR (PM1) | 85 A | 87 A | 85 A | 85 A | 100 (haute) |
| N 100 % + EBC sur 60 % | 79 A | 15 E ⛔ | 81 A | 15 E ⛔ | 100 (haute) |
| N 100 % + réserve naturelle (AC3) | 40 D ⚠ | 40 D ⚠ | 40 D ⚠ | 40 D ⚠ | 100 (haute) |
| A 100 % + ligne HT (I4) + canalisation gaz (I3) | 67 B | 66 B | 60 C | 67 B | 100 (haute) |
| A 100 % + code 99 « zone humide à préserver » sur 50 % | 74 B | 75 B | 74 B | 74 B | 100 (haute) |
| A 100 % + ER voirie sur 25 % | 68 B | 68 B | 68 B | 68 B | 100 (haute) |
| A 100 % + ER voirie sur 60 % | 15 E ⛔ | 15 E ⛔ | 15 E ⛔ | 15 E ⛔ | 100 (haute) |
| A 100 % + DPU | 71 B | 71 B | 71 B | 71 B | 100 (haute) |
| Commune au RNU (aucun zonage GPU) | 66 B | 66 B | 66 B | 66 B | 70 (moyenne) |
| Npv 100 % (secteur photovoltaïque) | 40 D ⚠ | 40 D ⚠ | 40 D ⚠ | 40 D ⚠ | 100 (haute) |
| 2AU 100 % (urbanisation future) | 40 D ⚠ | 40 D ⚠ | 40 D ⚠ | 40 D ⚠ | 100 (haute) |
| A 100 % + OAP habitat sur 40 % | 15 E ⛔ | 15 E ⛔ | 15 E ⛔ | 15 E ⛔ | 100 (haute) |
| 1AU saisie par erreur en typezone N | 15 E ⛔ | 15 E ⛔ | 15 E ⛔ | 15 E ⛔ | 92 (haute) |
| UB 100 % | 15 E ⛔ | 15 E ⛔ | 15 E ⛔ | 15 E ⛔ | 100 (haute) |

⛔ veto bloquant, ⚠ veto majeur.


## 11. Questions ouvertes pour l'expert (25)

- `psc_er_hydraulique` — Emplacement réservé — ouvrage hydraulique / eaux pluviales : Une mesure ZH sur l'emprise d'un ER hydraulique est-elle recevable si la collectivité bénéficiaire y consent ?
- `psc_transfert_constructibilite` — Transfert de constructibilité en zone N : Peut-on considérer par défaut le secteur comme émetteur ?
- `psc_boise_ancien` — Constructibilité des espaces boisés antérieurs au XXe siècle : Portée exacte de ce code (référence L151-20 citée par le standard) ?
- `sup_a9_zap` — A9 — Zone agricole protégée : interprétation à confirmer.
- `sup_a1_regime_forestier` — A1 — Bois et forêts soumis au régime forestier : interprétation à confirmer.
- `sup_a4_cours_eau` — A4 — Terrains riverains des cours d'eau non domaniaux : interprétation à confirmer.
- `sup_canalisations_eau` — A2, A3, A5 — Canalisations d'irrigation, d'eau, d'assainissement : interprétation à confirmer.
- `sup_a8_montagne` — A8 — Protection / restauration des sols en montagne : interprétation à confirmer.
- `sup_ac4_spr` — AC4 — Site patrimonial remarquable (ex-ZPPAUP/AVAP) : interprétation à confirmer.
- `sup_ar_defense` — AR — Servitudes de défense nationale : interprétation à confirmer.
- `sup_el10_parc_national` — EL10 — Cœur de parc national : interprétation à confirmer.
- `sup_el_autres` — EL — Autres servitudes de communication / équipement : interprétation à confirmer.
- `sup_i6_mines` — I6 — Mines et carrières : interprétation à confirmer.
- `sup_i_autres` — I — Autres servitudes énergie : interprétation à confirmer.
- `sup_js1` — JS1 — Installations sportives : interprétation à confirmer.
- `sup_pm2_icpe` — PM2 — Servitudes autour des installations classées : interprétation à confirmer.
- `sup_pm_autres` — PM — Autres servitudes de sécurité publique : PM4 (zones de rétention de crues, mobilité des cours d'eau, ZSGE ?) mériterait un bonus spécifique ZH : à confirmer.
- `sup_pt` — PT — Télécommunications : interprétation à confirmer.
- `sup_t_autres` — T — Autres servitudes transport : interprétation à confirmer.
- `sup_neutres` — Autres servitudes sans effet identifié : interprétation à confirmer.
- `sect_naturel_protege` — Sous-secteur naturel protégé / remarquable : Les suffixes Np/Nr/Ns sont-ils suffisamment homogènes pour être interprétés sans le libellé long ?
- `sect_equipements` — Sous-secteur équipements / services publics : Le suffixe 'e' est aussi parfois 'espace' ou 'écologique' selon les communes : garder la regex ou ne se fier qu'au libellé long ?
- `sect_risque` — Sous-secteur soumis à un risque (inondation...) : Suffixe 'i' (Ai, Ni) : assez fiable pour 'inondable' ?
- `sect_littoral_remarquable` — Sous-secteur espace remarquable (littoral / montagne) : Le suffixe 'Nl' désigne tantôt le littoral, tantôt les loisirs : faut-il exiger le libellé long ?
- `sect_agricole_constructible` — Sous-secteur agricole constructible / élevage : 'Ac' est parfois 'agricole constructible', parfois 'agricole carrière' : à confirmer.
