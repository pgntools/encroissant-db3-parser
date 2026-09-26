## Caissabase 2024 db3 parser

Export player games from the En Croissant `caissabase_2024.db3` SQLite database to PGN.

```bash
pip install -r requirements.txt
python export_pgn.py --search "Carlsen%"
python export_pgn.py "Carlsen, Magnus" -o carlsen.pgn
```

See [docs/CAISSABASE_DB.md](docs/CAISSABASE_DB.md) for the database schema, move encoding and extraction examples.
