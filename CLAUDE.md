# CLAUDE.md

Python tooling for extracting chess games from the Caissabase 2024 database into PGN, for use in a chess app.
The database (`caissabase_2024.db3`, ~1.3 GB, git-ignored) is an SQLite file produced by the En Croissant GUI.
All columns are plain SQL except `Games.Moves`. That column stores one byte per ply: the index of the move in
shakmaty's legal-move order. `caissabase.py` reproduces that order with python-chess to decode it.

## Key files

- `docs/CAISSABASE_DB.md`: schema, move encoding, data quirks and SQL/Python extraction examples. Read this first.
- `caissabase.py`: library for the read-only connection, player search, game queries, move decoding and PGN conversion.
- `export_pgn.py`: CLI to export a player's games to PGN (`--search`, repeatable `--name`/`--id`,
  comma-separated `--ids`,
  `--merge`/`--merge-as` to union identities under one name, `--color`, `--from`, `--to`, `--min-moves`, `--max-moves`). The filter SQL is shared by
  export and `--search` through `caissabase._game_filters()`, so search counts match the exports.
- `pgndoctor.py`: standalone CLI for any `.pgn`/`.zip`, with no DB access. `--info` prints a summary (the default),
  `--json` prints it as JSON, and `--dedup` writes `<input>.dedup.pgn`.
  Exact duplicates have the same start FEN and mainline, regardless of headers. Probable duplicates share
  players, year and result, and have a position-set Jaccard index ≥ `--similarity`. They are only removed with `--dedup-probable`.
  Kept games are copied verbatim through a `readline()`-recording reader, so don't re-serialize them.
  Duplicates are listed as `format_duplicate()` tables: header pairs, `difflib` move blocks in SAN, and conflicts
  (different results, swapped colors). This needs a compact `GameRef` for every earlier game (~2.2 KB/game in total).
  Parsing follows the lightweight `BaseVisitor` pattern of `~/src/chess-stuff/game-anal-v1/pgntools/repertoire.py`.
  `docs/PGNDOCTOR.md` has the full reference, the architecture and the verification checklist (section 8).
  Verify changes against the Lasker merge: 1187 games → 30 exact / 33 probable duplicates.
- `requirements.txt`: dependencies (`chess`).

## Environment

- Use the project virtualenv: `venv/bin/python` (Python 3.14).
- Always open the DB read-only (`caissabase.connect()` does this). Never modify the `.db3` file.

## Gotchas

- `Games.Result` is text (`'1-0'`) and `Games.Round` has mixed types, despite INTEGER declarations.
- A player can appear under several name spellings. Search with `LIKE` and pass all relevant IDs.
- When changing the decoder, re-validate it against `PawnHome` and `WhiteMaterial`/`BlackMaterial` (see doc section 4.4).
