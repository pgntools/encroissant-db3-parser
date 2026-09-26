"""Read games from an En Croissant ``.db3`` database (e.g. Caissabase 2024).

See docs/CAISSABASE_DB.md for a description of the schema and move encoding.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable, Iterator

import chess
import chess.pgn

DEFAULT_DB = "caissabase_2024.db3"

# Marker bytes used by newer En Croissant versions to embed comments, NAGs and
# variations in the Moves blob. Caissabase 2024 contains none of them, but we
# skip them anyway so the decoder works on databases built by newer versions.
VARIATION_START = 255
VARIATION_END = 254
COMMENT = 253
NAG = 252

# Promotion order used by shakmaty's push_promotions().
_PROMO_RANK = {chess.QUEEN: 0, chess.ROOK: 1, chess.BISHOP: 2, chess.KNIGHT: 3}
_PIECE_GROUP = {chess.KNIGHT: 6, chess.BISHOP: 7, chess.ROOK: 8, chess.QUEEN: 9}


def _shakmaty_key(board: chess.Board, move: chess.Move, in_check: bool) -> tuple:
    """Sort key reproducing the order of shakmaty's ``Chess::legal_moves()``.

    shakmaty (0.2x) generates, in this order:
      0. en passant captures (by from-square)
      then, when NOT in check:
      2. pawn captures towards the a-file, 3. pawn captures towards the h-file
         (non-promotions by to-square, then promotions by to-square, Q/R/B/N)
      4. single pawn pushes (non-promotions, then promotions)
      5. double pawn pushes
      6-9. knight, bishop, rook, queen moves (by from-square, then to-square)
      19. king moves (by to-square)
      20/21. castling king side, then queen side
      when in check, king moves come first (group 1) and castling is impossible.
    Illegal moves are then filtered out with an order-preserving retain().
    Squares are numbered a1=0 .. h8=63 in both libraries.
    """
    frm, to = move.from_square, move.to_square
    if board.is_en_passant(move):
        return (0, frm)
    piece = board.piece_type_at(frm)
    if piece == chess.KING:
        if board.is_castling(move):
            return (20 if board.is_kingside_castling(move) else 21,)
        return (1 if in_check else 19, to)
    if piece == chess.PAWN:
        promo = move.promotion is not None
        prank = _PROMO_RANK.get(move.promotion, 0)
        from_file, to_file = chess.square_file(frm), chess.square_file(to)
        if from_file != to_file:
            return (2 if to_file < from_file else 3, promo, to, prank)
        if abs(to - frm) == 16:
            return (5, to)
        return (4, promo, to, prank)
    return (_PIECE_GROUP[piece], frm, to)


def legal_moves_ordered(board: chess.Board) -> list[chess.Move]:
    """Legal moves of ``board`` in the order En Croissant indexes them."""
    in_check = board.is_check()
    return sorted(board.legal_moves, key=lambda m: _shakmaty_key(board, m, in_check))


def mainline_bytes(blob: bytes) -> Iterator[int]:
    """Yield the mainline move indices of a Moves blob, skipping annotations."""
    i, depth, n = 0, 0, len(blob)
    while i < n:
        b = blob[i]
        i += 1
        if b == VARIATION_START:
            depth += 1
        elif b == VARIATION_END:
            depth = max(depth - 1, 0)
        elif b in (COMMENT, NAG):
            if i + 2 > n:
                return
            length = int.from_bytes(blob[i:i + 2], "little")
            i += 2 + length
        elif depth == 0:
            yield b


def start_board(fen: str | None) -> chess.Board:
    if not fen:
        return chess.Board()
    board = chess.Board(fen)
    board.chess960 = board.has_chess960_castling_rights()
    return board


def decode_moves(blob: bytes | None, fen: str | None = None) -> list[chess.Move]:
    """Decode a Games.Moves blob into a list of python-chess moves."""
    board = start_board(fen)
    moves = []
    for idx in mainline_bytes(blob or b""):
        legal = legal_moves_ordered(board)
        if idx >= len(legal):
            raise ValueError(f"move index {idx} out of range at ply {len(moves) + 1}")
        move = legal[idx]
        board.push(move)
        moves.append(move)
    return moves


@dataclass
class Player:
    id: int
    name: str
    games: int | None = None


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    """Open the database read-only."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _game_filters(
    date_from: str | None = None,
    date_to: str | None = None,
    min_moves: int | None = None,
    max_moves: int | None = None,
) -> tuple[list[str], list]:
    """SQL conditions (on alias ``g``) and params for the common game filters.

    Moves are full moves (PGN move numbers): a game has N moves once White has
    played move N, so it has at least N moves if PlyCount >= 2N - 1 and at most
    N moves if PlyCount <= 2N.
    """
    where, params = [], []
    if date_from:
        where.append("g.Date >= ?")
        params.append(date_from)
    if date_to:
        where.append("g.Date <= ?")
        params.append(date_to + "~")  # '~' sorts after digits and '?', keeps "2020.??.??"
    if min_moves and min_moves > 0:
        where.append("g.PlyCount >= ?")
        params.append(2 * min_moves - 1)
    if max_moves is not None:
        where.append("g.PlyCount <= ?")
        params.append(2 * max_moves)
    return where, params


def find_players(
    con: sqlite3.Connection,
    pattern: str,
    with_counts: bool = True,
    color: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_moves: int | None = None,
    max_moves: int | None = None,
) -> list[Player]:
    """Find players whose name matches a SQL LIKE pattern (case-insensitive for ASCII).

    Game counts honour the same filters as iter_player_games(), so they match
    what an export with those filters would return.

    Example: find_players(con, "Carlsen%", min_moves=20)
    """
    rows = con.execute("SELECT ID, Name FROM Players WHERE Name LIKE ? ORDER BY Name", (pattern,)).fetchall()
    players = [Player(r["ID"], r["Name"]) for r in rows]
    if with_counts:
        filters, fparams = _game_filters(date_from, date_to, min_moves, max_moves)
        sides = [s for s in ("White", "Black") if color in (None, s.lower())]
        # One indexed count per side is faster than a single WhiteID = ? OR BlackID = ?.
        sql = " + ".join(
            "(SELECT COUNT(*) FROM Games g WHERE " + " AND ".join([f"g.{side}ID = ?"] + filters) + ")"
            for side in sides
        )
        for p in players:
            params = [v for _ in sides for v in [p.id] + fparams]
            p.games = con.execute("SELECT " + sql, params).fetchone()[0]
    return players


GAME_QUERY = """
SELECT g.ID, g.Date, g.UTCTime, g.Round, g.WhiteElo, g.BlackElo, g.Result,
       g.TimeControl, g.ECO, g.PlyCount, g.FEN, g.Moves, g.WhiteID, g.BlackID,
       w.Name AS White, b.Name AS Black, e.Name AS Event, s.Name AS Site
FROM Games g
JOIN Players w ON w.ID = g.WhiteID
JOIN Players b ON b.ID = g.BlackID
LEFT JOIN Events e ON e.ID = g.EventID
LEFT JOIN Sites s ON s.ID = g.SiteID
"""


def iter_player_games(
    con: sqlite3.Connection,
    player_ids: Iterable[int],
    color: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_moves: int | None = None,
    max_moves: int | None = None,
) -> Iterator[sqlite3.Row]:
    """Yield game rows for the given player IDs, oldest first.

    color: None (both), "white" or "black".
    date_from / date_to: inclusive bounds in PGN date format, e.g. "2015.01.01".
    min_moves / max_moves: inclusive bounds on the game length in full moves
        (PGN move numbers, "1. e4 e5" = 1 move). See _game_filters().
    """
    ids = list(player_ids)
    marks = ",".join("?" * len(ids))
    sides, params = [], []
    if color in (None, "white"):
        sides.append(f"g.WhiteID IN ({marks})")
        params += ids
    if color in (None, "black"):
        sides.append(f"g.BlackID IN ({marks})")
        params += ids
    filters, fparams = _game_filters(date_from, date_to, min_moves, max_moves)
    where = ["(" + " OR ".join(sides) + ")"] + filters
    params += fparams
    sql = GAME_QUERY + " WHERE " + " AND ".join(where) + " ORDER BY g.Date, g.ID"
    yield from con.execute(sql, params)


def _format_round(value) -> str:
    # Round has INTEGER affinity, so "3" is stored as 3 and "3.2" as the REAL 3.2.
    if value is None or value == "":
        return "?"
    return str(value)


def resolve_players(con: sqlite3.Connection, names: Iterable[str]) -> tuple[list[Player], list[str]]:
    """Look up exact player names. Returns (found players, names not found)."""
    found, missing = [], []
    for name in names:
        row = con.execute("SELECT ID, Name FROM Players WHERE Name = ?", (name,)).fetchone()
        if row is None:
            missing.append(name)
        else:
            found.append(Player(row["ID"], row["Name"]))
    return found, missing


def merge_identities(player_ids: Iterable[int], canonical_name: str) -> dict[int, str]:
    """Build a rename map for row_to_game() that reports several IDs under one name."""
    return {pid: canonical_name for pid in player_ids}


def row_to_game(row: sqlite3.Row, rename: dict[int, str] | None = None) -> chess.pgn.Game:
    """Convert a row from GAME_QUERY into a python-chess Game.

    rename: optional {player ID: name} map overriding the stored White/Black
    names, e.g. merge_identities([73583, 2876], "Carlsen, Magnus").
    """
    rename = rename or {}
    game = chess.pgn.Game()
    h = game.headers
    h["Event"] = row["Event"] or "?"
    h["Site"] = row["Site"] or "?"
    h["Date"] = row["Date"] or "????.??.??"
    h["Round"] = _format_round(row["Round"])
    h["White"] = rename.get(row["WhiteID"]) or row["White"] or "?"
    h["Black"] = rename.get(row["BlackID"]) or row["Black"] or "?"
    h["Result"] = row["Result"] or "*"
    if row["WhiteElo"]:
        h["WhiteElo"] = str(row["WhiteElo"])
    if row["BlackElo"]:
        h["BlackElo"] = str(row["BlackElo"])
    if row["ECO"]:
        h["ECO"] = row["ECO"]
    if row["TimeControl"]:
        h["TimeControl"] = row["TimeControl"]
    if row["UTCTime"]:
        h["UTCTime"] = row["UTCTime"]
    if row["FEN"]:
        game.setup(start_board(row["FEN"]))
    game.add_line(decode_moves(row["Moves"], row["FEN"]))
    return game


def row_to_pgn(row: sqlite3.Row, rename: dict[int, str] | None = None) -> str:
    return str(row_to_game(row, rename))
