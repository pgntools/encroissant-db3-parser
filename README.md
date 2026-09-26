# Caissabase 2024 db3 parser

Tools for exporting chess games from the [Caissabase 2024](https://db.encroissant.org/caissabase_2024.db3) database
(5.4M games, 321k players) to PGN.

The `.db3` file is an SQLite database produced by the [En Croissant](https://github.com/franciscoBSalgueiro/en-croissant)
chess GUI. Players, events, sites, dates, ratings and results are plain SQL columns. The moves are stored in a compact
binary format: one byte per move, which is the move's index in the legal-move list at that point.
`caissabase.py` decodes this format with [python-chess](https://python-chess.readthedocs.io/). It also finds players,
queries their games, and writes standard PGN.

For the full schema, the move encoding, data quirks and raw SQL recipes, see
**[docs/CAISSABASE_DB.md](docs/CAISSABASE_DB.md)**.

`pgndoctor.py` summarizes any PGN file and removes duplicate games, e.g. copies of one game stored under two spellings
of a player's name. See [below](#cli-pgndoctorpy) and **[docs/PGNDOCTOR.md](docs/PGNDOCTOR.md)**.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Download [`caissabase_2024.db3`](https://db.encroissant.org/caissabase_2024.db3) (~1.3 GB) into the project root.
It is git-ignored. Every tool opens it read-only.

## CLI: `export_pgn.py`

```bash
# 1. find how a player is spelled (SQL LIKE pattern, % = wildcard)
python export_pgn.py --search "Carlsen%"
#     2876    3263 games  Carlsen, M
#    73583    2543 games  Carlsen, Magnus
#    ...

# 2. export all games of one player
python export_pgn.py "Carlsen, Magnus" -o carlsen.pgn

# 3. several spellings of the same player (--name and --id are repeatable, --ids takes a list)
python export_pgn.py --ids 73583,2876 -o carlsen_all.pgn
python export_pgn.py --name "Carlsen, Magnus" --name "Carlsen, M" -o carlsen_all.pgn

# 4. union them into one identity: "Carlsen, M" is written as "Carlsen, Magnus"
python export_pgn.py --name "Carlsen, Magnus" --name "Carlsen, M" --merge -o carlsen_all.pgn

# 5. ...or under a canonical name of your choice
python export_pgn.py --name "Carlsen, M" --id 73583 --merge-as "Magnus Carlsen" -o carlsen_all.pgn

# 6. filters: color and inclusive date range (YYYY.MM.DD)
python export_pgn.py --id 73583 --color white --from 2020.01.01 -o carlsen_white.pgn
python export_pgn.py "Kasparov, Garry" --from 1985.01.01 --to 1990.12.31 -o kasparov_85_90.pgn

# 7. filter by game length in full moves (see "Filtering by game length" below)
python export_pgn.py "Carlsen, Magnus" --min-moves 20 --max-moves 40 -o carlsen_20_40.pgn

# 8. search counts honour the same filters, so they show what an export would return
python export_pgn.py --search "Carlsen, Ma%" --color white --min-moves 20 --from 2015.01.01
```

| Option | Meaning |
|---|---|
| `NAME` / `--name NAME` | exact player name as stored; repeatable |
| `--id ID` | player ID shown by `--search`; repeatable |
| `--ids ID,ID,...` | comma-separated player IDs, e.g. `--ids 73583,2876`; can be mixed with `--id`/`--name` (order is kept, first one names a `--merge`) |
| `--merge` | write all selected identities under the first given name/ID |
| `--merge-as NAME` | write all selected identities under `NAME` |
| `--color white\|black` | only games played with that color |
| `--from DATE` / `--to DATE` | inclusive date bounds, `YYYY.MM.DD` |
| `--min-moves N` | skip games shorter than N full moves (`1. e4 e5` = 1 move) |
| `--max-moves N` | skip games longer than N full moves |
| `--search PATTERN` | list matching players with game counts, then exit. Counts honour `--color`, `--from`, `--to`, `--min-moves` and `--max-moves` |
| `--db PATH` | database file (default `caissabase_2024.db3`) |
| `-o FILE` | output file (default: stdout) |

Example output:

```
[Event "Troll Masters"]
[Site "Gausdal NOR"]
[Date "2001.01.05"]
[Round "1"]
[White "Edvardsen, Ragnar"]
[Black "Carlsen, Magnus"]
[Result "1/2-1/2"]
[WhiteElo "2055"]
[ECO "D12m"]

1. d4 Nf6 2. Nf3 d5 3. e3 Bf5 4. c4 c6 5. Nc3 e6 6. Bd3 Bxd3 7. Qxd3 Nbd7 ... 23. Kf2 Ke7 1/2-1/2
```

Export runs at about 150 games/s. Summary messages go to stderr, so `python export_pgn.py NAME > out.pgn` stays clean.

### Filtering by game length

`--min-moves` and `--max-moves` count **full moves** as in PGN move numbers: a game with last move `20. d5` or
`20… Kh8` has 20 moves. Both bounds are inclusive and combine with every other option.

Examples with both Carlsen identities merged ("Carlsen, Magnus" + "Carlsen, M", 5806 games without filters):

```bash
# drop the empty (0-move) games                                               -> 5798 games
python export_pgn.py --name "Carlsen, Magnus" --name "Carlsen, M" --merge --min-moves 1 -o carlsen.pgn

# only games of 40+ moves (skips quick draws and short blitz games)           -> 3563 games
python export_pgn.py --name "Carlsen, Magnus" --name "Carlsen, M" --merge --min-moves 40 -o carlsen_40plus.pgn

# miniatures: at most 20 moves                                                -> 196 games
python export_pgn.py --name "Carlsen, Magnus" --name "Carlsen, M" --merge --max-moves 20 -o carlsen_miniatures.pgn

# a length range                                                              -> 3933 games
python export_pgn.py --name "Carlsen, Magnus" --name "Carlsen, M" --merge --min-moves 30 --max-moves 60 -o carlsen_30_60.pgn

# preview the counts per identity before exporting (search honours the filters)
python export_pgn.py --search "Carlsen, M%" --min-moves 40
#     2876    2077 games  Carlsen, M
#    73583    1486 games  Carlsen, Magnus
#   ...
```

| Filter (Carlsen, Magnus + Carlsen, M) | Games |
|---|---|
| none | 5806 |
| `--min-moves 1` | 5798 |
| `--min-moves 20` | 5646 |
| `--min-moves 40` | 3563 |
| `--min-moves 60` | 1129 |
| `--max-moves 20` | 196 |
| `--max-moves 25` | 456 |
| `--min-moves 30 --max-moves 60` | 3933 |

```python
# the same filters in Python: Carlsen's won miniatures (max 25 moves), both identities
ids = {73583, 2876}                                  # "Carlsen, Magnus", "Carlsen, M"
wins = [
    r for r in cb.iter_player_games(con, ids, max_moves=25)
    if (r["WhiteID"] in ids and r["Result"] == "1-0") or (r["BlackID"] in ids and r["Result"] == "0-1")
]
```

More examples, including raw SQL for length ranges and length distributions, are in
[docs/CAISSABASE_DB.md § 5.6](docs/CAISSABASE_DB.md#56-filtering-by-game-length).

## CLI: `pgndoctor.py`

`pgndoctor.py` works on any `.pgn`, or a `.zip` of PGN files. It doesn't need the database.
`--info` (the default) prints a summary of the file, and `--dedup` writes a copy without duplicate games.

```bash
# summary: games, players (with first-last year), date range and precision, results,
# lengths, events, sites, ECO codes, self-play games and duplicates
python pgndoctor.py -f lasker.pgn

# the same as JSON, e.g. for an app
python pgndoctor.py -f lasker.pgn --json > lasker.json

# list every duplicate instead of the first 20
python pgndoctor.py -f lasker.pgn --list 0

# remove exact duplicates                                        -> lasker.dedup.pgn
python pgndoctor.py -f lasker.pgn --dedup

# remove probable duplicates too (review them in the summary first)
python pgndoctor.py -f lasker.pgn --dedup --dedup-probable -o lasker_clean.pgn

# a zip of PGNs, stricter probable matching, summary and cleaned file in one pass
python pgndoctor.py -f games.zip --info --dedup --similarity 0.7
```

A typical workflow: export several spellings of one player, then clean the result.

```bash
python export_pgn.py --search "Lasker,%" --min-moves 15
#   297878     780 games  Lasker, E.
#   316247     409 games  Lasker, Emanuel
#   ...
python export_pgn.py --ids 316247,297878 --merge --min-moves 15 -o lasker.pgn       # 1187 games
python pgndoctor.py -f lasker.pgn
# Duplicates:             30 exact, 33 probable (similarity >= 0.5)
#   --dedup keeps:        1157 games (1124 with --dedup-probable)
python pgndoctor.py -f lasker.pgn --dedup
# Removed 30 duplicates (30 exact); kept 33 probable duplicates
# Wrote 1157 of 1187 games: lasker.dedup.pgn
```

| Option | Meaning |
|---|---|
| `-f FILE` | input `.pgn` or `.zip` (required) |
| `--info` | print the summary (default when `--dedup` isn't given) |
| `--json` | print the summary as JSON |
| `--dedup` | write the file without exact duplicates. The first copy of each game is kept |
| `--dedup-probable` | with `--dedup`, also remove probable duplicates |
| `-o FILE` | output of `--dedup` (default `<input>.dedup.pgn`) |
| `--similarity X` | threshold for probable duplicates, 0–1 (default 0.5) |
| `--min-plies N` | games shorter than N plies only match if players and date also match (default 6; `0` = moves only) |
| `--list N` / `--top N` | entries per duplicate listing (default 20, `0` = all) / per top list (default 10) |

**Exact duplicates** have the same start position and the same moves, whatever their headers say.
So copies with a different date precision, event spelling, round or player spelling are still caught.

**Probable duplicates** are copies whose moves differ, e.g. from a transcription error or two swapped moves. They
share the players, year and result, and at least half of their positions. They are only listed unless you add
`--dedup-probable`.

Every duplicate is listed as a table next to the game it duplicates. `similarity 0.85: 75 of 88 positions occur in
both games` means that of the 88 board positions reached in either game, 75 are reached in both. The `Move` rows show
exactly where the move lists differ (`-` = no moves there), and a `!` line flags conflicting copies, such as
different results or swapped colors:

```
  #26 ~ #25  probable duplicate, similarity 0.85: 75 of 88 positions occur in both games
                 kept #25            duplicate #26
    Date         1976.07.??          1976.08.27
    Event        Wattignies wch-jr   Wch U16
    White        Chandler, Murray G  (same)
    Black        Kasparov, Garry     (same)
    Result       1-0                 (same)
    Length       81 plies            82 plies
    Moves 19-20  19...g5 20. Rab1    -
    Move 22      -                   22. Rab1 g5
    Move 41      -                   41...Ka7
```

Here one copy plays `...g5` and `Rab1` two moves later than the other (a transcription error) and has one extra final
move. It is the same game.

The cleaned file contains each kept game exactly as it was in the input: comments, variations and formatting are
unchanged. The input file is never modified. See [docs/PGNDOCTOR.md](docs/PGNDOCTOR.md) for the full reference,
the detection rules and the architecture.

## Python API: `caissabase.py`

```python
import caissabase as cb

con = cb.connect("caissabase_2024.db3")                  # read-only sqlite3 connection

# find players by LIKE pattern (game counts accept the same filters as iter_player_games)
for p in cb.find_players(con, "Carlsen, M%", min_moves=20):
    print(p.id, p.name, p.games)

# resolve exact names to IDs
players, missing = cb.resolve_players(con, ["Carlsen, Magnus", "Carlsen, M"])
ids = [p.id for p in players]

# stream games to a PGN file, merging identities under one name
rename = cb.merge_identities(ids, "Carlsen, Magnus")
with open("carlsen.pgn", "w") as f:
    for row in cb.iter_player_games(con, ids, color="white", date_from="2019.01.01",
                                    min_moves=20, max_moves=60):
        f.write(cb.row_to_pgn(row, rename) + "\n\n")

# work with python-chess objects instead of text
row = next(cb.iter_player_games(con, ids))
game = cb.row_to_game(row, rename)                        # chess.pgn.Game
print(game.headers["White"], game.headers["Black"], game.end().board().fen())

# decode a raw Moves blob yourself
moves = cb.decode_moves(row["Moves"], row["FEN"])         # list[chess.Move]
```

| Function | Purpose |
|---|---|
| `connect(path)` | open the DB read-only, rows as `sqlite3.Row` |
| `find_players(con, pattern, **filters)` | players matching a LIKE pattern, with (filtered) game counts |
| `resolve_players(con, names)` | exact names → `(players, missing_names)` |
| `iter_player_games(con, ids, color, date_from, date_to, min_moves, max_moves)` | game rows for player IDs, oldest first |
| `merge_identities(ids, name)` | rename map for merging identities |
| `row_to_game(row, rename)` / `row_to_pgn(row, rename)` | row → `chess.pgn.Game` / PGN text |
| `decode_moves(blob, fen)` | `Games.Moves` blob → list of `chess.Move` |
| `legal_moves_ordered(board)` | legal moves in En Croissant's (shakmaty's) index order |

Queries that go beyond players are easiest to write in raw SQL. Examples include head-to-head results,
opening statistics, Elo filters and endgame search. See section 5.4 of
[docs/CAISSABASE_DB.md](docs/CAISSABASE_DB.md#54-raw-sql-recipes).

## Caveats

- **One player can have several spellings**, e.g. `Carlsen, Magnus` and `Carlsen, M`. Use `--search`, then
  `--name`/`--id` with `--merge`. Short spellings can also belong to other people.
- **The same game is sometimes stored under two spellings**, so a merged export can contain duplicates
  (30 exact + 33 probable in the 1187-game Lasker export). Clean it with `pgndoctor.py --dedup`.
- **A short spelling can mix several people.** `Lasker, E.` also contains Edward Lasker's games from 1946–1976.
  The top-player year span in the `pgndoctor.py` summary makes this visible.
- **Games contain only the moves actually played.** The database has no comments, variations or clock times.
- **The source data wasn't cleaned**, so it contains the occasional wrong result or odd round value. See
  [docs/CAISSABASE_DB.md § 6](docs/CAISSABASE_DB.md#6-gotchas--data-quality).

## Files

| File | Purpose |
|---|---|
| `export_pgn.py` | CLI exporter |
| `pgndoctor.py` | CLI to summarize any PGN file and remove duplicate games |
| `caissabase.py` | library: DB access, move decoder, PGN conversion |
| `docs/CAISSABASE_DB.md` | database structure, move encoding, SQL and Python extraction examples |
| `docs/PGNDOCTOR.md` | `pgndoctor.py` reference, duplicate detection and architecture |
| `CLAUDE.md` | project notes for Claude Code |
| `requirements.txt` | dependencies (`chess`) |
