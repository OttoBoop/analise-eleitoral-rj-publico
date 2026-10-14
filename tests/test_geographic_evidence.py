import csv
import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import duckdb

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("geographic_evidence", ROOT / "scripts/audit_geographic_evidence.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def multiset(tmp_path, rows, name, block_rows=2):
    hasher = audit.CanonicalMultiset(tmp_path / name, block_rows=block_rows)
    for row in rows:
        hasher.add(row)
    return hasher.finish()


def test_canonical_hash_preserves_duplicates_and_field_boundaries(tmp_path):
    rows = [["001", "A;B", "ç\n"], ["2", "", "#NULO"], ["001", "A;B", "ç\n"]]
    assert multiset(tmp_path, rows, "a") == multiset(tmp_path, rows[::-1], "b")
    assert multiset(tmp_path, rows, "c") != multiset(tmp_path, rows[:-1], "d")
    assert audit.CanonicalMultiset.digest_row(["a", "bc"]) != audit.CanonicalMultiset.digest_row(["ab", "c"])
    assert audit.CanonicalMultiset.digest_row(["001"]) != audit.CanonicalMultiset.digest_row(["1"])


def test_zip_parquet_hash_reads_full_rows_filters_uf_and_retains_duplicates(tmp_path):
    raw = tmp_path / "data/raw"
    processed = tmp_path / "data/processed"
    raw.mkdir(parents=True)
    processed.mkdir(parents=True)
    fields = ["SG_UF", "CD_MUNICIPIO", "NM_LOCAL_VOTACAO"]
    rows = [["RJ", "001", "Escola; Ç"], ["SP", "2", "Outra"], ["RJ", "001", "Escola; Ç"]]
    text = io.StringIO(newline="")
    writer = csv.writer(text, delimiter=";")
    writer.writerow(fields)
    writer.writerows(rows)
    member = "eleitorado_local_votacao_2024.csv"
    with zipfile.ZipFile(raw / "locais_2024.zip", "w") as archive:
        archive.writestr(member, text.getvalue().encode("latin-1"))
    data = [{**dict(zip(fields, row)), "SOURCE_MEMBER": member} for row in rows if row[0] == "RJ"]
    parquet = processed / "locais_2024.parquet"
    pq.write_table(pa.Table.from_pylist(data[::-1]), parquet)
    result = audit.audit_raw_multiset(tmp_path, 2024, "locais", cache=False)
    assert result["status"] == "verificada"
    assert result["original"]["rows"] == result["parquet"]["rows"] == 2
    pq.write_table(pa.Table.from_pylist(data[:1]), parquet)
    assert audit.audit_raw_multiset(tmp_path, 2024, "locais", cache=False)["status"] == "divergencia"


def test_school_matching_never_merges_different_facility_types():
    assert audit.school_key("E.D.I. Joaquim Venâncio") == audit.school_key("Espaço de Desenvolvimento Infantil Joaquim Venâncio")
    assert audit.school_key("E M Cuba") == audit.school_key("Escola Municipal Cuba")
    assert audit.school_key("CIEP Nação Mangueirense") != audit.school_key("Creche Municipal Nação Mangueirense")


def test_capital_review_cannot_claim_physical_absence_from_empty_aggregates():
    path = ROOT / "data/review/bairros_capital_evidencias.csv"
    rows = list(csv.DictReader(path.open()))
    assert len(rows) == 25
    assert sum(row["ano"] == "2024" for row in rows) == 12
    assert sum(row["ano"] == "2026" for row in rows) == 13
    assert all(row["status"] != "sem_local_confirmado" for row in rows)
    assert all(row["evidencia"] and row["acao"] and row["fontes"] for row in rows)
    assert all(row["status"] == "coordenadas_fontes_divergentes" for row in rows if row["bairro"] == "Manguinhos")


def test_school_snapshot_is_public_facility_data():
    data = json.loads((ROOT / "data/reference/evidence/sme-escolas-2023.geojson").read_text())
    assert len(data["features"]) == 1590
    assert all(f["geometry"]["type"] == "Point" for f in data["features"])
    # Only institutional metadata is allowed, never students' or staff records.
    forbidden = {"cpf", "aluno", "nome_aluno", "matricula", "responsavel"}
    assert all(not forbidden.intersection(f["properties"]) for f in data["features"])


def test_primary_door_search_records_every_historical_case_without_residential_candidates():
    path = ROOT / "data/reference/evidence/cadlog-conflitos-historicos.json"
    review = json.loads(path.read_text())
    assert len(review["cases"]) == 45
    assert len({row["name"] for row in review["cases"]}) == 18
    for row in review["cases"]:
        assert row["status"] and row["evidence"] and row["action"]
        assert "coordinates" not in row  # Unverified doors must not become point overrides.
        for door in row["door_candidates"]:
            label = audit.school_key(door["facility_label"])
            assert label.startswith(("ESCOLA ", "CIEP ", "CRECHE "))
    assert all(row["status"] != "porta_comprovada" for row in review["cases"]
               if row["name"] == "ESCOLA MUNICIPAL RODRIGO OTAVIO")


def synthetic_vote_files(tmp_path, duplicate=False):
    folder = tmp_path / "data/processed"
    folder.mkdir(parents=True)
    raw = {"CD_ELEICAO": "1", "CD_CARGO": "11", "NR_TURNO": "1", "SG_UE": "60011",
           "CD_MUNICIPIO": "60011", "NR_ZONA": "1", "CD_TIPO_ELEICAO": "2", "SQ_CANDIDATO": "candidate",
           "QT_VOTOS_NOMINAIS_VALIDOS": "100"}
    pq.write_table(pa.Table.from_pylist([raw, {**raw, "SQ_CANDIDATO": "zero", "QT_VOTOS_NOMINAIS_VALIDOS": "0"}]),
                   folder / "munzona_2024.parquet")
    section = {"election": "1", "cargo": "11", "turno": "1", "ue": "60011", "municipio": "60011",
               "candidate": "candidate", "name": "Paes", "zona": "1", "secao": "1", "local": "10", "verified": True,
               "bairro_id": "bairro", "geo_status": "coordenada_validada", "votes": 90,
               "local_name": "Escola", "address": "Rua 1", "lat": -22.5, "lon": -43.5}
    rows = [{**section, "votes": 50}, {**section, "votes": 50}] if duplicate else [
        section, {**section, "secao": "2", "bairro_id": None, "geo_status": "ligacao_local_pendente", "votes": 10}]
    pq.write_table(pa.Table.from_pylist(rows), folder / "votes_sections_geo_2024.parquet")


def test_independent_vote_audit_preserves_missing_location_and_real_zero(tmp_path):
    synthetic_vote_files(tmp_path)
    db = duckdb.connect()
    report = audit.audit_vote_conservation(db, tmp_path, 2024, {"60011"})
    assert report["status"] == "verificada"
    assert report["candidate_municipality_records"] == 2
    municipality = report["metropolitan_contests"][0]
    assert municipality["total"] == 100
    assert municipality["located"] == 90
    assert municipality["unlocated"] == 10
    assert municipality["unlocated_reasons"] == [{"reason": "ligacao_local_pendente", "votes": 10, "sections": 1}]
    db.close()


def test_independent_vote_audit_detects_repeated_section_even_if_total_balanced(tmp_path):
    synthetic_vote_files(tmp_path, duplicate=True)
    db = duckdb.connect()
    report = audit.audit_vote_conservation(db, tmp_path, 2024, {"60011"})
    assert report["status"] == "divergencia"
    assert report["failures"][0]["official_votes"] == report["failures"][0]["classified_votes"] == 100
    assert report["failures"][0]["rows"] == 2
    assert report["failures"][0]["section_keys"] == 1
    db.close()


def test_documented_applied_corrections_have_sources_and_coordinates_inside_declared_neighborhood():
    mesh = audit.IndependentMesh(ROOT)
    rows = list(csv.DictReader((ROOT / "data/review/correcoes_locais.csv").open()))
    assert rows
    keys = [(r["ano"], r["turno"], r["municipio"], r["zona"], r["local"]) for r in rows]
    assert len(keys) == len(set(keys))
    for row in rows:
        assert row["fonte"].startswith("https://") and row["justificativa"]
        bairro, _ = mesh.classify(row["municipio"], row["latitude"], row["longitude"])
        assert bairro == row["bairro_id"]


def test_caldeira_original_in_santa_cruz_cannot_be_corroborated_by_door_in_paciencia():
    mesh = audit.IndependentMesh(ROOT)
    original = {"municipio": "60011", "nome": "ESCOLA MUNICIPAL FRANCISCO CALDEIRA DE ALVARENGA",
                "lat": "-22.9170804", "lon": "-43.6529236"}
    doors = json.loads((ROOT / "data/reference/evidence/cadlog-portas-escolas.geojson").read_text())
    door = next(f for f in doors["features"] if f["properties"]["objectid"] == 179434)
    comparison = audit.compare_original_with_primary_door(mesh, original, door, "3304557:ipp:148")
    assert comparison["original_bairro"] == "3304557:ipp:149"
    assert comparison["primary_bairro"] == "3304557:ipp:148"
    assert not comparison["corroboration_valid"]
    corrections = list(csv.DictReader((ROOT / "data/review/correcoes_locais.csv").open()))
    confirmed = list(csv.DictReader((ROOT / "data/review/locais_fontes_conferidas.csv").open()))
    for year in ("2024", "2026"):
        matching = [r for r in corrections if (r["ano"], r["zona"], r["local"]) == (year, "246", "1074")]
        assert len(matching) == 1 and matching[0]["bairro_id"] == "3304557:ipp:148"
        assert not any((r["ano"], r["zona"], r["local"]) == (year, "246", "1074") for r in confirmed)


def test_actual_local_vote_exposures_and_pending_cases_are_aggregated_once(tmp_path):
    from types import SimpleNamespace
    synthetic_vote_files(tmp_path)
    db = duckdb.connect()
    exposure = audit.votes_at_locations(db, tmp_path, 2024)
    assert exposure[("1", "10")][0]["votes"] == 100
    assert exposure[("1", "10")][0]["sections"] == 2
    mesh = SimpleNamespace(metro={"60011"}, municipalities={"60011": {"nome": "Rio de Janeiro"}})
    cases = audit.metropolitan_local_reviews(db, tmp_path, 2024, mesh)
    assert len(cases) == 1
    assert cases[0]["local"] == "10"
    assert cases[0]["votes_by_contest"][0]["votes"] == 10
    db.close()


def test_correction_impact_counts_each_office_and_highlight_without_changing_votes(tmp_path):
    from types import SimpleNamespace
    synthetic_vote_files(tmp_path)
    for directory in ("data/review", "data/reference"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "data/review/correcoes_locais.csv").write_text(
        "ano,turno,municipio,zona,local,latitude,longitude,bairro_id,fonte,justificativa\n"
        "2024,1,60011,1,10,-22.5,-43.5,new,https://source.example,Prédio conferido\n")
    (tmp_path / "data/reference/candidaturas_destaque.csv").write_text(
        "ano,cargo,sq_candidato,person_id\n2024,11,candidate,paes\n")
    pq.write_table(pa.Table.from_pylist([{"NR_TURNO": "1", "CD_MUNICIPIO": "60011", "NR_ZONA": "1",
        "NR_LOCAL_VOTACAO": "10", "NR_LATITUDE": "-22.4", "NR_LONGITUDE": "-43.5"}]),
        tmp_path / "data/processed/locais_2024.parquet")
    mesh = SimpleNamespace(metro={"60011"}, classify=lambda municipio, lat, lon:
                           ("new" if float(lat) == -22.5 else "old", "point"))
    db = duckdb.connect()
    report = audit.correction_impacts(db, tmp_path, 2024, mesh)
    assert len(report) == 1
    assert report[0]["from_bairro"] == "old" and report[0]["to_bairro"] == "new"
    assert report[0]["change"] == "mudanca_de_bairro"
    assert report[0]["contests"][0]["nominal_valid_votes"] == 100
    assert report[0]["contests"][0]["highlight_votes"][0]["votes"] == 100
    db.close()
