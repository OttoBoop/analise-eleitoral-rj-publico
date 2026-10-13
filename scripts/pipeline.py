#!/usr/bin/env python3
"""Streaming TSE ingestion, audited local-of-vote geography, public aggregates.

Original columns are retained in Parquet. Only bounded aggregate datasets reach
the browser. Run from any directory; sources/classifications belong to ROOT.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import gzip
import io
import json
import math
import shutil
import time
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import Point, shape
from shapely import prepare
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
PROCESSED = ROOT / "data/processed"
PUBLIC = ROOT / "web/dist/data"
CAPITAL = "60011"


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    raw = json.dumps(clean(obj), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if path.suffix == ".gz":
        tmp.write_bytes(gzip.compress(raw, compresslevel=6, mtime=0))
    else:
        tmp.write_bytes(raw)
    tmp.replace(path)


def save_dataset(d, payload):
    """Bounded candidate shards avoid loading every ballot distribution on mobile."""
    zones, bairros = payload.pop("zones"), payload.pop("bairros")
    zone_by, bairro_by = {}, {}
    for row in zones:
        zone_by.setdefault(row["candidate"], []).append(row)
    for row in bairros:
        bairro_by.setdefault(row["candidate"], []).append(row)
    for start in range(0, len(payload["candidates"]), 32):
        candidates = payload["candidates"][start:start+32]
        filename = f"candidates/{d['id']}/{start//32}.json.gz"
        shard = {}
        for c in candidates:
            c["file"] = filename
            shard[c["id"]] = {"zones": zone_by.get(c["id"], []), "bairros": bairro_by.get(c["id"], [])}
        save_json(PUBLIC / filename, shard)
    payload["zones"], payload["bairros"] = [], []
    payload["sharded"] = True
    save_json(PUBLIC / d["file"], payload)


def norm(value):
    return " ".join(unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().upper().split())


def code(value):
    value = str(value or "").strip()
    return str(int(value)) if value.isdigit() else value


def valid_factor(nominal, valid):
    """Never estimate a section allocation from a fractional zone total."""
    if nominal == valid:
        return 1
    if valid == 0:
        return 0
    return None


def participation(votes, denominator):
    return 100 * votes / denominator if denominator else None


def ingest(year, kind, force=False):
    files = ([f"munzona_{year}.zip"] if kind == "munzona" else
             [f"secao_{year}_RJ.zip"] + ([f"secao_{year}_BR.zip"] if year in (2018, 2022, 2026) else []) if kind == "secao" else
             [f"{kind}_{year}.zip"])
    target = PROCESSED / f"{kind}_{year}.parquet"
    paths = [ROOT / "data/raw" / f for f in files]
    if not all(p.exists() and zipfile.is_zipfile(p) for p in paths):
        return None
    stamp = [{"path": str(p.relative_to(ROOT)), "size": p.stat().st_size, "mtime": p.stat().st_mtime_ns} for p in paths]
    marker = target.with_suffix(".source.json")
    if not force and target.exists() and marker.exists() and json.loads(marker.read_text()) == stamp:
        return target
    start = time.monotonic()
    writer = None
    total = 0
    tmp = target.with_suffix(".parquet.part")
    schema = None
    try:
        for path in paths:
            with zipfile.ZipFile(path) as archive:
                members = [m for m in archive.namelist() if m.lower().endswith(".csv")]
                if kind == "munzona":
                    members = [m for m in members if m.endswith(f"{year}_RJ.csv") or (year in (2018, 2022, 2026) and m.endswith(f"{year}_BR.csv"))]
                elif any(m.endswith("_RJ.csv") for m in members):
                    members = [m for m in members if m.endswith("_RJ.csv") or m.endswith("_BR.csv")]
                for member in members:
                    with archive.open(member) as stream:
                        for chunk in pd.read_csv(stream, sep=";", encoding="latin-1", dtype=str, keep_default_na=False, chunksize=100_000):
                            if "SG_UF" not in chunk:
                                raise ValueError(f"SG_UF missing: {member}")
                            chunk = chunk.loc[chunk.SG_UF.eq("RJ")]
                            if chunk.empty:
                                continue
                            chunk = chunk.assign(SOURCE_MEMBER=member)
                            if schema is None:
                                schema = pa.schema([(str(c), pa.string()) for c in chunk.columns])
                                writer = pq.ParquetWriter(tmp, schema, compression="zstd")
                            for col in schema.names:
                                if col not in chunk:
                                    chunk[col] = ""
                            writer.write_table(pa.Table.from_pandas(chunk[schema.names], schema=schema, preserve_index=False))
                            total += len(chunk)
        if not writer:
            raise ValueError(f"No RJ data in {files}")
        writer.close()
        writer = None
        tmp.replace(target)
        save_json(marker, stamp)
        print(f"ingest {year}/{kind}: {total:,} RJ rows, {time.monotonic()-start:.1f}s", flush=True)
    finally:
        if writer:
            writer.close()
    return target


def query_file(db, name, path):
    literal = str(path).replace("'", "''")
    db.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{literal}')")


def geography():
    bp = ROOT / "data/reference/bairros.geojson"
    mp = ROOT / "data/reference/municipios.geojson"
    if not bp.exists() or not mp.exists():
        return None
    capital_bairros = json.loads(bp.read_text())
    metro_path = ROOT / "data/reference/bairros-metro.geojson"
    if metro_path.exists():
        bairros = json.loads(metro_path.read_text())
    else:
        # A missing complementary source never assigns out-of-capital votes.
        bairros = {**capital_bairros, "features": [
            {**f, "properties": {**f["properties"], "municipio": CAPITAL,
             "municipio_ibge": "3304557", "source": "ipp", "legacy_id": str(f["properties"]["id"]),
             "id": f"3304557:ipp:{f['properties']['id']}"}} for f in capital_bairros["features"]]}
    municipios = json.loads(mp.read_text())
    invalid = [f for f in bairros["features"] if not shape(f["geometry"]).is_valid or shape(f["geometry"]).is_empty]
    if invalid:
        raise ValueError("Canonical neighborhood source contains invalid geometry; quarantine before ingestion")
    ids = [str(f["properties"]["id"]) for f in bairros["features"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Canonical neighborhood identifiers must be unique")
    geoms = [shape(f["geometry"]) for f in bairros["features"]]
    muni_geoms = {str(f["properties"]["tse"]): shape(f["geometry"]) for f in municipios["features"]}
    for geom in geoms + list(muni_geoms.values()):
        prepare(geom)
    return {"bairros": bairros, "capital_bairros": capital_bairros, "municipios": municipios, "tree": STRtree(geoms),
            "geom": geoms, "by_id": {str(f["properties"]["id"]): f["properties"] for f in bairros["features"]},
            "legacy_ids": {str(f["properties"]["legacy_id"]): str(f["properties"]["id"])
                           for f in bairros["features"] if f["properties"].get("legacy_id") is not None
                           and str(f["properties"].get("municipio")) == CAPITAL},
            "bairro_municipios": {str(f["properties"]["municipio"]) for f in bairros["features"]},
            "municipios_by_tse": {str(f["properties"]["tse"]): f["properties"] for f in municipios["features"]},
            "municipio_geoms": muni_geoms}


def classify_location(lat, lon, municipio, geo):
    try:
        lat, lon = float(str(lat).replace(",", ".")), float(str(lon).replace(",", "."))
    except (TypeError, ValueError):
        return None, "coordenada_ausente", None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat, lon) == (0, 0):
        return None, "coordenada_invalida", None
    point = Point(lon, lat)
    muni = geo["municipio_geoms"].get(str(municipio))
    if muni is None or not muni.covers(point):
        return None, "fora_municipio", [lat, lon]
    if str(municipio) not in geo.get("bairro_municipios", {CAPITAL}):
        return None, "sem_malha_bairros", [lat, lon]
    matches = [int(i) for i in geo["tree"].query(point)
               if geo["geom"][int(i)].covers(point)
               and str(geo["bairros"]["features"][int(i)]["properties"].get("municipio", CAPITAL)) == str(municipio)]
    if len(matches) != 1:
        return None, "bairro_ambiguo" if matches else "fora_bairros", [lat, lon]
    return str(geo["bairros"]["features"][matches[0]]["properties"]["id"]), "coordenada_validada", [lat, lon]


def locations_for_year(db, year, geo):
    localpath = ingest(year, "locais")
    if not localpath or not geo:
        return None, []
    query_file(db, "locais_raw", localpath)
    rows = db.execute("""SELECT DISTINCT NR_TURNO turno, CD_MUNICIPIO municipio, NR_ZONA zona,
        NR_SECAO secao, NR_LOCAL_VOTACAO AS "local", NM_LOCAL_VOTACAO nome, DS_ENDERECO endereco,
        NM_BAIRRO bairro_informado, NR_LATITUDE latitude, NR_LONGITUDE longitude,
        NR_SECAO_PRINCIPAL principal, CD_TIPO_SECAO_AGREGADA tipo_agregada
        FROM locais_raw WHERE DT_ELEICAO IN (SELECT date FROM mun WHERE election_type='2')""").df()
    fixes = pd.read_csv(ROOT / "data/review/correcoes_locais.csv", dtype=str, keep_default_na=False)
    overrides = {}
    for _, fix in fixes.iterrows():
        if str(fix.ano) != str(year):
            continue
        if not fix.fonte or not fix.justificativa:
            raise ValueError("Geographic correction needs source and justification")
        overrides[tuple(code(fix[k]) for k in ["turno", "municipio", "zona", "local"])] = fix
    records, pending, point_cache = [], [], {}
    for v in rows.to_dict("records"):
        for k in ["turno", "municipio", "zona", "secao", "local"]:
            v[k] = code(v[k])
        k = tuple(v[x] for x in ["turno", "municipio", "zona", "local"])
        fix = overrides.get(k)
        if fix is not None:
            if fix.latitude and fix.longitude:
                v["latitude"], v["longitude"] = fix.latitude, fix.longitude
        point_key = (v["latitude"], v["longitude"], v["municipio"])
        if point_key not in point_cache:
            point_cache[point_key] = classify_location(*point_key, geo)
        bairro, status, coords = point_cache[point_key]
        if fix is not None and fix.bairro_id:
            bairro = fix.bairro_id
            if bairro not in geo["by_id"] and v["municipio"] == CAPITAL:
                bairro = geo["legacy_ids"].get(bairro.zfill(3), bairro)
            if bairro not in geo["by_id"]:
                raise ValueError(f"Unknown correction bairro {bairro}")
            if str(geo["by_id"][bairro].get("municipio", CAPITAL)) != v["municipio"]:
                raise ValueError("Geographic correction bairro belongs to another municipality")
            status = "correcao_documentada"
        prop = geo["by_id"].get(bairro, {})
        v.update({"ano": year, "bairro_id": bairro, "geo_status": status,
                  "lat": coords[0] if coords else None, "lon": coords[1] if coords else None,
                  "bairro": prop.get("nome"), "ra": prop.get("ra"), "rp": prop.get("rp"),
                  "ap": prop.get("ap"), "macro": prop.get("macro"),
                  "divergencia_bairro": bool(bairro and norm(v["bairro_informado"]) != norm(prop.get("nome")))})
        records.append(v)
        if bool(geo["municipios_by_tse"].get(v["municipio"], {}).get("metropolitana")) and (not bairro or v["divergencia_bairro"]):
            pending.append(v)
    frame = pd.DataFrame(records)
    # Principal/aggregated metadata can repeat. Distinct section keys must resolve
    # to exactly one effective location: ambiguous keys are quarantined, not joined.
    keycols = ["turno", "municipio", "zona", "secao"]
    sizes = frame.groupby(keycols, dropna=False).size()
    duplicate_keys = set(sizes[sizes > 1].index)
    if duplicate_keys:
        mask = frame.apply(lambda r: tuple(r[k] for k in keycols) in duplicate_keys, axis=1)
        pending.extend(frame.loc[mask].assign(geo_status="secao_local_ambiguo").to_dict("records"))
        frame = frame.loc[~mask]
    frame.to_parquet(PROCESSED / f"locations_{year}.parquet", index=False)
    pd.DataFrame(pending).to_csv(ROOT / f"data/review/locais_pendentes_{year}.csv", index=False)
    db.register("locations_df", frame)
    db.execute("CREATE OR REPLACE TABLE locations AS SELECT * FROM locations_df")
    return frame, pending


def municipality_geography_status(verified, has_mesh):
    if verified is False:
        return "bloqueada_divergencia"
    if not has_mesh:
        return "sem_malha"
    return "verificada" if verified is True else "indisponivel"


def geographic_aggregates(official, located, status_by_municipio):
    """Conserve authoritative valid votes; unknown geography is an explicit null.

    Inputs are bounded candidate/municipality and candidate/neighborhood totals,
    never section rows. A blocked municipality releases no neighborhood counts.
    """
    official_by_key = {(str(r["candidate"]), str(r["municipio"])): int(r["votes"])
                       for r in official}
    assigned_by_key = {}
    rows = []
    for row in located:
        municipio = str(row["municipio"])
        if status_by_municipio.get(municipio) != "verificada" or not row.get("bairro_id"):
            continue
        candidate = str(row["candidate"])
        key = (candidate, municipio)
        votes = int(row["votes"])
        if key not in official_by_key or votes < 0:
            raise ValueError("Located candidate votes have no authoritative municipality total")
        rows.append({"candidate": candidate, "municipio": municipio,
                     "bairro_id": str(row["bairro_id"]), "votes": votes})
        assigned_by_key[key] = assigned_by_key.get(key, 0) + votes
    for (candidate, municipio), votes in official_by_key.items():
        missing = votes - assigned_by_key.get((candidate, municipio), 0)
        if missing < 0:
            raise ValueError("Neighborhood join multiplied authoritative candidate votes")
        # Include explicit zero too: missing geography and a zero candidate stay
        # distinct from an absent candidature, which has no authoritative row.
        rows.append({"candidate": candidate, "municipio": municipio, "bairro_id": None, "votes": missing})
    denoms = {}
    for row in rows:
        key = (row["municipio"], row["bairro_id"])
        denoms[key] = denoms.get(key, 0) + row["votes"]
    coverage = []
    for municipio in sorted({key[1] for key in official_by_key}):
        total = sum(v for (_, m), v in official_by_key.items() if m == municipio)
        local = sum(v for (m, bairro), v in denoms.items() if m == municipio and bairro is not None)
        coverage.append({"municipio": municipio, "total": total, "located": local,
                         "unlocated": total-local, "percent": participation(local, total),
                         "status": status_by_municipio[municipio]})
    denominators = [{"municipio": m, "bairro_id": b, "denominator": v} for (m, b), v in denoms.items()]
    assert sum(r["votes"] for r in rows) == sum(official_by_key.values())
    return rows, denominators, coverage


def build_year(year, geo, force=False):
    munpath = ingest(year, "munzona", force)
    if not munpath:
        return {"year": year, "status": "fontes_indisponiveis", "datasets": []}
    db = duckdb.connect(str(PROCESSED / f"work_{year}.duckdb"))
    db.execute("SET memory_limit='3GB'")
    db.execute("SET threads=4")
    query_file(db, "mun_raw", munpath)
    db.execute("""CREATE OR REPLACE TABLE mun AS SELECT CD_ELEICAO election, DT_ELEICAO date,
      CD_TIPO_ELEICAO election_type, DS_ELEICAO election_name, CD_CARGO cargo, DS_CARGO cargo_name,
      NR_TURNO turno, SG_UE ue, NM_UE ue_name, CD_MUNICIPIO municipio, NM_MUNICIPIO municipio_name,
      NR_ZONA zona, SQ_CANDIDATO candidate, NR_CANDIDATO number, NM_CANDIDATO full_name,
      NM_URNA_CANDIDATO AS "name", SG_PARTIDO party, NR_PARTIDO party_number, ST_VOTO_EM_TRANSITO transit,
      SUM(TRY_CAST(QT_VOTOS_NOMINAIS AS BIGINT)) nominal,
      SUM(TRY_CAST(QT_VOTOS_NOMINAIS_VALIDOS AS BIGINT)) votes
      FROM mun_raw GROUP BY ALL""")
    datasets = db.execute("""SELECT DISTINCT election,date,election_type,cargo,cargo_name,turno,ue,ue_name
       FROM mun WHERE election_type='2' ORDER BY cargo,turno,ue""").df().to_dict("records")
    party_path = ingest(year, "partido", force)
    if party_path:
        query_file(db, "party_raw", party_path)
        db.execute("""CREATE OR REPLACE TABLE party_votes AS SELECT CD_ELEICAO election,
           CD_CARGO cargo,NR_TURNO turno,SG_UE ue,CD_MUNICIPIO municipio,NR_ZONA zona,
           SG_PARTIDO party,SUM(TRY_CAST(QT_VOTOS_NOMINAIS_VALIDOS AS BIGINT)) nominal,
           SUM(TRY_CAST(QT_TOTAL_VOTOS_LEG_VALIDOS AS BIGINT)) legenda
           FROM party_raw WHERE CD_TIPO_ELEICAO='2' GROUP BY ALL""")
    secpath = ingest(year, "secao", force)
    localframe, pending = locations_for_year(db, year, geo)
    audit = {"year": year, "nominal_reconciliation": "unavailable", "locations": localframe is not None,
             "pending_geography": len(pending), "datasets": [], "issues": []}
    has_detail = False
    if secpath:
        query_file(db, "sec_raw", secpath)
        columns = {r[0] for r in db.execute("DESCRIBE sec_raw").fetchall()}
        if "SQ_CANDIDATO" in columns:
            candidate_join = "s.SQ_CANDIDATO=m.candidate"
        else:
            candidate_join = "s.NR_VOTAVEL=m.number"
        # Zone/contest aggregate is distinct to prevent judicial-destination or
        # transit rows from multiplying section votes.
        db.execute("""CREATE OR REPLACE TABLE zone_candidates AS SELECT election,cargo,turno,ue,municipio,zona,
          candidate,number,"name",full_name,party,party_number,SUM(nominal) nominal,SUM(votes) votes
          FROM mun WHERE election_type='2' GROUP BY ALL""")
        db.execute(f"""CREATE OR REPLACE TABLE sec AS SELECT s.CD_ELEICAO election,s.CD_CARGO cargo,
          s.NR_TURNO turno,s.SG_UE ue,s.CD_MUNICIPIO municipio,s.NR_ZONA zona,s.NR_SECAO secao,
          s.NR_LOCAL_VOTACAO AS "local",m.candidate,m."name",m.party,
          SUM(TRY_CAST(s.QT_VOTOS AS BIGINT)) nominal
          FROM sec_raw s JOIN zone_candidates m ON s.CD_ELEICAO=m.election AND s.CD_CARGO=m.cargo
          AND s.NR_TURNO=m.turno AND s.SG_UE=m.ue AND s.CD_MUNICIPIO=m.municipio
          AND s.NR_ZONA=m.zona AND {candidate_join}
          WHERE s.CD_TIPO_ELEICAO='2' GROUP BY ALL""")
        db.execute("""CREATE OR REPLACE TABLE reconciliation AS SELECT m.*,
          COALESCE(s.nominal,0) section_nominal,
          m.nominal=COALESCE(s.nominal,0) AND (m.votes=m.nominal OR m.votes=0) verified,
          CASE WHEN m.votes=m.nominal THEN 1 WHEN m.votes=0 THEN 0 ELSE NULL END factor
          FROM zone_candidates m LEFT JOIN (SELECT election,cargo,turno,ue,municipio,zona,candidate,
          SUM(nominal) nominal FROM sec GROUP BY ALL) s USING(election,cargo,turno,ue,municipio,zona,candidate)""")
        bad = db.execute("SELECT * FROM reconciliation WHERE NOT verified").df()
        bad.to_parquet(ROOT / f"data/review/reconciliation_{year}.parquet", index=False)
        if "SQ_CANDIDATO" in columns:
            orphans = db.execute("""SELECT s.CD_ELEICAO election,s.CD_CARGO cargo,s.NR_TURNO turno,
              s.SG_UE ue,s.CD_MUNICIPIO municipio,s.NR_ZONA zona,COUNT(*) section_rows,
              SUM(TRY_CAST(s.QT_VOTOS AS BIGINT)) votes FROM sec_raw s
              WHERE s.CD_TIPO_ELEICAO='2' AND TRY_CAST(s.SQ_CANDIDATO AS BIGINT)>0
              AND NOT EXISTS (SELECT 1 FROM zone_candidates m WHERE s.CD_ELEICAO=m.election
              AND s.CD_CARGO=m.cargo AND s.NR_TURNO=m.turno AND s.SG_UE=m.ue
              AND s.CD_MUNICIPIO=m.municipio AND s.NR_ZONA=m.zona AND s.SQ_CANDIDATO=m.candidate)
              GROUP BY ALL""").df()
        else:
            orphans = pd.DataFrame()
        # Independently reconcile the current valid-vote universe with the party
        # file before excluding rows removed from the candidate snapshot. This
        # establishes only exclusion from current nominal-valid totals, not a
        # guessed individual legal status. Unknown allocations remain blocked.
        if not orphans.empty and party_path:
            db.register("orphan_df", orphans)
            orphans = db.execute("""SELECT o.*,r.votes candidate_valid,p.nominal party_valid,
               r.section_valid,COALESCE(r.verified AND r.votes=p.nominal AND r.votes=r.section_valid,false)
               excluded_from_current_valid FROM orphan_df o
               LEFT JOIN (SELECT election,cargo,turno,ue,municipio,zona,BOOL_AND(verified) verified,
                          SUM(votes) votes,SUM(section_nominal*factor) section_valid FROM reconciliation GROUP BY ALL) r
               USING(election,cargo,turno,ue,municipio,zona)
               LEFT JOIN (SELECT election,cargo,turno,ue,municipio,zona,SUM(nominal) nominal
                          FROM party_votes GROUP BY ALL) p USING(election,cargo,turno,ue,municipio,zona)""").df()
        elif not orphans.empty:
            orphans["excluded_from_current_valid"] = False
        unresolved = orphans.loc[~orphans.excluded_from_current_valid] if not orphans.empty else orphans
        audit["unmatched_section_contests"] = orphans.to_dict("records")
        audit["version_exclusion_method"] = "Exclusão do nominal válido atual somente quando candidato/seção e total nominal válido partidário reconciliam exatamente na mesma zona/cargo/turno. Causa jurídica individual não inferida."
        audit.update({"nominal_reconciliation": ("verified_with_version_exclusions" if not orphans.empty else "verified") if bad.empty and unresolved.empty else "divergences",
                      "divergent_candidate_zones": len(bad),
                      "nominal_delta": int((bad.section_nominal - bad.nominal).sum())})
        if localframe is not None:
            db.execute("""CREATE OR REPLACE TABLE located AS SELECT s.*,s.nominal*r.factor votes,
              r.verified,l.bairro_id,l.bairro,l.ra,l.rp,l.ap,l.macro,l.geo_status,
              l.lat,l.lon,l.nome local_name,l.endereco address
              FROM sec s JOIN reconciliation r USING(election,cargo,turno,ue,municipio,zona,candidate)
              LEFT JOIN locations l ON s.turno=l.turno AND s.municipio=l.municipio AND s.zona=l.zona
              AND s.secao=l.secao AND s."local"=l."local" """)
            # A divergent zone blocks its municipality/contest only. Candidate
            # denominators are never recomputed after dropping failed rows.
            db.execute("""CREATE OR REPLACE TABLE geo_ok AS SELECT election,cargo,turno,ue,municipio,
               BOOL_AND(verified) verified FROM reconciliation GROUP BY ALL""")
            for v in unresolved.to_dict("records"):
                if v["votes"]:
                    db.execute("UPDATE geo_ok SET verified=false WHERE election=? AND cargo=? AND turno=? AND ue=? AND municipio=?",
                               [v[k] for k in ["election", "cargo", "turno", "ue", "municipio"]])
            audit["geography_by_municipio"] = db.execute("SELECT * FROM geo_ok ORDER BY election,cargo,turno,ue,municipio").df().to_dict("records")
            has_detail = True
            conservation = db.execute("""SELECT SUM(votes) all_votes,
              SUM(CASE WHEN bairro_id IS NOT NULL THEN votes ELSE 0 END) located_votes,
              SUM(CASE WHEN bairro_id IS NULL THEN votes ELSE 0 END) unlocated_votes
              FROM located WHERE municipio='60011' AND verified""").fetchone()
            audit["capital_conservation"] = dict(zip(["all_votes", "located_votes", "unlocated_votes"], [int(v or 0) for v in conservation]))
            assert conservation[0] is None or conservation[0] == (conservation[1] or 0) + (conservation[2] or 0)
            relationships = db.execute("""SELECT turno,municipio,zona,bairro_id,bairro,ra,rp,ap,macro,
               COUNT(DISTINCT "local") locals,COUNT(DISTINCT secao) sections
               FROM locations WHERE municipio='60011' GROUP BY ALL ORDER BY turno,zona,bairro""").df()
            localcols = ["turno","municipio","zona","local","nome","endereco","bairro_id","bairro","ra","rp","ap","macro","geo_status","lat","lon"]
            public_locals = localframe.loc[localframe.municipio.eq(CAPITAL), localcols].drop_duplicates()
            save_json(PUBLIC / f"zones-{year}.json", {"year": year, "status": "nao_verificada",
                      "relationships": relationships.where(pd.notnull(relationships), None).to_dict("records"),
                      "locations": public_locals.to_dict("records")})
    # The detailed apuração is preserved separately and adds blank/null/legend
    # ballot totals. It is never joined to each candidate row.
    detalhe = ingest(year, "detalhe", force)
    if detalhe:
        query_file(db, "det_raw", detalhe)
        cols = {r[0] for r in db.execute("DESCRIBE det_raw").fetchall()}
        ballot_cols = [x for x in ["QT_APTOS", "QT_COMPARECIMENTO", "QT_ABSTENCOES", "QT_VOTOS_NOMINAIS", "QT_VOTOS_BRANCOS", "QT_VOTOS_NULOS", "QT_VOTOS_LEGENDA", "QT_VOTOS_ANULADOS_APU_SEP"] if x in cols]
        select = ",".join(f"SUM(TRY_CAST({c} AS BIGINT)) {c}" for c in ballot_cols)
        db.execute(f"""COPY (SELECT CD_ELEICAO,CD_CARGO,NR_TURNO,SG_UE,CD_MUNICIPIO,NR_ZONA,{select}
          FROM det_raw WHERE CD_TIPO_ELEICAO='2' GROUP BY ALL) TO '{PROCESSED / f'ballots_{year}.parquet'}' (FORMAT PARQUET)""")
        audit["ballot_fields"] = ballot_cols
    # All candidate/zone authoritative aggregates stay queryable locally.
    party_path = ingest(year, "partido", force)
    if party_path:
        query_file(db, "party_raw", party_path)
        db.execute("""CREATE OR REPLACE TABLE party_votes AS SELECT CD_ELEICAO election,
           CD_CARGO cargo,NR_TURNO turno,SG_UE ue,CD_MUNICIPIO municipio,NR_ZONA zona,
           SG_PARTIDO party,SUM(TRY_CAST(QT_VOTOS_NOMINAIS_VALIDOS AS BIGINT)) nominal,
           SUM(TRY_CAST(QT_TOTAL_VOTOS_LEG_VALIDOS AS BIGINT)) legenda
           FROM party_raw WHERE CD_TIPO_ELEICAO='2' GROUP BY ALL""")
        db.execute(f"COPY party_votes TO '{PROCESSED / f'votes_parties_{year}.parquet'}' (FORMAT PARQUET)")
    db.execute(f"COPY mun TO '{PROCESSED / f'votes_zones_{year}.parquet'}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    if has_detail:
        db.execute(f"COPY located TO '{PROCESSED / f'votes_sections_geo_{year}.parquet'}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    manifest_datasets = []
    for d in datasets:
        d["year"] = year
        did = "-".join(str(d[k]) for k in ["year", "election", "cargo", "turno", "ue"])
        d["id"] = did
        params = [d[k] for k in ["election", "cargo", "turno", "ue"]]
        candidates = db.execute("""SELECT candidate id,number,"name",full_name,party,party_number,
           SUM(votes) votes,SUM(CASE WHEN municipio='60011' THEN votes ELSE 0 END) capital_votes
           FROM mun WHERE election=? AND cargo=? AND turno=? AND ue=? AND election_type='2'
           GROUP BY ALL ORDER BY votes DESC,"name" """, params).df().to_dict("records")
        identity_rows = list(csv.DictReader((ROOT / "data/reference/candidaturas_destaque.csv").open()))
        identity = {(r["ano"],r["cargo"],r["sq_candidato"]):r["person_id"] for r in identity_rows}
        for c in candidates:
            c["person_id"] = identity.get((str(year), d["cargo"], c["id"]))
        units = db.execute("""SELECT municipio,zona,SUM(votes) denominator FROM mun
           WHERE election=? AND cargo=? AND turno=? AND ue=? AND election_type='2' GROUP BY ALL""", params).df()
        zone_rows = db.execute("""SELECT candidate,municipio,zona,SUM(votes) votes FROM mun
           WHERE election=? AND cargo=? AND turno=? AND ue=? AND election_type='2' GROUP BY ALL""", params).df()
        total = int(units.denominator.sum())
        capital_total = int(units.loc[units.municipio.eq(CAPITAL), "denominator"].sum())
        d.update({"total": total, "capital_total": capital_total, "candidate_count": len(candidates),
                  "file": f"datasets/{did}.json", "geography_status": "indisponivel"})
        payload = {"meta": d, "candidates": candidates,
                   "zones": zone_rows.to_dict("records"), "denominators": units.to_dict("records"),
                   "bairros": [], "bairro_denominators": [], "coverage": None,
                   "coverage_by_municipio": [], "parties": []}
        party = db.execute("""SELECT party,municipio,zona,SUM(votes) nominal FROM mun
           WHERE election=? AND cargo=? AND turno=? AND ue=? AND election_type='2' GROUP BY ALL""", params).df()
        payload["parties"] = party.to_dict("records")
        payload["party_complete"] = False
        if party_path:
            party_rows = db.execute("""SELECT party,municipio,zona,nominal,legenda,nominal+legenda total
               FROM party_votes WHERE election=? AND cargo=? AND turno=? AND ue=?""", params).df()
            party_denominators = db.execute("""SELECT municipio,zona,SUM(nominal+legenda) denominator
               FROM party_votes WHERE election=? AND cargo=? AND turno=? AND ue=? GROUP BY ALL""", params).df()
            payload.update({"parties": party_rows.to_dict("records"), "party_denominators": party_denominators.to_dict("records"),
                            "party_complete": not party_rows.empty})
        if geo:
            metro = {m for m, prop in geo["municipios_by_tse"].items() if prop.get("metropolitana")}
            official = db.execute("""SELECT candidate,municipio,SUM(votes) votes FROM mun
               WHERE election=? AND cargo=? AND turno=? AND ue=? AND election_type='2' GROUP BY ALL""", params).df()
            official = official.loc[official.municipio.isin(metro)].to_dict("records")
            verified = dict(db.execute("SELECT municipio,verified FROM geo_ok WHERE election=? AND cargo=? AND turno=? AND ue=?", params).fetchall()) if has_detail else {}
            statuses = {r["municipio"]: municipality_geography_status(verified.get(r["municipio"]), r["municipio"] in geo["bairro_municipios"]) for r in official}
            bybairro = db.execute("""SELECT candidate,municipio,bairro_id,SUM(votes) votes FROM located
               WHERE election=? AND cargo=? AND turno=? AND ue=? AND bairro_id IS NOT NULL GROUP BY ALL""", params).df().to_dict("records") if has_detail else []
            bybairro = [r for r in bybairro if r["municipio"] in metro]
            rows, denoms, coverage = geographic_aggregates(official, bybairro, statuses)
            payload.update({"bairros": rows, "bairro_denominators": denoms, "coverage_by_municipio": coverage})
            capital_coverage = next((c for c in coverage if c["municipio"] == CAPITAL), None)
            payload["coverage"] = capital_coverage
            state_set = {c["status"] for c in coverage}
            if state_set == {"verificada"}:
                d["geography_status"] = "verificada"
            elif "verificada" in state_set:
                d["geography_status"] = "parcial"
            elif "bloqueada_divergencia" in state_set:
                d["geography_status"] = "bloqueada_divergencia"
            elif state_set == {"sem_malha"}:
                d["geography_status"] = "sem_malha"
            audit["datasets"].append({"id": did, "status": d["geography_status"], "coverage_by_municipio": coverage})
        save_dataset(d, payload)
        manifest_datasets.append(d)
        if not geo:
            audit["datasets"].append({"id": did, "status": d["geography_status"]})
    audit["status"] = "processed"
    save_json(ROOT / f"data/manifests/audit-{year}.json", audit)
    save_json(PUBLIC / f"audit-{year}.json", audit)
    db.close()
    print(f"built {year}: {len(manifest_datasets)} disputes; {audit['nominal_reconciliation']}", flush=True)
    return {"year": year, "status": "processed", "datasets": manifest_datasets}


def export_reference(geo):
    if not geo:
        return
    from shapely.geometry import mapping
    for key, source in [("bairros", geo["capital_bairros"]), ("bairros-metro", geo["bairros"]), ("municipios", geo["municipios"])]:
        display = {"type": "FeatureCollection", "features": [
            {**f, "geometry": mapping(shape(f["geometry"]).simplify(.00005, preserve_topology=True))}
            for f in source["features"]]}
        save_json(PUBLIC / f"{key}.geojson", display)
    # Fixed geometries aggregated by official relationships, never by vote weights.
    from shapely.ops import unary_union
    from shapely.geometry import mapping
    for level in ["ra", "rp", "ap", "macro"]:
        prebuilt = ROOT / f"data/reference/display/{level}.geojson"
        if prebuilt.exists():
            shutil.copyfile(prebuilt, PUBLIC / f"{level}.geojson")
            continue
        groups = {}
        for f in geo["capital_bairros"]["features"]:
            label = str(f["properties"][level])
            groups.setdefault(label, []).append(shape(f["geometry"]))
        save_json(PUBLIC / f"{level}.geojson", {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"id": name, "nome": name}, "geometry": mapping(unary_union(gs).simplify(.00005, preserve_topology=True))}
            for name, gs in groups.items()]})
    groups = {}
    for f in geo["municipios"]["features"]:
        groups.setdefault(f["properties"]["regiao"], []).append(shape(f["geometry"]).simplify(.00005, preserve_topology=True))
    save_json(PUBLIC / "regiao.geojson", {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": name, "nome": name}, "geometry": mapping(unary_union(gs).simplify(.00005, preserve_topology=True))}
        for name, gs in groups.items()]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", nargs="+", type=int, default=[2018, 2020, 2022, 2024, 2026])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    PROCESSED.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(parents=True, exist_ok=True)
    geo = geography()
    export_reference(geo)
    save_json(PUBLIC / "people.json", list(csv.DictReader((ROOT / "data/reference/candidaturas_destaque.csv").open())))
    completed = []
    for year in args.years:
        completed.append(build_year(year, geo, args.force))
    # Preserve previously processed years when running a subset.
    old_path = PUBLIC / "catalog.json"
    old = json.loads(old_path.read_text()) if old_path.exists() else {"years": []}
    by_year = {y["year"]: y for y in old["years"]}
    by_year.update({y["year"]: y for y in completed})
    for year in [2018, 2020, 2022, 2024, 2026]:
        by_year.setdefault(year, {"year": year, "status": "fontes_indisponiveis", "datasets": []})
    sources_path = ROOT / "data/manifests/sources.json"
    sources = json.loads(sources_path.read_text()) if sources_path.exists() else {}
    save_json(PUBLIC / "catalog.json", {"schema_version": 2, "generated_at": datetime.now(timezone.utc).isoformat(),
         "years": sorted(by_year.values(), key=lambda y: y["year"]), "capital": CAPITAL,
         "territory_reference": "Snapshot IPP/Data.Rio e IBGE fixado na execução; consulte fontes",
         "default": {"candidate": "EDUARDO PAES", "a": 2024, "b": 2026, "turno": "1"}})
    # Remove absolute local paths from public provenance.
    def sanitize(obj):
        if isinstance(obj, dict):
            return {k: sanitize(v) for k, v in obj.items() if k not in ["preserved_original", "invalid_preserved_original", "error"]}
        if isinstance(obj, list):
            return [sanitize(v) for v in obj]
        if isinstance(obj, str) and obj.startswith(("/home/", "/tmp/")):
            return Path(obj).name
        return obj
    save_json(PUBLIC / "sources.json", sanitize(sources))
    gp = ROOT / "data/reference/geography_sources.json"
    if gp.exists():
        save_json(PUBLIC / "geography-sources.json", sanitize(json.loads(gp.read_text())))
    mp = ROOT / "data/reference/metro_geography_sources.json"
    if mp.exists():
        save_json(PUBLIC / "metro_geography_sources.json", sanitize(json.loads(mp.read_text())))


if __name__ == "__main__":
    main()
