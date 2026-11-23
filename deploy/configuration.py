"""Load the project's private native-service environment, without shell evaluation."""
from __future__ import annotations

import os
from pathlib import Path
import re
import ssl

ROOT=Path(__file__).resolve().parents[1]


def use_system_ca_bundle():
    """Locate existing Linux trust roots when this Python build cannot find them."""
    if os.name == 'nt' or os.environ.get('SSL_CERT_FILE') or os.environ.get('SSL_CERT_DIR'):
        return
    if ssl.create_default_context().get_ca_certs():
        return
    for candidate in ('/etc/pki/tls/certs/ca-bundle.crt', '/etc/ssl/certs/ca-certificates.crt'):
        path=Path(candidate)
        if path.is_file() and ssl.create_default_context(cafile=str(path)).get_ca_certs():
            os.environ['SSL_CERT_FILE']=str(path)
            return


def load_environment(path=None):
    source=Path(path or ROOT/'config/application.env')
    if not source.is_file():raise RuntimeError('Missing config/application.env; run deploy/manage.py configure first.')
    values={}
    for line in source.read_text(encoding='utf-8-sig').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):continue
        key,separator,value=line.partition('=')
        if not separator or not re.fullmatch(r'[A-Z][A-Z0-9_]*',key) or key in values:
            raise RuntimeError('Invalid or duplicate environment setting; no values were printed.')
        value=value.strip()
        if value.startswith(('"',"'")):
            if len(value)<2 or value[-1]!=value[0]:raise RuntimeError('Unmatched configuration quotes.')
            value=value[1:-1]
        if key.endswith('_FILE') and value and not Path(value).is_absolute():value=str(ROOT/value)
        values[key]=value
    os.environ.update(values)
    use_system_ca_bundle()
    return values
