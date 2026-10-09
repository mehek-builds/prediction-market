import base64

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from fastlane.kalshi import KalshiClient, load_private_key
from conftest import pem_body


@pytest.mark.parametrize("fixture,cls", [("rsa_pem", rsa.RSAPrivateKey), ("ed25519_pem", ed25519.Ed25519PrivateKey)])
def test_load_all_forms(request, tmp_path, fixture, cls):
    pem = request.getfixturevalue(fixture)
    p = tmp_path / "k.pem"
    p.write_text(pem)
    assert isinstance(load_private_key(str(p)), cls)
    assert isinstance(load_private_key(pem), cls)
    assert isinstance(load_private_key(pem_body(pem)), cls)


def test_empty_and_garbage():
    assert load_private_key("") is None
    assert load_private_key(None) is None
    with pytest.raises(ValueError):
        load_private_key("not a key at all")
    with pytest.raises(ValueError):
        load_private_key("/no/such/file.pem")


def test_rsa_signature_verifies(rsa_pem):
    c = KalshiClient(key_id="k", private_key=rsa_pem)
    assert c.configured and c.key_type == "rsa"
    h = c.sign_headers("get", "/trade-api/ws/v2?x=1")
    assert set(h) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP", "KALSHI-ACCESS-SIGNATURE"}
    assert h["KALSHI-ACCESS-KEY"] == "k" and h["KALSHI-ACCESS-TIMESTAMP"].isdigit()
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/ws/v2").encode()  # query stripped
    c._key.public_key().verify(sig, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                      salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())


def test_ed25519_signature_verifies(ed25519_pem):
    c = KalshiClient(key_id="k", private_key=ed25519_pem)
    assert c.key_type == "ed25519"
    h = c.sign_headers("GET", "/trade-api/ws/v2")
    c._key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                               (h["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/ws/v2").encode())


def test_unconfigured(rsa_pem):
    assert not KalshiClient(key_id="", private_key=rsa_pem).configured
    c = KalshiClient()
    assert not c.configured and c.key_type == "none"
    with pytest.raises(RuntimeError):
        c.sign_headers("GET", "/x")


def test_reads_env(monkeypatch, rsa_pem):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "envkey")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", rsa_pem)
    assert KalshiClient().configured
