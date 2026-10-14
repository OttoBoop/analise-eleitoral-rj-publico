#!/usr/bin/env python3
"""Independently audit original TSE rows and evidence for geographic gaps.

Does not import pipeline.py, ingest, estimate votes, or replace coordinates.
The address evidence is deliberately separate from vote reconciliation and
polygon containment: an inside-polygon coordinate does not certify a building.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import heapq
import io
import json
import math
import tempfile
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
YEARS = (2018, 2020, 2022, 2024, 2026)
CAPITAL = "60011"
KEY = ("election", "cargo", "turno", "ue", "municipio", "candidate")
SME_URL = "https://pgeo3.rio.rj.gov.br/arcgis/rest/services/Educacao/SME/MapServer/1"
MPRJ_URL = "https://geo.mprj.mp.br/portal/home/item.html?id=6857463ae04d43219090d227ec0d41c2"


def write_json(path, value, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False,
                               **({"separators": (",", ":")} if compact else {"indent": 2})) + "\n")


def sha256_file(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def normalize_name(value):
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().upper()
    return " ".join("".join(c if c.isalnum() else " " for c in value).split())


def school_key(value):
    """Full name AND school type; creches and CIEPs must never be merged."""
    value = normalize_name(value)
    replacements = (("E D I ", "EDI "), ("E M ", "ESCOLA MUNICIPAL "),
                    ("ESPACO DE DESENVOLVIMENTO INFANTIL ", "EDI "))
    for prefix, expanded in replacements:
        if value.startswith(prefix):
            return expanded + value[len(prefix):]
    return value


def coordinate(lat, lon):
    try:
        lat, lon = float(str(lat).replace(",", ".")), float(str(lon).replace(",", "."))
    except (ValueError, TypeError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180) or (lat, lon) == (0, 0):
        return None
    return Point(lon, lat)


class IndependentMesh:
    """Independent raw-coordinate classification, including municipal checks."""
    def __init__(self, root):
        self.features = json.loads((root / "data/reference/bairros-metro.geojson").read_text())["features"]
        self.geometries = [shape(f["geometry"]) for f in self.features]
        self.tree = STRtree(self.geometries)
        municipalities = json.loads((root / "data/reference/municipios.geojson").read_text())["features"]
        self.municipalities = {str(f["properties"]["tse"]): f["properties"] for f in municipalities}
        self.municipality_geometry = {str(f["properties"]["tse"]): shape(f["geometry"]) for f in municipalities}
        self.metro = {m for m, p in self.municipalities.items() if p["metropolitana"]}
        self.by_municipality = defaultdict(list)
        self.by_id = {}
        for index, f in enumerate(self.features):
            self.by_municipality[str(f["properties"]["municipio"])].append(index)
            self.by_id[str(f["properties"]["id"])] = f["properties"]

    def classify(self, municipio, lat, lon):
        point = coordinate(lat, lon)
        if point is None:
            return None, "coordenada_ausente_ou_invalida"
        municipality = self.municipality_geometry.get(municipio)
        if municipality is None or not municipality.covers(point):
            return None, "fora_municipio"
        if not self.by_municipality.get(municipio):
            return None, "sem_malha_bairros"
        ids = [str(self.features[int(i)]["properties"]["id"]) for i in self.tree.query(point)
               if str(self.features[int(i)]["properties"]["municipio"]) == municipio
               and self.geometries[int(i)].covers(point)]
        return (ids[0], "ponto_no_poligono_sem_certificacao_endereco") if len(ids) == 1 else (
            None, "bairro_ambiguo" if ids else "fora_bairros")


class CanonicalMultiset:
    """SHA256(sorted(SHA256(canonical full row))) with bounded-memory sorting.

    Each 32-byte row digest is kept, including repetitions. Length-prefixed
    UTF-8 field values prevent ambiguous concatenations. Codes and spelling are
    preserved exactly; no geographic or electoral normalization happens here.
    """
    def __init__(self, folder, block_rows=100_000):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.block_rows = block_rows
        self.buffer = []
        self.paths = []
        self.count = 0

    @staticmethod
    def digest_row(values):
        hasher = hashlib.sha256()
        for value in values:
            raw = str(value if value is not None else "").encode("utf-8")
            hasher.update(len(raw).to_bytes(8, "big"))
            hasher.update(raw)
        return hasher.digest()

    def add(self, values):
        self.buffer.append(self.digest_row(values))
        self.count += 1
        if len(self.buffer) >= self.block_rows:
            self.flush()

    def flush(self):
        if self.buffer:
            path = self.folder / f"{len(self.paths)}.bin"
            self.buffer.sort()
            path.write_bytes(b"".join(self.buffer))
            self.paths.append(path)
            self.buffer.clear()

    @staticmethod
    def values(stream):
        for value in iter(lambda: stream.read(32), b""):
            if len(value) != 32:
                raise ValueError("Truncated canonical digest block")
            yield value

    def finish(self):
        self.flush()
        hasher = hashlib.sha256()
        streams = [path.open("rb") for path in self.paths]
        try:
            pending = bytearray()
            for value in heapq.merge(*(self.values(stream) for stream in streams)):
                pending.extend(value)
                if len(pending) >= 1024 * 1024:
                    hasher.update(pending)
                    pending.clear()
            hasher.update(pending)
        finally:
            for stream in streams:
                stream.close()
            for path in self.paths:
                path.unlink()
        return {"rows": self.count, "sha256_sorted_canonical_row_hashes": hasher.hexdigest()}


def raw_paths(root, year, kind):
    names = ([f"munzona_{year}.zip"] if kind == "munzona" else
             [f"secao_{year}_RJ.zip"] + ([f"secao_{year}_BR.zip"] if year in (2018, 2022, 2026) else [])
             if kind == "secao" else [f"{kind}_{year}.zip"])
    return [root / "data/raw" / name for name in names]


def selected_members(archive, year, kind):
    members = [m for m in archive.namelist() if m.lower().endswith(".csv")]
    if kind == "munzona":
        return [m for m in members if m.endswith(f"{year}_RJ.csv") or
                (year in (2018, 2022, 2026) and m.endswith(f"{year}_BR.csv"))]
    if any(m.endswith("_RJ.csv") for m in members):
        return [m for m in members if m.endswith("_RJ.csv") or m.endswith("_BR.csv")]
    return members


def audit_raw_multiset(root, year, kind, cache=True):
    parquet = root / f"data/processed/{kind}_{year}.parquet"
    paths = raw_paths(root, year, kind)
    if not parquet.exists() or not all(p.exists() for p in paths):
        return {"year": year, "kind": kind, "status": "fonte_indisponivel"}
    fingerprint = {"parquet_sha256": sha256_file(parquet), "sources": [
        {"file": p.name, "sha256": sha256_file(p)} for p in paths]}
    target = root / f"data/manifests/independent-raw/{year}-{kind}.json"
    if cache and target.exists():
        previous = json.loads(target.read_text())
        if previous.get("fingerprint") == fingerprint:
            return previous
    print(f"independent raw {year}/{kind}: hashing full original rows", flush=True)
    start = time.monotonic()
    schema = pq.read_schema(parquet)
    columns = schema.names
    with tempfile.TemporaryDirectory(prefix="rj-canonical-audit-") as temporary:
        original = CanonicalMultiset(Path(temporary) / "zip")
        for path in paths:
            with zipfile.ZipFile(path) as archive:
                for member in selected_members(archive, year, kind):
                    with archive.open(member) as stream:
                        reader = csv.reader(io.TextIOWrapper(stream, encoding="latin-1", newline=""), delimiter=";")
                        header = next(reader)
                        uf_index = header.index("SG_UF")
                        indices = [header.index(col) if col in header else None for col in columns]
                        for row in reader:
                            if not row or row[uf_index] != "RJ":
                                continue
                            original.add([member if col == "SOURCE_MEMBER" else row[index] if index is not None else ""
                                          for col, index in zip(columns, indices)])
        original_digest = original.finish()
        treated = CanonicalMultiset(Path(temporary) / "parquet")
        for batch in pq.ParquetFile(parquet).iter_batches(batch_size=20_000):
            arrays = [batch.column(i).to_pylist() for i in range(batch.num_columns)]
            for row in zip(*arrays):
                treated.add(row)
        parquet_digest = treated.finish()
    result = {"year": year, "kind": kind, "checked_at": datetime.now(timezone.utc).isoformat(),
              "method": "SHA256 of externally sorted SHA256 length-prefixed full UTF-8 rows; duplicates retained; RJ filtered; SOURCE_MEMBER retained",
              "columns": columns, "fingerprint": fingerprint, "original": original_digest,
              "parquet": parquet_digest, "status": "verificada" if original_digest == parquet_digest else "divergencia",
              "elapsed_seconds": round(time.monotonic() - start, 2)}
    write_json(target, result)
    print(f"independent raw {year}/{kind}: {result['status']} {original_digest['rows']:,} rows", flush=True)
    return result


def raw_locations(db, root, year):
    path = root / f"data/processed/locais_{year}.parquet"
    municipalities = root / f"data/processed/munzona_{year}.parquet"
    return db.execute("""SELECT DISTINCT NR_TURNO turno,CD_MUNICIPIO municipio,NR_ZONA zona,
        NR_LOCAL_VOTACAO AS "local",NM_LOCAL_VOTACAO nome,DS_ENDERECO endereco,NM_BAIRRO bairro_informado,
        NR_LATITUDE lat,NR_LONGITUDE lon FROM read_parquet(?)
        WHERE DT_ELEICAO IN (SELECT DISTINCT DT_ELEICAO FROM read_parquet(?) WHERE CD_TIPO_ELEICAO='2')""",
        [str(path), str(municipalities)]).df().to_dict("records")


def compare_schools(mesh, raw, schools):
    by_name = defaultdict(list)
    for feature in schools["features"]:
        by_name[school_key(feature["properties"]["denominacao"])].append(feature)
    result = {"exact_name_and_type_matches": 0, "coordinate_conflicts": []}
    seen = set()
    for row in raw:
        key = (row["zona"], row["local"], row["nome"], row["lat"], row["lon"])
        if row["municipio"] != CAPITAL or key in seen:
            continue
        seen.add(key)
        matches = by_name.get(school_key(row["nome"]), [])
        if len(matches) != 1:
            continue
        result["exact_name_and_type_matches"] += 1
        f = matches[0]
        lon, lat = f["geometry"]["coordinates"]
        tse_id, _ = mesh.classify(CAPITAL, row["lat"], row["lon"])
        sme_id, _ = mesh.classify(CAPITAL, lat, lon)
        if tse_id != sme_id:
            point = coordinate(row["lat"], row["lon"])
            result["coordinate_conflicts"].append({
                "zona": row["zona"], "local": row["local"], "nome": row["nome"], "endereco_tse": row["endereco"],
                "bairro_tse": tse_id, "bairro_sme": sme_id,
                "coordenada_tse": [point.x, point.y] if point else None,
                "coordenada_sme": [lon, lat], "nome_sme": f["properties"]["denominacao"],
                "status": "revisao_pendente", "correcao_aplicada": False,
                "motivo": "Nome completo e tipo coincidem; fonte SME de junho/2023 não comprova endereço efetivo na eleição nem a entrada usada. Não substituir automaticamente."})
    return result


def compare_original_with_primary_door(mesh, raw, door, declared_bairro=None):
    """A primary point in a bairro cannot corroborate an original point elsewhere."""
    lon, lat = door["geometry"]["coordinates"]
    original_id, original_status = mesh.classify(raw["municipio"], raw["lat"], raw["lon"])
    primary_id, primary_status = mesh.classify(raw["municipio"], lat, lon)
    original = coordinate(raw["lat"], raw["lon"])
    return {"original_coordinate": [original.x, original.y] if original else None,
            "original_bairro": original_id, "original_coordinate_status": original_status,
            "primary_coordinate": [lon, lat], "primary_bairro": primary_id,
            "primary_coordinate_status": primary_status,
            "declared_corroborated_bairro": declared_bairro,
            "same_name_and_type": school_key(raw["nome"]) == school_key(door["properties"]["nome_grupo"]),
            "same_bairro": bool(original_id and original_id == primary_id),
            "corroboration_valid": bool(original_id and original_id == primary_id == declared_bairro)
                                    if declared_bairro is not None else None}


def audit_primary_corroborations(root, year, mesh, raw):
    path = root / "data/review/locais_fontes_conferidas.csv"
    records = [r for r in csv.DictReader(path.open()) if int(r["ano"]) == year] if path.exists() else []
    door_path = root / "data/reference/evidence/cadlog-portas-escolas.geojson"
    doors = json.loads(door_path.read_text())["features"]
    by_name = defaultdict(list)
    for door in doors:
        by_name[school_key(door["properties"]["nome_grupo"])].append(door)
    checks, failures = [], []
    for record in records:
        matched_rows = [r for r in raw if tuple(r[k] for k in ("municipio", "zona", "local")) ==
                        tuple(record[k] for k in ("municipio", "zona", "local"))]
        if not matched_rows:
            failures.append({"zona": record["zona"], "local": record["local"],
                             "reason": "registro_original_nao_identificado"})
        for row in matched_rows:
            primary = by_name.get(school_key(row["nome"]), [])
            if len(primary) != 1:
                failures.append({"zona": record["zona"], "local": record["local"],
                                 "turno": row["turno"], "reason": "porta_nome_tipo_nao_identificada_unicamente"})
                continue
            check = {"municipio": row["municipio"], "zona": row["zona"], "local": row["local"],
                     "turno": row["turno"], "name": row["nome"], "original_address": row["endereco"],
                     "primary_door_id": primary[0]["properties"]["objectid"],
                     **compare_original_with_primary_door(mesh, row, primary[0], record["bairro_id"])}
            checks.append(check)
            if not check["corroboration_valid"] or not check["same_name_and_type"]:
                failures.append({**check, "reason": "corroboracao_contradiz_coordenada_original_ou_porta"})
    # Inspect all five years for the three schools originally claimed as preserved.
    known_doors = {("191", "1201"): "ESCOLA MUNICIPAL CUBA",
                   ("123", "1198"): "ESCOLA MUNICIPAL NORONHA SANTOS",
                   ("246", "1074"): "ESCOLA MUNICIPAL FRANCISCO CALDEIRA DE ALVARENGA"}
    original_checks = []
    for row in raw:
        name = known_doors.get((row["zona"], row["local"])) if row["municipio"] == CAPITAL else None
        primary = by_name.get(school_key(name), []) if name else []
        if len(primary) == 1:
            original_checks.append({"municipio": row["municipio"], "zona": row["zona"], "local": row["local"],
                                    "turno": row["turno"], "name": row["nome"],
                                    "original_address": row["endereco"],
                                    "primary_name": primary[0]["properties"]["nome_grupo"],
                                    "primary_door_id": primary[0]["properties"]["objectid"],
                                    **compare_original_with_primary_door(mesh, row, primary[0])})
    return {"year": year, "status": "verificada" if not failures else "divergencia", "checks": checks,
            "failures": failures, "original_school_point_checks": original_checks,
            "original_source": {"file": f"locais_{year}.parquet",
                                "sha256": sha256_file(root / f"data/processed/locais_{year}.parquet")},
            "primary_source": {"file": door_path.name, "sha256": sha256_file(door_path)},
            "scope": "Conservação exige igualdade numérica do bairro da coordenada original com o da porta primária comprovada. Contenção sozinha não certifica a identidade escolar histórica."}


def votes_at_locations(db, root, year):
    """Quantify actual votes attached to a disputed point, never relocate them."""
    path = root / f"data/processed/votes_sections_geo_{year}.parquet"
    columns = {r[0] for r in db.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()}
    effective = 'COALESCE(effective_local,"local")' if "effective_local" in columns else '"local"'
    values = db.execute(f"""SELECT zona,{effective} AS "local",election,cargo,turno,ue,
        SUM(votes)::BIGINT votes,COUNT(DISTINCT secao) sections
        FROM read_parquet(?) WHERE municipio=? AND verified GROUP BY ALL""", [str(path), CAPITAL]).df().to_dict("records")
    lookup = defaultdict(list)
    for row in values:
        lookup[(row["zona"], row.pop("local"))].append(row)
    return lookup


def independently_classified_coverage(db, root, year, mesh):
    raw = raw_locations(db, root, year)
    grouped = defaultdict(list)
    for row in raw:
        if row["municipio"] in mesh.metro:
            bairro, status = mesh.classify(row["municipio"], row["lat"], row["lon"])
            grouped[(row["turno"], row["municipio"])].append({**row, "bairro_id": bairro, "status": status})
    sections = dict(((str(turno), str(municipio)), int(count)) for turno, municipio, count in db.execute(
        """SELECT NR_TURNO,CD_MUNICIPIO,COUNT(DISTINCT (NR_ZONA,NR_SECAO)) FROM read_parquet(?)
        GROUP BY ALL""", [str(root / f"data/processed/locais_{year}.parquet")]).fetchall())
    turnos = sorted({key[0] for key in grouped})
    rows = []
    for turno in turnos:
        for municipio in sorted(mesh.metro):
            locations = grouped[(turno, municipio)]
            count = Counter(l["status"] for l in locations)
            ids = {l["bairro_id"] for l in locations if l["bairro_id"]}
            rows.append({"year": year, "turno": turno, "municipio": municipio,
                         "municipality_name": mesh.municipalities[municipio]["nome"],
                         "raw_locations": len(locations), "raw_sections": sections.get((turno, municipio), 0),
                         "mesh_neighborhoods": len(mesh.by_municipality[municipio]),
                         "neighborhoods_with_raw_points": len(ids),
                         "raw_coordinate_status": dict(count),
                         "address_validation": "nao_certificada_individualmente"})
    return raw, rows


def audit_processed_coordinates(db, root, year, mesh):
    rows = db.execute("""SELECT DISTINCT municipio,zona,"local",turno,bairro_id,geo_status,lat,lon
        FROM read_parquet(?)""", [str(root / f"data/processed/locations_{year}.parquet")]).df().to_dict("records")
    failures = []
    checked = 0
    correction_count = 0
    corrections = {(str(r["turno"]), str(r["municipio"]), str(r["zona"]), str(r["local"])): r
                   for r in csv.DictReader((root / "data/review/correcoes_locais.csv").open())
                   if str(r["ano"]) == str(year)}
    for row in rows:
        if row["municipio"] not in mesh.metro:
            continue
        checked += 1
        barrio = row["bairro_id"] if isinstance(row["bairro_id"], str) else None
        expected, status = mesh.classify(row["municipio"], row["lat"], row["lon"])
        correction_count += row["geo_status"] == "correcao_documentada"
        if barrio != expected:
            failures.append({"municipio": row["municipio"], "zona": row["zona"], "local": row["local"],
                             "turno": row["turno"], "processed_bairro": barrio, "independent_bairro": expected,
                             "independent_coordinate_status": status})
        correction = corrections.get(tuple(str(row[k]) for k in ("turno", "municipio", "zona", "local")))
        if correction and correction.get("latitude") and correction.get("longitude"):
            point = coordinate(correction["latitude"], correction["longitude"])
            actual = coordinate(row["lat"], row["lon"])
            if actual is None or point is None or actual.distance(point) > 1e-9:
                failures.append({"municipio": row["municipio"], "zona": row["zona"], "local": row["local"],
                                 "turno": row["turno"], "reason": "correcao_documentada_nao_aplicada"})
    return {"year": year, "metropolitan_local_turns_checked": checked,
            "documented_coordinates": correction_count, "status": "verificada" if not failures else "divergencia",
            "failures": failures,
            "scope": "Reclassificação independente das coordenadas tratadas na malha atual; não certifica a entrada/prédio."}


def audit_vote_conservation(db, root, year, metro):
    """Original official candidate totals vs all classified sections, all candidates."""
    raw_path = str(root / f"data/processed/munzona_{year}.parquet")
    sections_path = str(root / f"data/processed/votes_sections_geo_{year}.parquet")
    fields = ",".join(KEY)
    db.execute("""CREATE OR REPLACE TEMP TABLE official AS SELECT CD_ELEICAO election,
        CD_CARGO cargo,NR_TURNO turno,SG_UE ue,CD_MUNICIPIO municipio,SQ_CANDIDATO candidate,
        SUM(TRY_CAST(QT_VOTOS_NOMINAIS_VALIDOS AS BIGINT))::BIGINT votes
        FROM read_parquet(?) WHERE CD_TIPO_ELEICAO='2' GROUP BY ALL""", [raw_path])
    db.execute(f"""CREATE OR REPLACE TEMP TABLE classified AS SELECT {fields},
        SUM(votes)::BIGINT votes,
        SUM(CASE WHEN bairro_id IS NOT NULL THEN votes ELSE 0 END)::BIGINT located,
        SUM(CASE WHEN bairro_id IS NULL THEN votes ELSE 0 END)::BIGINT unlocated,
        COUNT(*) AS "rows",COUNT(DISTINCT (zona,secao,"local")) section_keys,
        BOOL_AND(verified) verified FROM read_parquet(?) GROUP BY ALL""", [sections_path])
    db.execute("""CREATE OR REPLACE TEMP TABLE official_zones AS SELECT CD_ELEICAO election,
        CD_CARGO cargo,NR_TURNO turno,SG_UE ue,CD_MUNICIPIO municipio,SQ_CANDIDATO candidate,NR_ZONA zona,
        SUM(TRY_CAST(QT_VOTOS_NOMINAIS_VALIDOS AS BIGINT))::BIGINT votes
        FROM read_parquet(?) WHERE CD_TIPO_ELEICAO='2' GROUP BY ALL""", [raw_path])
    db.execute(f"""CREATE OR REPLACE TEMP TABLE classified_neighborhoods AS SELECT {fields},bairro_id,
        SUM(votes)::BIGINT votes FROM read_parquet(?) WHERE verified GROUP BY ALL""", [sections_path])
    section_columns = {r[0] for r in db.execute("DESCRIBE SELECT * FROM read_parquet(?)", [sections_path]).fetchall()}
    if "local_id" in section_columns:
        db.execute(f"""CREATE OR REPLACE TEMP TABLE classified_locals AS SELECT {fields},zona,local_id,
            SUM(votes)::BIGINT votes FROM read_parquet(?) WHERE verified AND local_id IS NOT NULL GROUP BY ALL""", [sections_path])
    else:
        db.execute(f"""CREATE OR REPLACE TEMP TABLE classified_locals AS SELECT {fields},zona,
            CAST(NULL AS VARCHAR) local_id,votes FROM read_parquet(?) WHERE false""", [sections_path])
    failures = db.execute(f"""SELECT {fields},o.votes official_votes,c.votes classified_votes,c.located,c.unlocated,
        c."rows",c.section_keys,c.verified FROM official o FULL JOIN classified c USING({fields})
        WHERE o.votes IS NULL OR COALESCE(c.votes,0)<>o.votes OR COALESCE(c.located,0)+COALESCE(c.unlocated,0)<>o.votes
        OR COALESCE(c."rows",0)<>COALESCE(c.section_keys,0) OR c.verified=false""").df().to_dict("records")
    for row in failures:
        for k, v in row.items():
            if isinstance(v, float) and math.isnan(v):
                row[k] = None
    coverage = db.execute(f"""SELECT election,cargo,turno,ue,municipio,SUM(o.votes)::BIGINT total,
        SUM(COALESCE(c.located,0))::BIGINT located,SUM(COALESCE(c.unlocated,0))::BIGINT unlocated,
        COUNT(*) candidate_municipality_records FROM official o LEFT JOIN classified c USING({fields})
        GROUP BY ALL ORDER BY election,cargo,turno,ue,municipio""").df().to_dict("records")
    reason_rows = db.execute("""SELECT election,cargo,turno,ue,municipio,
        COALESCE(geo_status,'ligacao_local_pendente') reason,SUM(votes)::BIGINT votes,
        COUNT(DISTINCT (zona,secao)) sections
        FROM read_parquet(?) WHERE bairro_id IS NULL GROUP BY ALL""", [sections_path]).df().to_dict("records")
    reason_by_key = defaultdict(list)
    for row in reason_rows:
        if row["votes"]:
            reason_by_key[tuple(row[k] for k in KEY[:-1])].append({k: row[k] for k in ("reason", "votes", "sections")})
    metropolitan = []
    for row in coverage:
        if row["municipio"] not in metro:
            continue
        reasons = reason_by_key.get(tuple(row[k] for k in KEY[:-1]), [])
        row["unlocated_reasons"] = reasons
        row["votes_conserved"] = row["total"] == row["located"] + row["unlocated"]
        metropolitan.append(row)
    return {"year": year, "candidate_municipality_records": db.execute("SELECT COUNT(*) FROM official").fetchone()[0],
            "status": "verificada" if not failures else "divergencia", "failures": failures,
            "metropolitan_contests": metropolitan}


def metropolitan_local_reviews(db, root, year, mesh):
    path = root / f"data/processed/votes_sections_geo_{year}.parquet"
    columns = {r[0] for r in db.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()}
    effective = 'COALESCE(effective_local,"local")' if "effective_local" in columns else '"local"'
    rows = db.execute(f"""SELECT municipio,zona,{effective} AS "local",local_name AS "name",address,lat,lon,
        COALESCE(geo_status,'ligacao_local_pendente') reason,election,cargo,turno,ue,
        SUM(votes)::BIGINT votes,COUNT(DISTINCT secao) sections
        FROM read_parquet(?) WHERE verified AND bairro_id IS NULL GROUP BY ALL""", [str(path)]).df().to_dict("records")
    grouped = {}
    for row in rows:
        if row["municipio"] not in mesh.metro or not row["votes"]:
            continue
        for key, value in row.items():
            if isinstance(value, float) and math.isnan(value):
                row[key] = None
        key = (row["municipio"], row["zona"], row["local"], row["turno"], row["name"], row["address"], row["reason"])
        if key not in grouped:
            grouped[key] = {k: row[k] for k in ("municipio", "zona", "local", "turno", "name", "address", "lat", "lon", "reason")}
            grouped[key].update({"year": year, "municipality_name": mesh.municipalities[row["municipio"]]["nome"],
                                 "status": "revisao_pendente", "votes_by_contest": []})
        grouped[key]["votes_by_contest"].append({k: row[k] for k in ("election", "cargo", "turno", "ue", "votes", "sections")})
    return sorted(grouped.values(), key=lambda row: (row["municipio"], row["zona"], row["local"], row["turno"]))


def correction_impacts(db, root, year, mesh):
    """Actual candidate votes that move from the original point to a correction."""
    import pyarrow as pa
    fixes = [r for r in csv.DictReader((root / "data/review/correcoes_locais.csv").open())
             if int(r["ano"]) == year and r["municipio"] in mesh.metro]
    if not fixes:
        return []
    db.register("correction_keys", pa.Table.from_pylist(fixes))
    path = root / f"data/processed/votes_sections_geo_{year}.parquet"
    columns = {r[0] for r in db.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()}
    effective = 'COALESCE(s.effective_local,s."local")' if "effective_local" in columns else 's."local"'
    rows = db.execute(f"""SELECT s.municipio,s.zona,{effective} AS "local",s.turno,s.election,s.cargo,s.ue,
        s.candidate,s.name,SUM(s.votes)::BIGINT votes FROM read_parquet(?) s JOIN correction_keys f
        ON s.municipio=f.municipio AND s.zona=f.zona AND s.turno=f.turno AND {effective}=f.local
        WHERE s.verified GROUP BY ALL""", [str(path)]).df().to_dict("records")
    identities = {(r["ano"], r["cargo"], r["sq_candidato"]): r["person_id"] for r in
                  csv.DictReader((root / "data/reference/candidaturas_destaque.csv").open())}
    by_location = defaultdict(list)
    for row in rows:
        by_location[tuple(row[k] for k in ("turno", "municipio", "zona", "local"))].append(row)
    impacts = []
    for fix in fixes:
        key = tuple(fix[k] for k in ("turno", "municipio", "zona", "local"))
        original_points = db.execute("""SELECT DISTINCT NR_LATITUDE,NR_LONGITUDE FROM read_parquet(?)
            WHERE NR_TURNO=? AND CD_MUNICIPIO=? AND NR_ZONA=? AND NR_LOCAL_VOTACAO=?""",
            [str(root / f"data/processed/locais_{year}.parquet"), *key]).fetchall()
        previous = {mesh.classify(fix["municipio"], lat, lon)[0] for lat, lon in original_points}
        old_id = next(iter(previous)) if len(previous) == 1 else None
        new_id, _ = mesh.classify(fix["municipio"], fix["latitude"], fix["longitude"])
        contests = {}
        for row in by_location[key]:
            contest_key = tuple(row[k] for k in ("election", "cargo", "turno", "ue"))
            entry = contests.setdefault(contest_key, {k: row[k] for k in ("election", "cargo", "turno", "ue")})
            entry["nominal_valid_votes"] = entry.get("nominal_valid_votes", 0) + row["votes"]
            entry["candidates"] = entry.get("candidates", 0) + 1
            person = identities.get((str(year), row["cargo"], row["candidate"]))
            if person:
                entry.setdefault("highlight_votes", []).append({"person_id": person,
                    "candidate": row["candidate"], "name": row["name"], "votes": row["votes"]})
        impacts.append({"year": year, **dict(zip(("turno", "municipio", "zona", "local"), key)),
                        "from_bairro": old_id, "to_bairro": new_id,
                        "change": "recuperado_de_pendencia" if old_id is None and new_id else
                                  "mudanca_de_bairro" if old_id != new_id else "mesmo_bairro",
                        "original_point_bairro_ambiguous": len(previous) > 1,
                        "contests": list(contests.values()),
                        "source": fix["fonte"], "definition": "Votos nominais válidos por cargo/turno; não somar cargos como eleitores."})
    db.unregister("correction_keys")
    return impacts


def audit_public_products(root, year, db):
    """Public shards vs original valid-vote totals, preserving individual candidates."""
    folder = root / "web/dist/data"
    files = sorted((folder / "datasets").glob(f"{year}-*.json"))
    counts = Counter()
    failures = []
    for path in files:
        data = json.loads(path.read_text())
        meta = data["meta"]
        expected = {tuple(map(str, (candidate, municipio))): int(votes) for candidate, municipio, votes in db.execute(
            "SELECT candidate,municipio,votes FROM official WHERE election=? AND cargo=? AND turno=? AND ue=?",
            [meta[k] for k in ("election", "cargo", "turno", "ue")]).fetchall()}
        expected_zones = {(str(candidate), str(municipio), str(zona)): int(votes) for candidate, municipio, zona, votes in db.execute(
            "SELECT candidate,municipio,zona,votes FROM official_zones WHERE election=? AND cargo=? AND turno=? AND ue=?",
            [meta[k] for k in ("election", "cargo", "turno", "ue")]).fetchall()}
        status_by_municipality = {r["municipio"]: r["status"] for r in data.get("coverage_by_municipio", [])}
        expected_neighborhoods = Counter()
        for candidate, municipio, bairro, votes in db.execute(
                "SELECT candidate,municipio,bairro_id,votes FROM classified_neighborhoods WHERE election=? AND cargo=? AND turno=? AND ue=? AND bairro_id IS NOT NULL",
                [meta[k] for k in ("election", "cargo", "turno", "ue")]).fetchall():
            if status_by_municipality.get(municipio) == "verificada":
                expected_neighborhoods[(str(candidate), str(municipio), str(bairro))] += int(votes)
        localized_by_candidate = Counter()
        for (candidate, municipio, _), votes in expected_neighborhoods.items():
            localized_by_candidate[(candidate, municipio)] += votes
        for (candidate, municipio), votes in expected.items():
            if municipio in status_by_municipality:
                expected_neighborhoods[(candidate, municipio, None)] = votes - localized_by_candidate[(candidate, municipio)]
        local_statuses = {r["municipio"]: r["status"] for r in data.get("local_status_by_municipio", [])}
        expected_locals = Counter()
        for candidate, municipio, zona, local_id, votes in db.execute(
                "SELECT candidate,municipio,zona,local_id,votes FROM classified_locals WHERE election=? AND cargo=? AND turno=? AND ue=?",
                [meta[k] for k in ("election", "cargo", "turno", "ue")]).fetchall():
            if local_statuses.get(municipio) == "verificada":
                expected_locals[(str(candidate), str(municipio), str(zona), str(local_id))] += int(votes)
        assigned_by_zone = Counter()
        for (candidate, municipio, zona, _), votes in expected_locals.items():
            assigned_by_zone[(candidate, municipio, zona)] += votes
        if "local_denominators" in data:
            for (candidate, municipio, zona), votes in expected_zones.items():
                expected_locals[(candidate, municipio, zona, None)] = votes - assigned_by_zone[(candidate, municipio, zona)]
        zones = Counter()
        neighborhoods = Counter()
        locals_votes = Counter()
        loaded = set()
        for candidate in data["candidates"]:
            filename = candidate.get("file")
            if filename in loaded:
                continue
            loaded.add(filename)
            shard = json.loads(gzip.decompress((folder / filename).read_bytes())) if filename else {
                "all": {"zones": data["zones"], "bairros": data["bairros"]}}
            for payload in shard.values():
                for row in payload["zones"]:
                    zones[(str(row["candidate"]), str(row["municipio"]), str(row["zona"]))] += int(row["votes"])
                for row in payload["bairros"]:
                    neighborhoods[(str(row["candidate"]), str(row["municipio"]), row["bairro_id"])] += int(row["votes"])
                for row in payload.get("locals", []):
                    locals_votes[(str(row["candidate"]), str(row["municipio"]), str(row["zona"]), row["local_id"])] += int(row["votes"])
        for key in expected_zones.keys() | zones.keys():
            if key not in expected_zones or zones[key] != expected_zones[key]:
                failures.append({"contest": meta["id"], "candidate": key[0], "municipio": key[1],
                                 "zona": key[2], "kind": "zona", "expected": expected_zones.get(key), "public": zones[key]})
        for key in expected_neighborhoods.keys() | neighborhoods.keys():
            if key not in expected_neighborhoods or neighborhoods[key] != expected_neighborhoods[key]:
                failures.append({"contest": meta["id"], "candidate": key[0], "municipio": key[1],
                                 "bairro": key[2], "kind": "bairro", "expected": expected_neighborhoods.get(key), "public": neighborhoods[key]})
        expected_zone_denominators = Counter()
        for (_, municipio, zona), votes in expected_zones.items():
            expected_zone_denominators[(municipio, zona)] += votes
        for row in data.get("denominators", []):
            key = (str(row["municipio"]), str(row["zona"]))
            if int(row["denominator"]) != expected_zone_denominators[key]:
                failures.append({"contest": meta["id"], "municipio": key[0], "zona": key[1], "kind": "denominador_zona"})
        expected_neighborhood_denominators = Counter()
        for (_, municipio, bairro), votes in expected_neighborhoods.items():
            expected_neighborhood_denominators[(municipio, bairro)] += votes
        for row in data.get("bairro_denominators", []):
            key = (str(row["municipio"]), row["bairro_id"])
            if int(row["denominator"]) != expected_neighborhood_denominators[key]:
                failures.append({"contest": meta["id"], "municipio": key[0], "bairro": key[1], "kind": "denominador_bairro"})
        for key in expected_locals.keys() | locals_votes.keys():
            if key not in expected_locals or locals_votes[key] != expected_locals[key]:
                failures.append({"contest": meta["id"], "candidate": key[0], "municipio": key[1],
                                 "zona": key[2], "local_id": key[3], "kind": "local",
                                 "expected": expected_locals.get(key), "public": locals_votes[key]})
        expected_local_denominators = Counter()
        for (_, municipio, zona, local_id), votes in expected_locals.items():
            expected_local_denominators[(municipio, zona, local_id)] += votes
        for row in data.get("local_denominators", []):
            key = (str(row["municipio"]), str(row["zona"]), row["local_id"])
            if int(row["denominator"]) != expected_local_denominators[key]:
                failures.append({"contest": meta["id"], "municipio": key[0], "zona": key[1],
                                 "local_id": key[2], "kind": "denominador_local"})
        counts["contests"] += 1
        counts["candidate_municipality_records"] += len(expected)
        if sum(expected.values()) != meta["total"] or sum(int(c["votes"]) for c in data["candidates"]) != meta["total"]:
            failures.append({"contest": meta["id"], "kind": "total"})
    return {"year": year, **counts, "status": "verificada" if files and not failures else "divergencia" if failures else "indisponivel", "failures": failures}


def audit_capital_cases(root, mesh, raw_by_year, mprj, exposures=None):
    cases = list(csv.DictReader((root / "data/review/bairros_capital_evidencias.csv").open()))
    points = []
    for f in mprj["features"]:
        if str(f["properties"].get("cd_mun7")) != "3304557":
            continue
        lon, lat = f["geometry"]["coordinates"]
        bairro, _ = mesh.classify(CAPITAL, lat, lon)
        points.append((bairro, f))
    for row in cases:
        year = int(row.pop("ano"))
        bairro_id = row["bairro_id"]
        raw = [l for l in raw_by_year.get(year, []) if l["municipio"] == CAPITAL and l["turno"] == row["turno"]]
        located = [l for l in raw if mesh.classify(CAPITAL, l["lat"], l["lon"])[0] == bairro_id]
        matches = [f for b, f in points if b == bairro_id]
        contradictory = []
        for feature in matches:
            properties = feature["properties"]
            relevant = [l for l in raw if l["zona"] == str(properties["zona"])
                        and normalize_name(l["nome"]) == normalize_name(properties["local_vota"])
                        and normalize_name(l["endereco"]) == normalize_name(properties["endereco"])]
            contradictory.append({"zona": str(properties["zona"]), "name": properties["local_vota"],
                "address": properties["endereco"], "sections": properties["secoes"],
                "coordinate": feature["geometry"]["coordinates"],
                "tse_matches": [{"local": l["local"], "coordinate": [coordinate(l["lat"], l["lon"]).x,
                    coordinate(l["lat"], l["lon"]).y] if coordinate(l["lat"], l["lon"]) else None,
                    "votes_by_contest": (exposures or {}).get(year, {}).get((l["zona"], l["local"]), [])}
                    for l in relevant]})
        row.update({"year": year, "municipio": CAPITAL, "escolas_sme_no_poligono": int(row["escolas_sme_no_poligono"]),
                    "fontes": row["fontes"].split(";"), "raw_locations_in_polygon": len(located),
                    "physical_absence_confirmed": False,
                    "mprj_2024_locations_in_polygon": len(matches),
                    "mprj_2024_conflicting_locations": contradictory})
    return cases


def generate(root=ROOT, years=YEARS):
    root = Path(root)
    mesh = IndependentMesh(root)
    schools_path = root / "data/reference/evidence/sme-escolas-2023.geojson"
    mprj_path = root / "data/reference/evidence/mprj-locais-2024.geojson"
    schools = json.loads(schools_path.read_text())
    mprj = json.loads(mprj_path.read_text())
    report = {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
              "scope": "Votos pelo local de votação, não residência dos eleitores",
              "limitations": ["Contenção em polígono não certifica endereço nem entrada do prédio.",
                  "Fonte escolar SME de junho/2023 não substitui o cadastro efetivo de uma eleição.",
                  "Réplica MPRJ2024 do TSE é triangulação de coordenadas, não certificação independente do endereço.",
                  "Bairro sem ponto cadastrado não prova ausência física de local nem votos de moradores iguais a zero."],
              "sources": [{"id": "sme2023", "url": SME_URL, "reference_date": "2023-06",
                            "features": len(schools["features"]), "sha256": sha256_file(schools_path)},
                           {"id": "mprj2024", "url": MPRJ_URL, "reference_date": "2024",
                            "features": len(mprj["features"]), "sha256": sha256_file(mprj_path)}],
              "geography_sha256": sha256_file(root / "data/reference/bairros-metro.geojson"),
              "raw_coordinate_coverage": [], "school_coordinate_audits": [], "vote_conservation": [],
              "public_products": [], "capital_cases": [], "metropolitan_local_reviews": [], "processed_coordinate_checks": [], "correction_impacts": [],
              "primary_corroboration_checks": []}
    municipal_source = root / "data/reference/evidence/itaborai-escolas-2026.geojson"
    if municipal_source.exists():
        municipal = json.loads(municipal_source.read_text())
        report["sources"].append({"id": "itaborai2026", "url": municipal["source"],
                                  "reference_date": "2026", "features": len(municipal["features"]),
                                  "sha256": sha256_file(municipal_source),
                                  "role": "Coordenadas e endereços de equipamentos escolares da Prefeitura, Matrícula 2026"})
    cadastral_source = root / "data/reference/evidence/cadlog-portas-escolas.geojson"
    if cadastral_source.exists():
        cadastral = json.loads(cadastral_source.read_text())
        report["sources"].append({"id": "cadlog_portas", "url": cadastral["source"],
                                  "reference_date": "2026-10-14", "features": len(cadastral["features"]),
                                  "sha256": sha256_file(cadastral_source),
                                  "role": "Pontos municipais de número de porta, com nome do equipamento, número e código de logradouro conferidos"})
    historical_doors = root / "data/reference/evidence/cadlog-portas-escolas-historicas.geojson"
    if historical_doors.exists():
        historical = json.loads(historical_doors.read_text())
        report["sources"].append({"id": "cadlog_portas_historicas", "url": historical["source"],
                                  "reference_date": historical["checked_at"],
                                  "features": len(historical["features"]),
                                  "sha256": sha256_file(historical_doors),
                                  "role": "Portas institucionais adicionais, conferidas por nome completo, tipo, número e logradouro nos originais de cada eleição histórica"})
    historical_review_path = root / "data/reference/evidence/cadlog-conflitos-historicos.json"
    historical_reviews = {}
    if historical_review_path.exists():
        primary_reviews = json.loads(historical_review_path.read_text())
        report["historical_primary_door_reviews"] = primary_reviews["cases"]
        historical_reviews = {(int(row["year"]), str(row["zona"]), str(row["local"])): row
                              for row in primary_reviews["cases"]}
        report["sources"].append({"id": "cadlog_triagem_historica", "url": primary_reviews["sources"][0],
                                  "reference_date": primary_reviews["checked_at"],
                                  "case_years": len(primary_reviews["cases"]),
                                  "sha256": sha256_file(historical_review_path),
                                  "role": "Busca de evidência primária para todos os conflitos históricos restantes, com ausência ou divergência de porta/número/logradouro explícitas"})
    address_source = root / "data/reference/evidence/afranio-address-2024.json"
    if address_source.exists():
        address = json.loads(address_source.read_text())
        report["sources"].append({"id": "ivisa_afranio2024", "url": address["source"],
                                  "reference_date": address["reference_date"], "sha256": address["sha256"],
                                  "role": "Confirma nome da escola e endereço de porta quando o rótulo nominal CadLog diverge; identificação manual sem alias automático"})
    campus_source = root / "data/reference/evidence/inea-ciep451-location.json"
    if campus_source.exists():
        campus = json.loads(campus_source.read_text())
        report["sources"].append({"id": "inea_ciep451", "url": campus["source"],
                                  "reference_date": campus["reference_date"], "sha256": campus["sha256"],
                                  "role": "Corrobora localização física do campus CIEP451, nome/endereço e bairro. Coordenada de estação ambiental não usada como porta eleitoral."})
    raw_by_year, exposures = {}, {}
    db = duckdb.connect()
    db.execute("SET memory_limit='1GB'")
    db.execute("SET threads=2")
    try:
        for year in years:
            print(f"geographic evidence {year}: originals, coordinates, candidates, public products", flush=True)
            raw, coverage = independently_classified_coverage(db, root, year, mesh)
            raw_by_year[year] = raw
            report["raw_coordinate_coverage"].extend(coverage)
            report["primary_corroboration_checks"].append(audit_primary_corroborations(root, year, mesh, raw))
            exposures[year] = votes_at_locations(db, root, year)
            school_audit = {"year": year, **compare_schools(mesh, raw, schools)}
            corrections = {(str(r["municipio"]), str(r["zona"]), str(r["local"])): r
                           for r in csv.DictReader((root / "data/review/correcoes_locais.csv").open())
                           if str(r["ano"]) == str(year)}
            confirmed_path = root / "data/review/locais_fontes_conferidas.csv"
            confirmed = {(str(r["municipio"]), str(r["zona"]), str(r["local"])): r
                         for r in csv.DictReader(confirmed_path.open()) if str(r["ano"]) == str(year)} if confirmed_path.exists() else {}
            for conflict in school_audit["coordinate_conflicts"]:
                conflict["votes_by_contest"] = exposures[year].get((conflict["zona"], conflict["local"]), [])
                fix = corrections.get((CAPITAL, conflict["zona"], conflict["local"]))
                if fix:
                    point = coordinate(fix["latitude"], fix["longitude"])
                    conflict.update({"status": "correcao_documentada", "correcao_aplicada": True,
                                     "correction_source": fix["fonte"], "correction_justification": fix["justificativa"],
                                     "corrected_coordinate": [point.x, point.y] if point else None})
                    conflict["motivo"] = "Coordenada original divergente preservada; correção fundamentada em fonte municipal própria de nome/porta/logradouro, conferida com o local eleitoral efetivo."
                corroboration = confirmed.get((CAPITAL, conflict["zona"], conflict["local"]))
                if corroboration and not fix:
                    conflict.update({"status": corroboration["status"], "correcao_aplicada": False,
                                     "corroboration_source": corroboration["fonte"],
                                     "motivo": corroboration["justificativa"]})
                primary_review = historical_reviews.get((year, conflict["zona"], conflict["local"]))
                if primary_review:
                    conflict["primary_door_review"] = primary_review
                    if not fix and not corroboration:
                        conflict["motivo"] += " Conferência primária CadLog: " + primary_review["evidence"]
                        conflict["action"] = primary_review["action"]
            school_audit["pending_coordinate_reviews"] = sum(c["status"] == "revisao_pendente" for c in school_audit["coordinate_conflicts"])
            report["school_coordinate_audits"].append(school_audit)
            report["vote_conservation"].append(audit_vote_conservation(db, root, year, mesh.metro))
            report["public_products"].append(audit_public_products(root, year, db))
            report["metropolitan_local_reviews"].extend(metropolitan_local_reviews(db, root, year, mesh))
            report["processed_coordinate_checks"].append(audit_processed_coordinates(db, root, year, mesh))
            report["correction_impacts"].extend(correction_impacts(db, root, year, mesh))
        report["capital_cases"] = audit_capital_cases(root, mesh, raw_by_year, mprj, exposures)
    finally:
        db.close()
    manifests = root / "data/manifests/independent-raw"
    report["raw_multiset_checks"] = [{k: data[k] for k in ("year", "kind", "status", "original", "parquet") if k in data}
                                      for path in sorted(manifests.glob("*.json"))
                                      for data in [json.loads(path.read_text())] if "year" in data and "kind" in data]
    header_path = manifests / "header-preservation.json"
    if header_path.exists():
        report["header_preservation"] = json.loads(header_path.read_text())
    report["votes_and_public_products_verified"] = all(r["status"] == "verificada" for r in
                                                      report["vote_conservation"] + report["public_products"])
    report["processed_coordinates_match_current_mesh"] = all(r["status"] == "verificada" for r in report["processed_coordinate_checks"])
    report["primary_corroborations_verified"] = all(r["status"] == "verificada" for r in report["primary_corroboration_checks"])
    report["documented_corrections"] = [r for r in csv.DictReader((root / "data/review/correcoes_locais.csv").open())
                                       if r["municipio"] in mesh.metro and int(r["ano"]) in years]
    report["documented_corrections_sha256"] = sha256_file(root / "data/review/correcoes_locais.csv")
    write_json(root / "data/manifests/geographic-evidence.json", report)
    write_json(root / "web/dist/data/geographic-evidence.json", report, compact=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", nargs="+", type=int, default=list(YEARS))
    parser.add_argument("--raw-hashes", nargs="+", choices=("locais", "munzona", "secao", "detalhe", "partido"))
    parser.add_argument("--hashes-only", action="store_true")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    if args.raw_hashes:
        for year in args.years:
            for kind in args.raw_hashes:
                result = audit_raw_multiset(ROOT, year, kind, cache=not args.no_cache)
                if result["status"] == "divergencia":
                    raise SystemExit(f"Original rows differ from Parquet: {year}/{kind}")
    if not args.hashes_only:
        report = generate(years=args.years)
        print(json.dumps({"votes_and_public_products_verified": report["votes_and_public_products_verified"],
                          "processed_coordinates_match_current_mesh": report["processed_coordinates_match_current_mesh"],
                          "primary_corroborations_verified": report["primary_corroborations_verified"],
                          "documented_corrections": len(report["documented_corrections"]),
                          "capital_cases": len(report["capital_cases"]),
                          "metropolitan_coordinate_checks": len(report["raw_coordinate_coverage"]),
                          "coordinate_conflicts": {r["year"]: len(r["coordinate_conflicts"]) for r in report["school_coordinate_audits"]}},
                         ensure_ascii=False))
        if not all(report[key] for key in ("votes_and_public_products_verified",
                                          "processed_coordinates_match_current_mesh",
                                          "primary_corroborations_verified")):
            raise SystemExit("Vote, public-product, coordinate, or primary-evidence audit failed; consult geographic-evidence.json")


if __name__ == "__main__":
    main()
