"""Generates the signed ``nkparam`` header required by Naukri's search API."""

import base64
import time

from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5

from src.config.constants import PUBLIC_KEY

_cipher = PKCS1_v1_5.new(RSA.import_key(PUBLIC_KEY))


def generate_nkparam(page_type: str = "srp") -> str:
    timestamp = int(time.time() * 1000)
    plaintext = f"v0|{timestamp}|121_{page_type}"
    encrypted = _cipher.encrypt(plaintext.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")
