"""Export a player's games from Caissabase to a PGN file.

Examples:
    python export_pgn.py --search "Carlsen%"
    python export_pgn.py "Carlsen, Magnus" -o carlsen.pgn
    python export_pgn.py "Carlsen, Magnus" --color white --from 2020.01.01 -o carlsen_white.pgn
    python export_pgn.py --id 12345 --id 67890 -o player.pgn
    python export_pgn.py "Carlsen, Magnus" --min-moves 20 -o carlsen_20plus.pgn

    # several spellings of the same player, reported under the first name
    python export_pgn.py --name "Carlsen, Magnus" --name "Carlsen, M" --merge -o carlsen.pgn
    # ... or under a name of your choice
    python export_pgn.py --name "Carlsen, M" --id 73583 --merge-as "Carlsen, Magnus" -o carlsen.pgn
"""

import argparse
import sys

import caissabase as cb


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", metavar="NAME", help='exact player name as stored, e.g. "Carlsen, Magnus"')
    ap.add_argument("--name", action="append", default=[], help="exact player name (repeatable)")
    ap.add_argument("--id", type=int, action="append", default=[], help="player ID (repeatable)")
    ap.add_argument("--merge", action="store_true",
                    help="report all selected identities under the first given name/ID")
    ap.add_argument("--merge-as", metavar="NAME",
                    help="report all selected identities under NAME (implies --merge)")
    ap.add_argument("--search", metavar="PATTERN", help='list players matching a LIKE pattern, e.g. "Carlsen%%"')
    ap.add_argument("--db", default=cb.DEFAULT_DB, help="path to the .db3 file")
    ap.add_argument("-o", "--output", help="output PGN file (default: stdout)")
    ap.add_argument("--color", choices=["white", "black"], help="only games with this color")
    ap.add_argument("--from", dest="date_from", help="earliest date, YYYY.MM.DD")
    ap.add_argument("--to", dest="date_to", help="latest date, YYYY.MM.DD")
    ap.add_argument("--min-moves", type=int, metavar="N",
                    help="skip games shorter than N full moves (1. e4 e5 = 1 move)")
    args = ap.parse_args()

    con = cb.connect(args.db)

    if args.search:
        for p in cb.find_players(con, args.search):
            print(f"{p.id:>8}  {p.games:>6} games  {p.name}")
        return 0

    names = args.names + args.name
    players, missing = cb.resolve_players(con, names)
    if missing:
        for name in missing:
            print(f"Player not found: {name!r}. Try --search.", file=sys.stderr)
        return 1
    for pid in args.id:
        row = con.execute("SELECT Name FROM Players WHERE ID = ?", (pid,)).fetchone()
        if row is None:
            print(f"Player ID not found: {pid}", file=sys.stderr)
            return 1
        players.append(cb.Player(pid, row["Name"]))
    if not players:
        ap.error("give a player name, --name, --id or --search")

    ids = list(dict.fromkeys(p.id for p in players))
    rename = None
    if args.merge or args.merge_as:
        canonical = args.merge_as or players[0].name
        rename = cb.merge_identities(ids, canonical)
        print(f"merging {len(ids)} identities as {canonical!r}: "
              + ", ".join(repr(p.name) for p in players), file=sys.stderr)

    out = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout
    count = errors = 0
    try:
        for row in cb.iter_player_games(con, ids, args.color, args.date_from, args.date_to, args.min_moves):
            try:
                pgn = cb.row_to_pgn(row, rename)
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
