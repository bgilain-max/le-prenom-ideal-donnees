"""One-shot guarded ONS import. Does not modify INSEE/SSA tables."""
from __future__ import annotations
import csv
import hashlib
import os
import re
import sys
from pathlib import Path
import psycopg

EXPECTED_SHA256 = '776ff86fd55ee050fb69bea9c63583df7de99cb9cbbf91ffc7107dcada678ff7'
EXPECTED_ROWS = 364184
EXPECTED_2025 = {1: 6583, 2: 7617}
EXPECTED_LEGACY = (724645, 2181032)
HEADER = ['sexe', 'prenom', 'periode', 'valeur', 'rang']


def guard(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def validate_file(path):
    guard(path.is_file(), 'ONS CSV file missing')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    guard(digest == EXPECTED_SHA256, 'ONS SHA256 mismatch')
    counter = 0
    by_2025 = {1: 0, 2: 0}
    keys = set()
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f, strict=True)
        guard(next(reader, None) == HEADER, 'ONS header mismatch')
        for row in reader:
            guard(len(row) == 5, f'Wrong column count at record {counter+1}')
            s, name, yr, value, rank = row
            guard(re.fullmatch(r'[12]', s) is not None, f'Invalid sex at row {counter+1}')
            guard(bool(name.strip()) and name == name.strip() and '\x00' not in name,
                  f'Invalid name at row {counter+1}')
            guard(all(re.fullmatch(r'[0-9]+', v) for v in (yr, value, rank)),
                  f'Invalid number at row {counter+1}')
            sex, year, count, ranking = int(s), int(yr), int(value), int(rank)
            guard(1996 <= year <= 2025 and 3 <= count <= 2147483647
                  and 1 <= ranking <= 2147483647, f'Out of bounds at row {counter+1}')
            key = (sex, name, year)
            guard(key not in keys, f'Duplicate key at row {counter+1}')
            keys.add(key)
            if year == 2025:
                by_2025[sex] += 1
            counter += 1
    guard(counter == EXPECTED_ROWS, 'ONS record count mismatch')
    guard(by_2025 == EXPECTED_2025, 'ONS 2025 breakdown mismatch')
    print(f'CSV validation OK: {counter} rows; sha256={digest}', flush=True)


def import_csv(path):
    dsn = os.environ.get('SUPABASE_DATABASE_URL', '')
    guard(dsn.startswith(('postgresql://', 'postgres://')), 'Missing database URL secret')
    with psycopg.connect(dsn, sslmode='require', connect_timeout=15) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '25min'")
                cur.execute("SET LOCAL lock_timeout = '20s'")
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('le-prenom-ideal:ons-import')::bigint)")
                cur.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'public.ons_observations'::regclass")
                guard(cur.fetchone() == (True, True), 'ONS RLS not enforced')
                cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname='public' AND tablename='ons_observations'")
                guard(cur.fetchone()[0] == 0, 'ONS policies changed')
                cur.execute("SELECT has_table_privilege('anon','public.ons_observations','SELECT'), has_table_privilege('authenticated','public.ons_observations','SELECT'), has_table_privilege('anon','public.ons_observations','INSERT'), has_table_privilege('authenticated','public.ons_observations','INSERT')")
                guard(cur.fetchone() == (False, False, False, False), 'ONS public rights changed')
                cur.execute("SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull FROM pg_attribute a WHERE a.attrelid='public.ons_observations'::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum")
                guard(cur.fetchall() == [("sexe","smallint",True),("prenom","text",True),("periode","smallint",True),("valeur","integer",True),("rang","integer",True)], 'ONS schema changed')
                cur.execute('SELECT count(*) FROM public.insee_observations')
                insee = cur.fetchone()[0]
                cur.execute('SELECT count(*) FROM public.ssa_observations')
                ssa = cur.fetchone()[0]
                guard((insee, ssa) == EXPECTED_LEGACY, 'Legacy counts mismatch before import')
                cur.execute('LOCK TABLE public.ons_observations IN SHARE ROW EXCLUSIVE MODE')
                cur.execute('SELECT count(*) FROM public.ons_observations')
                guard(cur.fetchone()[0] == 0, 'ONS target not empty: refusing overwrite')
                cur.execute('CREATE TEMP TABLE ons_stage (LIKE public.ons_observations INCLUDING CONSTRAINTS) ON COMMIT DROP')
                with path.open('r', encoding='utf-8-sig', newline='') as file, cur.copy('COPY ons_stage (sexe,prenom,periode,valeur,rang) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)') as copy:
                    while chunk := file.read(256 * 1024):
                        copy.write(chunk)
                cur.execute('SELECT count(*), count(*) FILTER (WHERE periode=2025 AND sexe=1), count(*) FILTER (WHERE periode=2025 AND sexe=2) FROM ons_stage')
                guard(cur.fetchone() == (EXPECTED_ROWS, 6583, 7617), 'Stage verification failed')
                cur.execute('SELECT count(*) FROM (SELECT sexe,prenom,periode FROM ons_stage GROUP BY 1,2,3 HAVING count(*)>1) d')
                guard(cur.fetchone()[0] == 0, 'Duplicate in stage')
                cur.execute('INSERT INTO public.ons_observations (sexe,prenom,periode,valeur,rang) SELECT sexe,prenom,periode,valeur,rang FROM ons_stage')
                cur.execute('SELECT count(*) FROM public.ons_observations')
                guard(cur.fetchone()[0] == EXPECTED_ROWS, 'ONS final count mismatch')
                cur.execute('SELECT (SELECT count(*) FROM public.insee_observations),(SELECT count(*) FROM public.ssa_observations)')
                guard(cur.fetchone() == EXPECTED_LEGACY, 'Legacy counts changed')
                print('ONS import verified, committing transaction.', flush=True)
    print('SUCCESS: 364184 observations imported; INSEE and SSA counts unchanged.', flush=True)


if __name__ == '__main__':
    try:
        source = Path(os.getenv('ONS_CSV_PATH', 'ons-data/ONS_1996_2025_normalise_VALIDE.csv'))
        validate_file(source)
        import_csv(source)
    except Exception as exc:
        print(f'IMPORT STOPPED: {exc.__class__.__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
