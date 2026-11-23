"""Reverse-proxy origins remain explicit and cannot be granted by forwarded headers."""
from types import SimpleNamespace
import unittest

from audit_core.workbench_server import Handler


class WorkbenchProxyOriginTests(unittest.TestCase):
    def allowed(self, *, origin='https://offline-verify.example:8828', peer='192.168.2.187',
                host='192.168.2.187:8080', approved=True, fetch_site='same-origin', **extra):
        server = SimpleNamespace(deletion_hosts={'127.0.0.1', '192.168.2.187'})
        if approved:
            server.proxy_origin='https://offline-verify.example:8828'
            server.proxy_client_ips={'127.0.0.1','::1','192.168.2.187'}
        handler=Handler.__new__(Handler)
        handler.server=server
        handler.client_address=(peer,12345)
        handler.headers={'Origin':origin,'Host':host,'Sec-Fetch-Site':fetch_site,**extra}
        return Handler._same_origin_allowed(handler)

    def test_approved_same_host_https_proxy_is_allowed(self):
        self.assertTrue(self.allowed())
        self.assertTrue(self.allowed(peer='127.0.0.1'))

    def test_origin_port_scheme_path_and_proxy_peer_must_match(self):
        for changes in [
            {'origin':'https://other.example:8828'},
            {'origin':'https://offline-verify.example'},
            {'origin':'http://offline-verify.example:8828'},
            {'origin':'https://offline-verify.example:8828/ai/'},
            {'origin':'https://user@offline-verify.example:8828'},
            {'peer':'192.168.2.188'},
            {'fetch_site':'cross-site'},
            {'approved':False},
        ]:
            with self.subTest(changes=changes):self.assertFalse(self.allowed(**changes))

    def test_forwarded_headers_cannot_add_a_trusted_proxy(self):
        self.assertFalse(self.allowed(peer='192.168.2.188', **{
            'X-Forwarded-Host':'offline-verify.example:8828',
            'X-Forwarded-Proto':'https','X-Forwarded-For':'127.0.0.1'}))

    def test_original_direct_http_origin_and_demo_server_still_work(self):
        self.assertTrue(self.allowed(origin='http://127.0.0.1:8080',host='127.0.0.1:8080',approved=False))
        self.assertFalse(self.allowed(origin='http://other.example:8080',host='other.example:8080',approved=False))


if __name__=='__main__':unittest.main()
