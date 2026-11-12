"""Démarrage DB : DNS Railway, backoff, secours public, erreurs définitives.

Reproduit le crash observé : socket.gaierror [Errno -2] Name or service not known
sur postgres.railway.internal au premier create_pool.
"""
import asyncio
import socket

import asyncpg
import pytest

from arb import db

GAI = socket.gaierror(-2, "Name or service not known")
INTERNAL = "postgresql://postgres:secret@postgres.railway.internal:5432/railway"
PUBLIC = "postgresql://postgres:secret@shuttle.proxy.rlwy.net:41235/railway"


class FakePool:
    def __init__(self):
        self.terminated = False

    def acquire(self):
        pool = self

        class Ctx:
            async def __aenter__(self):
                class C:
                    async def fetchval(self, q):
                        assert q == "SELECT 1"
                        return 1
                return C()

            async def __aexit__(self, *a):
                return False
        return Ctx()

    def terminate(self):
        self.terminated = True


@pytest.fixture
def wire(monkeypatch):
    """Pas de réseau, pas d'attente réelle : DNS OK + sleeps instantanés."""
    slept = []

    async def no_sleep(d):
        slept.append(d)

    async def dns_ok(host, port, timeout=5.0):
        return ["fd12::42"]

    monkeypatch.setattr(db.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(db, "resolve_host", dns_ok)
    return slept


def test_clean_dsn_strippe_les_parametres_libpq_only():
    out = db.clean_dsn("postgres://u:p@h:5432/d?sslmode=require&channel_binding=require&pgbouncer=true")
    assert out == "postgresql://u:p@h:5432/d?sslmode=require"


def test_clean_dsn_detecte_un_template_railway_non_resolu():
    with pytest.raises(db.DatabaseConfigError, match="template non résolu"):
        db.clean_dsn("${{Postgres.DATABASE_URL}}")


def test_clean_dsn_vide_et_schema_invalide():
    with pytest.raises(db.DatabaseConfigError, match="vide"):
        db.clean_dsn("  ")
    with pytest.raises(db.DatabaseConfigError, match="Schéma"):
        db.clean_dsn("mysql://u:p@h:3306/d")
    with pytest.raises(db.DatabaseConfigError, match="pas une URL"):
        db.clean_dsn("host=db port=5432 dbname=arb")


def test_describe_ne_fuite_pas_le_mot_de_passe():
    d = db.describe(INTERNAL)
    assert "secret" not in d
    assert d == "postgres@postgres.railway.internal:5432/railway"


def test_dns_ko_au_demarrage_puis_ok_reessaie_au_lieu_de_crasher(wire, monkeypatch):
    """Le cas du bug : Postgres pas encore prêt -> gaierror -> on attend, on ne meurt pas."""
    calls = []

    async def create_pool(dsn, **kw):
        calls.append(dsn)
        if len(calls) < 3:
            raise GAI
        return FakePool()

    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)
    pool = asyncio.run(db.connect(INTERNAL, wait_s=120))
    assert isinstance(pool, FakePool) and len(calls) == 3
    assert len(wire) == 2 and wire[1] > wire[0]        # backoff croissant


def test_erreur_de_mot_de_passe_echoue_immediatement(wire, monkeypatch):
    calls = []

    async def create_pool(dsn, **kw):
        calls.append(dsn)
        raise asyncpg.InvalidPasswordError("password authentication failed")

    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)
    with pytest.raises(db.DatabaseConfigError, match="mot de passe"):
        asyncio.run(db.connect(INTERNAL, wait_s=300))
    assert len(calls) == 1 and not wire               # aucun retry inutile


def test_bascule_sur_l_url_publique_quand_le_dns_prive_ne_resout_pas(monkeypatch):
    calls = []

    async def no_sleep(d):
        pass

    async def dns(host, port, timeout=5.0):
        if host.endswith(".railway.internal"):
            raise GAI
        return ["1.2.3.4"]

    async def create_pool(dsn, **kw):
        calls.append(dsn)
        return FakePool()

    monkeypatch.setattr(db.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(db, "resolve_host", dns)
    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)
    pool = asyncio.run(db.connect(INTERNAL, fallback_url=PUBLIC, wait_s=6))
    assert isinstance(pool, FakePool)
    assert calls == [PUBLIC]                          # l'URL privée a été abandonnée


def test_budget_epuise_leve_une_erreur_actionnable(wire, monkeypatch):
    async def dns_ko(host, port, timeout=5.0):
        raise GAI

    monkeypatch.setattr(db, "resolve_host", dns_ko)
    with pytest.raises(db.DatabaseUnavailableError) as ei:
        asyncio.run(db.connect(INTERNAL, wait_s=0))
    msg = str(ei.value)
    assert "postgres.railway.internal:5432" in msg and "MÊME projet" in msg
    assert "secret" not in msg


def test_url_primaire_absente_mais_secours_present(wire, monkeypatch):
    async def create_pool(dsn, **kw):
        assert dsn == PUBLIC
        return FakePool()

    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)
    assert isinstance(asyncio.run(db.connect("", fallback_url=PUBLIC)), FakePool)


def test_db_ssl_invalide_et_pool_termine_si_validation_echoue(monkeypatch):
    with pytest.raises(db.DatabaseConfigError, match="DB_SSL"):
        asyncio.run(db.connect(INTERNAL, ssl="maybe"))

    async def dns_ok(host, port, timeout=5.0):
        return ["10.0.0.1"]

    made = []

    class BadPool(FakePool):
        def acquire(self):
            raise ConnectionResetError("server closed the connection unexpectedly")

    async def create_pool(dsn, **kw):
        p = BadPool()
        made.append(p)
        return p

    monkeypatch.setattr(db, "resolve_host", dns_ok)
    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)
    with pytest.raises(db.DatabaseUnavailableError):
        asyncio.run(db.connect(INTERNAL, wait_s=0))
    assert made and made[0].terminated                # pas de connexion orpheline
