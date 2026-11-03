#!/usr/bin/env python3
"""Import SSA US national names into the EXISTING protected Supabase schema.

Requires: psycopg[binary] >= 3.2, SUPABASE_DATABASE_URL, names.zip.
Default: validate and report only. --import performs real inserts.
Run via a protected GitHub Actions environment, never with credentials on a public client.
"""
import argparse
import csv
import hashlib
import io
import os
from pathlib import Path
import sys
import zipfile

EXPECTED_SHA = 'cd78e975ed7bb358e018dd62fbe14ced89295e9581c49172ca4eedcb011b3724'
EXPECTED_ROWS = 2_181_032
YEARS = range(1880, 2026)
# Safety threshold: leave a meaningful buffer below the 500 MiB free-tier quota.
SOFT_LIMIT_BYTES = 425 * 1024 * 1024


def records(z, year):
    with z.open(f'yob{year}.txt') as raw, io.TextIOWrapper(raw, encoding='utf-8-sig', newline='') as f:
        for row in csv.reader(f):
            if len(row) != 3:
                raise ValueError(f'Invalid column count in {year}')
            name, sex, count_str = row
            if not name or len(name) > 100 or '\x00' in name or sex not in ('F', 'M') or not count_str.isascii() or not count_str.isdecimal() or int(count_str) < 5:
                raise ValueError(f'Invalid observation in {year}')
            yield name, sex, int(count_str)


def validate(path):
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > 35_000_000:
        raise ValueError('Unexpected ZIP size')
    sha = hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
    if sha != EXPECTED_SHA:
        raise ValueError('SHA256 mismatch; refusing to import')
    total = 0
    with zipfile.ZipFile(path) as z:
        year_files = {n for n in z.namelist() if n.startswith('yob') and n.endswith('.txt')}
        if year_files != {f'yob{y}.txt' for y in YEARS}:
            raise ValueError('Unexpected or missing annual files')
        for year in YEARS:
            seen = set()
            sexes = set()
            for name, sex, _ in records(z, year):
                if (name, sex) in seen:
                    raise ValueError(f'Duplicate SSA name/sex in year {year}')
                seen.add((name, sex))
                sexes.add(sex)
                total += 1
            if sexes != {'F', 'M'}:
                raise ValueError(f'Incomplete sex coverage in year {year}')
    if total != EXPECTED_ROWS:
        raise ValueError(f'Unexpected total {total}, expected {EXPECTED_ROWS}')
    print(f'Validated {total:,} rows across {len(YEARS)} years; sha256={sha}', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--zip', default='names.zip', type=Path)
    parser.add_argument('--import', dest='do_import', action='store_true', help='Explicit opt-in to write database records')
    args = parser.parse_args()
    validate(args.zip)
    if not args.do_import:
        print('Dry run complete; no database connection or writes.', flush=True)
        return
    db_url = os.environ.get('SUPABASE_DATABASE_URL')
    if not db_url:
        raise RuntimeError('SUPABASE_DATABASE_URL missing')
    import psycopg
    with psycopg.connect(db_url, connect_timeout=25) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.ssa_prenoms')::text, to_regclass('public.ssa_observations')::text")
            if cur.fetchone() != ('ssa_prenoms', 'ssa_observations'):
                raise ValueError('SSA tables missing')
            cur.execute('SELECT count(*) FROM public.insee_observations')
            insee_start = cur.fetchone()[0]
            if insee_start != 724645:
                raise ValueError(f'Unexpected INSEE count {insee_start}; aborting')
        conn.commit()
        with zipfile.ZipFile(args.zip) as z:
            for year in YEARS:
                # One year = one transaction. Completed years remain committed after interruption.
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute('SELECT pg_database_size(current_database())')
                        current_bytes = cur.fetchone()[0]
                        if current_bytes > SOFT_LIMIT_BYTES:
                            raise RuntimeError(f'Database size {current_bytes} exceeds conservative stop threshold')
                        cur.execute('CREATE TEMP TABLE ssa_stage (prenom text NOT NULL, sexe char(1) NOT NULL, naissances integer NOT NULL) ON COMMIT DROP')
                        with cur.copy('COPY ssa_stage (prenom,sexe,naissances) FROM STDIN') as cp:
                            for record in records(z, year):
                                cp.write_row(record)
                        cur.execute('''
                            INSERT INTO public.ssa_prenoms (prenom,sexe)
                            SELECT DISTINCT prenom,sexe FROM ssa_stage WHERE true
                            ON CONFLICT (prenom,sexe) DO NOTHING
                        ''')
                        cur.execute('''
                            INSERT INTO public.ssa_observations (prenom_id,annee,naissances)
                            SELECT p.id, %s, s.naissances
                            FROM ssa_stage AS s
                            JOIN public.ssa_prenoms AS p ON p.prenom=s.prenom AND p.sexe=s.sexe
                            WHERE true
                            ON CONFLICT (prenom_id,annee) DO UPDATE SET naissances=EXCLUDED.naissances
                        ''', (year,))
                        cur.execute('SELECT count(*) FROM ssa_stage')
                        n = cur.fetchone()[0]
                print(f'{year}: committed {n} source rows', flush=True)
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM public.ssa_observations')
            actual = cur.fetchone()[0]
            cur.execute('SELECT COUNT(*) FROM public.insee_observations')
            insee_end = cur.fetchone()[0]
            cur.execute('SELECT pg_database_size(current_database())')
            size_bytes = cur.fetchone()[0]
        print(f'Final SSA: {actual:,}; INSEE: {insee_end:,}; database bytes: {size_bytes}', flush=True)
        if actual != EXPECTED_ROWS or insee_end != insee_start:
            raise RuntimeError('Final verification failed; review counts before publication')

if __name__ == '__main__':
    main()
