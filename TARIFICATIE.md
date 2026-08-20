# Tarificatiemodule — BTI-gestuurde classificatie

Nieuwe module die de classificatie onderbouwt met echte bronnen in plaats van
met wat het model zich herinnert.

## Waarom

De huidige pipeline ([app.py](app.py)) stuurt drie prompts achter elkaar zonder
één bron mee te geven. [prompts.py:90](utils/prompts.py#L90) zegt letterlijk
*"You may ONLY use: EU CN/TARIC structure, Section/Chapter Notes, GIR rules,
BTI... NO external knowledge allowed"* — maar in
[app.py:272](app.py#L272) wordt geen van die bronnen meegestuurd. Het model moet
dus uit zijn geheugen putten, terwijl de prompt hem ook nog opdraagt
*"ALWAYS state uncertainty"*. Vandaar de twijfel.

Dezelfde leemte zit in de validatiestap: die moet controleren of een code
"bestaat in de nomenclatuur", zonder de nomenclatuur te hebben.

## Fasering

| Fase | Wat | Status |
|---|---|---|
| 0 | Testset + nulmeting | code klaar, meting wacht op API-sleutel |
| 1 | BTI-index + delta-updater | **klaar** |
| 2 | CN/TARIC-nomenclatuur + aantekeningen → harde codevalidatie | nog te doen |
| 3 | Retrieval in de pipeline + bewijsgebaseerde confidence | nog te doen |
| 4 | Indelingsverordeningen + HvJ-arresten | nog te doen |

## Fase 1 — BTI-index

Bron: DG TAXUD DDS2, `DDS2-EBTI_Full.zip` (volledige export) plus dagelijkse
delta's `DDS2-EBTI_<timestamp>.zip`.

```bash
# Volledige export inlezen (~4 min, eenmalig)
python scripts/import_ebti.py --source "C:/Users/Luc/Downloads/EBTI"

# Dagelijkse delta bijwerken — voegt nieuwe BTI's toe en zet
# ingetrokken BTI's op INVALID (upsert op BTI_REFERENCE)
python scripts/import_ebti.py --delta "C:/Users/Luc/Downloads/DDS2-EBTI_20260720_044113.zip"
```

Resultaat in `data/ebti.db` (niet in git — zie [.gitignore](.gitignore)):

| | |
|---|---|
| records | 1.044.971 (2004–2026) |
| geldig | 123.067 |
| hoofdstukken | 96 |
| talen | 23 |
| bestandsgrootte | 2,06 GB |

### Eigenaardigheden in de bron, afgevangen in [tariff/ebti.py](tariff/ebti.py)

- `NOMENCLATURE_CODE` is met `*` opgevuld tot 22 tekens
- datums zijn `DD/MM/YYYY` in de volledige export, ISO in de delta
- de kolomnaam `DATE_OF _ISSUE` bevat een spatie te veel — dat staat zo in de bron
- `KEYWORDS` is soms Engels (Duitse BTI's), soms landstaal (Franse BTI's)
- één record heeft `end_date` in het jaar 2206 — kennelijk een typefout bij de uitgevende dienst

## Fase 0 — meten

```bash
python eval/build_testset.py --n 300 --set realistic   # NL/FR/EN, lijkt op DKM-praktijk
python eval/build_testset.py --n 300 --set broad       # alle talen, 94 hoofdstukken

export ANTHROPIC_API_KEY=sk-ant-...
python eval/run_baseline.py --testset eval/testsets/realistic_300.jsonl
python eval/run_baseline.py --testset eval/testsets/realistic_300.jsonl --model claude-opus-5

python eval/score.py --run eval/runs/realistic_300__sonnet420250514.jsonl \
                     --run eval/runs/realistic_300__opus5.jsonl
```

Elke geldige BTI is een gelabeld voorbeeld: de omschrijving van de goederen is
de input, de toegekende CN-code het juiste antwoord.

**Beperking:** een BTI-omschrijving is geschreven door iemand die de indeling al
kende, en is dus preciezer dan een doorsnee factuurregel. De score is een
bovengrens, geen voorspelling van praktijkprestatie. Voor het vergelijken van
versies van de tool is dat prima — daarvoor is hij bedoeld.

De scorer meet naast trefzekerheid twee dingen die rechtstreeks over de gemelde
twijfel gaan:

- **twijfelgraad** — hoe vaak de tool zelf om handmatige controle vraagt
- **kalibratie** — of `HIGH` confidence ook echt vaker klopt dan `LOW`

Zit `HIGH` even vaak fout als `LOW`, dan draagt het label geen informatie en
hoort het niet in de interface.

## Opslag — Neon in plaats van een bestand naast de app

Streamlit Cloud heeft geen plek voor een index van gigabytes, maar wel een
netwerkverbinding. De index staat dus in Neon, waar de app al voor
[classificaties en verbruik](utils/db.py) op aansluit.

**Werkverdeling:**

| Waar | Wat | Omvang |
|---|---|---|
| Neon | 123.067 geldige BTI's, voluit doorzoekbaar | ~350 MB geschat |
| Neon | `bti_code_stats` — samenvatting per CN8 over **het volledige archief** | 14.455 rijen, enkele MB |
| lokaal SQLite | alle 1.044.971 records, voor onderzoek en testsets | 2,06 GB |

Zo gaat er niets verloren. De 922k verlopen BTI's tellen mee in de
onderbouwing zonder dat ze zelf naar Neon hoeven. Voorbeeld: CN8 `63079098`
heeft 2.164 geldige BTI's uit 23 lidstaten plus 8.717 verlopen uit 28 — dat is
precies de zin die "MEDIUM confidence" moet vervangen.

```bash
python scripts/load_neon.py --check          # verbinding + opslag rapporteren
python scripts/load_neon.py                  # geldige BTI's + CN8-samenvatting
python scripts/load_neon.py --code-stats     # alleen de samenvatting herberekenen
python scripts/load_neon.py --include-expired  # alles (~2 GB in Neon)
```

Reken voor fase 3 op ongeveer 1 GB in totaal: embeddings over de geldige set
komen daar nog bij (123k × 1024 dimensies als `halfvec` ≈ 250 MB, plus een
vergelijkbare HNSW-index). Controleer in de Neon-console of je plan dat trekt.

**Let op bij de gepoolde endpoint:** psycopg3 maakt na vijf uitvoeringen
automatisch prepared statements aan, en Neon's pooler is PgBouncer in
transaction mode — die kan een volgende query naar een andere serververbinding
sturen waar dat statement niet bestaat. [tariff/neon.py](tariff/neon.py) zet
daarom `prepare_threshold=None`. Voor [utils/db.py](utils/db.py) is dat het
overwegen waard bij `log_usage_events`, dat `executemany` gebruikt.

## Openstaande beslissingen

**Meertalig zoeken.** Het zoeken is nu lexicaal: SQLite FTS5 lokaal, Postgres
full-text search in Neon (per taal geconfigureerd, dus Duitse stemming voor
Duitse BTI's). Dat werkt binnen een taal goed en over talen heen matig — met een
Nederlandse omschrijving vind je geen Duitse BTI, en 61% van de geldige BTI's is
Duits. Semantisch zoeken erbovenop hoort bij fase 3 en vraagt een
embeddingsleverancier (Anthropic heeft geen embeddings-API; Voyage AI is de
aanbevolen partner).

**Bron voor de nomenclatuur.** Nodig voor fase 2. DDS2 publiceert
nomenclatuurextracties naast EBTI; welke download precies bruikbaar is, moet nog
uitgezocht worden.

**DKM's eigen data.** De tabel `verified_codes` in [utils/db.py](utils/db.py)
bevat door senioren bevestigde codes. Dat is de betrouwbaarste bron die er is —
eigen dossiers, eigen vakmensen — en hoort als eigen laag in de onderbouwing,
zwaarder wegend dan een BTI van een andere lidstaat.

## Juridische nuance

Een BTI bindt alleen de houder, voor die specifieke goederen. Een
indelingsverordening bindt iedereen. De tool moet dat onderscheid tonen, anders
krijgen BTI's meer gewicht dan ze juridisch hebben.
