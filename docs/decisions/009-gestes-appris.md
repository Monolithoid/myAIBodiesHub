# ADR 009 — Gestes appris : générés sur le drone statique, joués par le robot

**Date :** 2026-11-18  
**Statut :** Accepté  
**Étend :** [ADR 008](008-univers-moteur-contraint.md)

## Contexte

Le projet [nao-emotion-motion](https://github.com/Monolithoid/nao-emotion-motion)
apprend, sur ce NAO, des gestes émotionnels à partir de prises enregistrées à
la main. Il exporte un générateur autonome (`nao_gesture.py` + `nao_gesture.json`,
Python standard) : émotion + intensité + événement (direction d'une voix) →
trajectoire de la tête, des bras, des poignets et des mains, validée par un
« guardian » (zone, vitesses, accélérations, transitions repos ↔ départ).
Le générateur est publié dans le dépôt public `nao-emotion-motion-dataset`.

L'ADR 008 limite NAO à trois chaînes (tête, épaules, coudes), sans poignet ni
main, avec des ordres « une cible par chaîne ». Le robot plante facilement :
rien de lourd ne doit tourner dessus.

## Décision

1. **Les gestes appris peuvent bouger poignets et mains**, en plus des trois
   chaînes de l'ADR 008, parce qu'ils ont été appris et validés sur ce robot.
   Les ordres bas niveau du LLM (`motion`) restent dans l'univers de l'ADR 008.
2. **Le générateur tourne sur le drone statique** (`emotion_gesture.py`), pas
   sur le robot ni sur le VPS. Le robot ne fait que donner ses angles, jouer
   une trajectoire (`ALMotion.angleInterpolation`) et s'arrêter sur `/nao/stop`.
3. **Version fixée à la main** : `GESTURE_DATASET_REF` (tag ou commit) dans
   `emotion_gesture.env` sur le drone. Pas de suivi automatique de `main`. Les
   fichiers sont vérifiés (sha256 de `dataset.json`) et gardés par version ;
   une version refusée laisse l'ancienne en service.
4. **Exécution automatique** dès que le guardian accepte le geste, sans
   validation manuelle.
5. **Poses de repos et de départ** : celles de la calibration de
   nao-emotion-motion. Le profil moteur de la régie doit les recopier ; un
   écart de plus de 0,05 rad est signalé à chaque chargement de version.
6. **Après un geste**, le robot reste raide à la pose de départ. Sans nouveau
   geste pendant `GESTURE_REST_DELAY` secondes, le drone envoie le retour au
   repos, validé lui aussi, et le robot se relâche à la fin.
7. **Les gestes sont rendus jouables sur le drone** (`gesture_playback.py`),
   comme le fait l'interface de nao-emotion-motion (`playable.py`) : le
   générateur exporté ne le fait pas et refuse sinon presque tous les gestes.
   nao-emotion-motion n'est pas modifié ; le guardian exporté juge toujours le
   mouvement final.
8. **Le driver NAO arbitre** : il refuse un geste pendant une posture, une
   marche ou avec une chaîne trop chaude, attend que les chaînes aient fini
   leur étape, puis les réserve au drone tant que celui-ci tient le corps
   (`owns_body`). Si le drone se tait trop longtemps, le driver reprend la main
   et ramène les chaînes au repos par le départ.

Répartition :

| Où | Quoi |
|---|---|
| `body-gateway` | action `gesture` dans le prompt (émotions connues du générateur), intensité, direction de la voix (signe inversé) |
| driver NAO (`motion_policy.py`) | refus (posture, marche, chaleur), réservation des chaînes, reprise si le drone se tait |
| drone statique (`emotion_gesture.py`) | version du générateur, posture mesurée, génération et guardian, retour au repos |
| pont sur le robot (`nao_ws_bridge.py`, module `trajectory`) | angles, contrôle de la trajectoire (articulations, plages matérielles, posture de départ), lecture, arrêt, relâchement |
| régie | page Robot : version en service, bouton « Recharger », test d'un geste |

## Conséquences

- Il faut publier un tag (ou noter un commit) du dataset et l'écrire dans
  `emotion_gesture.env` pour changer de modèle ; rien ne change tout seul.
- Le pont ne contrôle les gestes appris qu'avec les plages matérielles (moins
  une marge), pas avec la boîte du profil moteur : le guardian du générateur
  est le vrai filet, la boîte de l'ADR 008 ne s'applique qu'aux ordres `motion`.
- Les gestes sont joués à la vitesse apprise, plus vite que la vitesse plafond
  des ordres `motion` (0,3) : c'est le guardian qui borne vitesses et
  accélérations.
- `gesture_playback.py` copie la logique de `playable.py` : à reporter si
  elle évolue dans nao-emotion-motion.
- Pendant un geste appris, la tête ne suit plus la voix ; le geste s'oriente
  lui-même vers la voix entendue.
