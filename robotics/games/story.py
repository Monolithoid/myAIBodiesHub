"""
story — NAO reads a story: an activity without game rules (storyteller of the control room).

It takes game-master's single activity slot, so no game starts during a reading
(and the OBS scene follows /activity).  Only the control room starts it; its
public state is the book and the chapter, updated by the reader.
"""

import game_core
from game_core import Event, Player

VIEW_FIELDS = ("title", "author", "year", "chapter", "chapters")


class Story(game_core.Game):
    id = "story"
    name = "Histoire"
    hidden = True
    how_to_play = "aucun coup : NAO lit une histoire"

    def __init__(self):
        self._view: dict = {}

    def start(self, player: Player, options: dict) -> Event:
        self.set_view(options)
        return Event("started", "La lecture d'une histoire commence.")

    def set_view(self, view: dict):
        self._view = {k: view[k] for k in VIEW_FIELDS if view.get(k) is not None}

    def on_move(self, move: dict, player: Player) -> Event:
        return Event("invalid", "NAO lit une histoire : il n'y a aucun coup à jouer.",
                     "Chut, je lis une histoire !", ok=False)

    def public_view(self) -> dict:
        return dict(self._view)

    def summary(self) -> str:
        v = self._view
        if not v.get("title"):
            return ""
        return (f"Histoire en cours : {v['title']}, de {v.get('author', '')}, "
                f"chapitre {v.get('chapter', '?')} sur {v.get('chapters', '?')}.")

    def busy_event(self) -> Event:
        return Event("busy", "NAO lit une histoire : une seule activité à la fois.",
                     "Je suis en pleine histoire !", ok=False)

    def cancelled_event(self, player: Player) -> Event:
        return Event("cancelled", f"La lecture de l'histoire a été arrêtée par {player.label()}.",
                     "J'arrête là l'histoire.")
