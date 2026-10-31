from __future__ import annotations

import csv
import io
import time
import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg

EXPECTED_COMMIT = "01db0a59a6d0f3e82fa14ad36f4046df48819fd7"
EXPECTED_FILE_COUNT = 22
EXPECTED_TOTAL_BYTES = 16_522_806
EXPECTED_TOTAL_ROWS = 724_645

PILOT_FILE = "INSEE_prenoms_2025_national_partie_03_lot_006.csv"
PILOT_FILE_BYTES = 213
PILOT_ROWS = 8

HEADERS = ("sexe", "prenom", "periode", "valeur", "rang")
INTEGER_MAX = 2_147_483_647

class ImportAbort(Exception):
    """Arrêt de sécurité avec un message sans donnée sensible."""

def abort(message: str) -> None:
    raise ImportAbort(message)

def resolve_source_directory() -> Path:
    raw_path = os.environ.get("INSEE_SOURCE_DIR", "insee-lots")
    source_dir = Path(raw_path)

    if not source_dir.is_absolute():
        source_dir = Path.cwd() / source_dir

    source_dir = source_dir.resolve()

    if not source_dir.is_dir():
        abort("Le dossier source INSEE est absent.")

    repository_dir = source_dir.parent

    try:
        result = subprocess.run(
            ["git", "-C", str(repository_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        abort("Impossible de vérifier le commit source INSEE.")

    actual_commit = result.stdout.strip()
    if actual_commit != EXPECTED_COMMIT:
        abort("Le commit source INSEE ne correspond pas au commit autorisé.")

    return source_dir

def validate_inventory(source_dir: Path) -> tuple[list[Path], int]:
    files = sorted(
        path for path in source_dir.glob("*.csv")
        if path.is_file()
    )

    if len(files) != EXPECTED_FILE_COUNT:
        abort(
            f"Nombre de CSV inattendu : {len(files)} "
            f"(attendu : {EXPECTED_FILE_COUNT})."
        )

    total_bytes = sum(path.stat().st_size for path in files)

    if total_bytes != EXPECTED_TOTAL_BYTES:
        abort(
            f"Taille totale inattendue : {total_bytes} octets "
            f"(attendu : {EXPECTED_TOTAL_BYTES})."
        )

    pilot_path = source_dir / PILOT_FILE
    if not pilot_path.is_file():
        abort("Le CSV du pilote initial est absent de la source.")

    pilot_bytes = pilot_path.stat().st_size
    if pilot_bytes != PILOT_FILE_BYTES:
        abort(
            f"Taille inattendue pour le CSV du pilote : {pilot_bytes} octets "
            f"(attendu : {PILOT_FILE_BYTES})."
        )

    return files, total_bytes

def parse_integer(
    raw: str,
    *,
    label: str,
    minimum: int,
    maximum: int,
    file_name: str,
    line_number: int,
) -> int:
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)", raw):
        abort(
            f"Valeur numérique absente ou invalide dans {file_name}, "
            f"ligne {line_number}, champ {label}."
        )

    value = int(raw)

    if value < minimum or value > maximum:
        abort(
            f"Valeur hors limites dans {file_name}, "
            f"ligne {line_number}, champ {label}."
        )

    return value

def parse_csv_into_copy(
    path: Path,
    append_row,
    seen_keys: set[tuple[int, str, int]],
    pilot_rows: list[tuple[int, str, int, int, int]],
) -> int:
    count = 0

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.reader(csv_file, delimiter=";", strict=True)

            try:
                header = next(reader)
            except StopIteration:
                abort(f"Fichier CSV vide : {path.name}.")

            if tuple(header) != HEADERS:
                abort(
                    f"En-tête inattendu dans {path.name} : "
                    "les cinq colonnes attendues sont sexe, prenom, periode, "
                    "valeur, rang."
                )

            for record in reader:
                line_number = reader.line_num

                if len(record) != len(HEADERS):
                    abort(
                        f"Nombre de champs invalide dans {path.name}, "
                        f"ligne {line_number}."
                    )

                sexe_text, prenom, periode_text, valeur_text, rang_text = record

                if not prenom or prenom != prenom.strip() or "\x00" in prenom:
                    abort(
                        f"Prénom vide ou mal formé dans {path.name}, "
                        f"ligne {line_number}."
                    )

                sexe = parse_integer(
                    sexe_text,
                    label="sexe",
                    minimum=1,
                    maximum=2,
                    file_name=path.name,
                    line_number=line_number,
                )
                periode = parse_integer(
                    periode_text,
                    label="periode",
                    minimum=1900,
                    maximum=2025,
                    file_name=path.name,
                    line_number=line_number,
                )
                valeur = parse_integer(
                    valeur_text,
                    label="valeur",
                    minimum=0,
                    maximum=INTEGER_MAX,
                    file_name=path.name,
                    line_number=line_number,
                )
                rang = parse_integer(
                    rang_text,
                    label="rang",
                    minimum=1,
                    maximum=INTEGER_MAX,
                    file_name=path.name,
                    line_number=line_number,
                )

                if sexe not in (1, 2):
                    abort(
                        f"Code sexe inattendu dans {path.name}, "
                        f"ligne {line_number}."
                    )

                key = (sexe, prenom, periode)
                if key in seen_keys:
                    abort(
                        f"Doublon de clé détecté dans {path.name}, "
                        f"ligne {line_number}."
                    )
                seen_keys.add(key)

                row = (sexe, prenom, periode, valeur, rang)
                append_row(row)

                if path.name == PILOT_FILE:
                    pilot_rows.append(row)

                count += 1

    except ImportAbort:
        raise
    except (OSError, UnicodeError, csv.Error):
        abort(f"Lecture CSV impossible ou fichier mal formé : {path.name}.")

    return count

def verify_schema_and_protections(cur) -> None:
    cur.execute(
        """
        SELECT
            a.attname,
            format_type(a.atttypid, a.atttypmod),
            a.attnotnull
        FROM pg_attribute AS a
        JOIN pg_class AS c
          ON c.oid = a.attrelid
        JOIN pg_namespace AS n
          ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'insee_observations'
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY a.attnum
        """
    )
    columns = cur.fetchall()

    expected_columns = [
        ("sexe", "smallint", True),
        ("prenom", "text", True),
        ("periode", "smallint", True),
        ("valeur", "integer", True),
        ("rang", "integer", True),
    ]

    if columns != expected_columns:
        abort("Le schéma de public.insee_observations a changé.")

    cur.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
          ON kcu.constraint_catalog = tc.constraint_catalog
         AND kcu.constraint_schema = tc.constraint_schema
         AND kcu.constraint_name = tc.constraint_name
         AND kcu.table_schema = tc.table_schema
         AND kcu.table_name = tc.table_name
        WHERE tc.table_schema = 'public'
          AND tc.table_name = 'insee_observations'
          AND tc.constraint_type = 'PRIMARY KEY'
        ORDER BY kcu.ordinal_position
        """
    )
    primary_key = [row[0] for row in cur.fetchall()]

    if primary_key != ["sexe", "prenom", "periode"]:
        abort("La clé primaire de public.insee_observations a changé.")

    cur.execute(
        """
        SELECT pg_get_constraintdef(oid), convalidated
        FROM pg_constraint
        WHERE conrelid = 'public.insee_observations'::regclass
          AND contype = 'c'
        """
    )
    check_constraints = cur.fetchall()
    actual_checks = {
        re.sub(r"\s+", "", definition).lower()
        for definition, _ in check_constraints
    }
    expected_checks = {
        "check(((periode>=1900)and(periode<=2025)))",
        "check((length(btrim(prenom))>0))",
        "check((rang>=1))",
        "check((sexe=any(array[1,2])))",
        "check(((valeur>=0)and((valeur%5)=0)))",
    }

    if (
        len(check_constraints) != len(expected_checks)
        or actual_checks != expected_checks
        or any(not validated for _, validated in check_constraints)
    ):
        abort(
            "Les contraintes CHECK de public.insee_observations "
            "ont changé ou ne sont pas validées."
        )

    cur.execute(
        """
        SELECT c.relrowsecurity, c.relforcerowsecurity
        FROM pg_class AS c
        JOIN pg_namespace AS n
          ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relname = 'insee_observations'
        """
    )
    rls_state = cur.fetchone()

    if rls_state != (True, True):
        abort("RLS ou FORCE RLS n'est plus activé sur la table publique.")

    cur.execute(
        """
        SELECT policyname, cmd, roles, qual, with_check
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'insee_observations'
        """
    )
    policies = cur.fetchall()

    if len(policies) != 1:
        abort("Les politiques d'accès de la table publique ont changé.")

    _, command, roles, qualification, check_expression = policies[0]
    normalized_qualification = re.sub(
        r"[\s()]",
        "",
        qualification or "",
    ).lower()

    if (
        command != "SELECT"
        or set(roles or []) != {"anon", "authenticated"}
        or normalized_qualification != "false"
        or check_expression is not None
    ):
        abort("La politique publique de lecture n'est plus verrouillée.")

    cur.execute(
        """
        SELECT
            has_table_privilege(
                'anon', 'public.insee_observations', 'SELECT'
            ),
            has_table_privilege(
                'authenticated', 'public.insee_observations', 'SELECT'
            ),
            has_table_privilege(
                'anon', 'public.insee_observations', 'INSERT'
            ),
            has_table_privilege(
                'anon', 'public.insee_observations', 'UPDATE'
            ),
            has_table_privilege(
                'anon', 'public.insee_observations', 'DELETE'
            ),
            has_table_privilege(
                'authenticated', 'public.insee_observations', 'INSERT'
            ),
            has_table_privilege(
                'authenticated', 'public.insee_observations', 'UPDATE'
            ),
            has_table_privilege(
                'authenticated', 'public.insee_observations', 'DELETE'
            )
        """
    )
    privileges = cur.fetchone()

    if privileges != (True, True, False, False, False, False, False, False):
        abort("Les droits de lecture ou d'écriture publics ont changé.")

    cur.execute(
        """
        SELECT
            has_schema_privilege('anon', 'insee_private', 'USAGE'),
            has_schema_privilege('authenticated', 'insee_private', 'USAGE')
        """
    )
    private_schema_access = cur.fetchone()

    if private_schema_access != (False, False):
        abort("Le schéma privé n'est plus inaccessible aux utilisateurs publics.")

    cur.execute(
        """
        SELECT
            role.rolsuper,
            role.rolbypassrls,
            relation.relowner = role.oid,
            has_table_privilege(
                current_user, 'public.insee_observations', 'INSERT'
            ),
            has_table_privilege(
                current_user, 'public.insee_observations', 'SELECT'
            )
        FROM pg_roles AS role
        JOIN pg_class AS relation
          ON relation.oid = 'public.insee_observations'::regclass
        WHERE role.rolname = current_user
        """
    )
    db_role = cur.fetchone()

    if db_role is None:
        abort("Impossible de vérifier les droits du rôle de connexion.")

    is_superuser, bypass_rls, is_table_owner, can_insert, can_select = db_role
    can_bypass_forced_rls = bool(is_superuser) or (
        bool(is_table_owner) and bool(bypass_rls)
    )

    if not can_bypass_forced_rls or not can_insert or not can_select:
        abort(
            "La connexion n'a pas les droits administratifs nécessaires "
            "pour importer sans changer les politiques RLS."
        )

def rows_match(left_alias: str, right_alias: str) -> str:
    return (
        f"{left_alias}.sexe = {right_alias}.sexe AND "
        f"{left_alias}.prenom = {right_alias}.prenom AND "
        f"{left_alias}.periode = {right_alias}.periode AND "
        f"{left_alias}.valeur = {right_alias}.valeur AND "
        f"{left_alias}.rang = {right_alias}.rang"
    )

def run_import() -> tuple[int, int, int]:
    dsn = os.environ.get("SUPABASE_DATABASE_URL", "")
    if not dsn.startswith(("postgresql://", "postgres://")):
        abort(
            "Le secret SUPABASE_DATABASE_URL manque ou n'est pas "
            "une URL PostgreSQL."
        )

    source_dir = resolve_source_directory()
    files, total_bytes = validate_inventory(source_dir)

    seen_keys: set[tuple[int, str, int]] = set()
    pilot_rows: list[tuple[int, str, int, int, int]] = []
    total_rows = 0

    with psycopg.connect(
        dsn,
        sslmode="require",
        connect_timeout=15,
    ) as connection:
        with connection.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '25min'")
            cur.execute("SET LOCAL lock_timeout = '30s'")

            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
                ("le-prenom-ideal:insee-full-import",),
            )

            verify_schema_and_protections(cur)

            cur.execute(
                "LOCK TABLE public.insee_observations "
                "IN SHARE ROW EXCLUSIVE MODE"
            )

            cur.execute(
                """
                CREATE TEMP TABLE insee_stage (
                    sexe smallint NOT NULL,
                    prenom text NOT NULL,
                    periode smallint NOT NULL,
                    valeur integer NOT NULL,
                    rang integer NOT NULL
                ) ON COMMIT DROP
                """
            )

            cur.execute(
                """
                CREATE TEMP TABLE insee_pilot_stage (
                    sexe smallint NOT NULL,
                    prenom text NOT NULL,
                    periode smallint NOT NULL,
                    valeur integer NOT NULL,
                    rang integer NOT NULL
                ) ON COMMIT DROP
                """
            )

            # COPY CSV par blocs: validation des lignes inchangée, mais transferts
            # réseau groupés plutôt qu'un appel write_row pour chaque observation.
            print("ÉTAPE 1/5 — Chargement CSV dans la table temporaire", flush=True)
            started = time.monotonic()
            with cur.copy(
                """
                COPY insee_stage
                    (sexe, prenom, periode, valeur, rang)
                FROM STDIN WITH (FORMAT CSV, DELIMITER ';', NULL '\\N')
                """
            ) as copy:
                buffer = io.StringIO()
                writer = csv.writer(buffer, delimiter=";", lineterminator="\n")

                def append_row(row):
                    writer.writerow(row)
                    if buffer.tell() >= 128 * 1024:
                        copy.write(buffer.getvalue())
                        buffer.seek(0)
                        buffer.truncate(0)

                for path in files:
                    total_rows += parse_csv_into_copy(
                        path,
                        append_row,
                        seen_keys,
                        pilot_rows,
                    )

                if buffer.tell():
                    copy.write(buffer.getvalue())
            print(f"ÉTAPE 1/5 terminée — {total_rows} lignes, "
                  f"{time.monotonic() - started:.1f} s", flush=True)

            print("ÉTAPE 2/5 — Index et statistiques temporaires", flush=True)
            started = time.monotonic()
            # Index temporaire pour les comparaisons exactes dans les deux sens.
            # Sans cet index, NOT EXISTS peut rescanner les 724 645 lignes
            # pour chaque observation de la table finale.
            cur.execute(
                """
                CREATE INDEX insee_stage_match_idx
                ON insee_stage (sexe, prenom, periode, valeur, rang)
                """
            )
            cur.execute("ANALYZE insee_stage")
            print(f"ÉTAPE 2/5 terminée — {time.monotonic() - started:.1f} s", flush=True)

            if total_rows != EXPECTED_TOTAL_ROWS:
                abort(
                    f"Nombre total d'observations inattendu : {total_rows} "
                    f"(attendu : {EXPECTED_TOTAL_ROWS})."
                )

            if len(pilot_rows) != PILOT_ROWS:
                abort(
                    f"Le CSV pilote contient {len(pilot_rows)} observations "
                    f"(attendu : {PILOT_ROWS})."
                )

            cur.executemany(
                """
                INSERT INTO insee_pilot_stage
                    (sexe, prenom, periode, valeur, rang)
                VALUES (%s, %s, %s, %s, %s)
                """,
                pilot_rows,
            )

            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM insee_stage
                    GROUP BY sexe, prenom, periode
                    HAVING COUNT(*) > 1
                )
                """
            )
            if cur.fetchone()[0]:
                abort("Un doublon de clé a été détecté dans la table temporaire.")

            cur.execute("SELECT COUNT(*) FROM public.insee_observations")
            existing_count = cur.fetchone()[0]

            if existing_count < PILOT_ROWS:
                abort("Une ou plusieurs lignes du pilote initial sont absentes.")

            print("CONTRÔLE — Correspondance des lignes existantes", flush=True)
            cur.execute(
                f"""
                SELECT EXISTS (
                    SELECT 1
                    FROM public.insee_observations AS current_row
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM insee_stage AS source_row
                        WHERE {rows_match("source_row", "current_row")}
                    )
                )
                """
            )
            if cur.fetchone()[0]:
                abort(
                    "La table contient une ligne absente ou différente "
                    "de la source figée; aucune écriture ne sera conservée."
                )

            cur.execute(
                f"""
                SELECT EXISTS (
                    SELECT 1
                    FROM insee_pilot_stage AS pilot_row
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM public.insee_observations AS current_row
                        WHERE {rows_match("pilot_row", "current_row")}
                    )
                )
                """
            )
            if cur.fetchone()[0]:
                abort(
                    "Les huit observations du pilote ne sont pas toutes "
                    "présentes à l'identique; import annulé."
                )

            print("ÉTAPE 3/5 — Insertion transactionnelle", flush=True)
            started = time.monotonic()
            cur.execute(
                f"""
                INSERT INTO public.insee_observations
                    (sexe, prenom, periode, valeur, rang)
                SELECT sexe, prenom, periode, valeur, rang
                FROM insee_stage
                ON CONFLICT (sexe, prenom, periode) DO NOTHING
                """
            )
            inserted_this_run = cur.rowcount
            print(f"ÉTAPE 3/5 terminée — {inserted_this_run} lignes ajoutées; "
                  f"{time.monotonic() - started:.1f} s", flush=True)

            cur.execute("SELECT COUNT(*) FROM public.insee_observations")
            final_count = cur.fetchone()[0]

            if final_count != EXPECTED_TOTAL_ROWS:
                abort(
                    f"Total après import inattendu : {final_count} "
                    f"(attendu : {EXPECTED_TOTAL_ROWS})."
                )

            print("ÉTAPE 4/5 — Comparaison finale des cinq champs", flush=True)
            started = time.monotonic()
            cur.execute(
                f"""
                SELECT
                    EXISTS (
                        SELECT 1
                        FROM insee_stage AS source_row
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM public.insee_observations AS final_row
                            WHERE {rows_match("source_row", "final_row")}
                        )
                    ),
                    EXISTS (
                        SELECT 1
                        FROM public.insee_observations AS final_row
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM insee_stage AS source_row
                            WHERE {rows_match("source_row", "final_row")}
                        )
                    )
                """
            )
            missing_or_extra = cur.fetchone()

            if missing_or_extra != (False, False):
                abort(
                    "La comparaison finale des cinq champs a échoué; "
                    "transaction annulée."
                )

            print(f"ÉTAPE 4/5 terminée — {time.monotonic() - started:.1f} s", flush=True)
            print("ÉTAPE 5/5 — Vérification des protections", flush=True)
            verify_schema_and_protections(cur)
            print("ÉTAPE 5/5 terminée — validation de la transaction à suivre", flush=True)

    return total_rows, total_bytes, inserted_this_run

def main() -> int:
    try:
        rows, total_bytes, inserted = run_import()
    except ImportAbort as error:
        print(f"ARRÊT SÛR : {error}")
        print("Aucune écriture partielle n'est conservée.")
        return 1
    except psycopg.Error as error:
        sqlstate = getattr(error, "sqlstate", None) or "indisponible"
        print(
            "ARRÊT SÛR : erreur PostgreSQL "
            f"(SQLSTATE {sqlstate}). Détails de connexion masqués."
        )
        print("La transaction a été annulée.")
        return 1
    except Exception as error:
        print(
            "ARRÊT SÛR : erreur inattendue "
            f"({type(error).__name__}). Détails masqués."
        )
        print("La transaction a été annulée.")
        return 1

    print("IMPORT TERMINÉ ET CONTRÔLÉ.")
    print(f"Fichiers CSV contrôlés : {EXPECTED_FILE_COUNT}")
    print(f"Taille source contrôlée : {total_bytes} octets")
    print(f"Observations contrôlées : {rows}")
    print(f"Lignes ajoutées pendant cette exécution : {inserted}")
    print(f"Lignes déjà présentes et conservées : {rows - inserted}")
    print("Clé primaire, schéma, RLS et politiques vérifiés avant/après.")
    print("La table correspond exactement aux cinq champs de la source.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
