from __future__ import absolute_import, division, unicode_literals

import datetime

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa

import pytest

from validator import X509Validator, ValidationError, ValidationContext


class CertificatePair(object):
    def __init__(self, cert, key):
        self.cert = cert
        self.key = key


class KeyCache(object):
    def __init__(self):
        self._inuse_keys = []
        self._free_keys = []

    def generate_rsa_key(self):
        if self._free_keys:
            key = self._free_keys.pop()
        else:
            key = rsa.generate_private_key(65537, 2048, default_backend())
        self._inuse_keys.append(key)
        return key

    def reset(self):
        self._free_keys.extend(self._inuse_keys)
        del self._inuse_keys[:]


class CAWorkspace(object):
    def __init__(self, key_cache):
        self._key_cache = key_cache
        self._roots = []

    def build_validator(self):
        return X509Validator(self._roots)

    def build_validation_context(self, extra_certs=[]):
        return ValidationContext(extra_certs=[c.cert for c in extra_certs])

    def assert_doesnt_validate(self, cert, **kwargs):
        validator = self.build_validator()
        ctx = self.build_validation_context(**kwargs)
        with pytest.raises(ValidationError):
            validator.validate(cert.cert, ctx)

    def assert_validates(self, cert, **kwargs):
        validator = self.build_validator()
        chain = validator.validate(
            cert.cert, self.build_validation_context(**kwargs)
        )
        assert cert.cert in chain

    def _issue_new_cert(self, issuer=None, not_valid_before=None,
                        not_valid_after=None, extra_extensions=[]):
        key = self._key_cache.generate_rsa_key()
        subject_name = x509.Name([])

        if issuer is not None:
            issuer_name = issuer.cert.subject
            ca_key = issuer.key
        else:
            issuer_name = subject_name
            ca_key = key

        if not_valid_before is None:
            not_valid_before = datetime.datetime.utcnow()
        if not_valid_after is None:
            not_valid_after = (
                datetime.datetime.utcnow() + datetime.timedelta(hours=1)
            )

        builder = x509.CertificateBuilder().serial_number(
            1
        ).public_key(
            key.public_key()
        ).not_valid_before(
            not_valid_before
        ).not_valid_after(
            not_valid_after
        ).subject_name(
            subject_name
        ).issuer_name(
            issuer_name
        )
        for ext in extra_extensions:
            builder = builder.add_extension(ext, critical=False)
        cert = builder.sign(ca_key, hashes.SHA256(), default_backend())
        return CertificatePair(cert, key)

    def _issue_new_ca(self, issuer=None, path_length=None, **kwargs):
        return self._issue_new_cert(
            issuer=issuer,
            extra_extensions=[
                x509.BasicConstraints(ca=True, path_length=path_length)
            ],
            **kwargs
        )

    def issue_new_trusted_root(self, **kwargs):
        certpair = self._issue_new_ca(**kwargs)
        self._roots.append(certpair.cert)
        return certpair

    def issue_new_ca_certificate(self, ca):
        return self._issue_new_ca(issuer=ca)

    def issue_new_leaf(self, ca, **kwargs):
        return self._issue_new_cert(issuer=ca, **kwargs)

    def issue_new_self_signed(self):
        return self._issue_new_cert()


@pytest.fixture(scope="session")
def key_cache():
    return KeyCache()


@pytest.fixture
def ca_workspace(key_cache):
    workspace = CAWorkspace(key_cache)
    try:
        yield workspace
    finally:
        key_cache.reset()


def test_empty_trust_store(ca_workspace):
    cert = ca_workspace.issue_new_self_signed()
    ca_workspace.assert_doesnt_validate(cert)


def test_simple_issuance(ca_workspace):
    root = ca_workspace.issue_new_trusted_root()
    cert = ca_workspace.issue_new_leaf(root)

    ca_workspace.assert_validates(cert)


def test_untrusted_issuer(ca_workspace):
    ca_workspace.issue_new_trusted_root()
    root = ca_workspace.issue_new_self_signed()
    cert = ca_workspace.issue_new_leaf(root)

    ca_workspace.assert_doesnt_validate(cert)


def test_intermediate(ca_workspace):
    root = ca_workspace.issue_new_trusted_root()
    intermediate = ca_workspace.issue_new_ca_certificate(root)
    cert = ca_workspace.issue_new_leaf(intermediate)

    ca_workspace.assert_validates(cert, extra_certs=[intermediate])


def test_ca_true_required(ca_workspace):
    root = ca_workspace.issue_new_trusted_root()
    cert = ca_workspace.issue_new_leaf(root)
    untrusted = ca_workspace.issue_new_leaf(cert)

    ca_workspace.assert_validates(cert)
    ca_workspace.assert_doesnt_validate(untrusted, extra_certs=[cert])


def test_pathlen(ca_workspace):
    root = ca_workspace.issue_new_trusted_root(path_length=0)
    intermediate = ca_workspace.issue_new_ca_certificate(root)
    direct = ca_workspace.issue_new_leaf(root)
    cert = ca_workspace.issue_new_leaf(intermediate)

    ca_workspace.assert_validates(direct)
    ca_workspace.assert_doesnt_validate(cert, extra_certs=[intermediate])

    root = ca_workspace.issue_new_trusted_root(path_length=1)
    direct1 = ca_workspace.issue_new_leaf(root)
    intermediate1 = ca_workspace.issue_new_ca_certificate(root)
    direct2 = ca_workspace.issue_new_leaf(root)
    intermediate2 = ca_workspace.issue_new_ca_certificate(intermediate)
    cert = ca_workspace.issue_new_leaf(intermediate2)

    ca_workspace.assert_validates(direct1)
    ca_workspace.assert_validates(direct2, extra_certs=[intermediate1])
    ca_workspace.assert_doesnt_validate(
        cert, extra_certs=[intermediate1, intermediate2]
    )


def test_leaf_validity(ca_workspace):
    root = ca_workspace.issue_new_trusted_root()
    expired = ca_workspace.issue_new_leaf(
        root,
        not_valid_before=datetime.datetime.today() - datetime.timedelta(days=2),
        not_valid_after=datetime.datetime.today() - datetime.timedelta(days=1),
    )
    not_yet_valid = ca_workspace.issue_new_leaf(
        root,
        not_valid_before=datetime.datetime.today() + datetime.timedelta(days=1),
        not_valid_after=datetime.datetime.today() + datetime.timedelta(days=2),
    )

    ca_workspace.assert_doesnt_validate(expired)
    ca_workspace.assert_doesnt_validate(not_yet_valid)


def test_root_validity(ca_workspace):
    expired_root = ca_workspace.issue_new_trusted_root(
        not_valid_before=datetime.datetime.today() - datetime.timedelta(days=2),
        not_valid_after=datetime.datetime.today() - datetime.timedelta(days=1),
    )
    not_yet_valid_root = ca_workspace.issue_new_trusted_root(
        not_valid_before=datetime.datetime.today() + datetime.timedelta(days=1),
        not_valid_after=datetime.datetime.today() + datetime.timedelta(days=2),
    )

    expired_root_leaf = ca_workspace.issue_new_leaf(expired_root)
    not_yet_valid_root_leaf = ca_workspace.issue_new_leaf(not_yet_valid_root)

    ca_workspace.assert_doesnt_validate(expired_root_leaf)
    ca_workspace.assert_doesnt_validate(not_yet_valid_root_leaf)