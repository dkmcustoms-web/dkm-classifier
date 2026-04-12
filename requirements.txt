# DKM CN/TARIC Classificatie Tool

AI-gestuurde 3-staps classificatie pipeline voor EU douanecodes.

---

## Hoe het werkt

```
Input (tekst / afbeelding / factuur)
    │
    ▼
Stap 1 — Feature Extraction   →  gestructureerde productdata (JSON)
    │
    ▼
Stap 2 — CN/TARIC Classificatie  →  CN code + TARIC code + redenering
    │
    ▼
Stap 3 — Validatie              →  VALIDATED / PARTIALLY / NOT VALIDATED
    │
    ▼
Google Sheets logging (history per gebruiker)
```

---

## Setup: stap voor stap

### 1. GitHub repo aanmaken

1. Ga naar [github.com/new](https://github.com/new)
2. Naam: `dkm-classifier`  →  Private  →  Create
3. Upload alle bestanden van dit project

### 2. Google Service Account aanmaken

1. Ga naar [console.cloud.google.com](https://console.cloud.google.com)
2. Maak een nieuw project: `dkm-classifier`
3. Ga naar **APIs & Services → Library**
4. Activeer: **Google Sheets API** + **Google Drive API**
5. Ga naar **APIs & Services → Credentials**
6. Klik **+ Create Credentials → Service Account**
7. Naam: `dkm-classifier`  →  Create
8. Rol: **Editor**  →  Done
9. Klik op het service account → tab **Keys** → **Add Key → JSON**
10. Download het JSON-bestand — dit is je `GOOGLE_SERVICE_ACCOUNT`

### 3. Google Sheet voorbereiden

1. Maak een nieuwe Google Spreadsheet
2. Naam: `DKM Classifier History`
3. Kopieer de **Spreadsheet ID** uit de URL:
   `https://docs.google.com/spreadsheets/d/**[DIT IS HET ID]**/edit`
4. Klik **Share** → voeg het `client_email` uit je service account JSON toe
   → geef **Editor** rechten

### 4. Streamlit Cloud deployen

1. Ga naar [share.streamlit.io](https://share.streamlit.io)
2. Klik **New app**
3. Kies je GitHub repo: `dkm-classifier`
4. Main file: `app.py`
5. Klik **Advanced settings → Secrets** en plak dit:

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
GOOGLE_SHEETS_ID  = "jouw-spreadsheet-id"
GOOGLE_SERVICE_ACCOUNT = """
{ ... volledige inhoud van het service account JSON bestand ... }
"""
```

6. Klik **Deploy**

### 5. Lokaal testen (optioneel)

```bash
pip install -r requirements.txt

# Maak .streamlit/secrets.toml aan (zie secrets.toml.example)
# Vul je keys in

streamlit run app.py
```

---

## Projectstructuur

```
dkm-classifier/
├── app.py                    # Hoofdapplicatie
├── requirements.txt
├── .gitignore
├── assets/
│   └── dkm_logo.png          # Logo (zelf toevoegen)
├── utils/
│   ├── __init__.py
│   ├── prompts.py            # De 3 classificatie prompts
│   └── sheets.py             # Google Sheets logging
└── .streamlit/
    ├── config.toml           # DKM dark theme
    └── secrets.toml          # NIET op GitHub! (lokaal gebruik)
```

---

## Google Sheets kolommen (automatisch aangemaakt)

| Kolom | Inhoud |
|---|---|
| timestamp | Datum + tijdstip |
| user | Naam/initialen gebruiker |
| description | Productomschrijving |
| specs | Technische specs |
| has_image / has_invoice | ja/nee |
| product_id | Geëxtraheerde productnaam |
| category | Categoriehint |
| data_quality | high/medium/low |
| cn_code | Voorgestelde CN code |
| taric_code | Voorgestelde TARIC code |
| confidence | HIGH/MEDIUM/LOW |
| outcome | VALIDATED / PARTIALLY / NOT VALIDATED |
| validated_code | Definitieve code |
| manual_review | ja/nee |
| issues | Eventuele problemen |
| raw_step1/2/3 | Ruwe AI output (eerste 500 tekens) |

---

## Licentie

Intern gebruik DKM-Customs — niet voor publieke distributie.
