"""Native deployment and RDS boundaries, with isolated fake credentials only."""
import importlib.util
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock,patch

from audit_core import product_database as database
from audit_core.common import AuditError
from audit_core.private_credentials import read_private_file
from audit_core.model_settings import ModelSettingsStore

ROOT=Path(__file__).resolve().parents[1]


class RDSDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.password=self.root/'password'
        self.password.write_text('private-test-value\n',encoding='utf-8');self.password.chmod(0o600)
        # Some Windows Python builds cannot construct an SSLContext after the
        # whole environment is cleared. Construct it before patching os.environ:
        # this test is about the configured RDS boundary, not OpenSSL startup.
        self.ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.rows=[{'product_code':'TEST-1','product_name':'测试商品','barcode_69':'6970356167341','image_manifest_key':None}]
        self.env={'OFFLINE_AUDIT_PRODUCT_DB_HOST':'rds.example',
                  'OFFLINE_AUDIT_PRODUCT_DB_PASSWORD_FILE':str(self.password),
                  'OFFLINE_AUDIT_PRODUCT_DB_CA_FILE':str(self.root/'configured-ca.pem'),
                  'OFFLINE_AUDIT_PRODUCT_DB_NAME':'customer_catalog',
                  'OFFLINE_AUDIT_PRODUCT_DB_USER':'customer_viewer'}

    def connection(self,cipher='TLS_AES_256_GCM_SHA384',count=1):
        cursor=MagicMock();cursor.fetchone.side_effect=[('Ssl_cipher',cipher),(count,)]
        cursor.fetchall.return_value=[(json.dumps(row),) for row in self.rows]
        connection=MagicMock();connection.__enter__.return_value=connection
        connection.cursor.return_value.__enter__.return_value=cursor
        module=types.SimpleNamespace(connect=MagicMock(return_value=connection))
        return module,connection,cursor

    def test_native_rds_uses_configured_account_tls_and_read_only_snapshot(self):
        module,connection,cursor=self.connection()
        with patch.dict(os.environ,self.env,clear=True),patch.dict(sys.modules,{'pymysql':module}),patch('ssl.create_default_context',return_value=self.ssl_context):
            self.assertEqual(database._read_database_rows(),self.rows)
        options=module.connect.call_args.kwargs
        self.assertEqual(options['user'],'customer_viewer');self.assertEqual(options['database'],'customer_catalog')
        self.assertIsInstance(options['ssl'],ssl.SSLContext);self.assertFalse(options['local_infile'])
        self.assertTrue(options['ssl'].check_hostname);self.assertEqual(options['ssl'].verify_mode,ssl.CERT_REQUIRED)
        sql=[call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY',sql)
        self.assertIn('SELECT COUNT(*) FROM products',sql)
        self.assertTrue(any('FROM products ORDER BY product_code' in statement for statement in sql))
        self.assertFalse(any('product_catalog.' in statement for statement in sql))
        connection.commit.assert_called_once()

    def test_plaintext_or_truncated_database_results_fail_without_fallback(self):
        for cipher,count in [('',1),('TLS',2)]:
            module,connection,cursor=self.connection(cipher,count)
            with patch.dict(os.environ,self.env,clear=True),patch.dict(sys.modules,{'pymysql':module}),patch('ssl.create_default_context',return_value=self.ssl_context),self.assertRaises(AuditError) as error:
                database._read_database_rows()
            self.assertNotIn('private-test-value',str(error.exception));connection.commit.assert_not_called()

    def test_unconfigured_linux_has_no_container_or_json_fallback(self):
        if os.name=='nt':self.skipTest('Native Linux branch is verified in Linux regressions.')
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(AuditError,'RDS 尚未配置'):
            database._read_database_rows()

    def test_ca_validation_is_enabled_when_configured(self):
        module,_,_=self.connection();ca=self.root/'ca.pem';ca.write_text('invalid certificate')
        with patch.dict(os.environ,{**self.env,'OFFLINE_AUDIT_PRODUCT_DB_CA_FILE':str(ca)},clear=True),patch.dict(sys.modules,{'pymysql':module}),self.assertRaises(AuditError):
            database._read_database_rows()
        module.connect.assert_not_called()

    def test_explicit_disabled_mode_reads_without_ca_and_keeps_read_only_snapshot(self):
        module,connection,cursor=self.connection(cipher='')
        env={k:v for k,v in self.env.items() if k!='OFFLINE_AUDIT_PRODUCT_DB_CA_FILE'}
        env['OFFLINE_AUDIT_PRODUCT_DB_SSL_MODE']='DISABLED'
        with patch.dict(os.environ,env,clear=True),patch.dict(sys.modules,{'pymysql':module}),patch('ssl.create_default_context') as tls:
            self.assertEqual(database._read_database_rows(),self.rows)
        tls.assert_not_called()
        options=module.connect.call_args.kwargs
        self.assertTrue(options['ssl_disabled']);self.assertNotIn('ssl',options)
        self.assertFalse(options['local_infile']);self.assertFalse(options['autocommit'])
        sql=[call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY',sql)
        connection.commit.assert_called_once()

    def test_missing_ca_does_not_implicitly_disable_tls_and_unknown_mode_is_rejected(self):
        env={k:v for k,v in self.env.items() if k!='OFFLINE_AUDIT_PRODUCT_DB_CA_FILE'}
        for mode in (None,'PREFERRED','VERIFY_CA','invalid'):
            module,connection,_=self.connection(cipher='')
            settings=dict(env)
            if mode is not None:settings['OFFLINE_AUDIT_PRODUCT_DB_SSL_MODE']=mode
            with patch.dict(os.environ,settings,clear=True),patch.dict(sys.modules,{'pymysql':module}),self.assertRaises(AuditError):
                database._read_database_rows()
            module.connect.assert_not_called();connection.commit.assert_not_called()

    def test_explicit_disabled_mode_still_rejects_incomplete_catalog(self):
        module,connection,_=self.connection(cipher='',count=2)
        env={k:v for k,v in self.env.items() if k!='OFFLINE_AUDIT_PRODUCT_DB_CA_FILE'}
        env['OFFLINE_AUDIT_PRODUCT_DB_SSL_MODE']='DISABLED'
        with patch.dict(os.environ,env,clear=True),patch.dict(sys.modules,{'pymysql':module}),self.assertRaises(AuditError):
            database._read_database_rows()
        connection.commit.assert_not_called()

    def test_private_file_retains_password_spaces_and_rejects_links(self):
        self.password.write_text(' space around password \n',encoding='utf-8')
        self.assertEqual(read_private_file(self.password),' space around password ')
        link=self.root/'link'
        try:link.symlink_to(self.password)
        except OSError:self.skipTest('Link creation unavailable.')
        with self.assertRaises(AuditError):read_private_file(link)

    def test_linux_rejects_world_readable_secret_files(self):
        if os.name=='nt':self.skipTest('POSIX mode protection is checked in Linux.')
        self.password.chmod(0o644)
        with self.assertRaises(AuditError):read_private_file(self.password)

    def test_model_api_key_file_is_private_and_not_returned_by_public_settings(self):
        self.password.write_text('sk_fixture_key_only\n',encoding='utf-8')
        with patch.dict(os.environ,{'PPIO_API_KEY_FILE':str(self.password)},clear=True):
            store=ModelSettingsStore(self.root/'worktrees');value=store.public()
            self.assertTrue(value['profiles']['ppio']['has_api_key'])
            self.assertNotIn('sk_fixture_key_only',json.dumps(value));self.assertEqual(store.snapshot().api_key,'sk_fixture_key_only')

    def test_configuration_loader_does_not_execute_shell_text(self):
        spec=importlib.util.spec_from_file_location('delivery_config',ROOT/'deploy/configuration.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        source=self.root/'application.env';source.write_text('EXAMPLE_VALUE=$(echo secret)\nEXAMPLE_FILE=config/test.txt\n',encoding='utf-8')
        with patch.dict(os.environ,{},clear=True):
            result=module.load_environment(source)
        self.assertEqual(result['EXAMPLE_VALUE'],'$(echo secret)');self.assertTrue(Path(result['EXAMPLE_FILE']).is_absolute())

    def test_native_deployment_has_no_container_files_and_separates_secret_templates(self):
        self.assertFalse((ROOT/'compose.yaml').exists());self.assertFalse((ROOT/'deploy/Dockerfile').exists())
        template=(ROOT/'deploy/application.env.example').read_text(encoding='utf-8')
        self.assertIn('OFFLINE_AUDIT_PRODUCT_DB_CA_FILE=config/rds-ca.pem',template)
        self.assertIn('PPIO_API_KEY_FILE=config/secrets/ppio_api_key.txt',template)
        self.assertIn('OFFLINE_AUDIT_OSS_ENABLED=0',template)
        self.assertNotIn('BAIDU_MAPS',template)
        unit=(ROOT/'deploy/offline-activity-audit.service.example').read_text(encoding='utf-8')
        self.assertIn('User=@SERVICE_USER@',unit);self.assertIn('/deploy/service.py',unit)


if __name__=='__main__':unittest.main()
