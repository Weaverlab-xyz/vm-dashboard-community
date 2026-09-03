"""One encrypted SQL Server connection, for every workload that opens one.

**python-tds turns TLS on only when it is handed a CA file.** ``pytds.connect()``
derives its pre-login encryption flag from ``cafile`` and nothing else, so
``cafile=None`` advertises ``ENCRYPT_NOT_SUP`` — and not one of the managed SQL
Server flavors this dashboard provisions will complete a handshake on those terms:

  * **Azure SQL Database** answers ``ENCRYPT_REQ``, and python-tds raises
    *"Client does not have encryption enabled but it is required by server"*.
  * **RDS / Cloud SQL** answer ``ENCRYPT_OFF`` (encrypt the login packet), which
    sends python-tds into ``tls.establish_channel`` with no context at all and
    raises *"login.tls_ctx is not set unexpectedly"*.

Both come out of ``dispatch`` as an opaque ``500 {"error": "internal error"}``, on
every SQL Server route, in every deployment — which is exactly what a paired
``db_grant`` adapter did on Entitle's first real ``create_actor``. Nothing caught it
earlier because ``FN_DB_DRY_RUN`` defaults ON and a dry run opens no connection, and
because the MySQL half of the same code path (pymysql) needs no CA to encrypt.

The other half of the question is WHICH CA to trust, and the answer is the one the
jump-host client commands already settled on — see the TLS note in
``cloud_db_sql_service``: **encrypt, but do not verify**. A function's package pins
neither the DigiCert root Azure SQL presents, nor the Amazon RDS roots, nor Cloud
SQL's per-instance server CA, and there is no host trust store to fall back on, so
demanding verification here would only trade one guaranteed failure for another.
``FN_DB_CAFILE`` is the opt-in: point it at a PEM bundle in the package and the
connection verifies against that instead.

python-tds has no argument for "encrypt without verifying" — ``cafile`` is the one
switch and it means both things at once — so the context is built here and installed
over ``pytds.tls.create_context``. The sentinel keeps that override narrow: a real
``FN_DB_CAFILE`` still goes through python-tds's own builder, unchanged.

``pytds`` and ``OpenSSL`` are imported lazily, for the same reason ``secretref``
imports boto3 that way: they are vendored into the packages that need them
(``cloud_function_package._WORKLOAD_VENDOR``) and absent everywhere else, so
importing at module scope would break every stdlib-only workload's cold start.
"""
import os

# What is passed to pytds as ``cafile`` when there is no bundle to verify against.
# Deliberately not a path — the NUL makes it one no filesystem can produce — because
# its only jobs are to take pytds's encrypted branch and to be recognised below.
_TRUST_SERVER_CERT = "\0fnruntime.tds:trust-server-certificate"


def cafile() -> str:
    """The operator's CA bundle, or ``""`` for trust-without-verify."""
    return (os.environ.get("FN_DB_CAFILE", "") or "").strip()


def _trusting_context():
    """An encrypting, non-verifying TLS client context.

    TLS 1.2 as the floor rather than python-tds's ``TLSv1_2_METHOD``, which pins the
    version exactly: every one of these servers offers 1.2 today, and pinning would
    make this the thing that breaks when one of them stops.
    """
    from OpenSSL import SSL  # noqa: PLC0415 — vendored per workload; see the module docstring

    context = SSL.Context(SSL.TLS_METHOD)
    context.set_min_proto_version(SSL.TLS1_2_VERSION)
    context.set_verify(SSL.VERIFY_NONE)
    return context


def _install(pytds) -> None:
    """Teach ``pytds.tls.create_context`` about the sentinel. Idempotent — a warm
    function connects many times and wrapping the wrapper would recurse."""
    if not pytds.tls.OPENSSL_AVAILABLE:
        # pytds raises here too, but names only pyOpenSSL; the actionable fact is
        # that this package was built without the driver's TLS chain in it.
        raise RuntimeError(
            "SQL Server needs TLS and pyOpenSSL is not importable in this function "
            "package — the image's FN_VENDOR_DIR step and "
            "cloud_function_package._WORKLOAD_VENDOR have drifted apart")

    builder = pytds.tls.create_context
    if getattr(builder, "_fnruntime_wrapped", False):
        return

    def create_context(name):
        if name == _TRUST_SERVER_CERT:
            return _trusting_context()
        return builder(name)

    create_context._fnruntime_wrapped = True
    pytds.tls.create_context = create_context


def connect(*, host: str, port: int, database: str, user: str, password: str,
            timeout: int, autocommit: bool = True):
    """One TLS-encrypted SQL Server connection.

    ``validate_host`` stays off: with no CA bundle there is no chain to anchor a
    hostname to, and when there is one the operator asked for a trusted issuer, not
    for python-tds's own name matcher (which mishandles wildcards and SAN parsing).
    """
    import pytds  # noqa: PLC0415 — vendored per workload; see the module docstring

    _install(pytds)
    return pytds.connect(
        server=host, port=port, database=database or "master", user=user,
        password=password, login_timeout=timeout,
        cafile=cafile() or _TRUST_SERVER_CERT,
        validate_host=False, autocommit=autocommit)
