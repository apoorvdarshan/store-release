"""Import the hash-locked store tooling and mint one ES256 JWT, with no network call.

A broken pin (for example a Dependabot bump) fails here, in pull request checks,
instead of in a release job that holds store credentials.
"""
from __future__ import annotations

import importlib.util


def main() -> None:
    import jwt  # noqa: PLC0415
    import googleapiclient.discovery  # noqa: F401,PLC0415
    import google.oauth2.service_account  # noqa: F401,PLC0415
    import PIL.Image  # noqa: F401,PLC0415
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415

    if importlib.util.find_spec("yaml") is not None:
        import yaml  # noqa: F401,PLC0415

    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode("ascii")
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    token = jwt.encode({"iss": "smoke", "aud": "appstoreconnect-v1", "exp": 4102444800},
                       private_pem, algorithm="ES256", headers={"kid": "SMOKE", "typ": "JWT"})
    claims = jwt.decode(token, public_pem, algorithms=["ES256"], audience="appstoreconnect-v1")
    if claims.get("iss") != "smoke" or jwt.get_unverified_header(token).get("kid") != "SMOKE":
        raise SystemExit("error: the ES256 round trip returned the wrong claims")
    print("store tooling imports OK; ES256 JWT round trip OK")
