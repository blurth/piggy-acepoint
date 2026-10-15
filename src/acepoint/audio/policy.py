"""Reversible scoring history and deterministic commentary candidates."""
from dataclasses import dataclass
from acepoint.state import Action

PRIORITIES = {"win_comeback": 100, "win_dominant": 100, "win_normal": 100,
              "decider": 90, "deuce": 85, "match_point": 80, "comeback_lead": 75,
              "comeback_tie": 70, "streak_7": 65, "streak_5": 60, "streak_3": 55,
              "icebreaker": 52, "shutdown": 50, "banter": 10, "point": 0}
ONCE = {"deuce", "decider", "match_point", "comeback_lead", "comeback_tie", "icebreaker"}


@dataclass(frozen=True)
class Candidate:
    name: str
    side: str
    left: int
    right: int
    game: int
    version: int

    @property
    def priority(self):
        return PRIORITIES[self.name]

    @property
    def key(self):
        return (self.game, self.name, self.side, self.left, self.right)


class History:
    def __init__(self):
        self.game = 0
        self.base = {"left": 0, "right": 0}
        self.points = []
        self.rebuild()

    def rebuild(self):
        score = dict(self.base)
        self.deficits = {s: max(0, score[o] - score[s]) for s, o in
                         (("left", "right"), ("right", "left"))}
        self.streak_side, self.streak = None, 0
        for side in self.points:
            score[side] += 1
            self.streak = self.streak + 1 if side == self.streak_side else 1
            self.streak_side = side
            for s, o in (("left", "right"), ("right", "left")):
                self.deficits[s] = max(self.deficits[s], score[o] - score[s])
        self.score = score

    def baseline(self, snapshot):
        self.base = {"left": snapshot.left, "right": snapshot.right}
        self.points.clear()
        self.rebuild()

    def update(self, side, action, before, after, config, game):
        if action is Action.RESET:
            self.game += 1
            self.baseline(after)
            return []
        if (before.left, before.right) == (after.left, after.right):
            return []
        if self.score != {"left": before.left, "right": before.right}:
            self.baseline(before)
        if action is Action.DECREMENT:
            for i in range(len(self.points) - 1, -1, -1):
                if self.points[i] == side:
                    self.points.pop(i)
                    self.rebuild()
                    break
            else:
                self.baseline(after)
            return []
        if action is not Action.INCREMENT:
            self.baseline(after)
            return []
        old_streak, old_side = self.streak, self.streak_side
        deficit = self.deficits[side]
        self.points.append(side)
        self.rebuild()
        n, old = getattr(after, side), getattr(before, side)
        opponent = "right" if side == "left" else "left"
        other = getattr(after, opponent)
        names = []
        if after.winner and not before.winner:
            names.append("win_comeback" if deficit >= config.comeback_deficit else
                         "win_dominant" if n - other >= config.dominant_margin else "win_normal")
        if game.enabled and (game.target, game.win_by, game.cap) == (21, 2, 30):
            if after.left == after.right == 29:
                names.append("decider")
            elif after.left == after.right == 20:
                names.append("deuce")
        if side in after.match_points and side not in before.match_points:
            names.append("match_point")
        if deficit >= config.comeback_deficit:
            if old < other and n == other:
                names.append("comeback_tie")
            elif old == other and n > other:
                names.append("comeback_lead")
        if self.streak in config.streak_thresholds:
            names.append(f"streak_{self.streak}")
        if old_side == opponent and old_streak >= config.shutdown_min_streak:
            names.append("shutdown")
        if old == 0 and other >= config.icebreaker_min_opponent:
            names.append("icebreaker")
        if len(self.points) > config.history_limit:
            self.baseline(after)
        return [Candidate(name, side, after.left, after.right, self.game, after.version)
                for name in sorted(names, key=lambda n: -PRIORITIES[n])]
