"""Inspect and clean up PGN files: summary statistics and duplicate removal.

Built for PGNs merged from several sources or player spellings (e.g. an
export_pgn.py --merge export), where the same game appears more than once with
different metadata: date precision, event or player spelling, round number.

Duplicates come in two kinds:
  exact     identical start position and mainline moves. Headers are ignored,
            so copies with different dates or spellings are caught.
  probable  different move data (a transcription variant, e.g. two moves in
            swapped order), but the same players, year and result, and mostly
            the same positions (position-set similarity >= --similarity).

Every duplicate is listed as a table comparing it with the kept game: headers,
lengths, the moves that differ, and conflicts such as different results.

Usage:
    venv/bin/python pgndoctor.py -f games.pgn                     # summary (= --info)
    venv/bin/python pgndoctor.py -f games.pgn --info --json       # summary as JSON
    venv/bin/python pgndoctor.py -f games.pgn --dedup             # -> games.dedup.pgn
    venv/bin/python pgndoctor.py -f games.zip --dedup --dedup-probable -o clean.pgn

--dedup copies every kept game's original text unchanged and never modifies the
input file. The first copy of each duplicate group is kept.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import statistics
import sys
import zipfile
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, TextIO

import chess
import chess.pgn

# Games shorter than this (in plies) are only exact duplicates when they also
# share the players and the date, so two unrelated miniatures that happen to
# have the same moves (e.g. 1. f3 e5 2. g4 Qh4#) are both kept.
DEFAULT_MIN_PLIES = 6

# Probable duplicates: minimum position-set similarity (Jaccard index), and the
# minimum game length for the check (short games share too few positions).
# Calibrated on the Lasker, Emanuel / Lasker, E. identities in Caissabase:
# copies of one game scored 0.52-0.98, different games of a match <= 0.41.
DEFAULT_SIMILARITY = 0.5
PROBABLE_MIN_PLIES = 20

DEFAULT_LIST = 20
PROGRESS_EVERY = 5000

# Headers compared between a duplicate and the kept game. The seven-tag roster
# is always shown in the comparison table, the others only if either game has them.
COMPARE_TAGS = ("Date", "Event", "Site", "Round", "White", "Black", "Result", "WhiteElo", "BlackElo", "ECO")
ROSTER = COMPARE_TAGS[:7]

MAX_MOVE_DIFFS = 4    # differing move blocks shown per table (JSON has all)
CELL_WIDTH = 40       # table cells are cut to this many characters

CONFLICTS = {
    "result": "results differ: one copy has a wrong result",
    "colors": "White and Black are swapped: one copy has the colors wrong",
}


class _RecordingReader:
    """Wraps a text stream and records the lines read_game() consumes.

    read_game() only calls readline() and stops right after the blank line that
    ends a game, so the recorded lines are exactly that game's original text.
    """

    def __init__(self, stream: TextIO):
        self.stream = stream
        self.lines: list[str] = []

    def readline(self) -> str:
        line = self.stream.readline()
        self.lines.append(line)
        return line

    def take(self) -> str:
        text = "".join(self.lines)
        self.lines.clear()
        return text


def _encode_move(move: chess.Move) -> int:
    """A move in 15 bits (from, to, promotion piece), for compact storage in array('H')."""
    return move.from_square | move.to_square << 6 | (move.promotion or 0) << 12


def _decode_move(value: int) -> chess.Move:
    return chess.Move(value & 63, value >> 6 & 63, value >> 12 or None)


@dataclass(slots=True)
class GameRef:
    """What is remembered of a game to compare later games with.

    Kept for every game, so it is compact: the compared headers and 2 bytes per move.
    """
    index: int
    tags: tuple[str, ...]            # values of COMPARE_TAGS, "" if missing
    start_fen: str
    moves: array                     # array('H') of _encode_move() values
    positions: array | None = None   # array('q') of position hashes, only for probable matching

    def tag(self, name: str, default: str = "?") -> str:
        return self.tags[COMPARE_TAGS.index(name)] or default


@dataclass
class GameInfo:
    index: int = 0                    # 1-based position in the input, across all zip members
    headers: dict[str, str] = field(default_factory=dict)
    start_fen: str | None = None
    moves: list[chess.Move] = field(default_factory=list)   # mainline
    positions: set[int] = field(default_factory=set)        # hashes of the positions before each move
    error: Exception | None = None
    text: str = ""                    # original PGN text

    def tag(self, name: str, default: str = "?") -> str:
        return self.headers.get(name) or default

    def ref(self) -> GameRef:
        positions = array("q", self.positions) if len(self.moves) >= PROBABLE_MIN_PLIES else None
        return GameRef(self.index, tuple(self.tag(t, "") for t in COMPARE_TAGS),
                       self.start_fen or chess.STARTING_FEN, array("H", map(_encode_move, self.moves)), positions)


def describe(game: GameInfo | GameRef) -> str:
    """One line identifying a game in reports (PGN games have no IDs)."""
    return (f"#{game.index}: {game.tag('Date', '????.??.??')} | {game.tag('Event')} | "
            f"rd {game.tag('Round')} | {game.tag('White')} - {game.tag('Black')} | "
            f"{game.tag('Result', '*')} | {len(game.moves)} plies")


class _GameCollector(chess.pgn.BaseVisitor):
    """Lightweight visitor: headers, start FEN, mainline moves and positions only.

    Much faster than building full GameNode trees. Side variations are skipped;
    on an illegal/ambiguous move the game is recorded up to the last valid move.
    """

    def begin_game(self):
        self.info = GameInfo()

    def visit_header(self, tagname, tagvalue):
        # Undecodable bytes are kept as surrogates for the byte-exact --dedup
        # output; show them as U+FFFD in reports.
        self.info.headers[tagname] = tagvalue.encode("utf-8", "surrogateescape").decode("utf-8", "replace")

    def visit_board(self, board):
        if self.info.start_fen is None:
            self.info.start_fen = board.fen()

    def begin_parse_san(self, board, san):
        # Stop recording after the first error (remaining moves are unreliable).
        return chess.pgn.SKIP if self.info.error else None

    def visit_move(self, board, move):
        if self.info.error is None:
            self.info.moves.append(move)
            self.info.positions.add(hash((board.board_fen(), board.turn)))

    def begin_variation(self):
        return chess.pgn.SKIP

    def handle_error(self, error):
        if self.info.error is None:
            self.info.error = error

    def result(self):
        return self.info


def _open_pgn_streams(path: Path) -> Iterator[tuple[str, TextIO]]:
    """Yield text streams for the input: the file itself, or each .pgn inside a zip.

    surrogateescape keeps undecodable bytes (e.g. Latin-1 files), so --dedup can
    write every kept game back byte for byte.
    """
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".pgn")]
            if not names:
                raise ValueError(f"No .pgn files inside {path}")
            for name in names:
                with zf.open(name) as raw:
                    yield name, io.TextIOWrapper(raw, encoding="utf-8-sig", errors="surrogateescape")
    else:
        with open(path, encoding="utf-8-sig", errors="surrogateescape") as fh:
            yield path.name, fh


def read_games(path: Path) -> Iterator[GameInfo]:
    """Yield every game of a .pgn or .zip with its original text."""
    index = 0
    for _, stream in _open_pgn_streams(path):
        reader = _RecordingReader(stream)
        while (info := chess.pgn.read_game(reader, Visitor=_GameCollector)) is not None:
            index += 1
            info.index = index
            info.text = reader.take()
            if index % PROGRESS_EVERY == 0:
                print(f"  read {index} games...", file=sys.stderr)
            yield info


def exact_key(game: GameInfo, min_plies: int) -> bytes:
    """Digest identifying a game's content, for exact-duplicate detection.

    A digest instead of the move tuple itself keeps memory flat on big files.
    """
    if not game.moves:
        # No moves to compare: only an all-identical header set is a duplicate.
        parts = [game.tag(k, "") for k in ROSTER]
    else:
        parts = [game.start_fen or "", " ".join(m.uci() for m in game.moves)]
        if len(game.moves) < min_plies:
            parts += [game.tag("White"), game.tag("Black"), game.tag("Date")]
    return hashlib.blake2b("\x00".join(parts).encode("utf-8", "surrogateescape"), digest_size=16).digest()


def probable_key(game: GameInfo) -> tuple:
    """Headers that copies of one game share even when the moves were transcribed
    differently: the players in either color order, the year and the result."""
    return frozenset((game.tag("White"), game.tag("Black"))), game.tag("Date")[:4], game.tag("Result", "*")


def shared_positions(a: set[int], b: array) -> tuple[int, int]:
    """(positions in both games, positions in either game). Their ratio is the
    similarity (Jaccard index). Unlike a common move prefix, it stays high when a
    copy has two moves swapped early and the game transposes back."""
    common = len(a.intersection(b))
    return common, len(a) + len(b) - common


def _surname(name: str) -> str:
    return name.split(",")[0].strip().lower()


def _move_block(start_fen: str, moves: list[chess.Move], i: int, j: int) -> tuple[str, str]:
    """(move-number range, SAN) of moves[i:j], e.g. ("23-24", "23. Re1 Nd7 24. Bf4")."""
    board = chess.Board(start_fen)
    for move in moves[:i]:
        board.push(move)
    if i == j:
        return str(board.fullmove_number), "-"
    first = board.fullmove_number
    try:
        san = board.variation_san(moves[i:j])
    except ValueError:  # e.g. a Chess960 start that Board(fen) reads differently
        san = " ".join(m.uci() for m in moves[i:j])
    for move in moves[i:j - 1]:
        board.push(move)
    last = board.fullmove_number
    return (str(first) if first == last else f"{first}-{last}"), san


def move_differences(start_fen: str, kept: list[chess.Move], game: list[chess.Move]) -> list[dict]:
    """The blocks where two move lists differ, in SAN with move numbers."""
    matcher = difflib.SequenceMatcher(None, [m.uci() for m in kept], [m.uci() for m in game], autojunk=False)
    diffs = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        kept_range, kept_san = _move_block(start_fen, kept, i1, i2)
        game_range, game_san = _move_block(start_fen, game, j1, j2)
        if kept_san == game_san:
            # Same SAN, different move, e.g. Rxd8 by the other rook after an
            # earlier difference: show the squares.
            kept_san += f" ({' '.join(m.uci() for m in kept[i1:i2])})"
            game_san += f" ({' '.join(m.uci() for m in game[j1:j2])})"
        diffs.append({"moves": kept_range if i2 > i1 else game_range, "kept": kept_san, "duplicate": game_san})
    return diffs


@dataclass
class Duplicate:
    kind: str                       # "exact" or "probable"
    index: int                      # the duplicate's position in the input
    kept_index: int                 # the earlier game it duplicates
    game: str                       # describe() of both
    kept: str
    similarity: float = 1.0
    shared_positions: int | None = None     # probable only: positions in both games ...
    all_positions: int | None = None        # ... out of positions in either game
    plies: list[int] = field(default_factory=list)                  # [kept, duplicate]
    headers: dict[str, list[str]] = field(default_factory=dict)     # tag -> [kept, duplicate]
    moves: list[dict] = field(default_factory=list)                 # move_differences()
    conflicts: list[str] = field(default_factory=list)              # keys of CONFLICTS


def compare(kind: str, kept: GameRef, game: GameInfo) -> Duplicate:
    """Build the report entry for `game`, a duplicate of the earlier `kept` game."""
    dup = Duplicate(kind, game.index, kept.index, describe(game), describe(kept),
                    plies=[len(kept.moves), len(game.moves)])
    for tag in COMPARE_TAGS:
        values = [kept.tag(tag, ""), game.tag(tag, "")]
        if tag in ROSTER or any(values):
            dup.headers[tag] = [v or "?" for v in values]
    if kept.tag("Result") != game.tag("Result"):
        dup.conflicts.append("result")
    kw, kb, gw, gb = map(_surname, (kept.tag("White"), kept.tag("Black"), game.tag("White"), game.tag("Black")))
    if kw != kb and (kw, kb) == (gb, gw):
        dup.conflicts.append("colors")
    if kind == "probable":
        dup.moves = move_differences(kept.start_fen, list(map(_decode_move, kept.moves)), game.moves)
    return dup


@dataclass
class Report:
    """Statistics and duplicates of one PGN file."""
    path: str
    games: int = 0
    parse_errors: list[str] = field(default_factory=list)
    no_moves: int = 0
    custom_start: int = 0
    self_play: list[str] = field(default_factory=list)
    players: Counter[str] = field(default_factory=Counter)
    player_years: dict[str, list[str]] = field(default_factory=dict)   # name -> [first, last] year
    date_min: str | None = None
    date_max: str | None = None
    dates: Counter[str] = field(default_factory=Counter)               # complete / partial / missing
    decades: Counter[str] = field(default_factory=Counter)
    results: Counter[str] = field(default_factory=Counter)
    plies: list[int] = field(default_factory=list)
    events: Counter[str] = field(default_factory=Counter)
    sites: Counter[str] = field(default_factory=Counter)
    eco: Counter[str] = field(default_factory=Counter)
    duplicates: list[Duplicate] = field(default_factory=list)

    def add(self, game: GameInfo) -> None:
        self.games += 1
        if game.error is not None:
            self.parse_errors.append(f"{describe(game)} | {game.error}")
        if not game.moves and game.error is None:
            self.no_moves += 1
        if "FEN" in game.headers:
            self.custom_start += 1
        white, black = game.tag("White"), game.tag("Black")
        if white == black and white != "?":
            self.self_play.append(describe(game))

        date = game.tag("Date")
        year = date[:4]
        dated = year.isdigit()
        if not dated:
            self.dates["missing"] += 1
        else:
            self.dates["partial" if "?" in date else "complete"] += 1
            self.decades[year[:3] + "0s"] += 1
            self.date_min = min(self.date_min or date, date)
            self.date_max = max(self.date_max or date, date)
        for name in (white, black):
            self.players[name] += 1
            if dated:
                span = self.player_years.setdefault(name, [year, year])
                span[0], span[1] = min(span[0], year), max(span[1], year)

        self.results[game.tag("Result", "*")] += 1
        self.plies.append(len(game.moves))
        self.events[game.tag("Event")] += 1
        self.sites[game.tag("Site")] += 1
        if "ECO" in game.headers:
            self.eco[game.headers["ECO"]] += 1

    def count(self, kind: str) -> int:
        return sum(d.kind == kind for d in self.duplicates)

    def conflicts(self) -> Counter[str]:
        return Counter(c for d in self.duplicates for c in d.conflicts)

    def to_dict(self, top: int) -> dict:
        """The report as a JSON-serializable dict."""
        plies = self.plies
        return {
            "file": self.path,
            "games": self.games,
            "parse_errors": self.parse_errors,
            "games_without_moves": self.no_moves,
            "games_with_custom_start": self.custom_start,
            "self_play": self.self_play,
            "players": {
                "distinct": len(self.players),
                "top": [{"name": n, "games": c, "years": self.player_years.get(n)}
                        for n, c in self.players.most_common(top)],
            },
            "dates": {"min": self.date_min, "max": self.date_max, **{k: self.dates[k] for k in
                                                                     ("complete", "partial", "missing")},
                      "decades": dict(sorted(self.decades.items()))},
            "results": dict(self.results.most_common()),
            "plies": {"min": min(plies), "median": statistics.median(plies),
                      "mean": round(statistics.mean(plies), 1), "max": max(plies)} if plies else None,
            "events": {"distinct": len(self.events), "top": self.events.most_common(top)},
            "sites": {"distinct": len(self.sites), "top": self.sites.most_common(top)},
            "eco": {"distinct": len(self.eco), "top": self.eco.most_common(top)},
            "duplicates": {
                "exact": self.count("exact"),
                "probable": self.count("probable"),
                "conflicts": dict(self.conflicts()),
                "list": [vars(d) for d in self.duplicates],
            },
        }


def scan(
    path: Path,
    min_plies: int = DEFAULT_MIN_PLIES,
    min_similarity: float = DEFAULT_SIMILARITY,
    drop_probable: bool = False,
    out: TextIO | None = None,
) -> Report:
    """Read every game once, collect statistics and find duplicates.

    If `out` is given, every kept game's original text is written to it (the
    --dedup output): exact duplicates are dropped, probable ones only with
    `drop_probable`. Duplicate detection doesn't depend on `drop_probable`, so
    --info and --dedup always report the same pairs.
    """
    report = Report(str(path))
    first_seen: dict[bytes, GameRef] = {}                # exact key -> first copy
    groups: dict[tuple, list[GameRef]] = defaultdict(list)   # probable key -> distinct earlier games

    for game in read_games(path):
        report.add(game)
        dup = None
        # Games with parse errors are always kept: their moves stop at the error,
        # so comparing them could match a different, shorter game.
        if game.error is None:
            key = exact_key(game, min_plies)
            if key in first_seen:
                dup = compare("exact", first_seen[key], game)
            else:
                ref = first_seen[key] = game.ref()
                if ref.positions is not None:
                    group = groups[probable_key(game)]
                    best = max(((shared_positions(game.positions, g.positions), g) for g in group),
                               key=lambda b: b[0][0] / b[0][1], default=None)
                    if best and best[0][0] / best[0][1] >= min_similarity:
                        (common, union), kept = best
                        dup = compare("probable", kept, game)
                        dup.similarity = round(common / union, 2)
                        dup.shared_positions, dup.all_positions = common, union
                    else:
                        # Only distinct games join the group, so later copies are
                        # compared with the game that is kept.
                        group.append(ref)
        if dup:
            report.duplicates.append(dup)
        if out is not None and (dup is None or (dup.kind == "probable" and not drop_probable)):
            out.write(game.text.strip() + "\n\n")
    return report


def _cell(text: str, width: int = CELL_WIDTH) -> str:
    return text if len(text) <= width else text[:width - 1] + "…"


def format_duplicate(d: Duplicate) -> list[str]:
    """A duplicate as a table comparing it with the kept game, one line per row."""
    if d.kind == "exact":
        title = f"#{d.index} = #{d.kept_index}  exact duplicate: the same moves"
    else:
        title = (f"#{d.index} ~ #{d.kept_index}  probable duplicate, similarity {d.similarity:.2f}: "
                 f"{d.shared_positions} of {d.all_positions} positions occur in both games")

    rows = [(tag, kept, dup) for tag, (kept, dup) in d.headers.items()]
    rows.append(("Length", f"{d.plies[0]} plies", f"{d.plies[1]} plies"))
    for diff in d.moves[:MAX_MOVE_DIFFS]:
        label = f"Move {diff['moves']}" if "-" not in diff["moves"] else f"Moves {diff['moves']}"
        rows.append((label, diff["kept"], diff["duplicate"]))
    if len(d.moves) > MAX_MOVE_DIFFS:
        more = len(d.moves) - MAX_MOVE_DIFFS
        rows.append(("", f"... {more} more difference{'s' if more > 1 else ''} (see --json)", ""))

    label_w = max(len(r[0]) for r in rows)
    kept_w = min(CELL_WIDTH, max(len(f"kept #{d.kept_index}"), *(len(r[1]) for r in rows)))
    lines = [title, f"  {'':<{label_w}}  {f'kept #{d.kept_index}':<{kept_w}}  duplicate #{d.index}"]
    for label, kept, dup in rows:
        other = "(same)" if dup == kept and not label.startswith("Move") else dup
        lines.append(f"  {label:<{label_w}}  {_cell(kept, kept_w):<{kept_w}}  {_cell(other)}".rstrip())
    lines += [f"  ! {CONFLICTS[c]}" for c in d.conflicts]
    return lines


def print_report(report: Report, limit: int, top: int, min_similarity: float) -> None:
    """Print the --info summary."""
    def row(label: str, value) -> None:
        print(f"{label + ':':<24}{value}")

    def listing(title: str, items: list[str], note: str = "") -> None:
        shown = items if limit == 0 else items[:limit]
        if shown:
            print(f"\n{title}:")
            if note:
                print(note)
            for item in shown:
                print(f"  {item}")
            if len(items) > len(shown):
                print(f"  ... and {len(items) - len(shown)} more (--list 0 shows all)")

    row("File", report.path)
    row("Games", report.games)
    row("  with parse errors", f"{len(report.parse_errors)} (moves counted up to the error)")
    row("  without moves", report.no_moves)
    row("  custom start (FEN)", report.custom_start)
    row("  self-play", f"{len(report.self_play)} (White = Black)")
    if report.date_min:
        row("Dates", f"{report.date_min} .. {report.date_max}")
    else:
        row("Dates", "none")
    row("  precision", f"{report.dates['complete']} complete, {report.dates['partial']} partial "
                       f"(e.g. 1925.??.??), {report.dates['missing']} missing")
    if report.decades:
        row("  per decade", ", ".join(f"{d}: {n}" for d, n in sorted(report.decades.items())))
    row("Results", ", ".join(f"{r}: {n}" for r, n in report.results.most_common()))
    if report.plies:
        p = report.plies
        row("Length (plies)", f"min {min(p)}, median {statistics.median(p):g}, "
                              f"mean {statistics.mean(p):.1f}, max {max(p)}")
    row("Players", f"{len(report.players)} distinct")
    row("Events", f"{len(report.events)} distinct")
    row("Sites", f"{len(report.sites)} distinct")
    row("ECO codes", f"{len(report.eco)} distinct")
    exact, probable = report.count("exact"), report.count("probable")
    row("Duplicates", f"{exact} exact, {probable} probable (similarity >= {min_similarity:g})")
    conflicts = report.conflicts()
    if conflicts:
        row("  conflicting copies", f"{conflicts['result']} with different results, "
                                    f"{conflicts['colors']} with White/Black swapped")
    row("  --dedup keeps", f"{report.games - exact} games "
                           f"({report.games - exact - probable} with --dedup-probable)")

    print("\nTop players:\n   games  years      name")
    for name, n in report.players.most_common(top):
        years = report.player_years.get(name)
        span = f"{years[0]}-{years[1]}" if years else "?"
        print(f"  {n:>6}  {span:<9}  {name}")
    for title, counter in (("Top events", report.events), ("Top sites", report.sites), ("Top ECO codes", report.eco)):
        if counter:
            print(f"\n{title}:")
            for name, n in counter.most_common(top):
                print(f"  {n:>6}  {name}")

    listing("Parse errors", report.parse_errors)
    listing("Self-play games", report.self_play)
    tables = {kind: ["\n  ".join(format_duplicate(d)) + "\n" for d in report.duplicates if d.kind == kind]
              for kind in ("exact", "probable")}
    listing("Exact duplicates (removed by --dedup)", tables["exact"],
            "  The same moves as an earlier game; only the headers differ.\n")
    listing("Probable duplicates (removed only with --dedup-probable; review first)", tables["probable"],
            "  Different moves, but the same players, year and result, and mostly the same positions.\n"
            "  similarity = positions that occur in both games / positions that occur in either game\n"
            f"  (1.00 = the same positions; pairs below {min_similarity:g} are not listed). The Move rows\n"
            "  show where the move lists differ; '-' means the game has no moves there.\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--pgnfile", required=True, help="input .pgn, or .zip (all .pgn files inside are read)")
    ap.add_argument("--info", action="store_true",
                    help="print a summary: games, players, dates, results, lengths, events, "
                         "duplicates (default when --dedup isn't given)")
    ap.add_argument("--json", action="store_true", help="print the --info summary as JSON (implies --info)")
    ap.add_argument("--dedup", action="store_true",
                    help="write the games without exact duplicates to -o; the first copy is kept")
    ap.add_argument("--dedup-probable", action="store_true",
                    help="with --dedup, also remove probable duplicates (review them with --info first)")
    ap.add_argument("-o", "--output", help="output PGN for --dedup (default: <input>.dedup.pgn next to the input)")
    ap.add_argument("--similarity", type=float, default=DEFAULT_SIMILARITY, metavar="X",
                    help=f"minimum position-set similarity (0-1) for probable duplicates "
                         f"(default {DEFAULT_SIMILARITY})")
    ap.add_argument("--min-plies", type=int, default=DEFAULT_MIN_PLIES, metavar="N",
                    help="games shorter than N plies are only duplicates if players and date also match; "
                         f"0 compares moves only (default {DEFAULT_MIN_PLIES})")
    ap.add_argument("--list", type=int, default=DEFAULT_LIST, metavar="N",
                    help=f"list at most N duplicates/errors per section in --info, 0 = all (default {DEFAULT_LIST})")
    ap.add_argument("--top", type=int, default=10, metavar="N",
                    help="entries in the top players/events/sites/ECO lists (default 10)")
    args = ap.parse_args(argv)

    if args.dedup_probable and not args.dedup:
        ap.error("--dedup-probable requires --dedup")
    if not 0 < args.similarity <= 1:
        ap.error("--similarity must be in (0, 1]")
    for opt in ("min_plies", "list", "top"):
        if getattr(args, opt) < 0:
            ap.error(f"--{opt.replace('_', '-')} must not be negative")
    info = args.info or args.json or not args.dedup

    src = Path(args.pgnfile)
    if not src.is_file():
        print(f"File not found: {src}", file=sys.stderr)
        return 1

    out_path = None
    if args.dedup:
        out_path = Path(args.output) if args.output else src.with_suffix(".dedup.pgn")
        if out_path.resolve() == src.resolve():
            ap.error("the output file must not be the input file")

    try:
        if out_path:
            with open(out_path, "w", encoding="utf-8", errors="surrogateescape") as out:
                report = scan(src, args.min_plies, args.similarity, args.dedup_probable, out)
        else:
            report = scan(src, args.min_plies, args.similarity)
    except ValueError as e:  # e.g. a zip without .pgn files
        print(e, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.to_dict(args.top), indent=2))
    elif info:
        print_report(report, args.list, args.top, args.similarity)

    if out_path:
        exact, probable = report.count("exact"), report.count("probable")
        removed = exact + (probable if args.dedup_probable else 0)
        msg = f"Removed {removed} duplicates ({exact} exact"
        msg += f", {probable} probable)" if args.dedup_probable else f"); kept {probable} probable duplicates"
        # With --json, stdout is the JSON document, so the summary goes to stderr.
        print(f"{msg}\nWrote {report.games - removed} of {report.games} games: {out_path}",
              file=sys.stderr if args.json else sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
