# CLAUDE.md

Python tooling for extracting chess games from the Caissabase 2024 database into PGN, for use in a chess app.
The database (`caissabase_2024.db3`, ~1.3 GB, git-ignored) is an SQLite file produced by the En Croissant GUI.
All columns are plain SQL except `Games.Moves`. That column stores one byte per ply: the index of the move in
shakmaty's legal-move order. `caissabase.py` reproduces that order with python-chess to decode it.

## Key files

- `docs/CAISSABASE_DB.md`: schema, move encoding, data quirks and SQL/Python extraction examples. Read this first.
- `caissabase.py`: library for the read-only connection, player search, game queries, move decoding and PGN conversion.
- `export_pgn.py`: CLI to export a player's games to PGN (`--search`, repeatable `--name`/`--id`,
  `--merge`/`--merge-as` to union identities under one name, `--color`, `--from`, `--to`, `--min-moves`, `--max-moves`). The filter SQL is shared by
  export and `--search` through `caissabase._game_filters()`, so search counts match the exports.
- `requirements.txt`: dependencies (`chess`).

## Environment

- Use the project virtualenv: `venv/bin/python` (Python 3.14).
- Always open the DB read-only (`caissabase.connect()` does this). Never modify the `.db3` file.

## Gotchas

- `Games.Result` is text (`'1-0'`) and `Games.Round` has mixed types, despite INTEGER declarations.
- A player can appear under several name spellings. Search with `LIKE` and pass all relevant IDs.
- When changing the decoder, re-validate it against `PawnHome` and `WhiteMaterial`/`BlackMaterial` (see doc section 4.4).
