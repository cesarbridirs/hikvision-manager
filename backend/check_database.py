"""Check an existing SQLite database without executing application migrations."""
import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def check_integrity(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise RuntimeError(f"Banco nao encontrado: {path}")
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)) as conn:
            conn.execute('pragma query_only=ON')
            results = [row[0] for row in conn.execute('pragma integrity_check')]
    except sqlite3.Error as exc:
        raise RuntimeError(f"Falha na verificacao SQLite: {exc}. Pare o servico e preserve banco, WAL e chave antes de recuperar.") from exc
    if results != ['ok']:
        raise RuntimeError('Integridade SQLite invalida: ' + '\n'.join(results))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    args = parser.parse_args()
    try:
        check_integrity(args.database)
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(2)
    print('Integridade SQLite: OK. Nenhuma migracao executada.')
