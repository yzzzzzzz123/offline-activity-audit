"""RDS MySQL 8.x schema/data handover; SQL only, no source passwords or MySQL volume."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
from pathlib import Path
import re
import ssl
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'skills/orchestrate-offline-audit/scripts'))
from audit_core.private_credentials import read_private_file


def summary(connection):
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
        cursor.execute("SELECT JSON_OBJECT('barcode_69',barcode_69,'product_name',product_name,"
            "'product_code',product_code,'image_manifest_key',image_manifest_key) FROM products ORDER BY product_code")
        rows=[json.loads(row[0]) for row in cursor.fetchall()]
        connection.commit()
    return {'rows':len(rows),'sha256':hashlib.sha256(json.dumps(rows,ensure_ascii=False,
        sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()}


def checked_backup(source):
    source=Path(source)
    data=source.read_bytes()
    checksum=source.with_suffix(source.suffix+'.sha256').read_text(encoding='utf-8').split()[0]
    if not re.fullmatch('[0-9a-f]{64}',checksum) or hashlib.sha256(data).hexdigest()!=checksum:
        raise RuntimeError('SQL checksum mismatch; no target data was modified.')
    expected=json.loads(source.with_suffix('.manifest.json').read_text(encoding='utf-8'))
    if set(expected)!={'rows','sha256'} or type(expected['rows']) is not int or expected['rows']<=0:
        raise RuntimeError('Invalid source field manifest.')
    return data,expected


def validate_schema(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT COLUMN_NAME,DATA_TYPE,IS_NULLABLE,CHARACTER_MAXIMUM_LENGTH,COLLATION_NAME,COLUMN_KEY "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='products' ORDER BY ORDINAL_POSITION")
        fields=cursor.fetchall()
    expected=[('barcode_69','char','NO',13,'ascii_bin','MUL'),
              ('product_name','varchar','NO',512,'utf8mb4_bin',''),
              ('product_code','varchar','NO',64,'ascii_bin','PRI'),
              ('image_manifest_key','varchar','YES',1024,'utf8mb4_bin','')]
    if list(fields)!=expected:
        raise RuntimeError('Target products schema differs from deploy/rds/schema.sql; refusing to import.')


def run(args):
    import pymysql
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}',args.database):raise RuntimeError('Invalid database name.')
    data=expected=None
    if args.command=='import':data,expected=checked_backup(args.file)
    ca=Path(args.ca)
    tls=ssl.create_default_context(cafile=str(ca))
    password=read_private_file(args.password_file) if args.password_file else getpass.getpass('RDS account password: ')
    # Multi-statements are enabled only for the explicit, hash-verified data import.
    flags=pymysql.constants.CLIENT.MULTI_STATEMENTS if args.command=='import' else 0
    with pymysql.connect(host=args.host,port=args.port,user=args.user,password=password,
        database=args.database,charset='utf8mb4',ssl=tls,connect_timeout=10,read_timeout=120,
        write_timeout=120,autocommit=False,local_infile=False,client_flag=flags) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
            if not cursor.fetchone()[1]:raise RuntimeError('TLS was not negotiated.')
            cursor.execute('SELECT VERSION()');version=str(cursor.fetchone()[0])
            if not version.startswith('8.'):raise RuntimeError('This delivery targets RDS MySQL 8.x; verify another engine/version separately.')
            if args.command=='schema':
                if not args.apply:print('Preview only: create products using deploy/rds/schema.sql. Add --apply to execute.');return
                cursor.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='products'")
                if cursor.fetchone()[0]:raise RuntimeError('products already exists; it was not changed.')
                cursor.execute((ROOT/'deploy/rds/schema.sql').read_text(encoding='utf-8'))
                connection.commit();validate_schema(connection)
                print('Created the dedicated products table. No database/account was created.');return
            if args.command=='import':
                validate_schema(connection)
                cursor.execute('SELECT COUNT(*) FROM products')
                if cursor.fetchone()[0]:raise RuntimeError('Import requires an empty products table; existing data was preserved.')
                if not args.apply:print(json.dumps({'preview':True,'source':expected}));return
                # Remove mysqldump session configuration: only complete INSERTs are imported,
                # with charset controlled by the connection and one explicit transaction.
                statements=[]
                for line in data.decode('utf-8').splitlines():
                    if line.startswith('INSERT INTO `products` '):statements.append(line)
                    elif line.strip() and not line.startswith(('--','/*!','SET ','LOCK ','UNLOCK ')):
                        raise RuntimeError('Unexpected SQL in the checked source dump.')
                if not statements:raise RuntimeError('Source dump contains no product rows.')
                cursor.execute('START TRANSACTION')
                try:
                    cursor.execute('\n'.join(statements))
                    while cursor.nextset():pass
                    # Check within the same uncommitted transaction: rollback on any mismatch.
                    cursor.execute("SELECT JSON_OBJECT('barcode_69',barcode_69,'product_name',product_name,"
                        "'product_code',product_code,'image_manifest_key',image_manifest_key) FROM products ORDER BY product_code")
                    rows=[json.loads(row[0]) for row in cursor.fetchall()]
                    actual={'rows':len(rows),'sha256':hashlib.sha256(json.dumps(rows,ensure_ascii=False,
                        sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()}
                    if actual!=expected:raise RuntimeError('Restored fields differ from source; import rolled back.')
                    connection.commit()
                except BaseException:connection.rollback();raise
                print(json.dumps({'imported':actual,'mysql_version':version}));return
        actual=summary(connection)
        if args.manifest and actual!=json.loads(Path(args.manifest).read_text(encoding='utf-8')):
            raise RuntimeError('Target records do not match the source field manifest.')
        if args.read_only:
            with connection.cursor() as cursor:
                for statement in ('UPDATE products SET product_code=product_code WHERE 1=0',
                                  'DELETE FROM products WHERE 1=0',
                                  'INSERT INTO products (barcode_69,product_name,product_code) SELECT barcode_69,product_name,product_code FROM products WHERE 1=0'):
                    try:cursor.execute(statement)
                    except pymysql.err.OperationalError as exc:
                        if exc.args[0] not in (1142,1143):raise
                    else:raise RuntimeError('The application account has write permission; remove its DML grants.')
                connection.rollback()
        print(json.dumps({'verified':actual,'read_only_verified':args.read_only,'tls':True,'mysql_version':version}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('schema','import','verify'))
    parser.add_argument('--host',required=True);parser.add_argument('--port',type=int,default=3306)
    parser.add_argument('--database',default='product_catalog');parser.add_argument('--user',required=True)
    parser.add_argument('--ca',required=True);parser.add_argument('--password-file')
    parser.add_argument('--file',default=str(ROOT/'deploy/seed/product-catalog.sql'))
    parser.add_argument('--manifest');parser.add_argument('--read-only',action='store_true')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:run(args);return 0
    except Exception as exc:
        # Database/TLS response bodies can contain endpoint details or authentication material.
        print('RDS operation failed: '+(str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__)+
              '. No credential or SQL response was printed.',file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
