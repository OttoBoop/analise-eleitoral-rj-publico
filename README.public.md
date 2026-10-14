# Atlas eleitoral do Rio de Janeiro

Código e produtos públicos das eleições de 2018, 2020, 2022, 2024 e 2026, cobrindo os votos registrados no estado do RJ. [Abrir o painel](https://ottoboop.github.io/analise-eleitoral-rj-publico/).

## Reprodução local

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python scripts/fetch_sources.py
.venv/bin/python scripts/fetch_geography.py
.venv/bin/python scripts/fetch_metro_geography.py
.venv/bin/python scripts/pipeline.py --years 2018 2020 2022 2024 2026
.venv/bin/python scripts/build_site.py
.venv/bin/pytest -q
node --test tests/*.mjs
.venv/bin/python scripts/browser_qa.py
```

Os dados originais e Parquets ficam locais, fora do Git. Os manifestos registram fontes, versões e hashes. `--refresh` na coleta eleitoral arquiva a geração anterior. O processamento pesado usa Python, DuckDB e Parquet; o navegador carrega apenas agregados compactos e arquivos por candidato.

## Leitura dos mapas

Participação = votos nominais válidos / total nominal válido do mesmo cargo, turno e território. Diferenças são medidas em pontos percentuais. Os resultados por bairro representam o **bairro do local de votação**, sem identificar a residência dos eleitores. Cargos diferentes têm denominadores próprios.

As duas seleções são independentes. Candidatos distintos usam paletas Blues e Oranges, com limites numéricos compartilhados. A diferença indica vantagem na mesma disputa; comparações da mesma pessoa entre eleições indicam aumento ou redução. As cores derivam do [ColorBrewer](https://colorbrewer2.org/), de Cynthia Brewer, Mark Harrower e Pennsylvania State University; ver [licença e atribuição](https://github.com/axismaps/colorbrewer/blob/master/LICENCE.txt).

A referência territorial é fixa por município. A capital usa IPP; os demais municípios metropolitanos usam IBGE e fontes municipais/MPRJ documentadas. Petrópolis e Paracambi permanecem no nível municipal. Geometrias inválidas, sobrepostas no ponto consultado ou ausentes não recebem votos por estimativa. A cobertura informa os votos sem localização e as divergências bloqueiam o recorte afetado.

## Publicação

O GitHub Actions valida código, conservação dos votos e interface e publica **somente `web/dist`** no Pages. O CI não baixa nem processa os CSVs eleitorais nacionais. Os caminhos de arquivos são relativos, permitindo servir o site no subdiretório do projeto.

Este repositório tem histórico independente e contém somente código, testes, referências geográficas, manifestos públicos e produtos compactos. Documentos privados de trabalho e o histórico do projeto original não são distribuídos.

Para conferir localmente o painel já agregado, basta instalar dependências, executar `scripts/build_site.py` e servir `web/dist` por HTTP. A QA inicia e encerra seu próprio servidor e aceita Chromium instalado ou o navegador gerenciado pelo Playwright.

O atalho **Vereadores e deputados do PSD** permite buscar candidaturas por nome ou número, escolher cargo, eleição e município e selecionar independentemente referência e comparado. A filiação exibida é a registrada no TSE em cada eleição; a lista inclui todas as candidaturas, não apenas eleitos. O denominador de participação continua incluindo os votos nominais válidos de todos os partidos.

Bairros sem votos atribuídos são acompanhados do motivo na tabela, no mapa e nas exportações. Ausência de seções associadas, território fora da disputa e malha indisponível não são tratados como zero voto. A auditoria reproduzível de Paes está em `scripts/audit_neighborhoods.py`; requer os Parquets locais já processados. A contenção da coordenada TSE no polígono não certifica independentemente o endereço da escola.

Os atalhos no topo destacam Witzel (2018), Cláudio Castro (2022) e Douglas Ruas (2026) para governador no primeiro turno. Cada candidatura pode ser selecionada independentemente em qualquer lado; a busca mantém todos os candidatos e as participações usam o total completo da disputa.
