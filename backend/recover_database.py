"""Rebuild a COPY of the database, excluding only monitor_results data.

No application startup, network access, decryption or production replacement.
"""
import argparse
import ast
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from check_database import check_integrity


TABLES = (
    'locations',
    'nvrs', 'channels', 'monitor_results', 'camera_ip_changes',
    'channel_status_changes', 'hdds', 'app_settings',
    'alert_acknowledgements', 'scheduler_runs', 'app_users', 'app_sessions',
    'app_user_cities',
)


def schema_definition():
    # Parse trusted application source without importing it or running init_db.
    tree = ast.parse(Path(__file__).with_name('app.py').read_text(encoding='utf-8-sig'))
    schema = None
    additions = None
    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and function.name == 'init_db':
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'executescript':
                    schema = ast.literal_eval(node.args[0])
        if isinstance(function, ast.FunctionDef) and function.name == 'ensure_columns':
            for node in function.body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'expected' for t in node.targets):
                    additions = ast.literal_eval(node.value)
    if not isinstance(schema, str) or not isinstance(additions, dict):
        raise RuntimeError('Nao foi possivel ler o esquema do app.py. Nenhuma recuperacao executada.')
    return schema, additions


def identifier(name):
    return '"' + name.replace('"', '""') + '"'


def recover(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise RuntimeError('Banco de origem nao encontrado.')
    if destination == source:
        raise RuntimeError('Origem e destino devem ser diferentes.')
    production = Path(__file__).resolve().parents[1] / 'data' / 'hikvision.db'
    if source == production.resolve() or destination == production.resolve():
        raise RuntimeError('Use uma COPIA fora de data/hikvision.db; o banco de producao nao sera aberto.')
    partial = destination.with_name(destination.name + '.partial')
    report_path = destination.with_name(destination.name + '.report.json')
    if any(path.exists() for path in (destination, partial, report_path)):
        raise RuntimeError('Destino, arquivo parcial ou relatorio ja existe. Escolha um novo nome; nada sera sobrescrito.')
    if not destination.parent.is_dir():
        raise RuntimeError('Crie primeiro a pasta de destino.')
    schema, additions = schema_definition()
    report = {'status': 'incomplete', 'excluded_data': ['monitor_results'], 'copied_rows': {}}
    # Reserve exclusive outputs so a failed run cannot overwrite a previous attempt.
    with report_path.open('x', encoding='utf-8') as report_file:
        try:
            with partial.open('xb'):
                pass
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10)) as src, closing(sqlite3.connect(partial)) as dst:
                src.execute('pragma query_only=ON')
                src.execute('begin')
                dst.execute('pragma journal_mode=DELETE')
                dst.executescript(schema)
                for table, columns in additions.items():
                    present = {row[1] for row in dst.execute(f'pragma table_info({identifier(table)})')}
                    for column, definition in columns.items():
                        if column not in present:
                            dst.execute(f'alter table {identifier(table)} add column {identifier(column)} {definition}')
                source_tables = {row[0] for row in src.execute("select name from sqlite_schema where type='table' and name not like 'sqlite_%'")}
                if source_tables - set(TABLES) or set(TABLES) - source_tables - {'locations', 'app_user_cities'}:
                    raise RuntimeError('Tabelas da origem diferem do esquema esperado. Recuperacao interrompida sem descartar tabelas adicionais.')
                with dst:
                    for table in TABLES:
                        if table in ('locations', 'app_user_cities') and table not in source_tables:
                            report['copied_rows'][table] = 0
                            continue
                        if table == 'monitor_results':
                            report['copied_rows'][table] = 0
                            continue
                        target_info = {row[1]: row for row in dst.execute(f'pragma table_info({identifier(table)})')}
                        columns = [row[1] for row in src.execute(f'pragma table_info({identifier(table)})')]
                        if not columns or any(column not in target_info for column in columns):
                            raise RuntimeError(f'Colunas inesperadas na tabela {table}; nenhuma coluna sera descartada.')
                        names = ','.join(map(identifier, columns))
                        sql = f'insert into {identifier(table)} ({names}) values ({",".join("?" for _ in columns)})'
                        count = 0
                        # Full table scan avoids reliance on potentially damaged indexes.
                        for row in src.execute(f'select {names} from {identifier(table)} not indexed'):
                            for column, value in zip(columns, row):
                                kind = target_info[column][2].upper()
                                if value is not None and ((kind == 'TEXT' and not isinstance(value, str)) or (kind == 'INTEGER' and not isinstance(value, int))):
                                    raise RuntimeError(f'Tipo invalido em {table}.{column}. Recuperacao interrompida.')
                            dst.execute(sql, row)
                            count += 1
                        report['copied_rows'][table] = count
                        if dst.execute(f'select count(*) from {identifier(table)}').fetchone()[0] != count:
                            raise RuntimeError(f'Contagem divergente em {table}.')
                    # Retain AUTOINCREMENT high-water marks, including removed records.
                    for table, sequence in src.execute('select name,seq from sqlite_sequence'):
                        if table in TABLES:
                            if not isinstance(sequence, int) or sequence < 0:
                                raise RuntimeError('Sequencia invalida na origem.')
                            current = dst.execute('select seq from sqlite_sequence where name=?', (table,)).fetchone()
                            if current:
                                dst.execute('update sqlite_sequence set seq=? where name=?', (max(sequence, current[0]), table))
                            else:
                                dst.execute('insert into sqlite_sequence(name,seq) values(?,?)', (table, sequence))
                    violations = dst.execute('pragma foreign_key_check').fetchall()
                    if violations:
                        raise RuntimeError(f'{len(violations)} referencia(s) invalida(s) entre tabelas. Requer analise; nenhuma linha sera descartada automaticamente.')
                    report['encrypted_nvrs'] = dst.execute('select count(*) from nvrs where password_encrypted=1').fetchone()[0]
                    if dst.execute('select count(*) from nvrs where password_encrypted not in (0,1)').fetchone()[0]:
                        raise RuntimeError('Indicador de criptografia invalido.')
                    # Let the normal application migration validate credentials on activation.
                    dst.execute('pragma user_version=0')
            check_integrity(partial)
            # Atomic no-overwrite publication; the incomplete file is never a production DB.
            os.link(partial, destination)
            partial.unlink()
            report['status'] = 'validated'
            report['integrity'] = 'ok'
            return report
        except Exception:
            report['status'] = 'failed'
            raise
        finally:
            json.dump(report, report_file, ensure_ascii=True, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origem', required=True, type=Path)
    parser.add_argument('--destino', required=True, type=Path)
    parser.add_argument('--confirmar-copia', action='store_true', required=True,
                        help='Confirma servico parado e origem sendo uma copia preservada.')
    args = parser.parse_args()
    try:
        report = recover(args.origem, args.destino)
    except (RuntimeError, sqlite3.Error, OSError) as exc:
        print(f'RECUPERACAO NAO CONCLUIDA: {exc}')
        print('Nao substitua o banco em producao. Preserve a origem e o relatorio.')
        return 2
    print('RECUPERACAO VALIDADA. Banco de producao NAO substituido.')
    for table, count in report['copied_rows'].items():
        print(f'{table}: {count} registro(s)')
    print('monitor_results ficou vazio. A chave de credenciais original deve ser preservada.')
    print('Envie apenas o resumo; nao envie o banco nem a chave.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
