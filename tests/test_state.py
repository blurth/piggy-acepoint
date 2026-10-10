"""比分板：不低于 0、清零清双方、闪烁序号。"""

from __future__ import annotations

import pytest

from acepoint.config import GameConfig
from acepoint.state import Action, ScoreBoard


def board_at(left: int, right: int, game: GameConfig | None = None) -> ScoreBoard:
    board = ScoreBoard(game)
    board.set_score(left, right)
    return board


def test_no_winner_mid_game():
    assert board_at(15, 12).snapshot().winner is None


def test_winner_is_reported_and_rendered():
    snapshot = board_at(20, 19).apply("left", Action.INCREMENT)
    assert snapshot.winner == "left"
    assert snapshot.text == "21:19"
    assert snapshot.win_text == "L WIN"


def test_scoring_is_frozen_after_the_game_is_won():
    """庆祝时挥手误触不能再把比分刷上去。"""
    board = board_at(21, 19)
    before = board.snapshot()
    after = board.apply("left", Action.INCREMENT)
    assert (after.left, after.right) == (21, 19)
    assert after.version == before.version          # 完全没动，也不该闪
    assert after.flash_seq == before.flash_seq


def test_the_other_side_cannot_score_after_the_game_is_won():
    board = board_at(21, 19)
    assert board.apply("right", Action.INCREMENT).right == 19


def test_undo_still_works_after_a_win():
    """最后那一分要是误加的，撤销要能把胜负一起撤回来。"""
    board = board_at(21, 19)
    snapshot = board.apply("left", Action.DECREMENT)
    assert (snapshot.left, snapshot.right) == (20, 19)
    assert snapshot.winner is None                  # 回到未分胜负，可以继续打


def test_reset_starts_the_next_game():
    board = board_at(21, 19)
    snapshot = board.apply("left", Action.RESET)
    assert (snapshot.left, snapshot.right) == (0, 0)
    assert snapshot.winner is None


def test_deuce_does_not_freeze():
    board = board_at(20, 20)
    assert board.apply("left", Action.INCREMENT).winner is None      # 21:20 还没赢
    assert board.apply("left", Action.INCREMENT).winner == "left"    # 22:20 才赢


def test_cap_ends_the_game_without_a_two_point_lead():
    board = board_at(29, 29)
    snapshot = board.apply("right", Action.INCREMENT)
    assert (snapshot.left, snapshot.right) == (29, 30)
    assert snapshot.winner == "right"


def test_match_point_appears_in_the_snapshot():
    snapshot = board_at(20, 19).snapshot()
    assert snapshot.match_points == ("left",)
    assert snapshot.marked_text == "*20:19"


def test_both_sides_marked_at_29_all():
    snapshot = board_at(29, 29).snapshot()
    assert snapshot.match_points == ("left", "right")
    assert snapshot.marked_text == "*29:29*"


def test_match_point_clears_once_the_game_is_won():
    board = board_at(20, 19)
    assert board.snapshot().match_points == ("left",)
    assert board.apply("left", Action.INCREMENT).match_points == ()


def test_winner_appears_in_the_web_payload():
    data = board_at(21, 19).snapshot().to_dict()
    assert data["winner"] == "left"
    assert data["win_text"] == "L WIN"


def test_rules_can_be_disabled():
    board = board_at(30, 0, GameConfig(enabled=False))
    assert board.snapshot().winner is None
    assert board.apply("left", Action.INCREMENT).left == 31   # 不冻结


def test_increment_and_text():
    board = ScoreBoard()
    board.apply("left", Action.INCREMENT)
    board.apply("left", Action.INCREMENT)
    board.apply("right", Action.INCREMENT)
    snapshot = board.snapshot()
    assert (snapshot.left, snapshot.right) == (2, 1)
    assert snapshot.text == "2:1"


def test_score_never_goes_below_zero():
    board = ScoreBoard()
    board.apply("left", Action.DECREMENT)
    assert board.snapshot().left == 0


def test_reset_clears_both_sides():
    board = ScoreBoard()
    board.apply("left", Action.INCREMENT)
    board.apply("right", Action.INCREMENT)
    board.apply("right", Action.RESET)
    snapshot = board.snapshot()
    assert (snapshot.left, snapshot.right) == (0, 0)
    assert "reset by right" in snapshot.last_event


def test_flash_sequence_advances_on_every_scoring_action():
    board = ScoreBoard()
    before = board.snapshot().flash_seq
    board.apply("left", Action.INCREMENT)
    board.apply("left", Action.DECREMENT)
    assert board.snapshot().flash_seq == before + 2


def test_set_score_does_not_flash():
    board = ScoreBoard()
    flash = board.snapshot().flash_seq
    snapshot = board.set_score(11, 9)
    assert (snapshot.left, snapshot.right) == (11, 9)
    assert snapshot.flash_seq == flash


def test_snapshot_serialises_for_the_web_ui():
    board = ScoreBoard()
    board.apply("left", Action.INCREMENT)
    data = board.snapshot().to_dict()
    assert data["text"] == "1:0"
    assert data["left"] == 1 and data["version"] == 1
