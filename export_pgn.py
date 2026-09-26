"""Export a player's games from Caissabase to a PGN file.

Examples:
    python export_pgn.py --search "Carlsen%"
    python export_pgn.py "Carlsen, Magnus" -o carlsen.pgn
    python export_pgn.py "Carlsen, Magnus" --color white --from 2020.01.01 -o carlsen_white.pgn
    python export_pgn.py --id 12345 --id 67890 -o player.pgn
"""

import argparse
import sys

import caissabase as cb


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?", help='exact player name as stored, e.g. "Carlsen, Magnus"')
    ap.add_argument("--id", type=int, action="append", default=[], help="player ID (repeatable)")
    ap.add_argument("--search", metavar="PATTERN", help='list players matching a LIKE pattern, e.g. "Carlsen%%"')
    ap.add_argument("--db", default=cb.DEFAULT_DB, help="path to the .db3 file")
    ap.add_argument("-o", "--output", help="output PGN file (default: stdout)")
    ap.add_argument("--color", choices=["white", "black"], help="only games with this color")
    ap.add_argument("--from", dest="date_from", help="earliest date, YYYY.MM.DD")
    ap.add_argument("--to", dest="date_to", help="latest date, YYYY.MM.DD")
    args = ap.parse_args()

    con = cb.connect(args.db)

    if args.search:
        for p in cb.find_players(con, args.search):
            print(f"{p.id:>8}  {p.games:>6} games  {p.name}")
        return 0

    ids = list(args.id)
    if args.name:
        row = con.execute("SELECT ID FROM Players WHERE Name = ?", (args.name,)).fetchone()
        if row is None:
            print(f"Player not found: {args.name!r}. Try --search.", file=sys.stderr)
            return 1
        ids.append(row["ID"])
    if not ids:
        ap.error("give a player name, --id or --search")

    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    count = errors = 0
    try:
        for row in cb.iter_player_games(con, ids, args.color, args.date_from, args.date_to):
            try:
                pgn = cb.row_to_pgn(row)
            except ValueError as e:
                errors += 1
                print(f"skipping game {row['ID']}: {e}", file=sys.stderr)
                continue
            out.write(pgn + "\n\n")
            count += 1
    finally:
        if args.output:
            out.close()
    print(f"exported {count} games" + (f", skipped {errors}" if errors else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
