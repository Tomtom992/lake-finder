#!/usr/bin/env python3
"""
compare_runs.py -- zwei Ergebnisläufe gegenüberstellen.

Gedacht für die Abnahme nach einer Änderung an Filtern oder Scores: was hat
sich zwischen dem alten und dem neuen Lauf bewegt, und bei welchen Seen?

Aufruf:
    python tools/compare_runs.py
    python tools/compare_runs.py --old output/results_OLD.csv --new output/results.csv
    python tools/compare_runs.py --lakes Zermitten Gerlin Stiegsee

Ohne Argumente vergleicht das Skript ``output/results_OLD.csv`` mit
``output/results.csv`` und zeigt die in der Stechlin-Abnahme genannten Seen.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_LAKES = ["Zermitten", "Gerlin", "Stiegsee", "Wotzen", "Breutzen"]
DETAIL_COLUMNS = [
    "name", "wilderness_score", "visual_seclusion_score", "passes_filters",
    "filter_status", "developed_shore_percent", "shore_segment_p10_score",
    "distance_nearest_building_m", "building_count_100m", "building_count_500m",
    "data_confidence_score",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Zwei lake_finder-Läufe vergleichen")
    ap.add_argument("--old", default=str(ROOT / "output" / "results_OLD.csv"))
    ap.add_argument("--new", default=str(ROOT / "output" / "results.csv"))
    ap.add_argument("--lakes", nargs="*", default=DEFAULT_LAKES,
                    help="Namensteile der Seen, die einzeln gezeigt werden")
    args = ap.parse_args(argv)

    try:
        import pandas as pd
    except ImportError:
        print("pandas fehlt -- bitte 'pip install -r requirements.txt'.")
        return 1

    new_p = Path(args.new)
    if not new_p.exists():
        print(f"Neuer Lauf nicht gefunden: {new_p}\n"
              f"Erst 'python main.py --mode live --preset stechlin' ausführen.")
        return 1
    new = pd.read_csv(new_p)

    print("=" * 74)
    print(f"  Neuer Lauf: {new_p}  ({len(new)} Seen)")
    print("=" * 74)
    if "filter_status" in new.columns:
        print("Filterstatus:")
        for status, n in new["filter_status"].value_counts().items():
            print(f"   {status:<10} {n}")
    if "passes_filters" in new.columns:
        print(f"   bestanden  {int(new['passes_filters'].sum())}")

    old_p = Path(args.old)
    if old_p.exists():
        old = pd.read_csv(old_p)
        print(f"\nVorheriger Lauf: {old_p}  ({len(old)} Seen)")
        print(f"   Seen      {len(old)} -> {len(new)}")
        if "passes_filters" in old.columns:
            print(f"   bestanden {int(old['passes_filters'].sum())} -> "
                  f"{int(new['passes_filters'].sum())}")
        if "osm_id" in old.columns and "osm_id" in new.columns:
            merged = old.merge(new, on="osm_id", suffixes=("_old", "_new"))
            if {"passes_filters_old", "passes_filters_new"} <= set(merged.columns):
                gone = merged[merged.passes_filters_old & ~merged.passes_filters_new]
                added = merged[~merged.passes_filters_old & merged.passes_filters_new]
                print(f"\n   nicht mehr bestanden: {len(gone)}")
                for _, r in gone.head(15).iterrows():
                    # Spalten, die nur im neuen Lauf existieren, bekommen beim
                    # Merge KEIN Suffix -- beide Schreibweisen probieren.
                    reason = str(r.get("filter_reasons_new")
                                 or r.get("filter_reasons") or "")[:70]
                    print(f"      {str(r.get('name_new') or '(ohne Namen)')[:32]:<32} {reason}")
                print(f"   neu bestanden: {len(added)}")
                for _, r in added.head(15).iterrows():
                    print(f"      {str(r.get('name_new') or '(ohne Namen)')[:32]}")
    else:
        print(f"\n(Kein Vorher-Lauf unter {old_p} -- nur der neue Stand wird gezeigt.)")

    cols = [c for c in DETAIL_COLUMNS if c in new.columns]
    for needle in args.lakes:
        sub = new[new["name"].fillna("").str.contains(needle, case=False, na=False)]
        if sub.empty:
            print(f"\n--- {needle}: nicht in den Ergebnissen ---")
            continue
        print(f"\n--- {needle} ---")
        print(sub[cols].to_string(index=False))
        if "filter_reasons" in sub.columns:
            for _, r in sub.iterrows():
                if isinstance(r["filter_reasons"], str) and r["filter_reasons"].strip():
                    print(f"    Gründe: {r['filter_reasons']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
