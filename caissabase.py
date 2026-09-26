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


def find_players(con: sqlite3.Connection, pattern: str, with_counts: bool = True) -> list[Player]:
    """Find players whose name matches a SQL LIKE pattern (case-insensitive for ASCII).

    Example: find_players(con, "Carlsen%")
    """
    rows = con.execute("SELECT ID, Name FROM Players WHERE Name LIKE ? ORDER BY Name", (pattern,)).fetchall()
    players = [Player(r["ID"], r["Name"]) for r in rows]
    if with_counts:
        for p in players:
            p.games = con.execute(
                "SELECT (SELECT COUNT(*) FROM Games WHERE WhiteID = ?) + "
                "(SELECT COUNT(*) FROM Games WHERE BlackID = ?)",
                (p.id, p.id),
            ).fetchone()[0]
    return players


GAME_QUERY = """
SELECT g.ID, g.Date, g.UTCTime, g.Round, g.WhiteElo, g.BlackElo, g.Result,
       g.TimeControl, g.ECO, g.PlyCount, g.FEN, g.Moves,
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
) -> Iterator[sqlite3.Row]:
    """Yield game rows for the given player IDs, oldest first.

    color: None (both), "white" or "black".
    date_from / date_to: inclusive bounds in PGN date format, e.g. "2015.01.01".
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
    where = ["(" + " OR ".join(sides) + ")"]
    if date_from:
        where.append("g.Date >= ?")
        params.append(date_from)
    if date_to:
        where.append("g.Date <= ?")
        params.append(date_to + "~")  # '~' sorts after digits and '?', keeps "2020.??.??"
    sql = GAME_QUERY + " WHERE " + " AND ".join(where) + " ORDER BY g.Date, g.ID"
    yield from con.execute(sql, params)


def _format_round(value) -> str:
    # Round has INTEGER affinity, so "3" is stored as 3 and "3.2" as the REAL 3.2.
    if value is None or value == "":
        return "?"
    return str(value)


def row_to_game(row: sqlite3.Row) -> chess.pgn.Game:
    """Convert a row from GAME_QUERY into a python-chess Game."""
    game = chess.pgn.Game()
    h = game.headers
    h["Event"] = row["Event"] or "?"
    h["Site"] = row["Site"] or "?"
    h["Date"] = row["Date"] or "????.??.??"
    h["Round"] = _format_round(row["Round"])
    h["White"] = row["White"] or "?"
    h["Black"] = row["Black"] or "?"
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


def row_to_pgn(row: sqlite3.Row) -> str:
    return str(row_to_game(row))
