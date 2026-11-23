"""Native deployment uses installed trust roots without changing TLS verification."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest import mock


_SPEC = importlib.util.spec_from_file_location("native_configuration", Path(__file__).resolve().parents[1] / "deploy/configuration.py")
configuration = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(configuration)


class SystemCABundleTests(unittest.TestCase):
    def test_missing_python_trust_uses_existing_linux_bundle(self):
        empty = mock.Mock()
        empty.get_ca_certs.return_value = []
        trusted = mock.Mock()
        trusted.get_ca_certs.return_value = [{"subject": "system-root"}]
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(configuration.os, "name", "posix"), \
                mock.patch.object(configuration, "Path") as path, \
                mock.patch.object(configuration.ssl, "create_default_context", side_effect=[empty, trusted]) as context:
            path.return_value.is_file.return_value = True
            path.return_value.__str__.return_value = "/etc/pki/tls/certs/ca-bundle.crt"
            configuration.use_system_ca_bundle()
            self.assertEqual(os.environ["SSL_CERT_FILE"], "/etc/pki/tls/certs/ca-bundle.crt")
            self.assertEqual(context.call_args.kwargs, {"cafile": "/etc/pki/tls/certs/ca-bundle.crt"})

    def test_existing_or_explicit_trust_is_preserved(self):
        for environment in ({"SSL_CERT_FILE": "/custom/trust.pem"}, {"SSL_CERT_DIR": "/custom/roots"}):
            with self.subTest(environment=environment), mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(configuration.os, "name", "posix"), \
                    mock.patch.object(configuration.ssl, "create_default_context") as context:
                configuration.use_system_ca_bundle()
                self.assertEqual(dict(os.environ), environment)
                context.assert_not_called()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(configuration.os, "name", "posix"), \
                mock.patch.object(configuration.ssl, "create_default_context") as context:
            context.return_value.get_ca_certs.return_value = [{"subject": "existing-root"}]
            configuration.use_system_ca_bundle()
            self.assertNotIn("SSL_CERT_FILE", os.environ)
            self.assertEqual(context.call_count, 1)

    def test_missing_bundles_do_not_disable_verification(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(configuration.os, "name", "posix"), \
                mock.patch.object(configuration, "Path") as path, \
                mock.patch.object(configuration.ssl, "create_default_context") as context:
            context.return_value.get_ca_certs.return_value = []
            path.return_value.is_file.return_value = False
            configuration.use_system_ca_bundle()
            self.assertNotIn("SSL_CERT_FILE", os.environ)
            self.assertEqual(context.call_count, 1)


if __name__ == "__main__":
    unittest.main()
